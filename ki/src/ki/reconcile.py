from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from ki.client import KISClient
from ki.models import BrokerOrder, Fill, OrderEvent, OrderIntent
from ki.order_state import (
    ACCEPTED,
    CANCELED,
    EXPIRED,
    FILLED,
    PARTIALLY_FILLED,
    REJECTED,
    OrderStateError,
    assert_order_transition,
)
from ki.storage import SnapshotStore


class OrderReconciliationService:
    """KIS 주문 조회 결과를 로컬 원장에 맞춘다.

    - 주문 이벤트는 처음 본 주문이거나 상태가 바뀐 때만 남긴다(매 주기 같은 행을 다시 쓰지 않는다).
    - 상태 변경은 주문 상태 머신을 따른다. 종결 상태를 되돌리는 조회 결과는 반영하지 않는다.
    - KIS 체결 수량·금액은 누적값이므로 이미 기록한 합계와의 차이만 새 체결로 남긴다.
    """

    def __init__(self, client: KISClient, store: SnapshotStore) -> None:
        self.client = client
        self.store = store

    def reconcile_overseas(
        self,
        *,
        exchange: str = "NASD",
        start_date: str | None = None,
        end_date: str | None = None,
        days: int = 7,
    ) -> dict[str, Any]:
        if start_date is None or end_date is None:
            end = date.today()
            start = end - timedelta(days=days)
            start_date = start.strftime("%Y%m%d")
            end_date = end.strftime("%Y%m%d")

        pending_rows = self.client.overseas_pending_orders(exchange=exchange)
        history_rows = self.client.overseas_order_history(
            start_date=start_date,
            end_date=end_date,
            exchange=exchange,
        )

        counts = {
            "reconciled_pending": 0,
            "reconciled_history": 0,
            "fill_snapshots": 0,
            "new_fills": 0,
            "events": 0,
            "conflicts": 0,
        }
        for source, rows in (("pending", pending_rows), ("history", history_rows)):
            for row in rows:
                order_id, event_type = self._upsert_reconciled_order(row, source=source)
                counts[f"reconciled_{source}"] += 1
                if event_type == "RECONCILE_CONFLICT":
                    counts["conflicts"] += 1
                elif event_type is not None:
                    self._record_event(order_id, event_type, row)
                    counts["events"] += 1
                if source == "history" and _decimal(row.get("ft_ccld_qty")) > 0:
                    counts["fill_snapshots"] += 1
                    if self._record_fill_delta(order_id, row):
                        counts["new_fills"] += 1

        return {
            "exchange": exchange,
            "start_date": start_date,
            "end_date": end_date,
            "pending_rows": len(pending_rows),
            "history_rows": len(history_rows),
            **counts,
        }

    def _upsert_reconciled_order(
        self,
        row: dict[str, Any],
        source: str,
    ) -> tuple[int, str | None]:
        """원장 주문 ID와 남길 이벤트 종류를 돌려준다. 바뀐 것이 없으면 이벤트는 None."""
        kis_order_id = str(row.get("odno") or "").strip()
        if not kis_order_id:
            raise ValueError("KIS 주문 응답에 odno가 없습니다.")

        status = _status_from_kis_row(row)
        existing = self.store.find_broker_order_by_kis_order_id(kis_order_id)
        if existing is not None:
            order_id = int(existing["id"])
            current = str(existing["status"])
            if status is None or status == current:
                return order_id, None
            try:
                assert_order_transition(current, status)
            except OrderStateError:
                # 늦게 도착한 조회 결과가 종결 상태를 되돌리려는 경우. 원장을 바꾸지 않는다.
                return order_id, "RECONCILE_CONFLICT"
            self.store.update_broker_order_status(order_id, status)
            return order_id, f"RECONCILED_{source.upper()}"

        status = status or ACCEPTED
        intent_id = self.store.save_order_intent(
            OrderIntent(
                source=f"kis_reconcile:{source}",
                market="overseas",
                symbol=_symbol(row),
                side=_side(row),
                quantity=_int(row.get("ft_ord_qty")),
                order_type="limit",
                limit_price=_decimal(row.get("ft_ord_unpr3")),
                currency=str(row.get("tr_crcy_cd") or "USD"),
                exchange=str(row.get("ovrs_excg_cd") or "NASD"),
                status="RECONCILED",
            )
        )
        order = BrokerOrder(
            order_intent_id=intent_id,
            symbol=_symbol(row),
            side=_side(row),
            quantity=_int(row.get("ft_ord_qty")),
            order_type="limit",
            limit_price=_decimal(row.get("ft_ord_unpr3")),
            currency=str(row.get("tr_crcy_cd") or "USD"),
            exchange=str(row.get("ovrs_excg_cd") or "NASD"),
            status=status,
            kis_order_id=kis_order_id,
        )
        return self.store.save_broker_order(order), f"RECONCILED_{source.upper()}"

    def _record_event(
        self,
        order_id: int,
        event_type: str,
        row: dict[str, Any],
    ) -> None:
        self.store.save_order_event(
            OrderEvent(
                order_id=order_id,
                event_type=event_type,
                status=_status_from_kis_row(row) or ACCEPTED,
                message="KIS 해외주식 주문 조회 결과를 원장에 대사했습니다.",
                payload={"kis": row},
            )
        )

    def _record_fill_delta(self, order_id: int, row: dict[str, Any]) -> bool:
        """누적 체결에서 아직 기록하지 않은 만큼만 체결로 남긴다. 새로 기록하면 True."""
        kis_order_id = str(row.get("odno") or "").strip()
        cumulative_quantity = _int(row.get("ft_ccld_qty"))
        recorded_quantity, recorded_amount = self.store.fill_totals(order_id)
        delta = cumulative_quantity - recorded_quantity
        if delta <= 0:
            return False

        cumulative_amount = _decimal(row.get("ft_ccld_amt3"))
        if cumulative_amount > recorded_amount:
            price = (cumulative_amount - recorded_amount) / delta
        else:
            price = _decimal(row.get("ft_ccld_unpr3"))
        price = price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        self.store.save_fill(
            Fill(
                order_id=order_id,
                symbol=_symbol(row),
                side=_side(row),
                quantity=delta,
                price=price,
                currency=str(row.get("tr_crcy_cd") or "USD"),
                raw=row,
                # 누적 수량이 서명이다. 같은 누적 상태를 다시 봐도 중복 저장하지 않는다.
                fill_signature=f"{kis_order_id}:cumulative:{cumulative_quantity}",
            )
        )
        return True


