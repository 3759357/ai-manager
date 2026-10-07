import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ki.config import Settings
from ki.gateway import KISGateway
from ki.orderbook_watch import (
    OrderBookWatcher,
    OrderBookWatchOptions,
    orderbook_metrics,
    orderbook_tr_key,
)
from ki.storage import SnapshotStore
from ki.websocket_client import OverseasOrderBook


def settings(**kwargs) -> Settings:
    defaults = {
        "environment": "real",
        "app_key": "key",
        "app_secret": "secret",
        "account_no": "12345678",
        "max_overseas_order_amount_usd": Decimal("500"),
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class OrderBookWatchTest(unittest.TestCase):
    def test_orderbook_tr_key_uses_session_prefix(self) -> None:
        self.assertEqual(orderbook_tr_key("AAPL", "regular"), "DNASAAPL")
        self.assertEqual(orderbook_tr_key("googl", "daytime"), "RBAQGOOGL")
        with self.assertRaises(ValueError):
            orderbook_tr_key("AAPL", "unknown")

    def test_metrics_calculate_pressure_scores(self) -> None:
        metrics = orderbook_metrics(
            make_book(total_bid_quantity="1300", total_ask_quantity="700")
        )

        self.assertEqual(metrics["spread"], "0.1000")
        self.assertEqual(metrics["pressure_score"], "30.0000")
        self.assertEqual(metrics["top_pressure_score"], "-9.0909")
        self.assertEqual(metrics["imbalance_ratio"], "1.8571")

    def test_process_book_saves_snapshot_and_bid_pressure_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "orderbook.db")
            watcher = OrderBookWatcher(
                FakeWebSocketClient(),
                store,
                options=OrderBookWatchOptions(
                    bid_pressure_threshold=Decimal("20"),
                    ask_pressure_threshold=Decimal("-20"),
                ),
            )

            event = watcher.process_book(
                make_book(total_bid_quantity="1500", total_ask_quantity="500"),
                tr_key="DNASAAPL",
            )
            snapshots = store.list_orderbook_snapshots()
            signals = store.list_market_signals()

        self.assertEqual(event["snapshot_id"], snapshots[0]["id"])
        self.assertEqual(event["pressure_score"], "50.0000")
        self.assertEqual(event["signals"][0]["signal_type"], "ORDERBOOK_BID_PRESSURE")
        self.assertEqual(snapshots[0]["symbol"], "AAPL")
        self.assertEqual(snapshots[0]["tr_key"], "DNASAAPL")
        self.assertEqual(snapshots[0]["raw"]["symbol"], "AAPL")
        self.assertEqual(signals[0]["signal_type"], "ORDERBOOK_BID_PRESSURE")
        self.assertEqual(signals[0]["payload"]["pressure_score"], "50.0000")

    def test_process_book_saves_ask_pressure_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "orderbook.db")
            watcher = OrderBookWatcher(
                FakeWebSocketClient(),
                store,
                options=OrderBookWatchOptions(
                    bid_pressure_threshold=Decimal("20"),
                    ask_pressure_threshold=Decimal("-20"),
                ),
            )

            event = watcher.process_book(
                make_book(total_bid_quantity="400", total_ask_quantity="1600"),
                tr_key="DNASAAPL",
            )

        self.assertEqual(event["pressure_score"], "-60.0000")
        self.assertEqual(event["signals"][0]["signal_type"], "ORDERBOOK_ASK_PRESSURE")

    def test_bid_pressure_can_create_buy_order_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "orderbook.db")
            client = FakeOrderClient()
            watcher = OrderBookWatcher(
                FakeWebSocketClient(),
                store,
                gateway=KISGateway(settings(), store),
                order_client=client,
                options=OrderBookWatchOptions(
                    bid_pressure_threshold=Decimal("20"),
                    action_on_bid_pressure="buy",
                    quantity=1,
                    limit_offset_pct=Decimal("0.02"),
                ),
            )

            event = watcher.process_book(
                make_book(total_bid_quantity="1500", total_ask_quantity="500"),
                tr_key="DNASAAPL",
            )
            order = store.list_broker_orders()[0]
            signal = store.list_market_signals()[0]

        self.assertEqual(event["signals"][0]["action"], "buy")
        self.assertEqual(event["signals"][0]["order_id"], order["id"])
        self.assertEqual(order["symbol"], "AAPL")
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["limit_price"], "290.0580")
        self.assertEqual(order["status"], "RISK_APPROVED")
        self.assertEqual(signal["order_id"], order["id"])
        self.assertEqual(signal["payload"]["order_result"]["mode"], "dry-run")
        self.assertEqual(client.buying_power_calls[0], ("AAPL", "290.0580", "NASD"))

    def test_ask_pressure_can_create_sell_order_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "orderbook.db")
            client = FakeOrderClient()
            watcher = OrderBookWatcher(
                FakeWebSocketClient(),
                store,
                gateway=KISGateway(settings(), store),
                order_client=client,
                options=OrderBookWatchOptions(
                    ask_pressure_threshold=Decimal("-20"),
                    action_on_ask_pressure="sell",
                    quantity=1,
                    limit_offset_pct=Decimal("0.02"),
                ),
            )

            event = watcher.process_book(
                make_book(total_bid_quantity="400", total_ask_quantity="1600"),
                tr_key="DNASAAPL",
            )
            order = store.list_broker_orders()[0]

        self.assertEqual(event["signals"][0]["action"], "sell")
        self.assertEqual(event["signals"][0]["order_id"], order["id"])
        self.assertEqual(order["side"], "sell")
        self.assertEqual(order["limit_price"], "289.8420")
        self.assertEqual(order["status"], "RISK_APPROVED")
        self.assertEqual(client.balance_calls[0], ("NASD", "USD"))

    def test_order_candidate_cooldown_skips_repeated_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "orderbook.db")
            client = FakeOrderClient()
            watcher = OrderBookWatcher(
                FakeWebSocketClient(),
                store,
                gateway=KISGateway(settings(), store),
                order_client=client,
                options=OrderBookWatchOptions(
                    bid_pressure_threshold=Decimal("20"),
                    action_on_bid_pressure="buy",
                    order_cooldown_seconds=60,
                ),
            )

            first = watcher.process_book(
                make_book(total_bid_quantity="1500", total_ask_quantity="500"),
                tr_key="DNASAAPL",
            )
            second = watcher.process_book(
                make_book(total_bid_quantity="1500", total_ask_quantity="500"),
                tr_key="DNASAAPL",
            )
            orders = store.list_broker_orders()
            signals = store.list_market_signals()

        self.assertEqual(first["signals"][0]["order_id"], orders[0]["id"])
        self.assertIsNone(second["signals"][0]["order_id"])
        self.assertEqual(len(orders), 1)
        self.assertEqual(
            signals[0]["payload"]["order_skipped_reason"],
            "order_candidate_cooldown",
        )


