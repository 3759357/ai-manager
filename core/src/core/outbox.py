from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from core.events import (
    CHANNELS,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    DELIVERY_SENT,
    OutboxEvent,
    utc_now_iso,
)


class OutboxStore:
    """공통 이벤트 아웃박스와 채널별 전송 상태 저장소."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def append(
        self,
        event: OutboxEvent,
        *,
        channels: tuple[str, ...] = ("console",),
    ) -> dict[str, Any]:
        """이벤트를 저장하고 채널별 PENDING delivery를 만든다.

        같은 dedup_key가 이미 있으면 새로 저장하지 않고 기존 id를 돌려준다.
        """
        event.validate()
        for channel in channels:
            if channel not in CHANNELS:
                raise ValueError(
                    f"알림 채널은 {sorted(CHANNELS)} 중 하나여야 합니다: {channel}"
                )
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM event_outbox WHERE dedup_key = ?",
                (event.dedup_key,),
            ).fetchone()
            if existing is not None:
                return {"event_id": int(existing[0]), "created": False}
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO event_outbox (
                    dedup_key,
                    source,
                    category,
                    event_type,
                    broker,
                    symbol,
                    order_ref,
                    severity,
                    summary,
                    payload_json,
                    occurred_at,
                    recorded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.dedup_key,
                    event.source,
                    event.category,
                    event.event_type,
                    event.broker,
                    event.symbol,
                    event.order_ref,
                    event.severity,
                    event.summary,
                    json.dumps(
                        event.payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    event.occurred_at,
                    utc_now_iso(),
                ),
            )
            if cursor.rowcount == 0:
                # 다른 프로세스가 같은 dedup_key를 먼저 넣었다(SELECT와 INSERT 사이 경쟁).
                existing = connection.execute(
                    "SELECT id FROM event_outbox WHERE dedup_key = ?",
                    (event.dedup_key,),
                ).fetchone()
                return {"event_id": int(existing[0]), "created": False}
            event_id = cursor.lastrowid
            if event_id is None:
                raise RuntimeError("이벤트 아웃박스 ID를 생성하지 못했습니다.")
            for channel in channels:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO deliveries (event_id, channel, status)
                    VALUES (?, ?, ?)
                    """,
                    (event_id, channel, DELIVERY_PENDING),
                )
        return {"event_id": int(event_id), "created": True}

    def list_events(
        self,
        *,
        limit: int = 20,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM event_outbox"
        params: list[Any] = []
        if category:
            query += " WHERE category = ?"
            params.append(category)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        return [self._event_row(row) for row in rows]

    def list_events_after(
        self,
        *,
        after_id: int,
        category: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """커서 이후의 이벤트를 오름차순으로 돌려준다.

        relay_cursors와 함께 써서 워커가 이벤트를 한 번씩만 소비하게 한다.
        """
        query = "SELECT * FROM event_outbox WHERE id > ?"
        params: list[Any] = [after_id]
        if category:
            query += " AND category = ?"
            params.append(category)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        return [self._event_row(row) for row in rows]

    def list_pending_deliveries(
        self,
        *,
        channel: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                deliveries.id AS delivery_id,
                deliveries.channel AS channel,
                deliveries.status AS status,
                deliveries.attempts AS attempts,
                deliveries.last_error AS last_error,
                event_outbox.*
            FROM deliveries
            JOIN event_outbox ON event_outbox.id = deliveries.event_id
            WHERE deliveries.status = ?
        """
        params: list[Any] = [DELIVERY_PENDING]
        if channel:
            query += " AND deliveries.channel = ?"
            params.append(channel)
        query += " ORDER BY deliveries.id ASC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            item = self._event_row(row)
            item["delivery_id"] = int(row["delivery_id"])
            item["channel"] = row["channel"]
            item["attempts"] = int(row["attempts"])
            item["last_error"] = row["last_error"]
            results.append(item)
        return results

    def mark_sent(self, delivery_id: int) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE deliveries
                SET status = ?, attempts = attempts + 1, sent_at = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (DELIVERY_SENT, utc_now_iso(), delivery_id),
            ).rowcount
        if updated == 0:
            raise ValueError(f"delivery를 찾지 못했습니다: {delivery_id}")

    def mark_failed(
        self,
        delivery_id: int,
        error: str,
        *,
        max_attempts: int = 3,
    ) -> str:
        """실패를 기록한다. 시도 횟수가 한도에 도달하면 FAILED로 확정한다."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"delivery를 찾지 못했습니다: {delivery_id}")
            attempts = int(row[0]) + 1
            status = (
                DELIVERY_FAILED if attempts >= max_attempts else DELIVERY_PENDING
            )
            connection.execute(
                """
                UPDATE deliveries
                SET status = ?, attempts = ?, last_error = ?
                WHERE id = ?
                """,
                (status, attempts, error[:500], delivery_id),
            )
        return status

    def get_cursor(self, source: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_row_id FROM relay_cursors WHERE source = ?",
                (source,),
            ).fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, source: str, last_row_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO relay_cursors (source, last_row_id)
                VALUES (?, ?)
                ON CONFLICT(source) DO UPDATE SET last_row_id = excluded.last_row_id
                """,
                (source, last_row_id),
            )

    def delivery_status(self, delivery_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"delivery를 찾지 못했습니다: {delivery_id}")
        return {key: row[key] for key in row.keys()}

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        self._create_schema(connection)
        return connection

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "dedup_key": row["dedup_key"],
            "source": row["source"],
            "category": row["category"],
            "event_type": row["event_type"],
            "broker": row["broker"],
            "symbol": row["symbol"],
            "order_ref": row["order_ref"],
            "severity": row["severity"],
            "summary": row["summary"],
            "payload": json.loads(row["payload_json"]),
            "occurred_at": row["occurred_at"],
            "recorded_at": row["recorded_at"],
        }

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS event_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                category TEXT NOT NULL,
                event_type TEXT NOT NULL,
                broker TEXT,
                symbol TEXT,
                order_ref TEXT,
                severity TEXT NOT NULL DEFAULT 'info',
                summary TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES event_outbox(id),
                channel TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                sent_at TEXT,
                UNIQUE(event_id, channel)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS relay_cursors (
                source TEXT PRIMARY KEY,
                last_row_id INTEGER NOT NULL
            )
            """
        )
