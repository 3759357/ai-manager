from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ki.client import KISClient
from ki.config import Settings as KISSettings
from ki.gateway import KISGateway
from ki.storage import SnapshotStore
from toss.client import TossClient
from toss.config import Settings as TossSettings
from toss.gateway import TossGateway
from toss.storage import OrderStore

from core.config import Settings
from core.models import (
    BrokerCapabilities,
    NormalizedOrderBook,
    NormalizedQuote,
    UnifiedOrderRequest,
)
from core.portfolio import (
    KIS_TERMINAL_STATUSES,
    TOSS_TERMINAL_STATUSES,
    parse_kis_cash,
    parse_kis_positions,
    parse_toss_cash,
    parse_toss_holdings,
    pending_buy_amount,
)


class KISBrokerAdapter:
    name = "kis"

    def __init__(
        self,
        settings: KISSettings,
        client: KISClient | None = None,
        store: SnapshotStore | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or KISClient(settings)
        self.store = store or SnapshotStore(settings.database_path)
        self.gateway = KISGateway(settings, self.store)

    @classmethod
    def from_core(cls, core: Settings) -> "KISBrokerAdapter":
        settings = KISSettings(
            environment=core.kis_environment,
            app_key=core.kis_app_key,
            app_secret=core.kis_app_secret,
            account_no=core.kis_account_no,
            account_product_code=core.kis_account_product_code,
            allow_orders=core.kis_allow_orders,
            real_trading_ack=core.kis_real_trading_ack,
            max_order_amount=core.kis_max_order_amount,
            max_overseas_order_amount_usd=(
                core.kis_max_overseas_order_amount_usd
            ),
            timeout_seconds=core.kis_timeout_seconds,
            database_path=core.kis_database_path,
        )
        settings.validate()
        return cls(settings)

    @classmethod
    def from_local_store(cls, core: Settings) -> "KISBrokerAdapter":
        database_path = core.kis_database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        settings = KISSettings(
            environment="demo",
            app_key="local-read-only",
            app_secret="local-read-only",
            account_no="",
            database_path=database_path,
        )
        return cls(settings)

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker=self.name,
            rest_quotes=True,
            rest_orderbook=False,
            websocket_orderbook=True,
            commissions=False,
            orders=True,
            modify_orders=False,
            cancel_orders=True,
        )

    def quote(self, symbol: str) -> NormalizedQuote:
        raw = self.client.overseas_quote(symbol, "NAS")
        return NormalizedQuote(
            broker=self.name,
            symbol=symbol.upper(),
            price=_first_decimal(raw, "last", "stck_prpr", "price"),
            currency="USD",
            timestamp=_first_text(raw, "xymd", "timestamp"),
            raw=raw,
        )

    def create_order_candidate(
        self,
        request: UnifiedOrderRequest,
    ) -> dict[str, Any]:
        request.validate()
        if request.currency != "USD":
            raise ValueError("KIS 통합 Gateway MVP는 미국주식 USD 주문만 지원합니다.")
        result = self.gateway.create_order_intent(
            side=request.side,
            symbol=request.symbol,
            quantity=request.quantity,
            price=format(request.price, "f"),
            source=request.source,
            exchange="NASD",
            client=self.client,
            check_buying_power=request.side == "buy",
            check_position=request.side == "sell",
        )
        order = result.get("order")
        return {
            "broker": self.name,
            "order_id": order.get("id") if order else None,
            "status": (
                order.get("status")
                if order
                else result["risk_decision"]["decision"]
            ),
            "raw": result,
        }

    def list_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_broker_orders(limit=limit)

    def positions(self) -> list[dict[str, Any]]:
        return parse_kis_positions(self.client.overseas_balance())

    def cash(self, reference_symbol: str = "AAPL") -> Decimal:
        quote = self.quote(reference_symbol)
        buying_power = self.client.overseas_buying_power(
            reference_symbol,
            format(quote.price, "f"),
        )
        return parse_kis_cash(buying_power)

    def pending_buy_amount(self) -> Decimal:
        return pending_buy_amount(
            self.store.list_broker_orders(limit=500),
            KIS_TERMINAL_STATUSES,
        )

    def submit_order(self, order_id: int, *, execute: bool) -> dict[str, Any]:
        result = self.gateway.submit_order(
            order_id=order_id,
            client=self.client,
            execute=execute,
        )
        return {"broker": self.name, **result}

    def cancel_order(self, order_id: int, *, execute: bool) -> dict[str, Any]:
        result = self.gateway.cancel_order(
            order_id=order_id,
            client=self.client,
            execute=execute,
        )
        return {"broker": self.name, **result}


