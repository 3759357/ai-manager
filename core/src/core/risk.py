"""주문 전 계좌 단위 리스크 평가.

한도는 리스크관리부 정책 파일(policy/v1.toml) 하나에서 온다. 환경변수 한도는 정책보다
엄격하게 만드는 쪽으로만 반영한다. 새 주문이 있으면 그 주문을 반영한 뒤의 비중·노출·
일일 매수 합계로 판단한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from core.config import Settings
    from core.guard import Policy

ONE = Decimal("1")


@dataclass(frozen=True)
class PortfolioPosition:
    symbol: str
    quantity: Decimal
    price: Decimal
    currency: str = "USD"
    leveraged: bool = False
    # 레버리지 상품의 배수. 실효 노출 = 평가액 × 배수.
    leverage_multiplier: Decimal = ONE

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.price

    @property
    def effective_exposure(self) -> Decimal:
        return self.market_value * self.leverage_multiplier if self.leveraged else Decimal("0")

    def to_dict(self, total_equity: Decimal) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": _fmt(self.quantity),
            "price": _fmt(self.price),
            "currency": self.currency,
            "leveraged": self.leveraged,
            "leverage_multiplier": format(self.leverage_multiplier, "f"),
            "market_value": _fmt(self.market_value),
            "weight_pct": _fmt_pct(_pct(self.market_value, total_equity)),
        }


@dataclass(frozen=True)
class OrderImpact:
    """평가할 새 매수 주문."""

    symbol: str
    amount: Decimal
    leverage_multiplier: Decimal = ONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "amount": _fmt(self.amount),
            "leverage_multiplier": format(self.leverage_multiplier, "f"),
        }


@dataclass(frozen=True)
class PortfolioRiskLimits:
    # 기본값은 정책 v1과 같다. 운영 경로는 from_policy()로 만든다.
    min_cash_pct: Decimal = Decimal("20")
    max_leveraged_exposure_pct: Decimal = Decimal("10")
    max_single_position_pct: Decimal = Decimal("15")
    max_order_usd: Decimal = Decimal("1500")
    max_daily_new_buy_usd: Decimal = Decimal("1500")
    panic_buy_block_minutes: int = 30
    source: str = "defaults"

    @classmethod
    def from_policy(
        cls,
        policy: "Policy",
        settings: "Settings | None" = None,
    ) -> "PortfolioRiskLimits":
        """정책 한도를 쓰되, 설정값이 있으면 더 엄격한 쪽만 받아들인다."""
        min_cash = policy.account["min_cash_pct"]
        leveraged = policy.position["max_leveraged_effective_pct"]
        single = policy.position["max_single_pct"]
        order = policy.order["max_order_usd"]
        daily = policy.order["max_daily_buy_usd"]
        panic = 30
        tightened: list[str] = []
        if settings is not None:
            if settings.min_cash_pct is not None and settings.min_cash_pct > min_cash:
                min_cash = settings.min_cash_pct
                tightened.append("min_cash_pct")
            if (settings.max_leveraged_exposure_pct is not None
                    and settings.max_leveraged_exposure_pct < leveraged):
                leveraged = settings.max_leveraged_exposure_pct
                tightened.append("max_leveraged_exposure_pct")
            if (settings.max_single_position_pct is not None
                    and settings.max_single_position_pct < single):
                single = settings.max_single_position_pct
                tightened.append("max_single_position_pct")
            if settings.max_daily_new_buy_usd is not None and settings.max_daily_new_buy_usd < daily:
                daily = settings.max_daily_new_buy_usd
                order = min(order, daily)
                tightened.append("max_daily_new_buy_usd")
            panic = settings.panic_buy_block_minutes
        source = f"policy {policy.version} ({policy.source})"
        if tightened:
            source += " + 환경변수로 강화: " + ", ".join(tightened)
        return cls(
            min_cash_pct=min_cash,
            max_leveraged_exposure_pct=leveraged,
            max_single_position_pct=single,
            max_order_usd=order,
            max_daily_new_buy_usd=daily,
            panic_buy_block_minutes=panic,
            source=source,
        )

    def validate(self) -> None:
        for name, value in (
            ("min_cash_pct", self.min_cash_pct),
            ("max_leveraged_exposure_pct", self.max_leveraged_exposure_pct),
            ("max_single_position_pct", self.max_single_position_pct),
            ("max_order_usd", self.max_order_usd),
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
    # 오늘(뉴욕 기준) 원장에 기록된 매수 금액. 모르면 None.
    bought_today: Decimal | None = None

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
        return sum((position.effective_exposure for position in self.positions), Decimal("0"))

    def symbol_value(self, symbol: str) -> Decimal:
        return sum(
            (position.market_value for position in self.positions if position.symbol == symbol),
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
    *,
    order: OrderImpact | None = None,
) -> dict[str, Any]:
    """계좌 리스크를 평가한다.

    order가 있으면 그 매수가 들어간 뒤를 기준으로 판단한다. 이때 order 금액은 이미
    snapshot.pending_buy_amount에 들어 있어야 한다(현금 계산에 한 번만 반영).
    """
    snapshot.validate()
    limits.validate()

    total = snapshot.projected_total_equity
    cash_pct = _pct(snapshot.projected_cash, total)
    leveraged_exposure = snapshot.leveraged_exposure
    if order is not None and order.leverage_multiplier > ONE:
        leveraged_exposure += order.amount * order.leverage_multiplier
    leveraged_pct = _pct(leveraged_exposure, total)
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
            "레버리지 실효 노출이 최대 기준을 초과했습니다: "
            f"{_fmt_pct(leveraged_pct)} > "
            f"{_fmt_pct(limits.max_leveraged_exposure_pct)}"
        )
    if single and single_pct > limits.max_single_position_pct:
        reasons.append(
            "단일 포지션 비중이 최대 기준을 초과했습니다: "
            f"{single.symbol} {_fmt_pct(single_pct)} > "
            f"{_fmt_pct(limits.max_single_position_pct)}"
        )

    order_detail: dict[str, Any] | None = None
    if order is not None:
        after_value = snapshot.symbol_value(order.symbol) + order.amount
        after_pct = _pct(after_value, total)
        order_detail = {
            **order.to_dict(),
            "symbol_weight_after_pct": _fmt_pct(after_pct),
            "bought_today": _fmt(snapshot.bought_today) if snapshot.bought_today is not None else None,
        }
        if order.amount > limits.max_order_usd:
            reasons.append(
                f"주문 금액이 주문당 한도를 초과했습니다: {_fmt(order.amount)} > {_fmt(limits.max_order_usd)}"
            )
        if after_pct > limits.max_single_position_pct and not (single and single.symbol == order.symbol
                                                               and single_pct > limits.max_single_position_pct):
            reasons.append(
                "매수 후 종목 비중이 최대 기준을 초과합니다: "
                f"{order.symbol} {_fmt_pct(after_pct)} > {_fmt_pct(limits.max_single_position_pct)}"
            )
        if snapshot.bought_today is None:
            reasons.append("오늘 매수 합계를 확인할 수 없어 매수하지 않습니다.")
        elif snapshot.bought_today + order.amount > limits.max_daily_new_buy_usd:
            reasons.append(
                "오늘 매수 합계가 일일 한도를 초과합니다: "
                f"{_fmt(snapshot.bought_today + order.amount)} > {_fmt(limits.max_daily_new_buy_usd)}"
            )
    elif snapshot.pending_buy_amount > limits.max_daily_new_buy_usd:
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
        "leveraged_exposure": _fmt(leveraged_exposure),
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
        "order": order_detail,
        "limits": {
            "source": limits.source,
            "min_cash_pct": _fmt_pct(limits.min_cash_pct),
            "max_leveraged_exposure_pct": _fmt_pct(
                limits.max_leveraged_exposure_pct
            ),
            "max_single_position_pct": _fmt_pct(limits.max_single_position_pct),
            "max_order_usd": _fmt(limits.max_order_usd),
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
