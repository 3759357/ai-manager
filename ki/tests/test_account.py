import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ki.account import sync_overseas_account
from ki.config import Settings
from ki.storage import SnapshotStore


class FakeClient:
    def overseas_quote(self, symbol: str, exchange: str):
        return {"last": "200.50"}

    def overseas_balance(self, exchange: str, currency: str):
        return {
            "positions": [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "2"}],
            "summary": [{"frcr_pchs_amt1": "300.00"}],
            "context": {"fk200": "", "nk200": ""},
        }

    def overseas_buying_power(self, symbol: str, price: str, exchange: str):
        return {"ord_psbl_frcr_amt": "500.00", "max_ord_psbl_qty": "2"}


class AccountSyncTest(unittest.TestCase):
    def test_sync_saves_raw_account_snapshot(self) -> None:
        settings = Settings(
            environment="real",
            app_key="key",
            app_secret="secret",
            account_no="12345678",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "account.db"
            result = sync_overseas_account(
                FakeClient(),
                settings,
                SnapshotStore(database_path),
            )
            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    """
                    SELECT symbol, quote_price, balance_json, buying_power_json
                    FROM overseas_account_snapshots
                    """
                ).fetchone()

        self.assertEqual(result["snapshot_id"], 1)
        self.assertEqual(row[0], "AAPL")
        self.assertEqual(row[1], "200.50")
        self.assertEqual(json.loads(row[2])["positions"][0]["ovrs_pdno"], "AAPL")
        self.assertEqual(json.loads(row[3])["max_ord_psbl_qty"], "2")
