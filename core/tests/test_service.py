import unittest
from decimal import Decimal

from core.models import BrokerCapabilities, NormalizedQuote, UnifiedOrderRequest
from core.service import BrokerService


class BrokerServiceTest(unittest.TestCase):
    def test_compare_quotes_calculates_divergence(self) -> None:
        service = BrokerService(
            {
                "kis": FakeAdapter("kis", "200.00"),
                "toss": FakeAdapter("toss", "200.10"),
            }
        )

        result = service.compare_quotes(
            "AAPL",
            max_divergence_pct=Decimal("0.10"),
        )

        self.assertEqual(result["divergence_pct"], "0.0500")
        self.assertTrue(result["within_limit"])

    def test_order_candidate_routes_to_selected_broker(self) -> None:
        toss = FakeAdapter("toss", "200")
        service = BrokerService({"toss": toss})
        request = UnifiedOrderRequest(
            broker="toss",
            side="buy",
            symbol="AAPL",
            quantity=1,
            price=Decimal("200"),
        )

        result = service.create_order_candidate(request)

        self.assertEqual(result["broker"], "toss")
        self.assertEqual(toss.requests[0], request)


class FakeAdapter:
    def __init__(self, name: str, price: str) -> None:
        self.name = name
        self.price = Decimal(price)
        self.requests: list[UnifiedOrderRequest] = []

    def capabilities(self):
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

    def quote(self, symbol: str):
        return NormalizedQuote(
            broker=self.name,
            symbol=symbol,
            price=self.price,
            currency="USD",
            timestamp=None,
            raw={},
        )

    def create_order_candidate(self, request: UnifiedOrderRequest):
        self.requests.append(request)
        return {"broker": self.name, "order_id": 1, "status": "RISK_APPROVED"}


if __name__ == "__main__":
    unittest.main()
