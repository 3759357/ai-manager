import json
import tempfile
import unittest
from pathlib import Path

from core.events import OutboxEvent
from core.notify import DiscordNotifier, NotificationError
from core.notify_worker import DiscordChannel, NotificationWorker
from core.outbox import OutboxStore


class DiscordRetryTest(unittest.TestCase):
    def _notifier(self, responses, sleeps):
        calls = iter(responses)

        def poster(url, data, headers, timeout):
            return next(calls)

        return DiscordNotifier(
            "https://discord.example/webhook",
            poster=poster,
            sleep=sleeps.append,
        )

    def test_429_waits_retry_after_then_succeeds(self) -> None:
        sleeps = []
        notifier = self._notifier(
            [
                (429, json.dumps({"retry_after": 2.5})),
                (204, ""),
            ],
            sleeps,
        )

        result = notifier.send("hello")

        self.assertEqual(result["status"], 204)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(sleeps, [2.5])

    def test_5xx_uses_exponential_backoff(self) -> None:
        sleeps = []
        notifier = self._notifier(
            [(500, "err"), (502, "err"), (204, "")],
            sleeps,
        )

        result = notifier.send("hello")

        self.assertEqual(result["attempts"], 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_exhausted_retries_raise(self) -> None:
        sleeps = []
        notifier = self._notifier(
            [(500, "err")] * 4,
            sleeps,
        )

        with self.assertRaises(NotificationError):
            notifier.send("hello")
        self.assertEqual(len(sleeps), 3)

    def test_4xx_is_not_retried(self) -> None:
        sleeps = []
        notifier = self._notifier([(400, "bad request")], sleeps)

        with self.assertRaises(NotificationError):
            notifier.send("hello")
        self.assertEqual(sleeps, [])

    def test_retry_after_is_capped(self) -> None:
        sleeps = []
        notifier = self._notifier(
            [
                (429, json.dumps({"retry_after": 999})),
                (204, ""),
            ],
            sleeps,
        )

        notifier.send("hello")

        self.assertEqual(sleeps, [30.0])


class DiscordChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OutboxStore(Path(self._tmp.name) / "core.db")
        self.store.append(
            OutboxEvent(
                dedup_key="e:1",
                source="ki",
                category="FILL",
                event_type="FILLED",
                severity="warning",
                summary="GOOGL BUY 1주 체결",
                occurred_at="2026-07-23T00:00:00+00:00",
            ),
            channels=("discord",),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sends_formatted_message_and_marks_sent(self) -> None:
        sent = []

        def poster(url, data, headers, timeout):
            sent.append(data.decode("utf-8"))
            return 204, ""

        channel = DiscordChannel(
            DiscordNotifier("https://discord.example/webhook", poster=poster)
        )
        worker = NotificationWorker(self.store, {"discord": channel})

        result = worker.run_once()

        self.assertEqual(result["sent"], 1)
        self.assertIn("[경고]", sent[0])
        self.assertIn("GOOGL BUY 1주 체결", sent[0])

    def test_notifier_failure_marks_delivery_failed_with_error(self) -> None:
        def poster(url, data, headers, timeout):
            return 400, "bad request"

        channel = DiscordChannel(
            DiscordNotifier("https://discord.example/webhook", poster=poster)
        )
        worker = NotificationWorker(
            self.store,
            {"discord": channel},
            max_attempts=1,
        )

        result = worker.run_once()

        self.assertEqual(result["failed"], 1)
        delivery_id = 1
        status = self.store.delivery_status(delivery_id)
        self.assertEqual(status["status"], "FAILED")
        self.assertIn("HTTP 400", status["last_error"])


if __name__ == "__main__":
    unittest.main()
