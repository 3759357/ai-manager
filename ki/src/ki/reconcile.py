from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ki.client import KISClient
from ki.models import BrokerOrder, Fill, OrderEvent, OrderIntent
from ki.order_state import (
    ACCEPTED,
    CANCELED,
    FILLED,
    PARTIALLY_FILLED,
    REJECTED,
)
from ki.storage import SnapshotStore


class OrderReconciliationService:
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

        pending_count = 0
        history_count = 0
        fill_count = 0

        for row in pending_rows:
            order_id = self._upsert_reconciled_order(row, source="pending")
            self._record_event(order_id, "RECONCILED_PENDING", row)
            pending_count += 1

        for row in history_rows:
            order_id = self._upsert_reconciled_order(row, source="history")
            self._record_event(order_id, "RECONCILED_HISTORY", row)
            history_count += 1
            if _decimal(row.get("ft_ccld_qty")) > 0:
                self._save_fill_snapshot(order_id, row)
                fill_count += 1

        return {
            "exchange": exchange,
            "start_date": start_date,
            "end_date": end_date,
            "pending_rows": len(pending_rows),
            "history_rows": len(history_rows),
            "reconciled_pending": pending_count,
            "reconciled_history": history_count,
            "fill_snapshots": fill_count,
        }

    def _upsert_reconciled_order(self, row: dict[str, Any], source: str) -> int:
        kis_order_id = str(row.get("odno") or "").strip()
        if not kis_order_id:
            raise ValueError("KIS 주문 응답에 odno가 없습니다.")

        status = _status_from_kis_row(row)
        existing = self.store.find_broker_order_by_kis_order_id(kis_order_id)
        if existing is not None:
            self.store.update_broker_order_status(int(existing["id"]), status)
            return int(existing["id"])

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
        return self.store.save_broker_order(order)

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
                status=_status_from_kis_row(row),
                message="KIS 해외주식 주문 조회 결과를 원장에 대사했습니다.",
                payload={"kis": row},
            )
        )

    def _save_fill_snapshot(self, order_id: int, row: dict[str, Any]) -> None:
        kis_order_id = str(row.get("odno") or "").strip()
        quantity = _int(row.get("ft_ccld_qty"))
        price = _decimal(row.get("ft_ccld_unpr3"))
        signature = ":".join(
            [
                kis_order_id,
                str(row.get("ord_dt") or ""),
                str(row.get("ft_ccld_qty") or ""),
                str(row.get("ft_ccld_unpr3") or ""),
                str(row.get("ft_ccld_amt3") or ""),
            ]
        )
        self.store.save_fill(
            Fill(
                order_id=order_id,
                symbol=_symbol(row),
                side=_side(row),
                quantity=quantity,
                price=price,
                currency=str(row.get("tr_crcy_cd") or "USD"),
                raw=row,
                fill_signature=signature,
            )
        )


def _status_from_kis_row(row: dict[str, Any]) -> str:
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
    pending = _decimal(row.get("nccs_qty"))

    if filled > 0 and pending <= 0:
        return FILLED
    if filled > 0 and pending > 0:
        return PARTIALLY_FILLED
    if ordered > 0 and pending > 0:
        return ACCEPTED
    return ACCEPTED


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
