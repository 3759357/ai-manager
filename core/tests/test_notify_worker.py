import io
import tempfile
import unittest
from pathlib import Path

from core.events import OutboxEvent, utc_now_iso
from core.notify_worker import ConsoleChannel, NotificationWorker
from core.outbox import OutboxStore


def _event(dedup_key: str, severity: str = "info") -> OutboxEvent:
    return OutboxEvent(
        dedup_key=dedup_key,
        source="ki",
        category="ORDER",
        event_type="SUBMITTED",
        broker="kis",
        symbol="GOOGL",
        severity=severity,
        summary="GOOGL BUY 1주 제출",
        occurred_at="2026-07-14T00:00:00+00:00",
    )


class NotificationWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OutboxStore(Path(self._tmp.name) / "core.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_once_sends_pending_console_deliveries(self) -> None:
        self.store.append(_event("e:1"))
        self.store.append(_event("e:2", severity="warning"))
        stream = io.StringIO()
        worker = NotificationWorker(
            self.store,
            {"console": ConsoleChannel(stream)},
        )

        result = worker.run_once()

        self.assertEqual(result["sent"], 2)
        output = stream.getvalue()
        self.assertIn("[알림] 2026-07-14T00:00:00 GOOGL BUY 1주 제출", output)
        self.assertIn("[경고]", output)
        self.assertEqual(self.store.list_pending_deliveries(), [])

    def test_run_once_is_idempotent(self) -> None:
        self.store.append(_event("e:1"))
        stream = io.StringIO()
        worker = NotificationWorker(
            self.store,
            {"console": ConsoleChannel(stream)},
        )

        worker.run_once()
        second = worker.run_once()

        self.assertEqual(second["sent"], 0)
        self.assertEqual(stream.getvalue().count("GOOGL"), 1)

    def test_handler_failure_retries_then_marks_failed(self) -> None:
        self.store.append(_event("e:1"))

        def broken(_item):
            raise RuntimeError("전송 실패")

        worker = NotificationWorker(
            self.store,
            {"console": broken},
            max_attempts=2,
        )

        first = worker.run_once()
        second = worker.run_once()
        third = worker.run_once()

        self.assertEqual(first["still_pending"], 1)
        self.assertEqual(second["failed"], 1)
        self.assertEqual(third, {"sent": 0, "failed": 0, "still_pending": 0})
        deliveries = self.store.list_pending_deliveries()
        self.assertEqual(deliveries, [])

    def test_failure_does_not_block_other_deliveries(self) -> None:
        self.store.append(_event("e:1"))
        self.store.append(_event("e:2"))
        calls = []

        def flaky(item):
            calls.append(item["dedup_key"])
            if item["dedup_key"] == "e:1":
                raise RuntimeError("전송 실패")

        worker = NotificationWorker(self.store, {"console": flaky})

        result = worker.run_once()

        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["still_pending"], 1)
        self.assertEqual(calls, ["e:1", "e:2"])

    def test_requires_at_least_one_handler(self) -> None:
        with self.assertRaises(ValueError):
            NotificationWorker(self.store, {})

    def test_run_forever_respects_max_cycles(self) -> None:
        self.store.append(_event("e:1"))
        stream = io.StringIO()
        sleeps = []
        worker = NotificationWorker(
            self.store,
            {"console": ConsoleChannel(stream)},
        )

        worker.run_forever(
            interval_seconds=5,
            sleep=sleeps.append,
            max_cycles=3,
        )

        self.assertEqual(sleeps, [5, 5])
        self.assertEqual(stream.getvalue().count("GOOGL"), 1)


if __name__ == "__main__":
    unittest.main()
