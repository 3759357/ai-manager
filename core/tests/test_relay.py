import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.outbox import OutboxStore
from core.relay import OutboxRelay


def _create_ki_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE broker_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, quantity INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT, order_id INTEGER, event_type TEXT,
                status TEXT, message TEXT, payload_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE order_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, quantity INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE risk_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT, order_intent_id INTEGER,
                decision TEXT, reasons_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT, order_id INTEGER, symbol TEXT, side TEXT,
                quantity INTEGER, price TEXT, currency TEXT, fee TEXT
            )
            """
        )


def _create_toss_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, quantity INTEGER, status TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT, order_id INTEGER,
                event_type TEXT, payload_json TEXT
            )
            """
        )


class OutboxRelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.ki_db = base / "ki.db"
        self.toss_db = base / "toss.db"
        _create_ki_db(self.ki_db)
        _create_toss_db(self.toss_db)
        self.store = OutboxStore(base / "core.db")
        self.relay = OutboxRelay(
            self.store,
            kis_database_path=self.ki_db,
            toss_database_path=self.toss_db,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _insert_ki_order_event(self, message: str = "제출 완료") -> None:
        with sqlite3.connect(self.ki_db) as connection:
            connection.execute(
                "INSERT INTO broker_orders (symbol, side, quantity) "
                "VALUES ('GOOGL', 'buy', 1)"
            )
            connection.execute(
                """
                INSERT INTO order_events (
                    created_at, order_id, event_type, status, message,
                    payload_json
                )
                VALUES ('2026-07-14T00:00:00+00:00', 1, 'SUBMITTED',
                        'SUBMITTED', ?, ?)
                """,
                (
                    message,
                    json.dumps({"CANO": "12345678", "price": "368.84"}),
                ),
            )

    def test_relays_ki_order_event_with_summary_and_masking(self) -> None:
        self._insert_ki_order_event()

        result = self.relay.sync()

        self.assertEqual(result["ki:order_events"]["relayed"], 1)
        event = self.store.list_events(category="ORDER")[0]
        self.assertEqual(event["source"], "ki")
        self.assertEqual(event["symbol"], "GOOGL")
        self.assertIn("[KIS] GOOGL BUY 1주", event["summary"])
        self.assertEqual(event["payload"]["CANO"], "***")
        self.assertEqual(event["payload"]["price"], "368.84")
        self.assertEqual(
            len(self.store.list_pending_deliveries(channel="console")),
            1,
        )

    def test_sync_is_idempotent_via_cursor_and_dedup(self) -> None:
        self._insert_ki_order_event()

        self.relay.sync()
        second = self.relay.sync()

        self.assertEqual(second["relayed_total"], 0)
        self.assertEqual(len(self.store.list_events()), 1)
        self.assertEqual(self.store.get_cursor("ki:order_events"), 1)

    def test_relays_fill_and_risk_block(self) -> None:
        with sqlite3.connect(self.ki_db) as connection:
            connection.execute(
                """
                INSERT INTO fills (
                    created_at, order_id, symbol, side, quantity, price,
                    currency, fee
                )
                VALUES ('2026-07-14T00:00:00+00:00', 1, 'GOOGL', 'buy', 1,
                        '368.76', 'USD', '0.09')
                """
            )
            connection.execute(
                "INSERT INTO order_intents (symbol, side, quantity) "
                "VALUES ('SOXL', 'buy', 3)"
            )
            connection.execute(
                """
                INSERT INTO risk_decisions (
                    created_at, order_intent_id, decision, reasons_json
                )
                VALUES ('2026-07-14T00:00:00+00:00', 1, 'BLOCK',
                        '["주문 한도 초과"]')
                """
            )

        result = self.relay.sync()

        self.assertEqual(result["ki:fills"]["relayed"], 1)
        self.assertEqual(result["ki:risk_decisions"]["relayed"], 1)
        fill = self.store.list_events(category="FILL")[0]
        self.assertIn("체결", fill["summary"])
        risk = self.store.list_events(category="RISK")[0]
        self.assertEqual(risk["severity"], "warning")
        self.assertIn("주문 한도 초과", risk["summary"])

    def test_relays_toss_order_event(self) -> None:
        with sqlite3.connect(self.toss_db) as connection:
            connection.execute(
                "INSERT INTO orders (symbol, side, quantity, status) "
                "VALUES ('AAPL', 'sell', 2, 'SUBMITTED')"
            )
            connection.execute(
                """
                INSERT INTO order_events (
                    created_at, order_id, event_type, payload_json
                )
                VALUES ('2026-07-14T00:00:00+00:00', 1, 'SUBMITTED',
                        '{"tossOrderId": "T-1"}')
                """
            )

        result = self.relay.sync()

        self.assertEqual(result["toss:order_events"]["relayed"], 1)
        event = self.store.list_events(category="ORDER")[0]
        self.assertEqual(event["broker"], "toss")
        self.assertIn("[Toss] AAPL SELL 2주", event["summary"])

    def test_missing_database_is_skipped(self) -> None:
        relay = OutboxRelay(
            self.store,
            kis_database_path=Path(self._tmp.name) / "없음.db",
            toss_database_path=self.toss_db,
        )

        result = relay.sync()

        self.assertEqual(
            result["ki:order_events"]["skipped_reason"],
            "source-unavailable",
        )
        self.assertEqual(result["relayed_total"], 0)

    def test_rejected_event_gets_warning_severity(self) -> None:
        with sqlite3.connect(self.ki_db) as connection:
            connection.execute(
                "INSERT INTO broker_orders (symbol, side, quantity) "
                "VALUES ('GOOGL', 'buy', 1)"
            )
            connection.execute(
                """
                INSERT INTO order_events (
                    created_at, order_id, event_type, status, message,
                    payload_json
                )
                VALUES ('2026-07-14T00:00:00+00:00', 1, 'REJECTED',
                        'REJECTED', '증거금 부족', '{}')
                """
            )

        self.relay.sync()

        event = self.store.list_events(category="ORDER")[0]
        self.assertEqual(event["severity"], "warning")


if __name__ == "__main__":
    unittest.main()
