import unittest
from decimal import Decimal

from core.models import NormalizedOrderBook, UnifiedOrderRequest


class ModelsTest(unittest.TestCase):
    def test_order_request_validation(self) -> None:
        request = UnifiedOrderRequest(
            broker="toss",
            side="buy",
            symbol="AAPL",
            quantity=1,
            price=Decimal("200"),
        )
        request.validate()

    def test_orderbook_spread(self) -> None:
        book = NormalizedOrderBook(
            broker="toss",
            symbol="AAPL",
            bid_price=Decimal("199.90"),
            ask_price=Decimal("200.10"),
            bid_quantity=Decimal("10"),
            ask_quantity=Decimal("12"),
            currency="USD",
            timestamp=None,
            raw={},
        )
        self.assertEqual(book.spread, Decimal("0.20"))
        self.assertEqual(book.to_dict()["spread"], "0.20")


if __name__ == "__main__":
    unittest.main()
