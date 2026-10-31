from __future__ import annotations

from pathlib import Path
from typing import Any

from ki.config import Settings
from ki.killswitch import kill_switch_reason
from ki.models import OrderIntent, RiskDecision


class RiskEngine:
    def __init__(
        self,
        settings: Settings,
        kill_switch_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.kill_switch_paths = (
            (kill_switch_path,) if kill_switch_path else settings.kill_switch_paths
        )

    def kill_switch_reason(self) -> str | None:
        return kill_switch_reason(self.kill_switch_paths)

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        execute: bool = False,
        buying_power: dict[str, Any] | None = None,
        position: dict[str, Any] | None = None,
    ) -> RiskDecision:
        reasons: list[str] = []
        checks: dict[str, Any] = {
            "execute": execute,
            "kill_switch_paths": [str(path) for path in self.kill_switch_paths if path],
            "estimated_amount": format(intent.estimated_amount, "f"),
            "max_overseas_order_amount_usd": format(
                self.settings.max_overseas_order_amount_usd,
                "f",
            ),
        }

        if self.kill_switch_reason() is not None:
            reasons.append("Kill Switch가 활성화되어 신규 주문 후보를 차단했습니다.")

        if intent.market != "overseas":
            reasons.append("Gateway MVP는 해외주식 주문 후보만 지원합니다.")

        if intent.side not in {"buy", "sell"}:
            reasons.append("side는 buy 또는 sell이어야 합니다.")

        if intent.quantity <= 0:
            reasons.append("주문 수량은 1주 이상이어야 합니다.")

        if intent.order_type != "limit":
            reasons.append("시장가 주문은 금지되어 있습니다. 지정가 주문만 허용합니다.")

        if intent.limit_price <= 0:
            reasons.append("지정가 주문에는 0보다 큰 가격이 필요합니다.")

        if intent.estimated_amount > self.settings.max_overseas_order_amount_usd:
            reasons.append(
                "추정 주문금액이 해외주식 1회 주문 한도를 초과했습니다."
            )

        if execute:
            reasons.append("Gateway MVP는 아직 실전 주문 전송을 지원하지 않습니다.")

        if buying_power is not None and intent.side == "buy":
            max_quantity = _safe_int(
                buying_power.get("max_ord_psbl_qty")
                or buying_power.get("ovrs_max_ord_psbl_qty")
                or buying_power.get("ord_psbl_qty")
            )
            checks["max_orderable_quantity"] = max_quantity
            if max_quantity is not None and intent.quantity > max_quantity:
                reasons.append("매수가능수량이 주문 후보 수량보다 적습니다.")

        if position is not None and intent.side == "sell":
            sellable_quantity = _safe_int(
                position.get("ord_psbl_qty")
                or position.get("ovrs_cblc_qty")
                or position.get("cblc_qty")
            )
            checks["sellable_quantity"] = sellable_quantity
            if sellable_quantity is not None and intent.quantity > sellable_quantity:
                reasons.append("매도가능수량이 주문 후보 수량보다 적습니다.")

        return RiskDecision(
            decision="BLOCK" if reasons else "ALLOW",
            reasons=reasons,
            checks=checks,
        )


def _safe_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return None
