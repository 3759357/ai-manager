import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.instructions import InstructionStore
from core.judgments import JudgmentStore
from core.manager_cli import RequestStore, main, run_background_once
from core.push import PushStore
from core.theses import ThesisStore


class ReadOnlyAccount:
    def holdings(self):
        return {"items": [{"symbol": "TEST", "currency": "USD", "quantity": "2", "lastPrice": "100"}]}

    def buying_power(self, currency):
        return {"currency": currency, "cashBuyingPower": "500"}

    def orders(self, *, status, limit):
        return {"orders": []}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = RequestStore(self.root)
        self.inbox = InstructionStore(self.store)

    def run_worker(self):
        with patch("core.config._load_dotenv"), \
                patch.dict(os.environ, {"AI_MANAGER_PLANNER_ENABLED": "false"}), \
                patch("core.push.APNsSender.configured", return_value=False), \
                patch("core.reporting.make_client", return_value=ReadOnlyAccount()), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--data-dir", str(self.root), "worker", "--once"]), 0)
        return json.loads(output.getvalue())

    def test_worker_reports_without_creating_virtual_account_or_executing_instructions(self):
        self.store.enqueue("현재 계좌", broker="toss", channel="local", request_id="report")
        pending = self.inbox.submit('{"orders":[{"symbol":"TEST","side":"buy","quantity":1}],"reason":"legacy"}', "pending")
        self.assertEqual(self.run_worker()["state"], "completed")
        self.assertEqual(self.store.report("report")["positions"][0]["quantity"], "2")
        self.assertEqual(self.inbox.get("pending"), pending)
        with self.store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {"engine", "paper_positions", "paper_fills", "paper_runs", "pilot_config", "pilot_plans", "execution_runs", "execution_orders"})

    def test_existing_virtual_data_and_inflight_instructions_are_preserved_but_inactive(self):
        waiting = self.inbox.submit("기존 검토", "waiting")
        self.inbox.submit("기존 처리", "running")
        running = self.inbox.transition("running", "running")
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE engine (id INTEGER PRIMARY KEY, enabled INTEGER, cash TEXT);
                INSERT INTO engine VALUES (1,1,'9500');
                CREATE TABLE paper_positions (symbol TEXT PRIMARY KEY, quantity TEXT);
                INSERT INTO paper_positions VALUES ('TEST','5');
                CREATE TABLE paper_fills (id TEXT PRIMARY KEY, instruction_id TEXT, symbol TEXT,
                    side TEXT, quantity TEXT, price TEXT, created_at TEXT);
                INSERT INTO paper_fills VALUES ('fill','running','TEST','buy','5','100','2026-10-01');
                CREATE TABLE pilot_config (id INTEGER PRIMARY KEY, enabled INTEGER);
                INSERT INTO pilot_config VALUES (1,1);
            """)
        self.assertEqual(self.run_worker(), {"worker": "idle"})
        self.assertEqual(self.inbox.get("waiting"), waiting)
        self.assertEqual(self.inbox.get("running"), running)
        with self.store.connect() as db:
            self.assertEqual(tuple(db.execute("SELECT enabled,cash FROM engine").fetchone()), (1, "9500"))
            self.assertEqual(tuple(db.execute("SELECT symbol,quantity FROM paper_positions").fetchone()), ("TEST", "5"))
            self.assertEqual(db.execute("SELECT count(*) FROM paper_fills").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM push_events").fetchone()[0], 0)

    def test_removed_commands_fail_before_creating_any_state(self):
        for command in ("engine", "pilot"):
            for action in ("status", "start", "stop"):
                with self.subTest(command=command, action=action), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    main(["--data-dir", str(self.root / "unused"), command, action])
                self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / "unused").exists())

    def test_worker_keeps_daily_judgment_requests_and_response_ingestion(self):
        theses = ThesisStore(self.store)
        theses.add("TEST", "사용자 투자논리", thesis_id="test-thesis", card={"thesis": "사용자 근거"})
        theses.approve("test-thesis", 1)
        request_daily = JudgmentStore.request_daily
        now = datetime(2026, 10, 13, 1, tzinfo=timezone.utc)

        def daily(judgments, spool):
            return request_daily(judgments, spool, now=now)

        with patch.dict(os.environ, {"AI_MANAGER_PLANNER_ENABLED": "true"}), \
                patch.object(JudgmentStore, "request_daily", daily), \
                patch("core.push.APNsSender.configured", return_value=False):
            run_background_once(self.store)
            requests = list((self.root / "spool/requests").glob("*.json"))
            self.assertEqual(len(requests), 1)
            body = json.loads(requests[0].read_text())
            self.assertEqual(body["_role"], "judge")
            self.assertEqual(body["theses"][0]["id"], "test-thesis")
            (self.root / "spool/responses" / requests[0].name).write_text(json.dumps({"judgments": [{
                "thesisId": "test-thesis", "health": "weakened", "tilt": -1,
                "rule": "", "checks": [], "eventRisks": [], "explanation": "추가 확인 필요"
            }]}))
            run_background_once(self.store)
        history = JudgmentStore(self.store).history("test-thesis")
        self.assertEqual({row["kind"] for row in history}, {"thesis_health", "tilt"})
        self.assertEqual(theses.get("test-thesis")["health"], "weakened")
        self.assertEqual(len(list((self.root / "spool/requests").glob("*.json"))), 1)


class PushTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RequestStore(Path(tmp.name))
        self.inbox = InstructionStore(self.store)
        self.push = PushStore(self.store)
        self.push.register("phone", "ab" * 32, "sandbox")

    def complete(self):
        self.inbox.submit("PRIVATE_SYMBOL 검토", "review")
        self.inbox.transition("review", "running")
        self.inbox.transition("review", "completed", "PRIVATE_BALANCE 검토 결과")

    def test_completion_push_is_deduplicated_and_does_not_leak_account_details(self):
        self.complete()
        calls = []

        def send(token, environment, payload, event_id):
            calls.append(payload)
            return 200, ""

        self.assertEqual(self.push.deliver_once(send), {"push": "sent"})
        self.assertEqual(self.push.deliver_once(send), {"push": "idle"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["instruction_id"], "review")
        self.assertNotIn("PRIVATE_", json.dumps(calls))

    def test_invalid_token_deactivated_and_transient_retried(self):
        self.complete()
        self.push.deliver_once(lambda *args: (503, "ServiceUnavailable"))
        self.assertEqual(self.push.status()["delivery_counts"], {"pending": 1})
        with self.store.connect() as db:
            db.execute("UPDATE push_delivery SET next_attempt=0")
        self.push.deliver_once(lambda *args: (410, "Unregistered"))
        self.assertEqual(self.push.status()["active_devices"], 0)
        self.assertEqual(self.push.status()["delivery_counts"], {"failed": 1})

    def test_queued_legacy_fill_notification_is_not_delivered_or_counted(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO push_events VALUES ('old','old-instruction','paper_fill','2026-10-01')")
            db.execute("INSERT INTO push_delivery(event_id,device_id,state) VALUES ('old','phone','pending')")
        calls = []
        self.assertEqual(self.push.deliver_once(lambda *args: calls.append(args)), {"push": "idle"})
        self.assertEqual(calls, [])
        self.assertEqual(self.push.status()["delivery_counts"], {})
        self.complete()
        self.assertEqual(self.push.deliver_once(lambda *args: (200, "")), {"push": "sent"})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM push_delivery WHERE event_id='old'").fetchone()[0], "pending")


if __name__ == "__main__":
    unittest.main()
