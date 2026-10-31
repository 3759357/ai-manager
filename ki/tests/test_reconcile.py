import tempfile
import unittest
from pathlib import Path

from ki.reconcile import OrderReconciliationService
from ki.storage import SnapshotStore


class FakeClient:
    def overseas_pending_orders(self, exchange: str = "NASD"):
        return [
            {
                "odno": "PENDING1",
                "pdno": "AAPL",
                "sll_buy_dvsn_cd": "02",
                "ft_ord_qty": "2",
                "ft_ccld_qty": "0",
                "nccs_qty": "2",
                "ft_ord_unpr3": "250.00",
                "tr_crcy_cd": "USD",
                "ovrs_excg_cd": exchange,
            }
        ]

    def overseas_order_history(
        self,
        start_date: str,
        end_date: str,
        exchange: str = "NASD",
    ):
        return [
            {
                "ord_dt": start_date,
                "odno": "FILLED1",
                "pdno": "MSFT",
                "sll_buy_dvsn_cd": "01",
                "ft_ord_qty": "1",
                "ft_ccld_qty": "1",
                "nccs_qty": "0",
                "ft_ord_unpr3": "300.00",
                "ft_ccld_unpr3": "301.00",
                "ft_ccld_amt3": "301.00",
                "tr_crcy_cd": "USD",
                "ovrs_excg_cd": exchange,
            },
            {
                "ord_dt": start_date,
                "odno": "CANCELED1",
                "pdno": "AAPL",
                "sll_buy_dvsn_cd": "02",
                "ft_ord_qty": "1",
                "ft_ccld_qty": "0",
                "nccs_qty": "0",
                "ft_ord_unpr3": "250.00",
                "rvse_cncl_dvsn_name": "취소",
                "tr_crcy_cd": "USD",
                "ovrs_excg_cd": exchange,
            }
        ]


class ReconcileTest(unittest.TestCase):
    def test_reconcile_creates_orders_events_and_fill_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "reconcile.db")
            service = OrderReconciliationService(FakeClient(), store)
            result = service.reconcile_overseas(
                start_date="20260601",
                end_date="20260612",
            )

            orders = store.list_broker_orders(limit=10)
            fills = store.list_fills(limit=10)
            second_result = service.reconcile_overseas(
                start_date="20260601",
                end_date="20260612",
            )
            second_fills = store.list_fills(limit=10)

        self.assertEqual(result["pending_rows"], 1)
        self.assertEqual(result["history_rows"], 2)
        self.assertEqual(result["fill_snapshots"], 1)
        self.assertEqual(
            {order["kis_order_id"] for order in orders},
            {"PENDING1", "FILLED1", "CANCELED1"},
        )
        self.assertIn(
            {"kis_order_id": "CANCELED1", "status": "CANCELED"},
            [
                {"kis_order_id": order["kis_order_id"], "status": order["status"]}
                for order in orders
            ],
        )
        self.assertEqual(fills[0]["symbol"], "MSFT")
        self.assertEqual(fills[0]["side"], "sell")
        self.assertEqual(len(second_fills), 1)
        self.assertEqual(second_result["fill_snapshots"], 1)


def history_row(**overrides):
    row = {
        "ord_dt": "20261030",
        "odno": "0001",
        "pdno": "AAPL",
        "sll_buy_dvsn_cd": "02",
        "ft_ord_qty": "5",
        "ft_ccld_qty": "0",
        "nccs_qty": "5",
        "ft_ord_unpr3": "200",
        "ft_ccld_unpr3": "0",
        "ft_ccld_amt3": "0",
        "tr_crcy_cd": "USD",
        "ovrs_excg_cd": "NASD",
    }
    row.update(overrides)
    return row


class ScriptedClient:
    """reconcile 주기마다 바꿔 끼울 수 있는 KIS 주문 조회."""

    def __init__(self) -> None:
        self.pending: list[dict] = []
        self.history: list[dict] = []

    def overseas_pending_orders(self, exchange: str = "NASD"):
        return list(self.pending)

    def overseas_order_history(self, start_date: str, end_date: str, exchange: str = "NASD"):
        return list(self.history)


