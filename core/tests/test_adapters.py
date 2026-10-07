import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ki.config import Settings as KISSettings
from ki.storage import SnapshotStore
from toss.config import Settings as TossSettings
from toss.storage import OrderStore

from core.adapters import KISBrokerAdapter, TossBrokerAdapter
from core.config import Settings


class AdapterTest(unittest.TestCase):
    def test_local_store_adapters_do_not_require_broker_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = Settings(
                kis_database_path=root / "ki/data/custom-kis.db",
                toss_database_path=root / "toss/data/custom-toss.db",
            )

            kis = KISBrokerAdapter.from_local_store(settings)
            toss = TossBrokerAdapter.from_local_store(settings)

            self.assertEqual(
                kis.store.database_path,
                root / "ki/data/custom-kis.db",
            )
            self.assertEqual(
                toss.store.database_path,
                root / "toss/data/custom-toss.db",
            )
            self.assertEqual(kis.list_orders(), [])
            self.assertEqual(toss.list_orders(), [])

    def test_kis_quote_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = KISBrokerAdapter(
                KISSettings(
                    environment="real",
                    app_key="key",
                    app_secret="secret",
                    account_no="12345678",
                ),
                client=FakeKISClient(),
                store=SnapshotStore(Path(temp_dir) / "kis.db"),
            )
            quote = adapter.quote("aapl")

        self.assertEqual(quote.broker, "kis")
        self.assertEqual(quote.symbol, "AAPL")
        self.assertEqual(quote.price, Decimal("200.01"))

    def test_toss_quote_and_orderbook_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = TossBrokerAdapter(
                TossSettings(
                    client_id="client",
                    client_secret="secret",
                    account_seq=1,
                ),
                client=FakeTossClient(),
                store=OrderStore(Path(temp_dir) / "toss.db"),
            )
            quote = adapter.quote("aapl")
            book = adapter.orderbook("aapl")

        self.assertEqual(quote.broker, "toss")
        self.assertEqual(quote.price, Decimal("200.03"))
        self.assertEqual(book.bid_price, Decimal("200.00"))
        self.assertEqual(book.ask_price, Decimal("200.05"))


class FakeKISClient:
    def overseas_quote(self, symbol: str, exchange: str):
        return {"last": "200.01", "symbol": symbol, "exchange": exchange}


class FakeTossClient:
    def prices(self, symbols):
        return [
            {
                "symbol": symbols[0],
                "lastPrice": "200.03",
                "currency": "USD",
                "timestamp": "2026-06-19T10:00:00+09:00",
            }
        ]

    def orderbook(self, symbol: str):
        return {
            "currency": "USD",
            "bids": [{"price": "200.00", "volume": "10"}],
            "asks": [{"price": "200.05", "volume": "12"}],
        }


if __name__ == "__main__":
    unittest.main()
