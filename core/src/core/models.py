from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class BrokerCapabilities:
    broker: str
    rest_quotes: bool
    rest_orderbook: bool
    websocket_orderbook: bool
    commissions: bool
    orders: bool
    modify_orders: bool
    cancel_orders: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "rest_quotes": self.rest_quotes,
            "rest_orderbook": self.rest_orderbook,
            "websocket_orderbook": self.websocket_orderbook,
            "commissions": self.commissions,
            "orders": self.orders,
            "modify_orders": self.modify_orders,
            "cancel_orders": self.cancel_orders,
        }


@dataclass(frozen=True)
class NormalizedQuote:
    broker: str
    symbol: str
    price: Decimal
    currency: str
    timestamp: str | None
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "symbol": self.symbol,
            "price": format(self.price, "f"),
            "currency": self.currency,
            "timestamp": self.timestamp,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class NormalizedOrderBook:
    broker: str
    symbol: str
    bid_price: Decimal | None
    ask_price: Decimal | None
    bid_quantity: Decimal | None
    ask_quantity: Decimal | None
    currency: str
    timestamp: str | None
    raw: dict[str, Any]

    @property
    def spread(self) -> Decimal | None:
        if self.bid_price is None or self.ask_price is None:
            return None
        return self.ask_price - self.bid_price

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "symbol": self.symbol,
            "bid_price": _format_optional(self.bid_price),
            "ask_price": _format_optional(self.ask_price),
            "bid_quantity": _format_optional(self.bid_quantity),
            "ask_quantity": _format_optional(self.ask_quantity),
            "spread": _format_optional(self.spread),
            "currency": self.currency,
            "timestamp": self.timestamp,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class UnifiedOrderRequest:
    broker: str
    side: str
    symbol: str
    quantity: int
    price: Decimal
    currency: str = "USD"
    source: str = "core"

    def validate(self) -> None:
        if self.broker not in {"kis", "toss"}:
            raise ValueError("broker는 kis 또는 toss여야 합니다.")
        if self.side not in {"buy", "sell"}:
            raise ValueError("side는 buy 또는 sell이어야 합니다.")
        if not self.symbol:
            raise ValueError("symbol이 필요합니다.")
        if self.quantity <= 0:
            raise ValueError("quantity는 1 이상이어야 합니다.")
        if self.price <= 0:
            raise ValueError("price는 0보다 커야 합니다.")
        if self.currency not in {"USD", "KRW"}:
            raise ValueError("currency는 USD 또는 KRW여야 합니다.")


def _format_optional(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
