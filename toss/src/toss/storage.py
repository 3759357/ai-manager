from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class OrderStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def create_order(
        self,
        *,
        source: str,
        symbol: str,
        side: str,
        quantity: int,
        price: str,
        currency: str,
        estimated_amount: str,
        payload: dict[str, Any],
        preflight: dict[str, Any],
        status: str,
    ) -> int:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                """
                INSERT INTO orders (
                    created_at,
                    updated_at,
                    source,
                    symbol,
                    side,
                    quantity,
                    order_type,
                    price,
                    currency,
                    estimated_amount,
                    status,
                    client_order_id,
                    toss_order_id,
                    payload_json,
                    preflight_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    source,
                    symbol,
                    side,
                    quantity,
                    "LIMIT",
                    price,
                    currency,
                    estimated_amount,
                    status,
                    payload["clientOrderId"],
                    None,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(preflight, ensure_ascii=False, sort_keys=True),
                ),
            )
            order_id = cursor.lastrowid
        if order_id is None:
            raise RuntimeError("토스 주문 원장 ID를 생성하지 못했습니다.")
        return int(order_id)

    def get_order(self, order_id: int) -> dict[str, Any] | None:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM orders WHERE id = ?",
                (order_id,),
            ).fetchone()
        return _order_row(row) if row is not None else None

    def list_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_order_row(row) for row in rows]

    def mark_submitted(
        self,
        order_id: int,
        *,
        toss_order_id: str,
        response: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.execute(
                """
                UPDATE orders
                SET status = ?, toss_order_id = ?, updated_at = ?
                WHERE id = ?
                """,
                ("SUBMITTED", toss_order_id, now, order_id),
            )
            self._save_event(
                connection,
                order_id=order_id,
                event_type="SUBMITTED",
                payload=response,
            )

    def transition(
        self,
        order_id: int,
        *,
        expected: set[str],
        status: str,
        event_type: str,
        payload: dict[str, Any],
        toss_order_id: str | None = None,
    ) -> bool:
        """expected 상태일 때만 원자적으로 바꾼다. 두 프로세스가 같은 후보를 동시에 제출하지 못한다."""
        now = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" for _ in expected)
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            cursor = connection.execute(
                f"""
                UPDATE orders
                SET status = ?, toss_order_id = COALESCE(?, toss_order_id), updated_at = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (status, toss_order_id, now, order_id, *sorted(expected)),
            )
            if cursor.rowcount != 1:
                return False
            self._save_event(
                connection,
                order_id=order_id,
                event_type=event_type,
                payload=payload,
            )
        return True

    def last_event_at(self, order_id: int, event_type: str) -> str | None:
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            row = connection.execute(
                "SELECT created_at FROM order_events WHERE order_id = ? AND event_type = ? ORDER BY id DESC LIMIT 1",
                (order_id, event_type),
            ).fetchone()
        return row[0] if row else None

    def update_status(
        self,
        order_id: int,
        *,
        status: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            self._create_schema(connection)
            connection.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, order_id),
            )
            self._save_event(
                connection,
                order_id=order_id,
                event_type=event_type,
                payload=payload,
            )

    @staticmethod
    def _save_event(
        connection: sqlite3.Connection,
        *,
        order_id: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO order_events (created_at, order_id, event_type, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                order_id,
                event_type,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                order_type TEXT NOT NULL,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                estimated_amount TEXT NOT NULL,
                status TEXT NOT NULL,
                client_order_id TEXT NOT NULL UNIQUE,
                toss_order_id TEXT,
                payload_json TEXT NOT NULL,
                preflight_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                order_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id)
            )
            """
        )


def _order_row(row: sqlite3.Row) -> dict[str, Any]:
    result = {key: row[key] for key in row.keys()}
    result["payload"] = json.loads(result.pop("payload_json"))
    result["preflight"] = json.loads(result.pop("preflight_json"))
    return result
