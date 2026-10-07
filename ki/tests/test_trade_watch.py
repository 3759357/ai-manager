import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ki.storage import SnapshotStore
from ki.trade_watch import (
    TradeWatcher,
    TradeWatchOptions,
    candle_bucket,
    trade_tr_key,
)
from ki.websocket_client import OverseasTrade


class TradeWatchTest(unittest.TestCase):
    def test_trade_key_and_bucket(self) -> None:
        self.assertEqual(trade_tr_key("aapl", "regular"), "DNASAAPL")
        self.assertEqual(trade_tr_key("aapl", "daytime"), "RBAQAAPL")
        self.assertEqual(
            candle_bucket("20260623", "103159", 5),
            "2026-06-23T10:30:00",
        )

    def test_trade_tick_builds_candle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "trades.db")
            watcher = TradeWatcher(FakeClient(), store)
            first = watcher.process_trade(make_trade(price="100", quantity="3"), tr_key="DNASAAPL")
            second = watcher.process_trade(make_trade(price="102", quantity="2"), tr_key="DNASAAPL")
            ticks = store.list_trade_ticks()
            candles = store.list_trade_candles(symbol="AAPL")

        self.assertEqual(len(ticks), 2)
        self.assertEqual(first["candle"]["open_price"], "100")
        self.assertEqual(second["candle"]["close_price"], "102")
        self.assertEqual(candles[0]["high_price"], "102")
        self.assertEqual(Decimal(candles[0]["volume"]), Decimal("5"))
        self.assertEqual(candles[0]["trade_count"], 2)

    def test_volume_and_sell_pressure_create_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "trades.db")
            watcher = TradeWatcher(
                FakeClient(),
                store,
                options=TradeWatchOptions(
                    baseline_bars=2,
                    volume_surge_multiplier=Decimal("2"),
                    sell_pressure_ratio=Decimal("1.5"),
                ),
            )
            watcher.process_trade(
                make_trade(date="20260623", time="103000", quantity="10"),
                tr_key="DNASAAPL",
            )
            watcher.process_trade(
                make_trade(date="20260623", time="103100", quantity="10"),
                tr_key="DNASAAPL",
            )
            result = watcher.process_trade(
                make_trade(
                    date="20260623",
                    time="103200",
                    quantity="25",
                    sell_quantity="300",
                    buy_quantity="100",
                ),
                tr_key="DNASAAPL",
            )

        self.assertEqual(result["signal"]["signal_type"], "TRADE_SELL_VOLUME_SURGE")
        self.assertEqual(result["signal"]["volume_ratio"], "2.5000")


class FakeClient:
    pass


def make_trade(
    *,
    date: str = "20260623",
    time: str = "103015",
    price: str = "100",
    quantity: str = "1",
    sell_quantity: str = "100",
    buy_quantity: str = "100",
) -> OverseasTrade:
    return OverseasTrade(
        symbol="AAPL",
        local_business_date=date,
        local_date=date,
        local_time=time,
        korea_date=date,
        korea_time=time,
        open_price="99",
        high_price="101",
        low_price="98",
        last_price=price,
        change_sign="2",
        change="1",
        change_rate="1",
        bid_price="99.9",
        ask_price="100.1",
        bid_quantity="10",
        ask_quantity="10",
        trade_quantity=quantity,
        cumulative_volume="1000",
        cumulative_amount="100000",
        sell_trade_quantity=sell_quantity,
        buy_trade_quantity=buy_quantity,
        execution_strength="100",
        market_status="2",
    )
