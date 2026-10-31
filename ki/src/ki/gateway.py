from __future__ import annotations

from dataclasses import replace
from typing import Any

from ki.client import KISClient, KISError, KISOutcomeUnknown
from ki.config import Settings
from ki.models import OrderEvent, broker_order_from_intent, make_order_intent
from ki.order_state import (
    SUBMITTING,
    SUBMIT_UNKNOWN,
    ACCEPTED,
    CANCEL_REQUESTED,
    PREFLIGHT_BLOCKED,
    REJECTED,
    SUBMITTED,
    VOIDED,
    assert_order_cancelable,
    assert_order_submittable,
    assert_order_transition,
    assert_order_voidable,
)
from ki.killswitch import assert_orders_allowed
from ki.risk import RiskEngine
from ki.storage import SnapshotStore


class KISGateway:
    def __init__(
        self,
        settings: Settings,
        store: SnapshotStore,
        risk_engine: RiskEngine | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.risk_engine = risk_engine or RiskEngine(settings)

    def create_order_intent(
        self,
        *,
        side: str,
        symbol: str,
        quantity: int,
        price: str,
        source: str = "cli",
        exchange: str = "NASD",
        order_type: str = "limit",
        execute: bool = False,
        buying_power: dict[str, Any] | None = None,
        position: dict[str, Any] | None = None,
        client: KISClient | None = None,
        check_buying_power: bool = False,
        check_position: bool = False,
    ) -> dict[str, Any]:
        intent = make_order_intent(
            side=side,
            symbol=symbol,
            quantity=quantity,
            price=price,
            source=source,
            exchange=exchange,
            order_type=order_type,
        )
        if (
            check_buying_power
            and buying_power is None
            and client is not None
            and intent.side == "buy"
            and intent.order_type == "limit"
            and intent.limit_price > 0
        ):
            buying_power = client.overseas_buying_power(
                intent.symbol,
                format(intent.limit_price, "f"),
                intent.exchange,
            )
        if (
            check_position
            and position is None
            and client is not None
            and intent.side == "sell"
        ):
            balance = client.overseas_balance(intent.exchange, "USD")
            position = _find_overseas_position(
                balance.get("positions", []),
                intent.symbol,
            )

        decision = self.risk_engine.evaluate(
            intent,
            execute=execute,
            buying_power=buying_power,
            position=position,
        )
        intent_status = "BLOCKED" if decision.decision == "BLOCK" else "DRY_RUN_READY"
        intent = replace(intent, status=intent_status)

        intent_id = self.store.save_order_intent(intent)
        intent = replace(intent, id=intent_id)

        decision_id = self.store.save_risk_decision(
            replace(decision, order_intent_id=intent_id)
        )
        decision = replace(
            decision,
            id=decision_id,
            order_intent_id=intent_id,
        )

        order = None
        order_event = None
        if decision.decision == "ALLOW":
            draft_order = broker_order_from_intent(intent)
            order_id = self.store.save_broker_order(draft_order)
            order = replace(draft_order, id=order_id)
            draft_event = OrderEvent(
                order_id=order_id,
                event_type="DRY_RUN_CREATED",
                status=order.status,
                message="리스크 검증을 통과한 dry-run 주문 원장을 생성했습니다.",
                payload={"order_intent_id": intent_id},
            )
            event_id = self.store.save_order_event(draft_event)
            order_event = replace(draft_event, id=event_id)

        return {
            "mode": "dry-run",
            "message": "Gateway MVP는 주문 후보와 리스크 결정만 기록하며 주문 API를 호출하지 않습니다.",
            "intent": intent.to_dict(),
            "risk_decision": decision.to_dict(),
            "buying_power": buying_power,
            "position": position,
            "order": order.to_dict() if order else None,
            "order_event": order_event.to_dict() if order_event else None,
        }

    def submit_order(
        self,
        *,
        order_id: int,
        client: KISClient,
        execute: bool,
    ) -> dict[str, Any]:
        self.settings.require_order_permission(execute=execute)
        # 원장 상태를 바꾸기 전에 막는다. 해제 후에는 같은 후보를 다시 제출할 수 있다.
        assert_orders_allowed(self.risk_engine.kill_switch_paths)

        order = self.store.get_broker_order(order_id)
        if order is None:
            raise ValueError(f"주문 원장을 찾을 수 없습니다: {order_id}")
        assert_order_submittable(order["status"])
        if order["order_type"] != "limit":
            raise ValueError("해외주식 Gateway는 지정가 주문만 전송합니다.")

        preflight = self._submit_preflight(order, client)
        if preflight["risk_decision"]["decision"] == "BLOCK":
            assert_order_transition(order["status"], PREFLIGHT_BLOCKED)
            self.store.update_broker_order_status(order_id, PREFLIGHT_BLOCKED)
            event_id = self.store.save_order_event(
                OrderEvent(
                    order_id=order_id,
                    event_type="SUBMIT_PREFLIGHT_BLOCKED",
                    status=PREFLIGHT_BLOCKED,
                    message="실전 주문 전송 직전 리스크 재검증에서 차단했습니다.",
                    payload=preflight,
                )
            )
            return {
                "mode": "blocked",
                "order_id": order_id,
                "status": PREFLIGHT_BLOCKED,
                "risk_decision": preflight["risk_decision"],
                "buying_power": preflight["buying_power"],
                "position": preflight["position"],
                "order_event_id": event_id,
            }

        preflight_event_id = self.store.save_order_event(
            OrderEvent(
                order_id=order_id,
                event_type="SUBMIT_PREFLIGHT_PASSED",
                status=order["status"],
                message="실전 주문 전송 직전 리스크 재검증을 통과했습니다.",
                payload=preflight,
            )
        )

        # 전송 전에 먼저 기록한다. 이후 어떤 실패가 나도 이 후보는 다시 전송될 수 없다.
        if not self.store.claim_broker_order(order_id, expected=order["status"], status=SUBMITTING):
            raise ValueError("이미 다른 프로세스가 이 주문을 전송 중이거나 전송했습니다.")
        self.store.save_order_event(
            OrderEvent(order_id=order_id, event_type="SUBMITTING", status=SUBMITTING,
                       message="KIS 주문 API 호출 직전 상태를 기록했습니다.", payload={})
        )
        try:
            response = client.overseas_order(
                side=order["side"],
                symbol=order["symbol"],
                quantity=int(order["quantity"]),
                price=order["limit_price"],
                exchange=order["exchange"],
                order_type=order["order_type"],
            )
        except KISOutcomeUnknown as exc:
            self.store.update_broker_order_status(order_id, SUBMIT_UNKNOWN)
            self.store.save_order_event(
                OrderEvent(order_id=order_id, event_type="SUBMIT_UNKNOWN", status=SUBMIT_UNKNOWN,
                           message="전송 결과를 알 수 없습니다. 재전송하지 말고 대사로 확인하세요.",
                           payload={"error": type(exc).__name__})
            )
            return {
                "mode": "unknown",
                "order_id": order_id,
                "status": SUBMIT_UNKNOWN,
                "message": "주문이 KIS에 접수됐는지 알 수 없습니다. 다시 전송하지 말고 "
                           "`ki reconcile-orders`와 KIS 앱으로 확인하세요.",
            }
        except KISError as exc:
            # 업무 오류(rt_cd != 0)나 4xx: KIS가 주문을 거부했다.
            self.store.update_broker_order_status(order_id, REJECTED)
            self.store.save_order_event(
                OrderEvent(order_id=order_id, event_type="SUBMIT_REJECTED", status=REJECTED,
                           message="KIS가 주문을 거부했습니다.", payload={"error": type(exc).__name__})
            )
            raise
        kis_order_id = _extract_kis_order_id(response)
        submitted_status = ACCEPTED if kis_order_id else SUBMITTED
        assert_order_transition(SUBMITTING, submitted_status)
        self.store.mark_broker_order_submitted(
            order_id,
            status=submitted_status,
            kis_order_id=kis_order_id,
        )
        event_id = self.store.save_order_event(
            OrderEvent(
                order_id=order_id,
                event_type="SUBMITTED_TO_KIS",
                status=submitted_status,
                message="KIS 해외주식 주문 API로 지정가 주문을 전송했습니다.",
                payload={"kis_response": response},
            )
        )
        return {
            "mode": "live",
            "order_id": order_id,
            "status": submitted_status,
            "kis_order_id": kis_order_id,
            "kis_response": response,
            "order_event_id": event_id,
            "preflight_event_id": preflight_event_id,
        }

    def cancel_order(
        self,
        *,
        order_id: int,
        client: KISClient,
        execute: bool,
        quantity: int | None = None,
    ) -> dict[str, Any]:
        self.settings.require_order_permission(execute=execute)

        order = self.store.get_broker_order(order_id)
        if order is None:
            raise ValueError(f"주문 원장을 찾을 수 없습니다: {order_id}")
        assert_order_cancelable(order["status"])
        if not order["kis_order_id"]:
            raise ValueError("KIS 원주문번호가 없어 취소할 수 없습니다.")

        cancel_quantity = quantity or int(order["quantity"])
        if cancel_quantity <= 0:
            raise ValueError("취소 수량은 1주 이상이어야 합니다.")
        if cancel_quantity > int(order["quantity"]):
            raise ValueError("취소 수량이 원 주문 수량보다 큽니다.")

        response = client.overseas_cancel_order(
            symbol=order["symbol"],
            original_order_id=order["kis_order_id"],
            quantity=cancel_quantity,
            exchange=order["exchange"],
        )
        assert_order_transition(order["status"], CANCEL_REQUESTED)
        self.store.update_broker_order_status(order_id, CANCEL_REQUESTED)
        event_id = self.store.save_order_event(
            OrderEvent(
                order_id=order_id,
                event_type="CANCEL_REQUESTED",
                status=CANCEL_REQUESTED,
                message="KIS 해외주식 주문 취소 API로 취소 요청을 전송했습니다.",
                payload={
                    "cancel_quantity": cancel_quantity,
                    "original_kis_order_id": order["kis_order_id"],
                    "kis_response": response,
                },
            )
        )
        return {
            "mode": "live",
            "order_id": order_id,
            "status": CANCEL_REQUESTED,
            "cancel_quantity": cancel_quantity,
            "original_kis_order_id": order["kis_order_id"],
            "kis_response": response,
            "order_event_id": event_id,
        }

    def void_order(
        self,
        *,
        order_id: int,
        reason: str = "",
        confirm_not_placed: bool = False,
    ) -> dict[str, Any]:
        order = self.store.get_broker_order(order_id)
        if order is None:
            raise ValueError(f"주문 원장을 찾을 수 없습니다: {order_id}")
        assert_order_voidable(order["status"])
        if order["status"] == SUBMIT_UNKNOWN and not confirm_not_placed:
            raise ValueError(
                "결과 불명 주문은 KIS 앱·대사로 미접수를 확인한 뒤 --confirm-not-placed로만 폐기할 수 있습니다."
            )
        if order["kis_order_id"]:
            raise ValueError("KIS 주문번호가 있는 주문은 로컬 폐기할 수 없습니다.")

        assert_order_transition(order["status"], VOIDED)
        self.store.update_broker_order_status(order_id, VOIDED)
        event_id = self.store.save_order_event(
            OrderEvent(
                order_id=order_id,
                event_type="VOIDED",
                status=VOIDED,
                message="KIS에 전송하지 않은 주문 후보를 로컬에서 폐기했습니다.",
                payload={"reason": reason},
            )
        )
        return {
            "mode": "local",
            "order_id": order_id,
            "status": VOIDED,
            "reason": reason,
            "order_event_id": event_id,
        }

    def _submit_preflight(
        self,
        order: dict[str, Any],
        client: KISClient,
    ) -> dict[str, Any]:
        intent = make_order_intent(
            side=order["side"],
            symbol=order["symbol"],
            quantity=int(order["quantity"]),
            price=order["limit_price"],
            exchange=order["exchange"],
            order_type=order["order_type"],
            source="submit-preflight",
        )
        buying_power = None
        position = None
        if intent.side == "buy":
            buying_power = client.overseas_buying_power(
                intent.symbol,
                format(intent.limit_price, "f"),
                intent.exchange,
            )
        if intent.side == "sell":
            balance = client.overseas_balance(intent.exchange, "USD")
            position = _find_overseas_position(
                balance.get("positions", []),
                intent.symbol,
            )

        decision = self.risk_engine.evaluate(
            intent,
            buying_power=buying_power,
            position=position,
        )
        decision_id = self.store.save_risk_decision(
            replace(decision, order_intent_id=order["order_intent_id"])
        )
        decision = replace(
            decision,
            id=decision_id,
            order_intent_id=order["order_intent_id"],
        )
        return {
            "intent": intent.to_dict(),
            "risk_decision": decision.to_dict(),
            "buying_power": buying_power,
            "position": position,
        }


def _extract_kis_order_id(response: dict[str, Any]) -> str | None:
    for key in ("ODNO", "odno", "ORD_NO", "ord_no"):
        value = response.get(key)
        if value:
            return str(value)
    return None


def _find_overseas_position(
    positions: list[dict[str, Any]],
    symbol: str,
) -> dict[str, Any]:
    normalized_symbol = symbol.upper()
    for position in positions:
        position_symbol = (
            position.get("ovrs_pdno")
            or position.get("pdno")
            or position.get("symbol")
        )
        if str(position_symbol).upper() == normalized_symbol:
            return position
    return {"ovrs_pdno": normalized_symbol, "ord_psbl_qty": "0"}
