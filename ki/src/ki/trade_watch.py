from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, AsyncIterator

from ki.storage import SnapshotStore
from ki.websocket_client import KISWebSocketClient, OverseasTrade


@dataclass(frozen=True)
class TradeWatchOptions:
    session: str = "regular"
    max_events: int | None = None
    interval_minutes: int = 1
    baseline_bars: int = 5
    volume_surge_multiplier: Decimal = Decimal("3")
    sell_pressure_ratio: Decimal = Decimal("1.5")


class TradeWatcher:
    def __init__(
        self,
        client: KISWebSocketClient,
        store: SnapshotStore,
        *,
        options: TradeWatchOptions | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.options = options or TradeWatchOptions()
        validate_trade_watch_options(self.options)
        self._signaled_buckets: set[tuple[str, str]] = set()

    async def watch(self, symbol: str) -> AsyncIterator[dict[str, Any]]:
        symbol = symbol.upper()
        tr_key = trade_tr_key(symbol, self.options.session)
        seen = 0
        stream = self.client.trades(tr_key)
        try:
            async for trade in stream:
                seen += 1
                yield self.process_trade(trade, tr_key=tr_key)
                if (
                    self.options.max_events is not None
                    and seen >= self.options.max_events
                ):
                    break
        finally:
            await stream.aclose()

    def process_trade(
        self,
        trade: OverseasTrade,
        *,
        tr_key: str,
    ) -> dict[str, Any]:
        raw = trade.to_dict()
        tick_id = self.store.save_trade_tick(
            symbol=trade.symbol,
            tr_key=tr_key,
            session=self.options.session,
            local_date=trade.local_date,
            local_time=trade.local_time,
            korea_date=trade.korea_date,
            korea_time=trade.korea_time,
            price=trade.last_price,
            quantity=trade.trade_quantity,
            cumulative_volume=trade.cumulative_volume,
            cumulative_amount=trade.cumulative_amount,
            sell_trade_quantity=trade.sell_trade_quantity,
            buy_trade_quantity=trade.buy_trade_quantity,
            execution_strength=trade.execution_strength,
            raw=raw,
        )
        bucket_at = candle_bucket(
            trade.local_date,
            trade.local_time,
            self.options.interval_minutes,
        )
        candle = self.store.upsert_trade_candle(
            symbol=trade.symbol,
            session=self.options.session,
            interval_minutes=self.options.interval_minutes,
            bucket_at=bucket_at,
            price=trade.last_price,
            quantity=trade.trade_quantity,
        )
        signal = self._detect_signal(trade, candle)
        return {
            "tick_id": tick_id,
            "symbol": trade.symbol,
            "tr_key": tr_key,
            "session": self.options.session,
            "price": trade.last_price,
            "quantity": trade.trade_quantity,
            "cumulative_volume": trade.cumulative_volume,
            "execution_strength": trade.execution_strength,
            "candle": candle,
            "signal": signal,
        }

    def _detect_signal(
        self,
        trade: OverseasTrade,
        candle: dict[str, Any],
    ) -> dict[str, Any] | None:
        bucket_at = str(candle["bucket_at"])
        signal_key = (trade.symbol, bucket_at)
        if signal_key in self._signaled_buckets:
            return None

        previous = self.store.previous_trade_candle_volumes(
            symbol=trade.symbol,
            session=self.options.session,
            interval_minutes=self.options.interval_minutes,
            before_bucket_at=bucket_at,
            limit=self.options.baseline_bars,
        )
        if len(previous) < self.options.baseline_bars:
            return None
        average_volume = sum((_decimal(value) for value in previous), Decimal("0"))
        average_volume /= Decimal(len(previous))
        current_volume = _decimal(candle["volume"])
        if average_volume <= 0:
            return None
        volume_ratio = current_volume / average_volume
        if volume_ratio < self.options.volume_surge_multiplier:
            return None

        sell_quantity = _decimal(trade.sell_trade_quantity)
        buy_quantity = _decimal(trade.buy_trade_quantity)
        sell_buy_ratio = (
            sell_quantity / buy_quantity
            if buy_quantity > 0
            else Decimal("999")
        )
        sell_pressure = sell_buy_ratio >= self.options.sell_pressure_ratio
        signal_type = (
            "TRADE_SELL_VOLUME_SURGE"
            if sell_pressure
            else "TRADE_VOLUME_SURGE"
        )
        payload = {
            "bucket_at": bucket_at,
            "interval_minutes": self.options.interval_minutes,
            "current_volume": format(current_volume, "f"),
            "average_volume": format(average_volume, "f"),
            "volume_ratio": format(volume_ratio, ".4f"),
            "sell_trade_quantity": trade.sell_trade_quantity,
            "buy_trade_quantity": trade.buy_trade_quantity,
            "sell_buy_ratio": format(sell_buy_ratio, ".4f"),
            "execution_strength": trade.execution_strength,
            "last_price": trade.last_price,
        }
        signal_id = self.store.save_market_signal(
            symbol=trade.symbol,
            signal_type=signal_type,
            severity="HIGH" if sell_pressure else "MEDIUM",
            message=(
                f"{trade.symbol} {signal_type}: "
                f"volume_ratio={payload['volume_ratio']}, "
                f"sell_buy_ratio={payload['sell_buy_ratio']}"
            ),
            payload=payload,
        )
        self._signaled_buckets.add(signal_key)
        return {
            "id": signal_id,
            "signal_type": signal_type,
            **payload,
        }


def validate_trade_watch_options(options: TradeWatchOptions) -> None:
    if options.session not in {"regular", "daytime"}:
        raise ValueError("session은 regular 또는 daytime이어야 합니다.")
    if options.max_events is not None and options.max_events <= 0:
        raise ValueError("--max-events는 1 이상이어야 합니다.")
    if options.interval_minutes <= 0:
        raise ValueError("--interval-minutes는 1 이상이어야 합니다.")
    if options.baseline_bars <= 0:
        raise ValueError("--baseline-bars는 1 이상이어야 합니다.")
    if options.volume_surge_multiplier <= 0:
        raise ValueError("--volume-surge-multiplier는 0보다 커야 합니다.")
    if options.sell_pressure_ratio <= 0:
        raise ValueError("--sell-pressure-ratio는 0보다 커야 합니다.")


def trade_tr_key(symbol: str, session: str) -> str:
    symbol = symbol.upper()
    if session == "regular":
        return f"DNAS{symbol}"
    if session == "daytime":
        return f"RBAQ{symbol}"
    raise ValueError("--session은 regular 또는 daytime이어야 합니다.")


def candle_bucket(date_text: str, time_text: str, interval_minutes: int) -> str:
    if len(date_text) != 8 or len(time_text) < 4:
        raise ValueError("체결 일시 형식이 올바르지 않습니다.")
    hour = int(time_text[0:2])
    minute = int(time_text[2:4])
    bucket_minute = (
        Decimal(minute) / Decimal(interval_minutes)
    ).to_integral_value(rounding=ROUND_FLOOR) * interval_minutes
    return (
        f"{date_text[0:4]}-{date_text[4:6]}-{date_text[6:8]}"
        f"T{hour:02d}:{int(bucket_minute):02d}:00"
    )


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"숫자 변환에 실패했습니다: {value}") from exc
