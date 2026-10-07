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
