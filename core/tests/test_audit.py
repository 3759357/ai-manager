import tempfile
import unittest
from pathlib import Path

from core.audit import record_core_error, record_core_event
from core.notify_worker import NotificationWorker
from core.outbox import OutboxStore


class _BrokenRelay:
    def sync(self):
        raise RuntimeError("브로커 DB 접근 실패")


class AuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OutboxStore(Path(self._tmp.name) / "core.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_core_event_appends_with_unique_keys(self) -> None:
        first = record_core_event(
            self.store,
            category="ORDER",
            event_type="INTENT_CREATED",
            summary="[Core] toss AAPL BUY 1주 후보 생성",
            broker="toss",
            symbol="AAPL",
        )
        second = record_core_event(
            self.store,
            category="ORDER",
            event_type="INTENT_CREATED",
            summary="[Core] toss AAPL BUY 1주 후보 생성",
            broker="toss",
            symbol="AAPL",
        )

        self.assertTrue(first["created"])
        self.assertTrue(second["created"])
        self.assertNotEqual(first["event_id"], second["event_id"])
        events = self.store.list_events(category="ORDER")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["source"], "core")

    def test_record_core_error_is_warning_system_event(self) -> None:
        record_core_error(
            self.store,
            command="submit",
            error="주문 한도 초과",
        )

        event = self.store.list_events(category="SYSTEM")[0]
        self.assertEqual(event["event_type"], "COMMAND_ERROR")
        self.assertEqual(event["severity"], "warning")
        self.assertIn("submit 실패", event["summary"])

    def test_worker_records_relay_error_as_system_event(self) -> None:
        sent = []
        worker = NotificationWorker(
            self.store,
            {"console": lambda item: sent.append(item["summary"])},
            relay=_BrokenRelay(),
        )

        result = worker.run_once()

        self.assertIn("relay_error", result)
        self.assertEqual(result["sent"], 1)
        system = self.store.list_events(category="SYSTEM")[0]
        self.assertEqual(system["event_type"], "RELAY_ERROR")
        self.assertEqual(system["severity"], "critical")
        self.assertIn("RELAY_ERROR", sent[0])


if __name__ == "__main__":
    unittest.main()
