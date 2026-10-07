from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from ki.client import KISClient
from ki.config import Settings
from ki.gateway import KISGateway
from ki.storage import SnapshotStore


WATCHLIST_ACTIONS = {"none", "buy", "sell"}


@dataclass(frozen=True)
class WatchlistConfig:
    symbol: str
    quote_exchange: str = "NAS"
    account_exchange: str = "NASD"
    enabled: bool = True
    quantity: int = 1
    hot_up_pct: Decimal = Decimal("1")
    hot_down_pct: Decimal = Decimal("1")
    action_on_hot_up: str = "none"
    action_on_hot_down: str = "none"
    limit_offset_pct: Decimal = Decimal("0.02")


@dataclass(frozen=True)
class MarketWatchOptions:
    auto_submit: bool = False
    execute: bool = False


class MarketWatcher:
    def __init__(
        self,
        client: KISClient,
        settings: Settings,
        store: SnapshotStore,
        gateway: KISGateway | None = None,
        *,
        options: MarketWatchOptions | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.gateway = gateway or KISGateway(settings, store)
        self.options = options or MarketWatchOptions()

    def scan_once(self) -> dict[str, Any]:
        results = []
        for item in self.store.list_watchlist(enabled_only=True):
            results.append(self.scan_symbol(item))
        return {
            "symbols": len(results),
            "results": results,
        }

    def scan_symbol(self, item: dict[str, Any]) -> dict[str, Any]:
        config = _config_from_row(item)
        quote = self.client.overseas_quote(config.symbol, config.quote_exchange)
        price = _quote_price(quote)
        previous = self.store.latest_market_snapshot(config.symbol)
        snapshot_id = self.store.save_market_snapshot(
            symbol=config.symbol,
            price=format(price, "f"),
            quote=quote,
        )
        price_change_pct = _price_change_pct(price, previous)

        signals = []
        if price_change_pct is not None:
            if price_change_pct >= config.hot_up_pct:
                signals.append(
                    self._handle_signal(
                        config,
                        signal_type="HOT_UP",
                        price=price,
                        previous=previous,
                        price_change_pct=price_change_pct,
                        snapshot_id=snapshot_id,
                    )
                )
            elif price_change_pct <= -config.hot_down_pct:
                signals.append(
                    self._handle_signal(
                        config,
                        signal_type="HOT_DOWN",
                        price=price,
                        previous=previous,
                        price_change_pct=price_change_pct,
                        snapshot_id=snapshot_id,
                    )
                )

        return {
            "symbol": config.symbol,
            "price": format(price, "f"),
            "previous_price": previous["price"] if previous else None,
            "price_change_pct": (
                format(price_change_pct, "f") if price_change_pct is not None else None
            ),
            "snapshot_id": snapshot_id,
            "signals": signals,
        }

    def _handle_signal(
        self,
        config: WatchlistConfig,
        *,
        signal_type: str,
        price: Decimal,
        previous: dict[str, Any] | None,
        price_change_pct: Decimal,
        snapshot_id: int,
    ) -> dict[str, Any]:
        action = (
            config.action_on_hot_up
            if signal_type == "HOT_UP"
            else config.action_on_hot_down
        )
        message = (
            f"{config.symbol} {signal_type}: "
            f"{format(price_change_pct, 'f')}% change"
        )
        payload: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "price": format(price, "f"),
            "previous_price": previous["price"] if previous else None,
            "price_change_pct": format(price_change_pct, "f"),
            "action": action,
            "auto_submit": self.options.auto_submit,
            "execute": self.options.execute,
        }
        order_id = None

        if action in {"buy", "sell"}:
            order_result = self._create_order_candidate(config, action, price)
            payload["order_result"] = order_result
            if order_result.get("order"):
                order_id = int(order_result["order"]["id"])
                if self.options.auto_submit:
                    submit_result = self.gateway.submit_order(
                        order_id=order_id,
                        client=self.client,
                        execute=self.options.execute,
                    )
                    payload["submit_result"] = submit_result

        signal_id = self.store.save_market_signal(
            symbol=config.symbol,
            signal_type=signal_type,
            severity="HIGH",
            message=message,
            payload=payload,
            order_id=order_id,
        )
        return {
            "id": signal_id,
            "symbol": config.symbol,
            "signal_type": signal_type,
            "message": message,
            "action": action,
            "order_id": order_id,
        }

    def _create_order_candidate(
        self,
        config: WatchlistConfig,
        side: str,
        price: Decimal,
    ) -> dict[str, Any]:
        limit_price = _limit_price(
            price,
            side=side,
            offset_pct=config.limit_offset_pct,
        )
        return self.gateway.create_order_intent(
            side=side,
            symbol=config.symbol,
            quantity=config.quantity,
            price=format(limit_price, "f"),
            source="market-watch",
            exchange=config.account_exchange,
            client=self.client,
            check_buying_power=side == "buy",
            check_position=side == "sell",
        )


def validate_watchlist_config(config: WatchlistConfig) -> None:
    if not config.symbol:
        raise ValueError("관심종목 symbol이 필요합니다.")
    if config.quantity <= 0:
        raise ValueError("관심종목 주문 수량은 1 이상이어야 합니다.")
    if config.hot_up_pct < 0 or config.hot_down_pct < 0:
        raise ValueError("핫 시그널 기준 퍼센트는 0 이상이어야 합니다.")
    if config.limit_offset_pct < 0:
        raise ValueError("지정가 offset은 0 이상이어야 합니다.")
    if config.action_on_hot_up not in WATCHLIST_ACTIONS:
        raise ValueError("action_on_hot_up은 none, buy, sell 중 하나여야 합니다.")
    if config.action_on_hot_down not in WATCHLIST_ACTIONS:
        raise ValueError("action_on_hot_down은 none, buy, sell 중 하나여야 합니다.")


def _config_from_row(row: dict[str, Any]) -> WatchlistConfig:
    config = WatchlistConfig(
        symbol=str(row["symbol"]).upper(),
        quote_exchange=str(row["quote_exchange"]).upper(),
        account_exchange=str(row["account_exchange"]).upper(),
        enabled=bool(row["enabled"]),
        quantity=int(row["quantity"]),
        hot_up_pct=_decimal(row["hot_up_pct"]),
        hot_down_pct=_decimal(row["hot_down_pct"]),
        action_on_hot_up=str(row["action_on_hot_up"]),
        action_on_hot_down=str(row["action_on_hot_down"]),
        limit_offset_pct=_decimal(row["limit_offset_pct"]),
    )
    validate_watchlist_config(config)
    return config


def _quote_price(quote: dict[str, Any]) -> Decimal:
    for key in ("last", "stck_prpr", "ovrs_nmix_prpr", "price"):
        value = quote.get(key)
        if value is not None and value != "":
            return _decimal(value)
    raise ValueError("시세 응답에서 현재가를 찾지 못했습니다.")


def _price_change_pct(
    price: Decimal,
    previous: dict[str, Any] | None,
) -> Decimal | None:
    if previous is None:
        return None
    previous_price = _decimal(previous["price"])
    if previous_price <= 0:
        return None
    return ((price - previous_price) / previous_price * Decimal("100")).quantize(
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
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"숫자 변환에 실패했습니다: {value}") from exc
