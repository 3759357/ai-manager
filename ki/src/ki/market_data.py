"""Collect quotes and trades on a shared KIS websocket connection."""
from __future__ import annotations

import asyncio
import logging
import os

from ki.config import Settings
from ki.orderbook_watch import OrderBookWatcher, OrderBookWatchOptions, orderbook_tr_key
from ki.storage import SnapshotStore
from ki.trade_watch import TradeWatcher, TradeWatchOptions, trade_tr_key
from ki.websocket_client import (
    KISWebSocketClient,
    OVERSEAS_ORDERBOOK_TR_ID,
    OVERSEAS_TRADE_TR_ID,
    parse_overseas_orderbook,
    parse_overseas_trades,
)


async def collect() -> None:
    settings = Settings.from_env()
    session = os.getenv("KIS_MARKET_SESSION", "regular")
    book_key = orderbook_tr_key(os.getenv("KIS_ORDERBOOK_SYMBOL", "AAPL"), session)
    trade_key = trade_tr_key(os.getenv("KIS_TRADE_SYMBOL", "GOOGL"), session)
    client = KISWebSocketClient(settings)
    store = SnapshotStore(settings.database_path)
    books = OrderBookWatcher(client, store, options=OrderBookWatchOptions(session=session))
    trades = TradeWatcher(client, store, options=TradeWatchOptions(session=session))
    logging.info("Collecting orderbook=%s trades=%s on one connection", book_key, trade_key)
    async for tr_id, payload, count in client.stream_many([
        (OVERSEAS_ORDERBOOK_TR_ID, book_key), (OVERSEAS_TRADE_TR_ID, trade_key),
    ]):
        if tr_id == OVERSEAS_ORDERBOOK_TR_ID:
            books.process_book(parse_overseas_orderbook(payload), tr_key=book_key)
        elif tr_id == OVERSEAS_TRADE_TR_ID:
            for trade in parse_overseas_trades(payload, data_count=count):
                trades.process_trade(trade, tr_key=trade_key)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(collect())
