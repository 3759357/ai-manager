import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ki.config import REAL_TRADING_ACK, Settings
from ki.market_watch import MarketWatcher, WatchlistConfig
from ki.storage import SnapshotStore


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


class MarketWatchTest(unittest.TestCase):
    def test_watchlist_store_upserts_lists_and_removes_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "market-watch.db")
            item_id = store.upsert_watchlist_item(
                symbol="aapl",
                quote_exchange="nas",
                account_exchange="nasd",
                enabled=True,
                quantity=1,
                hot_up_pct="1",
                hot_down_pct="2",
                action_on_hot_up="buy",
                action_on_hot_down="sell",
                limit_offset_pct="0.02",
            )
            updated_id = store.upsert_watchlist_item(
                symbol="AAPL",
                quote_exchange="NAS",
                account_exchange="NASD",
                enabled=False,
                quantity=2,
                hot_up_pct="3",
                hot_down_pct="4",
                action_on_hot_up="none",
                action_on_hot_down="sell",
                limit_offset_pct="0.01",
            )
            items = store.list_watchlist()
            enabled_items = store.list_watchlist(enabled_only=True)
            removed = store.remove_watchlist_item("aapl")

        self.assertEqual(updated_id, item_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "AAPL")
        self.assertFalse(items[0]["enabled"])
        self.assertEqual(items[0]["quantity"], 2)
        self.assertEqual(enabled_items, [])
        self.assertTrue(removed)

    def test_scan_creates_hot_signal_and_dry_run_order_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "market-watch.db")
            store.upsert_watchlist_item(
                symbol="AAPL",
                quote_exchange="NAS",
                account_exchange="NASD",
                enabled=True,
                quantity=1,
                hot_up_pct="1",
                hot_down_pct="1",
                action_on_hot_up="buy",
                action_on_hot_down="none",
                limit_offset_pct="0.02",
            )
            client = FakeMarketClient(["100", "102"])
            watcher = MarketWatcher(client, settings(), store)

            first = watcher.scan_once()
            second = watcher.scan_once()
            signals = store.list_market_signals()
            orders = store.list_broker_orders()

        self.assertEqual(first["results"][0]["signals"], [])
        self.assertEqual(second["results"][0]["price_change_pct"], "2.0000")
        self.assertEqual(second["results"][0]["signals"][0]["signal_type"], "HOT_UP")
        self.assertEqual(second["results"][0]["signals"][0]["action"], "buy")
        self.assertEqual(signals[0]["signal_type"], "HOT_UP")
        self.assertEqual(signals[0]["payload"]["order_result"]["mode"], "dry-run")
        self.assertEqual(orders[0]["symbol"], "AAPL")
        self.assertEqual(orders[0]["side"], "buy")
        self.assertEqual(orders[0]["status"], "RISK_APPROVED")

    def test_live_settings_still_only_create_candidates(self) -> None:
        """실주문 설정이 켜져 있어도 market-watch는 후보만 만들고 제출하지 않는다."""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "market-watch.db")
            store.upsert_watchlist_item(
                symbol="GOOGL",
                quote_exchange="NAS",
                account_exchange="NASD",
                enabled=True,
                quantity=1,
                hot_up_pct="0.5",
                hot_down_pct="1",
                action_on_hot_up="buy",
                action_on_hot_down="none",
                limit_offset_pct="0.02",
            )
            client = FakeMarketClient(["100", "101"])
            live_settings = settings(
                allow_orders=True,
                real_trading_ack=REAL_TRADING_ACK,
            )
            watcher = MarketWatcher(client, live_settings, store)

            watcher.scan_once()
            result = watcher.scan_once()
            order = store.list_broker_orders()[0]
            signal = store.list_market_signals()[0]

        self.assertEqual(result["results"][0]["signals"][0]["order_id"], order["id"])
        self.assertEqual(order["status"], "RISK_APPROVED")
        self.assertIsNone(order["kis_order_id"])
        self.assertEqual(client.orders, [])
        self.assertNotIn("submit_result", signal["payload"])


class FakeMarketClient:
    def __init__(self, prices: list[str]) -> None:
        self.prices = list(prices)
        self.orders: list[dict[str, object]] = []

    def overseas_quote(self, symbol: str, exchange: str) -> dict[str, str]:
        if not self.prices:
            raise AssertionError("테스트 시세가 부족합니다.")
        return {
            "symbol": symbol,
            "exchange": exchange,
            "last": self.prices.pop(0),
        }

    def overseas_buying_power(
        self,
        symbol: str,
        price: str,
        exchange: str = "NASD",
    ) -> dict[str, str]:
        return {
            "symbol": symbol,
            "price": price,
            "exchange": exchange,
            "max_ord_psbl_qty": "10",
        }

    def overseas_balance(
        self,
        exchange: str = "NASD",
        currency: str = "USD",
    ) -> dict[str, object]:
        return {
            "positions": [
                {"ovrs_pdno": "AAPL", "ord_psbl_qty": "10"},
                {"ovrs_pdno": "GOOGL", "ord_psbl_qty": "10"},
            ],
            "summary": [],
        }

    def overseas_order(
        self,
        *,
        side: str,
        symbol: str,
        quantity: int,
        price: str,
        exchange: str = "NASD",
        order_type: str = "limit",
    ) -> dict[str, str]:
        self.orders.append(
            {
                "side": side,
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "exchange": exchange,
                "order_type": order_type,
            }
        )
        return {"ODNO": "KIS777"}


if __name__ == "__main__":
    unittest.main()
