import json
import io
from contextlib import redirect_stdout
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from core.manager_cli import RequestStore, main
from core.notify import DiscordMessage
from core.reporting import collect_report, number, position, recent_order, write_artifacts


class FakeToss:
    def holdings(self):
        return {"items": [
            {"symbol": "FRACTION", "currency": "USD", "quantity": "0.01313", "lastPrice": "71.82",
             "marketValue": {"amount": "0.9429966", "purchaseAmount": "1"}},
            {"symbol": "LOCAL", "currency": "KRW", "quantity": "2", "lastPrice": "1000"},
        ]}

    def buying_power(self, currency):
        return {"currency": currency, "cashBuyingPower": "15"}

    def orders(self, *, status, limit):
        if status == "OPEN":
            return {"orders": []}
        return {"orders": [{"orderId": "broker-order-1", "symbol": "FRACTION",
                            "side": "BUY", "status": "FILLED", "currency": "USD",
                            "quantity": "0.01313", "orderedAt": "2026-09-24T01:14:02+09:00",
                            "execution": {"filledQuantity": "0.01313",
                                          "averageFilledPrice": "71.82", "filledAmount": "0.9429966",
                                          "commission": "0"}}]}


def fixture(report_id="example", brokers="toss"):
    return collect_report(report_id, brokers, lambda _: FakeToss())


