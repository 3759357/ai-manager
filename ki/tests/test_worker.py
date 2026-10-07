import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ki.config import Settings
from ki.storage import SnapshotStore
from ki.worker import OperationsWorker, WorkerOptions


class FakeWorkerClient:
    def overseas_pending_orders(self, exchange: str = "NASD"):
        return [
            {
                "odno": "PENDING1",
                "pdno": "AAPL",
                "sll_buy_dvsn_cd": "02",
                "ft_ord_qty": "1",
                "ft_ccld_qty": "0",
                "nccs_qty": "1",
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
        return []

    def overseas_quote(self, symbol: str, exchange: str):
        return {"last": "250.00"}

    def overseas_balance(self, exchange: str, currency: str):
        return {
            "positions": [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "1"}],
            "summary": [],
        }

    def overseas_buying_power(self, symbol: str, price: str, exchange: str):
        return {"max_ord_psbl_qty": "1"}


def settings() -> Settings:
    return Settings(
        environment="real",
        app_key="key",
        app_secret="secret",
        account_no="12345678",
    )


class WorkerTest(unittest.TestCase):
    def test_worker_reconciles_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "worker.db"
            store = SnapshotStore(database_path)
            worker = OperationsWorker(
                FakeWorkerClient(),
                settings(),
                store,
                options=WorkerOptions(max_cycles=1),
                sleep=lambda _: None,
            )
            result = worker.run()
            orders = store.list_broker_orders(limit=10)

        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["last_result"]["reconcile"]["pending_rows"], 1)
        self.assertEqual(orders[0]["kis_order_id"], "PENDING1")
        self.assertEqual(orders[0]["status"], "ACCEPTED")

    def test_worker_can_sync_account_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "worker.db"
            store = SnapshotStore(database_path)
            worker = OperationsWorker(
                FakeWorkerClient(),
                settings(),
                store,
                options=WorkerOptions(max_cycles=1, sync_account=True),
                sleep=lambda _: None,
            )
            result = worker.run()

            with sqlite3.connect(database_path) as connection:
                snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM overseas_account_snapshots"
                ).fetchone()[0]

        self.assertEqual(result["last_result"]["account_sync"]["symbol"], "AAPL")
        self.assertEqual(snapshot_count, 1)

    def test_worker_records_error_and_continues_when_configured(self) -> None:
        class FailingClient(FakeWorkerClient):
            def overseas_pending_orders(self, exchange: str = "NASD"):
                raise RuntimeError("temporary failure")

        worker = OperationsWorker(
            FailingClient(),
            settings(),
            SnapshotStore(Path(tempfile.gettempdir()) / "ki-worker-error.db"),
            options=WorkerOptions(max_cycles=1, continue_on_error=True),
            sleep=lambda _: None,
            logger=logging.getLogger("ki.tests.worker.silent"),
        )
        worker.logger.disabled = True
        result = worker.run()

        self.assertEqual(result["last_result"]["error"], "RuntimeError")