class TossBrokerAdapter:
    name = "toss"

    def __init__(
        self,
        settings: TossSettings,
        client: TossClient | None = None,
        store: OrderStore | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or TossClient(settings)
        self.store = store or OrderStore(settings.database_path)
        self.gateway = TossGateway(settings, self.client, self.store)

    @classmethod
    def from_core(cls, core: Settings) -> "TossBrokerAdapter":
        settings = TossSettings(
            client_id=core.toss_client_id,
            client_secret=core.toss_client_secret,
            account_seq=core.toss_account_seq,
            allow_orders=core.toss_allow_orders,
            real_trading_ack=core.toss_real_trading_ack,
            max_order_amount_usd=core.toss_max_order_amount_usd,
            max_order_amount_krw=core.toss_max_order_amount_krw,
            max_slippage_pct=core.toss_max_slippage_pct,
            timeout_seconds=core.toss_timeout_seconds,
            database_path=core.toss_database_path,
        )
        settings.validate()
        return cls(settings)

    @classmethod
    def from_local_store(cls, core: Settings) -> "TossBrokerAdapter":
        database_path = core.toss_database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        settings = TossSettings(
            client_id="local-read-only",
            client_secret="local-read-only",
            database_path=database_path,
        )
        return cls(settings)

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            broker=self.name,
            rest_quotes=True,
            rest_orderbook=True,
            websocket_orderbook=False,
            commissions=True,
            orders=True,
            modify_orders=True,
            cancel_orders=True,
        )

    def quote(self, symbol: str) -> NormalizedQuote:
        rows = self.client.prices([symbol])
        if not rows:
            raise ValueError(f"토스 현재가 응답이 없습니다: {symbol}")
        raw = rows[0]
        return NormalizedQuote(
            broker=self.name,
            symbol=str(raw.get("symbol") or symbol).upper(),
            price=_first_decimal(raw, "lastPrice"),
            currency=str(raw.get("currency") or "USD"),
            timestamp=_first_text(raw, "timestamp"),
            raw=raw,
        )

    def orderbook(self, symbol: str) -> NormalizedOrderBook:
        raw = self.client.orderbook(symbol)
        bids = raw.get("bids", [])
        asks = raw.get("asks", [])
        best_bid = bids[0] if bids else {}
        best_ask = asks[0] if asks else {}
        return NormalizedOrderBook(
            broker=self.name,
            symbol=symbol.upper(),
            bid_price=_optional_decimal(best_bid.get("price")),
            ask_price=_optional_decimal(best_ask.get("price")),
            bid_quantity=_optional_decimal(best_bid.get("volume")),
            ask_quantity=_optional_decimal(best_ask.get("volume")),
            currency=str(raw.get("currency") or "USD"),
            timestamp=_first_text(raw, "timestamp"),
            raw=raw,
        )

    def commissions(self) -> list[dict[str, Any]]:
        return self.client.commissions()

    def create_order_candidate(
        self,
        request: UnifiedOrderRequest,
    ) -> dict[str, Any]:
        request.validate()
        result = self.gateway.create_order_candidate(
            side=request.side,
            symbol=request.symbol,
            quantity=request.quantity,
            price=format(request.price, "f"),
            currency=request.currency,
            source=request.source,
        )
        return {"broker": self.name, **result, "raw": result}

    def list_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.list_orders(limit=limit)

    def positions(self) -> list[dict[str, Any]]:
        return parse_toss_holdings(self.client.holdings())

    def cash(self) -> Decimal:
        return parse_toss_cash(self.client.buying_power("USD"))

    def pending_buy_amount(self) -> Decimal:
        return pending_buy_amount(
            self.store.list_orders(limit=500),
            TOSS_TERMINAL_STATUSES,
        )

    def submit_order(self, order_id: int, *, execute: bool) -> dict[str, Any]:
        result = self.gateway.submit_order(order_id, execute=execute)
        return {"broker": self.name, **result}

    def cancel_order(self, order_id: int, *, execute: bool) -> dict[str, Any]:
        result = self.gateway.cancel_order(order_id, execute=execute)
        return {"broker": self.name, **result}


def _first_decimal(data: dict[str, Any], *keys: str) -> Decimal:
    for key in keys:
        value = data.get(key)
        if value not in {None, ""}:
            return _decimal(value)
    raise ValueError(f"가격 필드를 찾지 못했습니다: {keys}")


def _optional_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return _decimal(value)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"숫자 변환에 실패했습니다: {value}") from exc


def _first_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value not in {None, ""}:
            return str(value)
    return None