class ReportTests(unittest.TestCase):
    def test_fractional_nested_values_and_separate_currencies(self):
        report = fixture()
        self.assertEqual(report["positions"][0]["quantity"], "0.01313")
        self.assertEqual(report["positions"][0]["unrealized_pnl"], "-0.0570034")
        self.assertEqual(report["market_value_by_currency"], {"KRW": "2000", "USD": "0.9429966"})
        self.assertNotIn("total_assets", report)
        self.assertEqual(report["recent_orders"][0]["filled_quantity"], "0.01313")
        self.assertEqual(report["recent_orders"][0]["commission"], "0")
        self.assertEqual(report["sources"][-1]["source"], "toss:recent_orders")

    def test_recent_orders_are_broker_fills_not_new_balance_movements(self):
        report = fixture()
        self.assertEqual(report["market_value_by_currency"]["USD"], "0.9429966")
        order = report["recent_orders"][0]
        self.assertEqual((order["status"], order["quantity"], order["filled_quantity"]),
                         ("FILLED", "0.01313", "0.01313"))
        self.assertIsNone(recent_order({"orderId": "x", "symbol": "X", "side": "BUY",
                                         "quantity": "1", "currency": "USD",
                                         "status": "OPEN"}, "toss")["average_filled_price"])
        with self.assertRaises(ValueError):
            recent_order({"orderId": "x", "symbol": "X", "side": "BUY",
                          "quantity": "1", "currency": "USD",
                          "execution": {"filledQuantity": "2"}}, "toss")
        with self.assertRaises(ValueError):
            recent_order({"orderId": "x", "symbol": "X", "side": "BUY",
                          "quantity": "1", "currency": "USD", "status": "FILLED"}, "toss")

    def test_kis_history_distinguishes_partial_fill_and_unknown_status(self):
        row = {"odno": "42", "pdno": "AMZN", "sll_buy_dvsn_cd": "02",
               "ft_ord_qty": "2", "ft_ccld_qty": "1", "nccs_qty": "1",
               "ft_ccld_unpr3": "250.27", "tr_crcy_cd": "USD", "ord_dt": "20260924"}
        order = recent_order(row, "kis")
        self.assertEqual((order["status"], order["filled_quantity"], order["commission"]),
                         ("PARTIALLY_FILLED", "1", None))
        row.update(ft_ccld_qty="0", nccs_qty="0")
        self.assertEqual(recent_order(row, "kis")["status"], "UNKNOWN")

    def test_invalid_numbers_and_missing_price_are_not_cost_basis(self):
        for value in ("NaN", "Infinity", "-Infinity", {}, [], True, "bad", ""):
            self.assertIsNone(number(value))
        self.assertEqual(number("1,234.50"), Decimal("1234.50"))
        p = position({"symbol": "X", "currency": "USD", "quantity": "0.1", "averagePurchasePrice": "10"}, "toss")
        self.assertIsNone(p["market_value"])

    def test_failures_and_pagination_are_explicit(self):
        kis = Mock()
        kis.balance.side_effect = RuntimeError("SECRET_TOKEN")
        kis.overseas_balance.return_value = {"positions": [], "context": {"nk200": "more"}}
        report = collect_report("partial", "both", lambda b: kis if b == "kis" else FakeToss())
        self.assertEqual(report["status"], "partial")
        self.assertEqual(len(report["positions"]), 2)
        self.assertNotIn("SECRET_TOKEN", json.dumps(report))
        self.assertEqual([s["status"] for s in report["sources"]], ["unavailable", "partial", "unavailable", "ok", "ok"])

    def test_malformed_rows_not_treated_as_zero(self):
        client = FakeToss()
        client.holdings = lambda: {"items": [{"symbol": "X", "currency": "USD", "quantity": "NaN"}]}
        report = collect_report("invalid", "toss", lambda _: client)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["market_value_by_currency"], {})

    def test_kis_domestic_and_overseas_fields(self):
        kr = position({"pdno": "000001", "hldg_qty": "2", "prpr": "1000",
                       "evlu_amt": "2000", "pchs_amt": "1800"}, "kis", True)
        us = position({"ovrs_pdno": "EXAMPLE", "ovrs_cblc_qty": "3", "now_pric2": "10",
                       "ovrs_stck_evlu_amt": "30", "frcr_pchs_amt1": "36", "tr_crcy_cd": "USD"}, "kis")
        self.assertEqual((kr["currency"], kr["unrealized_pnl"]), ("KRW", "200"))
        self.assertEqual((us["currency"], us["unrealized_pnl"]), ("USD", "-6"))

    def test_artifacts_are_private_and_escape_untrusted_text(self):
        report = fixture()
        report["positions"][0]["symbol"] = '<script>alert(1)</script>|[click](https://bad)'
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_artifacts(Path(tmp), report)
            page = Path(paths["html"]).read_text()
            self.assertNotIn("<script>", page)
            self.assertIn("&lt;script&gt;", page)
            self.assertIn("0.01313", page)
            self.assertEqual(json.loads(Path(paths["json"]).read_text()), report)
            self.assertIn(r"\|\[click\]", Path(paths["md"]).read_text())
            for p in paths.values():
                self.assertEqual(Path(p).stat().st_mode & 0o777, 0o600)
        self.assertEqual(DiscordMessage("@everyone").payload()["allowed_mentions"], {"parse": []})


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RequestStore(Path(self.temp.name))

    def test_idempotent_receipt_and_validation(self):
        first = self.store.enqueue("현재 계좌", request_id="repeat")
        self.assertEqual(self.store.enqueue("현재 계좌", request_id="repeat"), first)
        with self.assertRaises(ValueError):
            self.store.enqueue("현재 계좌", broker="kis", request_id="repeat")
        for text in ("매수", "$(touch /tmp/no)"):
            with self.assertRaises(ValueError):
                self.store.enqueue(text)
        with self.assertRaises(ValueError):
            self.store.enqueue("보고", request_id="../escape")

    def test_cli_inbox_and_report_even_when_discord_is_unavailable(self):
        self.store.enqueue("보고", broker="toss", request_id="readable")
        sender = Mock()
        sender.send.side_effect = RuntimeError("offline")
        self.store.run_once(fixture, sender)
        with redirect_stdout(io.StringIO()) as output:
            main(["--data-dir", self.temp.name, "report", "readable"])
        self.assertIn("AI Manager 계좌 보고", output.getvalue())
        self.assertEqual(self.store.report()["report_id"], "readable")
        with redirect_stdout(io.StringIO()) as output:
            main(["--data-dir", self.temp.name, "reports", "--json"])
        self.assertEqual(json.loads(output.getvalue())["requests"][0]["state"], "ready")
        with redirect_stdout(io.StringIO()) as output:
            main(["--data-dir", self.temp.name, "report", "readable", "--format", "json"])
        self.assertEqual(json.loads(output.getvalue()), self.store.report("readable"))
        with self.assertRaises(ValueError):
            self.store.list_requests(101)
        with self.assertRaises(ValueError):
            self.store.report("missing")

    def test_local_delivery_does_not_call_discord(self):
        self.store.enqueue("보고", broker="toss", channel="local", request_id="local")
        sender = Mock()
        result = self.store.run_once(fixture, sender)
        self.assertEqual(result["state"], "completed")
        self.assertIsNone(result["delivered_at"])
        sender.send.assert_not_called()

    def test_retry_reuses_saved_snapshot_after_restart(self):
        self.store.enqueue("보고", broker="toss", request_id="retry")
        collector = Mock(side_effect=fixture)
        sender = Mock()
        sender.send.side_effect = RuntimeError("https://secret-webhook")
        first = self.store.run_once(collector, sender)
        self.assertEqual(first["state"], "ready")
        self.assertEqual(first["error"], "RuntimeError")
        self.assertTrue(Path(first["paths"]["html"]).exists())
        with self.store.connect() as db:
            db.execute("UPDATE requests SET next_attempt=0")
        restarted = RequestStore(Path(self.temp.name))
        sender.send.side_effect = None
        final = restarted.run_once(collector, sender)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(first["report_summary"], final["report_summary"])
        self.assertEqual(collector.call_count, 1)
        self.assertEqual(restarted.run_once(collector, sender), {"worker": "idle"})
        self.assertEqual(sender.send.call_count, 2)

    def test_worker_lock_blocks_concurrent_delivery(self):
        import fcntl
        self.store.enqueue("계좌", request_id="lock")
        with (self.store.root / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            collector = Mock()
            self.assertEqual(self.store.run_once(collector), {"worker": "busy"})
            collector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
