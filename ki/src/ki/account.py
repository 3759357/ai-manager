from __future__ import annotations

from typing import Any

from ki.client import KISClient
from ki.config import Settings
from ki.storage import SnapshotStore


def sync_overseas_account(
    client: KISClient,
    settings: Settings,
    store: SnapshotStore,
    *,
    symbol: str = "AAPL",
    quote_exchange: str = "NAS",
    account_exchange: str = "NASD",
    currency: str = "USD",
) -> dict[str, Any]:
    symbol = symbol.upper()
    quote = client.overseas_quote(symbol, quote_exchange)
    quote_price = str(quote.get("last", "")).strip()
    if not quote_price:
        raise ValueError(f"{symbol} 현재가 응답에 last 값이 없습니다.")

    balance = client.overseas_balance(account_exchange, currency)
    buying_power = client.overseas_buying_power(
        symbol,
        quote_price,
        account_exchange,
    )
    snapshot_id = store.save_overseas_account(
        environment=settings.environment,
        symbol=symbol,
        exchange=account_exchange.upper(),
        currency=currency.upper(),
        quote_price=quote_price,
        balance=balance,
        buying_power=buying_power,
    )
    return {
        "snapshot_id": snapshot_id,
        "database": str(store.database_path),
        "environment": settings.environment,
        "symbol": symbol,
        "quote_price": quote_price,
        "balance": balance,
        "buying_power": buying_power,
    }
