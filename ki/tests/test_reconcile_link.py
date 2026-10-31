import tempfile
import unittest
from pathlib import Path

from ki.client import KISOutcomeUnknown
from ki.gateway import KISGateway
from ki.reconcile import OrderReconciliationService
from ki.storage import SnapshotStore
from test_gateway import FakeOrderClient, settings
from test_reconcile import ScriptedClient, history_row


class LinkUnknownSubmissionTest(unittest.TestCase):
    """결과 불명으로 남은 주문을 KIS 조회 결과와 연결해 예약 금액이 두 번 잡히지 않게 한다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(Path(self.temp.name) / "ki.db")
        self.gateway = KISGateway(
            settings(allow_orders=True, real_trading_ack="I_UNDERSTAND_REAL_TRADING"), self.store
        )
        self.kis = ScriptedClient()
        self.service = OrderReconciliationService(self.kis, self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def unknown_order(self, price: str = "250") -> int:
        order_id = self.gateway.create_order_intent(side="buy", symbol="AAPL", quantity=1, price=price)["order"]["id"]
        client = FakeOrderClient()
        client.order_failure = KISOutcomeUnknown("timeout")
        self.gateway.submit_order(order_id=order_id, client=client, execute=True)
        return order_id

    def reconcile(self, **row):
        self.kis.history = [history_row(odno="KIS9", ft_ord_qty="1", ft_ord_unpr3="250.0000", nccs_qty="1", **row)]
        return self.service.reconcile_overseas(start_date="20261024", end_date="20261031")

    def test_single_matching_unknown_order_is_linked(self) -> None:
        order_id = self.unknown_order()
        result = self.reconcile()
        orders = self.store.list_broker_orders(limit=10)

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["id"], order_id)
        self.assertEqual(orders[0]["kis_order_id"], "KIS9")
        self.assertEqual(orders[0]["status"], "ACCEPTED")
        self.assertEqual(result["events"], 1)

    def test_ambiguous_matches_are_not_guessed(self) -> None:
        first, second = self.unknown_order(), self.unknown_order()
        self.reconcile()
        statuses = {o["id"]: (o["status"], o["kis_order_id"]) for o in self.store.list_broker_orders(limit=10)}

        self.assertEqual(statuses[first], ("SUBMIT_UNKNOWN", None))
        self.assertEqual(statuses[second], ("SUBMIT_UNKNOWN", None))
        self.assertEqual(len(statuses), 3)

    def test_different_price_is_not_linked(self) -> None:
        order_id = self.unknown_order(price="249")
        self.reconcile()
        self.assertIsNone(self.store.get_broker_order(order_id)["kis_order_id"])


if __name__ == "__main__":
    unittest.main()
