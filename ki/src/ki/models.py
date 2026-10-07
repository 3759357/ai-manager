from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


def parse_money(value: str | int | float | Decimal) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("가격은 숫자여야 합니다.") from exc
    return amount


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal
    source: str = "cli"
    market: str = "overseas"
    exchange: str = "NASD"
    order_type: str = "limit"
    currency: str = "USD"
    status: str = "REQUESTED"
    id: int | None = None

    @property
    def estimated_amount(self) -> Decimal:
        return self.limit_price * Decimal(self.quantity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "market": self.market,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": format(self.limit_price, "f"),
            "currency": self.currency,
            "exchange": self.exchange,
            "estimated_amount": format(self.estimated_amount, "f"),
            "status": self.status,
        }


@dataclass(frozen=True)
class RiskDecision:
    decision: str
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    order_intent_id: int | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_intent_id": self.order_intent_id,
            "decision": self.decision,
            "reasons": self.reasons,
            "checks": self.checks,
        }


@dataclass(frozen=True)
class BrokerOrder:
    order_intent_id: int
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Decimal
    currency: str = "USD"
    exchange: str = "NASD"
    status: str = "RISK_APPROVED"
    kis_order_id: str | None = None
    id: int | None = None

    @property
    def estimated_amount(self) -> Decimal:
        return self.limit_price * Decimal(self.quantity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_intent_id": self.order_intent_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "limit_price": format(self.limit_price, "f"),
            "currency": self.currency,
            "exchange": self.exchange,
            "estimated_amount": format(self.estimated_amount, "f"),
            "status": self.status,
            "kis_order_id": self.kis_order_id,
        }


@dataclass(frozen=True)
class Fill:
    order_id: int
    symbol: str
    side: str
    quantity: int
    price: Decimal
    currency: str = "USD"
    fee: Decimal = Decimal("0")
    raw: dict[str, Any] = field(default_factory=dict)
    fill_signature: str | None = None
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "price": format(self.price, "f"),
            "currency": self.currency,
            "fee": format(self.fee, "f"),
            "fill_signature": self.fill_signature,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class OrderEvent:
    order_id: int
    event_type: str
    status: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "event_type": self.event_type,
            "status": self.status,
            "message": self.message,
            "payload": self.payload,
        }


def make_order_intent(
    *,
    side: str,
    symbol: str,
    quantity: int,
    price: str | int | float | Decimal,
    source: str = "cli",
    exchange: str = "NASD",
    order_type: str = "limit",
) -> OrderIntent:
    return OrderIntent(
        symbol=symbol.upper(),
        side=side.lower(),
        quantity=quantity,
        limit_price=parse_money(price),
        source=source,
        exchange=exchange.upper(),
        order_type=order_type.lower(),
    )


def broker_order_from_intent(intent: OrderIntent) -> BrokerOrder:
    if intent.id is None:
        raise ValueError("BrokerOrder 생성에는 저장된 OrderIntent ID가 필요합니다.")
    return BrokerOrder(
        order_intent_id=intent.id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        currency=intent.currency,
        exchange=intent.exchange,
    )
