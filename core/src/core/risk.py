from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


@dataclass(frozen=True)
class PortfolioPosition:
    symbol: str
    quantity: Decimal
    price: Decimal
    currency: str = "USD"
    leveraged: bool = False

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.price

    def to_dict(self, total_equity: Decimal) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": _fmt(self.quantity),
            "price": _fmt(self.price),
            "currency": self.currency,
            "leveraged": self.leveraged,
            "market_value": _fmt(self.market_value),
            "weight_pct": _fmt_pct(_pct(self.market_value, total_equity)),
        }


@dataclass(frozen=True)
class PortfolioRiskLimits:
    min_cash_pct: Decimal = Decimal("35")
    max_leveraged_exposure_pct: Decimal = Decimal("35")
    max_single_position_pct: Decimal = Decimal("25")
    max_daily_new_buy_usd: Decimal = Decimal("1500")
    panic_buy_block_minutes: int = 30

    def validate(self) -> None:
        for name, value in (
            ("min_cash_pct", self.min_cash_pct),
            ("max_leveraged_exposure_pct", self.max_leveraged_exposure_pct),
            ("max_single_position_pct", self.max_single_position_pct),
            ("max_daily_new_buy_usd", self.max_daily_new_buy_usd),
        ):
            if value < 0:
                raise ValueError(f"{name}는 0 이상이어야 합니다.")
        if self.panic_buy_block_minutes < 0:
            raise ValueError("panic_buy_block_minutes는 0 이상이어야 합니다.")


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: Decimal
    positions: tuple[PortfolioPosition, ...]
    currency: str = "USD"
    pending_buy_amount: Decimal = Decimal("0")

    @property
    def position_value(self) -> Decimal:
        return sum((position.market_value for position in self.positions), Decimal("0"))

    @property
    def total_equity(self) -> Decimal:
        return self.cash + self.position_value

    @property
    def projected_cash(self) -> Decimal:
        return self.cash - self.pending_buy_amount

    @property
    def projected_total_equity(self) -> Decimal:
        return self.total_equity

    @property
    def leveraged_exposure(self) -> Decimal:
        return sum(
            (
                position.market_value
                for position in self.positions
                if position.leveraged
            ),
            Decimal("0"),
        )

    def validate(self) -> None:
        if self.cash < 0:
            raise ValueError("cash는 0 이상이어야 합니다.")
        if self.pending_buy_amount < 0:
            raise ValueError("pending_buy_amount는 0 이상이어야 합니다.")
        if self.total_equity <= 0:
            raise ValueError("총자산은 0보다 커야 합니다.")
        for position in self.positions:
            if position.quantity <= 0:
                raise ValueError("포지션 수량은 0보다 커야 합니다.")
            if position.price <= 0:
                raise ValueError("포지션 가격은 0보다 커야 합니다.")
            if position.currency != self.currency:
                raise ValueError("포트폴리오 통화가 섞여 있습니다.")


def evaluate_portfolio_risk(
    snapshot: PortfolioSnapshot,
    limits: PortfolioRiskLimits,
) -> dict[str, Any]:
    snapshot.validate()
    limits.validate()

    total = snapshot.projected_total_equity
    cash_pct = _pct(snapshot.projected_cash, total)
    leveraged_pct = _pct(snapshot.leveraged_exposure, total)
    single = _largest_position(snapshot.positions)
    single_pct = _pct(single.market_value, total) if single else Decimal("0")
    reasons: list[str] = []
    warnings: list[str] = []

    if snapshot.projected_cash < 0:
        reasons.append("대기 매수 반영 후 현금이 음수입니다.")
    if cash_pct < limits.min_cash_pct:
        reasons.append(
            "현금 비중이 최소 기준보다 낮습니다: "
            f"{_fmt_pct(cash_pct)} < {_fmt_pct(limits.min_cash_pct)}"
        )
    if leveraged_pct > limits.max_leveraged_exposure_pct:
        reasons.append(
            "레버리지 노출이 최대 기준을 초과했습니다: "
            f"{_fmt_pct(leveraged_pct)} > "
            f"{_fmt_pct(limits.max_leveraged_exposure_pct)}"
        )
    if single and single_pct > limits.max_single_position_pct:
        reasons.append(
            "단일 포지션 비중이 최대 기준을 초과했습니다: "
            f"{single.symbol} {_fmt_pct(single_pct)} > "
            f"{_fmt_pct(limits.max_single_position_pct)}"
        )
    if snapshot.pending_buy_amount > limits.max_daily_new_buy_usd:
        warnings.append(
            "대기 매수 금액이 일일 신규매수 기준을 초과했습니다: "
            f"{_fmt(snapshot.pending_buy_amount)} > "
            f"{_fmt(limits.max_daily_new_buy_usd)}"
        )

    decision = "BLOCK" if reasons else ("WARN" if warnings else "ALLOW")
    return {
        "decision": decision,
        "currency": snapshot.currency,
        "cash": _fmt(snapshot.cash),
        "projected_cash": _fmt(snapshot.projected_cash),
        "position_value": _fmt(snapshot.position_value),
        "total_equity": _fmt(total),
        "pending_buy_amount": _fmt(snapshot.pending_buy_amount),
        "cash_pct": _fmt_pct(cash_pct),
        "leveraged_exposure": _fmt(snapshot.leveraged_exposure),
        "leveraged_exposure_pct": _fmt_pct(leveraged_pct),
        "largest_position": (
            {
                "symbol": single.symbol,
                "market_value": _fmt(single.market_value),
                "weight_pct": _fmt_pct(single_pct),
            }
            if single
            else None
        ),
        "limits": {
            "min_cash_pct": _fmt_pct(limits.min_cash_pct),
            "max_leveraged_exposure_pct": _fmt_pct(
                limits.max_leveraged_exposure_pct
            ),
            "max_single_position_pct": _fmt_pct(limits.max_single_position_pct),
            "max_daily_new_buy_usd": _fmt(limits.max_daily_new_buy_usd),
            "panic_buy_block_minutes": limits.panic_buy_block_minutes,
        },
        "positions": [
            position.to_dict(total)
            for position in sorted(snapshot.positions, key=lambda row: row.symbol)
        ],
        "reasons": reasons,
        "warnings": warnings,
    }


def _largest_position(
    positions: tuple[PortfolioPosition, ...],
) -> PortfolioPosition | None:
    if not positions:
        return None
    return max(positions, key=lambda position: position.market_value)


def _pct(value: Decimal, total: Decimal) -> Decimal:
    if total <= 0:
        return Decimal("0")
    return (value / total * Decimal("100")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _fmt(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _fmt_pct(value: Decimal) -> str:
    return _fmt(value)