class FakeWebSocketClient:
    pass


class FakeOrderClient:
    def __init__(self) -> None:
        self.buying_power_calls: list[tuple[str, str, str]] = []
        self.balance_calls: list[tuple[str, str]] = []

    def overseas_buying_power(
        self,
        symbol: str,
        price: str,
        exchange: str = "NASD",
    ) -> dict[str, str]:
        self.buying_power_calls.append((symbol, price, exchange))
        return {"max_ord_psbl_qty": "10"}

    def overseas_balance(
        self,
        exchange: str = "NASD",
        currency: str = "USD",
    ) -> dict[str, object]:
        self.balance_calls.append((exchange, currency))
        return {
            "positions": [{"ovrs_pdno": "AAPL", "ord_psbl_qty": "10"}],
            "summary": [],
        }


def make_book(
    *,
    total_bid_quantity: str = "1000",
    total_ask_quantity: str = "1000",
) -> OverseasOrderBook:
    return OverseasOrderBook(
        symbol="AAPL",
        local_date="20260615",
        local_time="113000",
        korea_date="20260616",
        korea_time="003000",
        bid_price="289.9000",
        ask_price="290.0000",
        bid_quantity="50",
        ask_quantity="60",
        total_bid_quantity=total_bid_quantity,
        total_ask_quantity=total_ask_quantity,
    )


if __name__ == "__main__":
    unittest.main()
