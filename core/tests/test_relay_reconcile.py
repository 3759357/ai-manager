import tempfile
import unittest
from pathlib import Path

from ki.reconcile import OrderReconciliationService
from ki.storage import SnapshotStore

from core.outbox import OutboxStore
from core.portfolio import KIS_TERMINAL_STATUSES, pending_buy_amount
from core.relay import OutboxRelay


class OneOrderClient:
    def __init__(self) -> None:
        self.filled, self.remaining = "0", "5"

    def overseas_pending_orders(self, exchange: str = "NASD"):
        return []

    def overseas_order_history(self, start_date: str, end_date: str, exchange: str = "NASD"):
        return [{
            "ord_dt": "20261030", "odno": "0001", "pdno": "AAPL", "sll_buy_dvsn_cd": "02",
            "ft_ord_qty": "5", "ft_ccld_qty": self.filled, "nccs_qty": self.remaining,
            "ft_ord_unpr3": "200", "ft_ccld_unpr3": "200",
            "ft_ccld_amt3": str(200 * int(self.filled)), "tr_crcy_cd": "USD",
        }]


class RelayAfterReconcileTest(unittest.TestCase):
    """대사 워커가 매 주기 돌아도 바뀐 것만 알림으로 나간다."""

    def test_notifications_only_for_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = OneOrderClient()
            service = OrderReconciliationService(client, SnapshotStore(root / "ki.db"))
            outbox = OutboxStore(root / "core.db")
            relay = OutboxRelay(outbox, kis_database_path=root / "ki.db",
                                toss_database_path=root / "toss.db", channels=("discord",))
            relayed = []
            for cycle in range(6):
                if cycle == 3:
                    client.filled, client.remaining = "5", "0"
                service.reconcile_overseas(start_date="20261024", end_date="20261031")
                relayed.append(relay.sync()["relayed_total"])

        # 0: 주문 발견 1건, 3: 체결 이벤트 1건 + 체결 1건. 나머지 주기는 0건.
        self.assertEqual(relayed, [1, 0, 0, 2, 0, 0])

    def test_expired_orders_are_not_pending_exposure(self) -> None:
        orders = [
            {"side": "buy", "status": "EXPIRED", "estimated_amount": "1000"},
            {"side": "buy", "status": "ACCEPTED", "estimated_amount": "300"},
        ]
        self.assertEqual(str(pending_buy_amount(orders, KIS_TERMINAL_STATUSES)), "300")


if __name__ == "__main__":
    unittest.main()
