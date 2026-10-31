from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from toss.client import TossClient, TossError, TossOutcomeUnknown
from toss.config import Settings
from toss.killswitch import assert_orders_allowed, kill_switch_reason
from toss.storage import OrderStore


class TossGateway:
    def __init__(
        self,
        settings: Settings,
        client: TossClient,
        store: OrderStore,
    ) -> None:
        self.settings = settings
        self.client = client
        self.store = store

    def create_order_candidate(
        self,
        *,
        side: str,
        symbol: str,
        quantity: int,
        price: str,
        currency: str,
        source: str = "cli",
    ) -> dict[str, Any]:
        side = side.upper()
        symbol = symbol.upper()
        currency = currency.upper()
        price_value = _decimal(price)
        if side not in {"BUY", "SELL"}:
            raise ValueError("side는 buy 또는 sell이어야 합니다.")
        if quantity <= 0:
            raise ValueError("quantity는 1 이상이어야 합니다.")
        if price_value <= 0:
            raise ValueError("지정가는 0보다 커야 합니다.")
        if currency not in {"KRW", "USD"}:
            raise ValueError("currency는 KRW 또는 USD여야 합니다.")

        estimated_amount = price_value * quantity
        reasons: list[str] = []
        if kill_switch_reason([self.settings.kill_switch_path]) is not None:
            reasons.append("Kill Switch가 활성화되어 신규 주문 후보를 차단했습니다.")
        preflight: dict[str, Any] = {
            "estimatedAmount": format(estimated_amount, "f"),
            "currency": currency,
        }
        max_amount = (
            self.settings.max_order_amount_usd
            if currency == "USD"
            else self.settings.max_order_amount_krw
        )
        preflight["maxOrderAmount"] = format(max_amount, "f")
        if estimated_amount > max_amount:
            reasons.append("추정 주문금액이 설정된 1회 주문 한도를 초과했습니다.")

        if side == "BUY":
            buying_power = self.client.buying_power(currency)
            preflight["buyingPower"] = buying_power
            available = _decimal(buying_power.get("cashBuyingPower", "0"))
            if estimated_amount > available:
                reasons.append("현금 매수 가능 금액이 부족합니다.")
        else:
            sellable = self.client.sellable_quantity(symbol)
            preflight["sellableQuantity"] = sellable
            available_quantity = _decimal(
                sellable.get("sellableQuantity", "0")
            )
            if Decimal(quantity) > available_quantity:
                reasons.append("판매 가능 수량이 부족합니다.")

        payload = {
            "clientOrderId": f"tp-{uuid4().hex[:20]}",
            "symbol": symbol,
            "side": side,
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "quantity": str(quantity),
            "price": format(price_value, "f"),
        }
        status = "BLOCKED" if reasons else "RISK_APPROVED"
        preflight["decision"] = "BLOCK" if reasons else "ALLOW"
        preflight["reasons"] = reasons
        order_id = self.store.create_order(
            source=source,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=format(price_value, "f"),
            currency=currency,
            estimated_amount=format(estimated_amount, "f"),
            payload=payload,
            preflight=preflight,
            status=status,
        )
        return {
            "mode": "dry-run",
            "order_id": order_id,
            "status": status,
            "payload": payload,
            "preflight": preflight,
        }

    def submit_order(self, order_id: int, *, execute: bool) -> dict[str, Any]:
        self.settings.require_order_permission(execute=execute)
        # 원장 상태를 바꾸기 전에 막는다. 해제 후에는 같은 후보를 다시 제출할 수 있다.
        assert_orders_allowed([self.settings.kill_switch_path])
        order = self._approved_order(order_id)
        latest = self._submit_preflight(order)
        if latest["decision"] == "BLOCK":
            self.store.transition(
                order_id,
                expected={"RISK_APPROVED"},
                status="PREFLIGHT_BLOCKED",
                event_type="SUBMIT_PREFLIGHT_BLOCKED",
                payload=latest,
            )
            return {
                "mode": "blocked",
                "order_id": order_id,
                "status": "PREFLIGHT_BLOCKED",
                "preflight": latest,
            }

        # 전송 전에 먼저 기록한다. 이후 어떤 실패가 나도 이 후보는 다시 제출될 수 없다.
        claimed = self.store.transition(
            order_id,
            expected={"RISK_APPROVED"},
            status="SUBMITTING",
            event_type="SUBMITTING",
            payload={"clientOrderId": order["client_order_id"], "preflight": latest},
        )
        if not claimed:
            raise ValueError("이미 다른 프로세스가 이 주문을 제출 중이거나 제출했습니다.")

        try:
            response = self.client.create_order(order["payload"])
        except TossOutcomeUnknown as exc:
            return self._mark_unknown(order_id, _error_payload(exc))
        except TossError as exc:
            # 4xx: 토스가 명확히 거절했다. 주문은 생성되지 않았다.
            self.store.transition(
                order_id,
                expected={"SUBMITTING"},
                status="SUBMIT_REJECTED",
                event_type="SUBMIT_REJECTED",
                payload=_error_payload(exc),
            )
            raise

        toss_order_id = str(response.get("orderId") or "")
        if not toss_order_id:
            return self._mark_unknown(order_id, {"response": response, "reason": "orderId 없음"})
        self.store.transition(
            order_id,
            expected={"SUBMITTING"},
            status="SUBMITTED",
            event_type="SUBMITTED",
            payload=response,
            toss_order_id=toss_order_id,
        )
        return {
            "mode": "live",
            "order_id": order_id,
            "status": "SUBMITTED",
            "toss_order_id": toss_order_id,
            "response": response,
        }

    def _mark_unknown(self, order_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.store.transition(
            order_id,
            expected={"SUBMITTING"},
            status="SUBMIT_UNKNOWN",
            event_type="SUBMIT_UNKNOWN",
            payload=payload,
        )
        return {
            "mode": "unknown",
            "order_id": order_id,
            "status": "SUBMIT_UNKNOWN",
            "message": (
                "주문이 토스에 접수됐는지 알 수 없습니다. 다시 제출하지 말고 "
                f"`toss sync-order {order_id}`로 확인하세요."
            ),
        }

    def cancel_order(self, order_id: int, *, execute: bool) -> dict[str, Any]:
        self.settings.require_order_permission(execute=execute)
        order = self.store.get_order(order_id)
        if order is None:
            raise ValueError(f"주문 원장을 찾을 수 없습니다: {order_id}")
        toss_order_id = str(order.get("toss_order_id") or "")
        if not toss_order_id:
            raise ValueError("토스 주문번호가 없어 취소할 수 없습니다.")
        try:
            response = self.client.cancel_order(toss_order_id)
        except TossOutcomeUnknown as exc:
            return self._unknown_action(order_id, "CANCEL_UNKNOWN", exc)
        self.store.update_status(
            order_id,
            status="CANCEL_REQUESTED",
            event_type="CANCEL_REQUESTED",
            payload=response,
        )
        return {
            "mode": "live",
            "order_id": order_id,
            "status": "CANCEL_REQUESTED",
            "response": response,
        }

    def modify_order(
        self,
        order_id: int,
        *,
        price: str,
        execute: bool,
    ) -> dict[str, Any]:
        self.settings.require_order_permission(execute=execute)
        # 정정은 체결 가능성을 바꾸므로 Kill Switch 대상이다. 취소는 막지 않는다.
        assert_orders_allowed([self.settings.kill_switch_path])
        order = self.store.get_order(order_id)
        if order is None:
            raise ValueError(f"주문 원장을 찾을 수 없습니다: {order_id}")
        toss_order_id = str(order.get("toss_order_id") or "")
        if not toss_order_id:
            raise ValueError("토스 주문번호가 없어 정정할 수 없습니다.")
        price_value = _decimal(price)
        if price_value <= 0:
            raise ValueError("정정 가격은 0보다 커야 합니다.")
        payload: dict[str, Any] = {
            "orderType": "LIMIT",
            "price": format(price_value, "f"),
        }
        if order["currency"] == "KRW":
            payload["quantity"] = str(order["quantity"])
        try:
            response = self.client.modify_order(toss_order_id, payload)
        except TossOutcomeUnknown as exc:
            return self._unknown_action(order_id, "MODIFY_UNKNOWN", exc)
        self.store.update_status(
            order_id,
            status="REPLACE_REQUESTED",
            event_type="REPLACE_REQUESTED",
            payload={"request": payload, "response": response},
        )
        return {
            "mode": "live",
            "order_id": order_id,
            "status": "REPLACE_REQUESTED",
            "response": response,
        }

    def sync_order(self, order_id: int) -> dict[str, Any]:
        order = self.store.get_order(order_id)
        if order is None:
            raise ValueError(f"주문 원장을 찾을 수 없습니다: {order_id}")
        if order["status"] in UNCERTAIN and not order.get("toss_order_id"):
            recovered = self._recover(order)
            if recovered["status"] != "SUBMITTED":
                return recovered
            order = self.store.get_order(order_id)
        toss_order_id = str(order.get("toss_order_id") or "")
        if not toss_order_id:
            raise ValueError("토스 주문번호가 없어 대사할 수 없습니다.")
        response = self.client.order(toss_order_id)
        status = str(response.get("status") or order["status"])
        self.store.update_status(
            order_id,
            status=status,
            event_type="RECONCILED",
            payload=response,
        )
        return {
            "order_id": order_id,
            "status": status,
            "response": response,
        }

    def resolve_not_placed(self, order_id: int) -> dict[str, Any]:
        """결과 불명 주문을 '생성되지 않음'으로 확정한다. 토스 조회에 없을 때만 허용한다."""
        order = self.store.get_order(order_id)
        if order is None:
            raise ValueError(f"주문 원장을 찾을 수 없습니다: {order_id}")
        if order["status"] not in UNCERTAIN or order.get("toss_order_id"):
            raise ValueError(f"결과 불명 상태가 아닙니다: {order['status']}")
        since = self.store.last_event_at(order_id, "SUBMITTING")
        if since and datetime.now(timezone.utc) - datetime.fromisoformat(since) < timedelta(minutes=2):
            raise ValueError("제출 후 2분이 지나야 미생성으로 확정할 수 있습니다. 토스 반영 지연을 기다리세요.")
        result = self._recover(order)
        if result["status"] == "SUBMITTED" or result.get("candidates"):
            raise ValueError("토스에 일치할 수 있는 주문이 있어 미생성으로 확정할 수 없습니다.")
        self.store.transition(
            order_id,
            expected=UNCERTAIN,
            status="SUBMIT_FAILED",
            event_type="RESOLVED_NOT_PLACED",
            payload={"checked": result.get("checked")},
        )
        return {"order_id": order_id, "status": "SUBMIT_FAILED",
                "message": "주문이 생성되지 않은 것으로 확정했습니다. 필요하면 새 후보를 만드세요."}

    def _recover(self, order: dict[str, Any]) -> dict[str, Any]:
        """clientOrderId 또는 (종목·방향·수량·가격·시각)으로 토스 주문을 찾아 원장에 연결한다."""
        order_id = order["id"]
        since_text = self.store.last_event_at(order_id, "SUBMITTING") or order["updated_at"]
        since = datetime.fromisoformat(since_text) - timedelta(seconds=60)
        exact: dict[str, Any] | None = None
        candidates: list[tuple[dict[str, Any], bool]] = []
        checked = 0
        for status in ("OPEN", "CLOSED"):
            rows = self.client.orders(status=status, symbol=order["symbol"], limit=50).get("orders")
            if not isinstance(rows, list):
                raise ValueError("토스 주문 목록 응답 형식이 올바르지 않습니다.")
            for row in rows:
                checked += 1
                client_id = row.get("clientOrderId")
                if client_id:
                    if client_id == order["client_order_id"]:
                        exact = row
                    continue
                if not _same_order(row, order):
                    continue
                candidates.append((row, _ordered_after(row, since)))
        if exact is None and len(candidates) == 1 and candidates[0][1]:
            exact = candidates[0][0]
        if exact is not None and exact.get("orderId"):
            toss_order_id = str(exact["orderId"])
            self.store.transition(
                order_id,
                expected=UNCERTAIN,
                status="SUBMITTED",
                event_type="RECOVERED",
                payload=exact,
                toss_order_id=toss_order_id,
            )
            return {"order_id": order_id, "status": "SUBMITTED", "toss_order_id": toss_order_id,
                    "recovered": True}
        return {
            "order_id": order_id,
            "status": order["status"],
            "recovered": False,
            "checked": checked,
            "candidates": [row.get("orderId") for row, _ in candidates],
            "message": (
                "일치하는 주문이 여러 개라 자동 연결하지 않았습니다. 토스 앱에서 확인하세요."
                if candidates else
                "토스에서 이 주문을 찾지 못했습니다. 잠시 뒤 다시 확인하고, 계속 없으면 "
                f"`toss resolve-order {order_id} --not-placed`로 확정하세요. 재제출하지 마세요."
            ),
        }

    def _unknown_action(self, order_id: int, event_type: str, exc: TossOutcomeUnknown) -> dict[str, Any]:
        order = self.store.get_order(order_id)
        self.store.update_status(order_id, status=order["status"], event_type=event_type, payload=_error_payload(exc))
        return {"mode": "unknown", "order_id": order_id, "status": order["status"],
                "message": f"요청 결과를 알 수 없습니다. `toss sync-order {order_id}`로 확인하세요."}

    def _approved_order(self, order_id: int) -> dict[str, Any]:
        order = self.store.get_order(order_id)
        if order is None:
            raise ValueError(f"주문 원장을 찾을 수 없습니다: {order_id}")
        if order["status"] != "RISK_APPROVED":
            raise ValueError(
                f"실전 제출 가능한 상태가 아닙니다: {order['status']}"
            )
        age = datetime.now(timezone.utc) - datetime.fromisoformat(order["created_at"])
        if age > timedelta(seconds=self.settings.candidate_ttl_seconds):
            self.store.transition(
                order_id,
                expected={"RISK_APPROVED"},
                status="EXPIRED",
                event_type="CANDIDATE_EXPIRED",
                payload={"age_seconds": int(age.total_seconds())},
            )
            raise ValueError("주문 후보 유효시간이 지났습니다. 새 후보를 만드세요.")
        return order

    def _submit_preflight(self, order: dict[str, Any]) -> dict[str, Any]:
        side = order["side"]
        quantity = int(order["quantity"])
        estimated_amount = _decimal(order["estimated_amount"])
        order_price = _decimal(order["price"])
        reasons: list[str] = []
        result: dict[str, Any] = {}
        orderbook = self.client.orderbook(order["symbol"])
        result["orderbook"] = orderbook
        entries = orderbook.get("asks" if side == "BUY" else "bids", [])
        if not entries:
            reasons.append("토스 최우선 호가를 확인할 수 없습니다.")
        else:
            market_price = _decimal(entries[0].get("price", "0"))
            if market_price <= 0:
                reasons.append("토스 최우선 호가가 올바르지 않습니다.")
            else:
                slippage_pct = (
                    abs(market_price - order_price)
                    / order_price
                    * Decimal("100")
                )
                result["referencePrice"] = format(market_price, "f")
                result["slippagePct"] = format(slippage_pct, "f")
                result["maxSlippagePct"] = format(
                    self.settings.max_slippage_pct,
                    "f",
                )
                if slippage_pct > self.settings.max_slippage_pct:
                    reasons.append("토스 호가와 주문 후보 가격의 괴리가 한도를 초과했습니다.")
        if side == "BUY":
            buying_power = self.client.buying_power(order["currency"])
            result["buyingPower"] = buying_power
            if estimated_amount > _decimal(
                buying_power.get("cashBuyingPower", "0")
            ):
                reasons.append("제출 직전 현금 매수 가능 금액이 부족합니다.")
        else:
            sellable = self.client.sellable_quantity(order["symbol"])
            result["sellableQuantity"] = sellable
            if Decimal(quantity) > _decimal(
                sellable.get("sellableQuantity", "0")
            ):
                reasons.append("제출 직전 판매 가능 수량이 부족합니다.")
        result["decision"] = "BLOCK" if reasons else "ALLOW"
        result["reasons"] = reasons
        return result


UNCERTAIN = {"SUBMITTING", "SUBMIT_UNKNOWN"}


def _error_payload(exc: TossError) -> dict[str, Any]:
    # 예외 메시지 원문은 저장하지 않는다(인증 정보가 섞일 수 있음).
    return {"error": type(exc).__name__, "status_code": exc.status_code,
            "code": exc.code, "request_id": exc.request_id}


def _same_order(row: dict[str, Any], order: dict[str, Any]) -> bool:
    try:
        return (
            str(row.get("symbol") or "").upper() == order["symbol"]
            and str(row.get("side") or "").upper() == order["side"]
            and _decimal(row.get("quantity")) == Decimal(order["quantity"])
            and (row.get("price") is None or _decimal(row["price"]) == _decimal(order["price"]))
        )
    except ValueError:
        return False


def _ordered_after(row: dict[str, Any], since: datetime) -> bool:
    try:
        stamp = datetime.fromisoformat(str(row.get("orderedAt")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return stamp.tzinfo is not None and stamp >= since


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"숫자 변환에 실패했습니다: {value}") from exc