def count_events(store: SnapshotStore) -> int:
    import sqlite3

    with sqlite3.connect(store.database_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM order_events").fetchone()[0]


class ReconcileIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(Path(self.temp.name) / "ki.db")
        self.client = ScriptedClient()
        self.service = OrderReconciliationService(self.client, self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cycle(self):
        return self.service.reconcile_overseas(start_date="20261024", end_date="20261031")

    def order(self):
        return self.store.find_broker_order_by_kis_order_id("0001")

    def test_unchanged_orders_do_not_create_events_every_cycle(self) -> None:
        self.client.history = [history_row()]
        first = self.run_cycle()
        events_after_first = count_events(self.store)
        second = self.run_cycle()
        third = self.run_cycle()

        self.assertEqual(first["events"], 1)
        self.assertEqual(second["events"], 0)
        self.assertEqual(third["events"], 0)
        self.assertEqual(count_events(self.store), events_after_first)

    def test_status_change_creates_exactly_one_event(self) -> None:
        self.client.history = [history_row()]
        self.run_cycle()
        self.client.history = [history_row(ft_ccld_qty="5", nccs_qty="0", ft_ccld_amt3="1000")]
        changed = self.run_cycle()
        again = self.run_cycle()

        self.assertEqual(changed["events"], 1)
        self.assertEqual(again["events"], 0)
        self.assertEqual(self.order()["status"], "FILLED")

    def test_cumulative_partial_fills_are_recorded_as_increments(self) -> None:
        self.client.history = [history_row(ft_ccld_qty="2", nccs_qty="3", ft_ccld_amt3="400")]
        self.run_cycle()
        self.run_cycle()
        self.client.history = [history_row(ft_ccld_qty="5", nccs_qty="0", ft_ccld_amt3="1015")]
        self.run_cycle()
        self.run_cycle()
        fills = sorted(self.store.list_fills(limit=10), key=lambda fill: fill["id"])

        self.assertEqual([fill["quantity"] for fill in fills], [2, 3])
        self.assertEqual(sum(fill["quantity"] for fill in fills), 5)
        # 두 번째 체결가는 누적 금액 차이(1015-400)/3 이다.
        self.assertEqual([fill["price"] for fill in fills], ["200.0000", "205.0000"])
        self.assertEqual(self.order()["status"], "FILLED")

    def test_terminal_status_is_not_reverted_by_stale_rows(self) -> None:
        self.client.history = [history_row(ft_ccld_qty="5", nccs_qty="0", ft_ccld_amt3="1000")]
        self.run_cycle()
        events = count_events(self.store)
        self.client.history = []
        self.client.pending = [history_row()]
        result = self.run_cycle()

        self.assertEqual(self.order()["status"], "FILLED")
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(count_events(self.store), events)

    def test_unfilled_order_that_ended_is_expired_not_open(self) -> None:
        self.client.history = [history_row(nccs_qty="0")]
        self.run_cycle()

        self.assertEqual(self.order()["status"], "EXPIRED")

    def test_partially_filled_order_that_ended_is_expired_with_its_fill(self) -> None:
        self.client.history = [history_row(ft_ccld_qty="2", nccs_qty="3", ft_ccld_amt3="400")]
        self.run_cycle()
        self.client.history = [history_row(ft_ccld_qty="2", nccs_qty="0", ft_ccld_amt3="400")]
        self.run_cycle()

        self.assertEqual(self.order()["status"], "EXPIRED")
        self.assertEqual([fill["quantity"] for fill in self.store.list_fills()], [2])

    def test_missing_remaining_quantity_keeps_existing_status(self) -> None:
        self.client.history = [history_row()]
        self.run_cycle()
        row = history_row()
        del row["nccs_qty"]
        self.client.history = [row]
        result = self.run_cycle()

        self.assertEqual(self.order()["status"], "ACCEPTED")
        self.assertEqual(result["events"], 0)