def _status_from_kis_row(row: dict[str, Any]) -> str | None:
    """KIS 주문 행의 상태. 판단할 근거가 없으면 None(기존 원장 상태 유지)."""
    rejected = str(row.get("rjct_rson") or row.get("rjct_rson_name") or "").strip()
    if rejected:
        return REJECTED

    cancel_value = str(
        row.get("cncl_yn")
        or row.get("rvse_cncl_dvsn_name")
        or row.get("rvse_cncl_dvsn")
        or row.get("rvse_cncl_dvsn_cd")
        or ""
    ).strip()
    if cancel_value in {"Y", "취소", "02"}:
        return CANCELED

    ordered = _decimal(row.get("ft_ord_qty"))
    filled = _decimal(row.get("ft_ccld_qty"))
    if ordered > 0 and filled >= ordered:
        return FILLED

    raw_pending = row.get("nccs_qty")
    if raw_pending in {None, ""}:
        # 미체결 수량을 모르면 끝났는지 알 수 없다.
        return PARTIALLY_FILLED if filled > 0 else None
    pending = _decimal(raw_pending)
    if pending > 0:
        return PARTIALLY_FILLED if filled > 0 else ACCEPTED
    # 취소·거부 표시 없이 잔량이 0인데 전량 체결도 아니면 주문 기간이 끝난 것이다.
    return EXPIRED


def _symbol(row: dict[str, Any]) -> str:
    return str(row.get("pdno") or row.get("ovrs_pdno") or "").upper()


def _side(row: dict[str, Any]) -> str:
    value = str(row.get("sll_buy_dvsn_cd") or row.get("sll_buy_dvsn") or "")
    if value == "01":
        return "sell"
    if value == "02":
        return "buy"
    name = str(row.get("sll_buy_dvsn_cd_name") or "").lower()
    if "sell" in name or "매도" in name:
        return "sell"
    return "buy"


def _int(value: Any) -> int:
    return int(_decimal(value))


def _decimal(value: Any) -> Decimal:
    if value in {None, ""}:
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return Decimal("0")
