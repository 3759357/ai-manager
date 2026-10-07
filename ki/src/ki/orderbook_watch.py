from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, AsyncIterator

from ki.client import KISClient
from ki.gateway import KISGateway
from ki.storage import SnapshotStore
from ki.websocket_client import KISWebSocketClient, OverseasOrderBook


ORDERBOOK_ACTIONS = {"none", "buy", "sell"}


@dataclass(frozen=True)
class OrderBookWatchOptions:
    session: str = "regular"
    max_events: int | None = None
    bid_pressure_threshold: Decimal = Decimal("30")
    ask_pressure_threshold: Decimal = Decimal("-30")
    action_on_bid_pressure: str = "none"
    action_on_ask_pressure: str = "none"
    quantity: int = 1
    account_exchange: str = "NASD"
    limit_offset_pct: Decimal = Decimal("0.02")
    order_cooldown_seconds: float = 60.0


class OrderBookWatcher:
    def __init__(
        self,
        client: KISWebSocketClient,
        store: SnapshotStore,
        gateway: KISGateway | None = None,
        order_client: KISClient | None = None,
        *,
        options: OrderBookWatchOptions | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.gateway = gateway
        self.order_client = order_client
        self.options = options or OrderBookWatchOptions()
        validate_orderbook_options(self.options)
        self._last_order_candidate_at: dict[tuple[str, str, str], float] = {}

    async def watch(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        symbol = symbol.upper()
        tr_key = orderbook_tr_key(symbol, self.options.session)
        seen = 0
        stream = self.client.orderbooks(tr_key)
        try:
            async for book in stream:
                seen += 1
                yield self.process_book(book, tr_key=tr_key)
                if (
                    self.options.max_events is not None
                    and seen >= self.options.max_events
                ):
                    break
        finally:
            await stream.aclose()

    def process_book(self, book: OverseasOrderBook, *, tr_key: str) -> dict[str, Any]:
        metrics = orderbook_metrics(book)
        snapshot_id = self.store.save_orderbook_snapshot(
            symbol=book.symbol,
            tr_key=tr_key,
            session=self.options.session,
            bid_price=book.bid_price,
            ask_price=book.ask_price,
            bid_quantity=book.bid_quantity,
            ask_quantity=book.ask_quantity,
            total_bid_quantity=book.total_bid_quantity,
            total_ask_quantity=book.total_ask_quantity,
            spread=metrics["spread"],
            pressure_score=metrics["pressure_score"],
            raw=book.to_dict(),
        )
        signals = []
        pressure = _decimal(metrics["pressure_score"])
        if pressure >= self.options.bid_pressure_threshold:
            signals.append(
                self._save_signal(
                    book,
                    signal_type="ORDERBOOK_BID_PRESSURE",
                    snapshot_id=snapshot_id,
                    metrics=metrics,
                )
            )
        elif pressure <= self.options.ask_pressure_threshold:
            signals.append(
                self._save_signal(
                    book,
                    signal_type="ORDERBOOK_ASK_PRESSURE",
                    snapshot_id=snapshot_id,
                    metrics=metrics,
                )
            )

        return {
            "snapshot_id": snapshot_id,
            "symbol": book.symbol,
            "tr_key": tr_key,
            "session": self.options.session,
            "bid_price": book.bid_price,
            "ask_price": book.ask_price,
            "bid_quantity": book.bid_quantity,
            "ask_quantity": book.ask_quantity,
            "total_bid_quantity": book.total_bid_quantity,
            "total_ask_quantity": book.total_ask_quantity,
            **metrics,
            "signals": signals,
        }

    def _save_signal(
        self,
        book: OverseasOrderBook,
        *,
        signal_type: str,
        snapshot_id: int,
        metrics: dict[str, str],
    ) -> dict[str, Any]:
        message = (
            f"{book.symbol} {signal_type}: "
            f"pressure={metrics['pressure_score']}, spread={metrics['spread']}"
        )
        payload = {
            "snapshot_id": snapshot_id,
            "session": self.options.session,
            "bid_price": book.bid_price,
            "ask_price": book.ask_price,
            "bid_quantity": book.bid_quantity,
            "ask_quantity": book.ask_quantity,
            "total_bid_quantity": book.total_bid_quantity,
            "total_ask_quantity": book.total_ask_quantity,
            **metrics,
        }
        order_id = None
        action = (
            self.options.action_on_bid_pressure
            if signal_type == "ORDERBOOK_BID_PRESSURE"
            else self.options.action_on_ask_pressure
        )
        payload["action"] = action
        if action in {"buy", "sell"}:
            cooldown_key = (book.symbol, signal_type, action)
            if self._cooldown_active(cooldown_key):
                payload["order_skipped_reason"] = "order_candidate_cooldown"
            else:
                order_result = self._create_order_candidate(book, action)
                payload["order_result"] = order_result
                self._last_order_candidate_at[cooldown_key] = time.monotonic()
                if order_result.get("order"):
                    order_id = int(order_result["order"]["id"])

        signal_id = self.store.save_market_signal(
            symbol=book.symbol,
            signal_type=signal_type,
            severity="MEDIUM",
            message=message,
            payload=payload,
            order_id=order_id,
        )
        return {
            "id": signal_id,
            "symbol": book.symbol,
            "signal_type": signal_type,
            "message": message,
            "action": action,
            "order_id": order_id,
        }

    def _cooldown_active(self, key: tuple[str, str, str]) -> bool:
        if self.options.order_cooldown_seconds <= 0:
            return False
        last_seen = self._last_order_candidate_at.get(key)
        if last_seen is None:
            return False
        return (time.monotonic() - last_seen) < self.options.order_cooldown_seconds

    def _create_order_candidate(
        self,
        book: OverseasOrderBook,
        side: str,
    ) -> dict[str, Any]:
        if self.gateway is None or self.order_client is None:
            raise ValueError(
                "호가 신호 주문 후보 생성에는 gateway와 order_client가 필요합니다."
            )

        base_price = book.ask_price if side == "buy" else book.bid_price
        limit_price = _limit_price(
            _decimal(base_price),
            side=side,
            offset_pct=self.options.limit_offset_pct,
        )
        return self.gateway.create_order_intent(
            side=side,
            symbol=book.symbol,
            quantity=self.options.quantity,
            price=format(limit_price, "f"),
            source="orderbook-watch",
            exchange=self.options.account_exchange,
            client=self.order_client,
            check_buying_power=side == "buy",
            check_position=side == "sell",
        )


def validate_orderbook_options(options: OrderBookWatchOptions) -> None:
    if options.max_events is not None and options.max_events <= 0:
        raise ValueError("--max-events는 1 이상이어야 합니다.")
    if options.quantity <= 0:
        raise ValueError("호가 신호 주문 후보 수량은 1 이상이어야 합니다.")
    if options.limit_offset_pct < 0:
        raise ValueError("지정가 offset은 0 이상이어야 합니다.")
    if options.order_cooldown_seconds < 0:
        raise ValueError("주문 후보 쿨다운은 0 이상이어야 합니다.")
    if options.action_on_bid_pressure not in ORDERBOOK_ACTIONS:
        raise ValueError("action_on_bid_pressure는 none, buy, sell 중 하나여야 합니다.")
    if options.action_on_ask_pressure not in ORDERBOOK_ACTIONS:
        raise ValueError("action_on_ask_pressure는 none, buy, sell 중 하나여야 합니다.")


def orderbook_tr_key(symbol: str, session: str) -> str:
    symbol = symbol.upper()
    if session == "regular":
        return f"DNAS{symbol}"
    if session == "daytime":
        return f"RBAQ{symbol}"
    raise ValueError("--session은 regular 또는 daytime이어야 합니다.")


def orderbook_metrics(book: OverseasOrderBook) -> dict[str, str]:
    total_bid = _decimal(book.total_bid_quantity)
    total_ask = _decimal(book.total_ask_quantity)
    bid_qty = _decimal(book.bid_quantity)
    ask_qty = _decimal(book.ask_quantity)
    spread = _decimal(book.spread)

    total_pressure = _pressure_score(total_bid, total_ask)
    top_pressure = _pressure_score(bid_qty, ask_qty)
    imbalance_ratio = _ratio(total_bid, total_ask)

    return {
        "spread": format(spread, "f"),
        "pressure_score": format(total_pressure, "f"),
        "top_pressure_score": format(top_pressure, "f"),
        "imbalance_ratio": format(imbalance_ratio, "f"),
    }


def _pressure_score(bid_quantity: Decimal, ask_quantity: Decimal) -> Decimal:
    total = bid_quantity + ask_quantity
    if total <= 0:
        return Decimal("0")
    return ((bid_quantity - ask_quantity) / total * Decimal("100")).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (numerator / denominator).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )


def _limit_price(
    price: Decimal,
    *,
    side: str,
    offset_pct: Decimal,
) -> Decimal:
    factor = offset_pct / Decimal("100")
    if side == "buy":
        value = price * (Decimal("1") + factor)
    else:
        value = price * (Decimal("1") - factor)
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal:
    try:
        text = str(value).replace(",", "")
        if text == "-":
            return Decimal("0")
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"숫자 변환에 실패했습니다: {value}") from exc
