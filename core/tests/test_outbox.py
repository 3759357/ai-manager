import tempfile
import unittest
from pathlib import Path

from core.events import OutboxEvent, utc_now_iso
from core.outbox import OutboxStore


def _event(dedup_key: str = "ki:order_events:1") -> OutboxEvent:
    return OutboxEvent(
        dedup_key=dedup_key,
        source="ki",
        category="ORDER",
        event_type="SUBMITTED",
        broker="kis",
        symbol="GOOGL",
        order_ref="42",
        summary="GOOGL BUY 1주 지정가 제출",
        payload={"orderId": 42},
        occurred_at=utc_now_iso(),
    )


class OutboxStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = OutboxStore(Path(self._tmp.name) / "core.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_creates_event_and_pending_deliveries(self) -> None:
        result = self.store.append(
            _event(),
            channels=("console", "discord"),
        )

        self.assertTrue(result["created"])
        pending_console = self.store.list_pending_deliveries(channel="console")
        pending_discord = self.store.list_pending_deliveries(channel="discord")
        self.assertEqual(len(pending_console), 1)
        self.assertEqual(len(pending_discord), 1)
        self.assertEqual(pending_console[0]["summary"], "GOOGL BUY 1주 지정가 제출")
        self.assertEqual(pending_console[0]["payload"], {"orderId": 42})

    def test_append_deduplicates_by_dedup_key(self) -> None:
        first = self.store.append(_event())
        second = self.store.append(_event())

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(self.store.list_events()), 1)
        self.assertEqual(len(self.store.list_pending_deliveries()), 1)

    def test_append_rejects_invalid_event(self) -> None:
        event = OutboxEvent(
            dedup_key="x",
            source="ki",
            category="INVALID",
            event_type="SUBMITTED",
            summary="s",
            occurred_at=utc_now_iso(),
        )
        with self.assertRaises(ValueError):
            self.store.append(event)

    def test_append_rejects_unknown_channel(self) -> None:
        with self.assertRaises(ValueError):
            self.store.append(_event(), channels=("kakao",))

    def test_mark_sent_records_status_and_attempt(self) -> None:
        self.store.append(_event())
        delivery_id = self.store.list_pending_deliveries()[0]["delivery_id"]

        self.store.mark_sent(delivery_id)

        status = self.store.delivery_status(delivery_id)
        self.assertEqual(status["status"], "SENT")
        self.assertEqual(status["attempts"], 1)
        self.assertIsNotNone(status["sent_at"])
        self.assertEqual(self.store.list_pending_deliveries(), [])

    def test_mark_failed_keeps_pending_until_max_attempts(self) -> None:
        self.store.append(_event())
        delivery_id = self.store.list_pending_deliveries()[0]["delivery_id"]

        first = self.store.mark_failed(delivery_id, "timeout", max_attempts=3)
        second = self.store.mark_failed(delivery_id, "timeout", max_attempts=3)
        third = self.store.mark_failed(delivery_id, "timeout", max_attempts=3)

        self.assertEqual(first, "PENDING")
        self.assertEqual(second, "PENDING")
        self.assertEqual(third, "FAILED")
        status = self.store.delivery_status(delivery_id)
        self.assertEqual(status["attempts"], 3)
        self.assertEqual(status["last_error"], "timeout")
        self.assertEqual(self.store.list_pending_deliveries(), [])

    def test_list_events_filters_by_category(self) -> None:
        self.store.append(_event("ki:order_events:1"))
        fill = OutboxEvent(
            dedup_key="ki:fills:1",
            source="ki",
            category="FILL",
            event_type="FILLED",
            summary="GOOGL BUY 1주 체결",
            occurred_at=utc_now_iso(),
        )
        self.store.append(fill)

        orders = self.store.list_events(category="ORDER")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["category"], "ORDER")

    def test_unknown_delivery_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.store.mark_sent(999)
        with self.assertRaises(ValueError):
            self.store.mark_failed(999, "x")


if __name__ == "__main__":
    unittest.main()
