import tempfile
import unittest
from pathlib import Path

from toss.config import REAL_TRADING_ACK
from toss.gateway import TossGateway
from toss.storage import OrderStore
from test_gateway import FakeClient, settings


class StatusClient(FakeClient):
    def __init__(self, *statuses: str) -> None:
        super().__init__()
        self.statuses = list(statuses)

    def order(self, order_id: str):
        return {"orderId": order_id, "status": self.statuses.pop(0)}


class SyncStatusTest(unittest.TestCase):
    """토스 조회 상태를 원장에 옮길 때 모르는 이름이나 역행을 그대로 쓰지 않는다."""

    def submitted(self, temp_dir, *statuses):
        store = OrderStore(Path(temp_dir) / "toss.db")
        client = StatusClient(*statuses)
        gateway = TossGateway(settings(allow_orders=True, real_trading_ack=REAL_TRADING_ACK), client, store)
        order_id = gateway.create_order_candidate(side="buy", symbol="AAPL", quantity=1,
                                                  price="200", currency="USD")["order_id"]
        gateway.submit_order(order_id, execute=True)
        return gateway, store, order_id

    def test_unknown_status_keeps_the_order_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gateway, store, order_id = self.submitted(temp_dir, "SETTLING_SOMEHOW")
            gateway.sync_order(order_id)
            self.assertEqual(store.get_order(order_id)["status"], "SUBMITTED")

    def test_spelling_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gateway, store, order_id = self.submitted(temp_dir, "Cancelled")
            gateway.sync_order(order_id)
            self.assertEqual(store.get_order(order_id)["status"], "CANCELED")

    def test_terminal_status_is_not_reverted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gateway, store, order_id = self.submitted(temp_dir, "FILLED", "OPEN")
            gateway.sync_order(order_id)
            result = gateway.sync_order(order_id)
            self.assertEqual(result["status"], "FILLED")
            self.assertEqual(store.get_order(order_id)["status"], "FILLED")


if __name__ == "__main__":
    unittest.main()
