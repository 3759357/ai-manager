from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from core.events import OutboxEvent
from core.outbox import OutboxStore

_WARNING_MARKERS = (
    "REJECT",
    "FAIL",
    "ERROR",
    "CANCEL",
    "BLOCK",
)

_SENSITIVE_KEY_MARKERS = (
    "account",
    "acct",
    "cano",
    "secret",
    "token",
    "key",
)

_MASK = "***"


class OutboxRelay:
    """브로커 로컬 원장의 이벤트를 read-only로 읽어 아웃박스에 복제한다."""

    def __init__(
        self,
        store: OutboxStore,
        *,
        kis_database_path: Path,
        toss_database_path: Path,
        channels: tuple[str, ...] = ("console",),
        batch_size: int = 200,
    ) -> None:
        self.store = store
        self.kis_database_path = kis_database_path
        self.toss_database_path = toss_database_path
        self.channels = channels
        self.batch_size = batch_size

    def sync(self) -> dict[str, Any]:
        results = {
            "ki:order_events": self._sync_ki_order_events(),
            "ki:fills": self._sync_ki_fills(),
            "ki:risk_decisions": self._sync_ki_risk_decisions(),
            "toss:order_events": self._sync_toss_order_events(),
        }
        results["relayed_total"] = sum(
            item["relayed"] for item in results.values()
        )
        return results

    def _sync_ki_order_events(self) -> dict[str, Any]:
        return self._sync_source(
            source="ki:order_events",
            database_path=self.kis_database_path,
            query="""
                SELECT
                    order_events.id AS row_id,
                    order_events.created_at AS created_at,
                    order_events.order_id AS order_id,
                    order_events.event_type AS event_type,
                    order_events.status AS status,
                    order_events.message AS message,
                    order_events.payload_json AS payload_json,
                    broker_orders.symbol AS symbol,
                    broker_orders.side AS side,
                    broker_orders.quantity AS quantity
                FROM order_events
                LEFT JOIN broker_orders
                    ON broker_orders.id = order_events.order_id
                WHERE order_events.id > ?
                ORDER BY order_events.id ASC
                LIMIT ?
            """,
            build=self._build_ki_order_event,
        )

    def _sync_ki_fills(self) -> dict[str, Any]:
        return self._sync_source(
            source="ki:fills",
            database_path=self.kis_database_path,
            query="""
                SELECT
                    id AS row_id,
                    created_at,
                    order_id,
                    symbol,
                    side,
                    quantity,
                    price,
                    currency,
                    fee
                FROM fills
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
            """,
            build=self._build_ki_fill,
        )

    def _sync_ki_risk_decisions(self) -> dict[str, Any]:
        return self._sync_source(
            source="ki:risk_decisions",
            database_path=self.kis_database_path,
            query="""
                SELECT
                    risk_decisions.id AS row_id,
                    risk_decisions.created_at AS created_at,
                    risk_decisions.order_intent_id AS order_intent_id,
                    risk_decisions.decision AS decision,
                    risk_decisions.reasons_json AS reasons_json,
                    order_intents.symbol AS symbol,
                    order_intents.side AS side,
                    order_intents.quantity AS quantity
                FROM risk_decisions
                LEFT JOIN order_intents
                    ON order_intents.id = risk_decisions.order_intent_id
                WHERE risk_decisions.id > ?
                ORDER BY risk_decisions.id ASC
                LIMIT ?
            """,
            build=self._build_ki_risk_decision,
        )

    def _sync_toss_order_events(self) -> dict[str, Any]:
        return self._sync_source(
            source="toss:order_events",
            database_path=self.toss_database_path,
            query="""
                SELECT
                    order_events.id AS row_id,
                    order_events.created_at AS created_at,
                    order_events.order_id AS order_id,
                    order_events.event_type AS event_type,
                    order_events.payload_json AS payload_json,
                    orders.symbol AS symbol,
                    orders.side AS side,
                    orders.quantity AS quantity,
                    orders.status AS status
                FROM order_events
                LEFT JOIN orders ON orders.id = order_events.order_id
                WHERE order_events.id > ?
                ORDER BY order_events.id ASC
                LIMIT ?
            """,
            build=self._build_toss_order_event,
        )

    def _sync_source(
        self,
        *,
        source: str,
        database_path: Path,
        query: str,
        build: Any,
    ) -> dict[str, Any]:
        rows = self._read_rows(database_path, query, source)
        if rows is None:
            return {"relayed": 0, "skipped_reason": "source-unavailable"}
        relayed = 0
        last_row_id = self.store.get_cursor(source)
        for row in rows:
            event = build(row)
            result = self.store.append(event, channels=self.channels)
            if result["created"]:
                relayed += 1
            last_row_id = int(row["row_id"])
        if rows:
            self.store.set_cursor(source, last_row_id)
        return {"relayed": relayed, "cursor": last_row_id}

    def _read_rows(
        self,
        database_path: Path,
        query: str,
        source: str,
    ) -> list[sqlite3.Row] | None:
        if not database_path.exists():
            return None
        uri = f"file:{database_path}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                return connection.execute(
                    query,
                    (self.store.get_cursor(source), self.batch_size),
                ).fetchall()
        except sqlite3.OperationalError:
            return None

    def _build_ki_order_event(self, row: sqlite3.Row) -> OutboxEvent:
        symbol = row["symbol"]
        side = (row["side"] or "").upper()
        quantity = row["quantity"]
        event_type = str(row["event_type"])
        status = str(row["status"])
        message = str(row["message"] or "").strip()
        parts = ["[KIS]"]
        if symbol:
            parts.append(f"{symbol} {side} {quantity}주")
        parts.append(f"{event_type} ({status})")
        if message:
            parts.append(message)
        return OutboxEvent(
            dedup_key=f"ki:order_events:{row['row_id']}",
            source="ki",
            category="ORDER",
            event_type=event_type,
            broker="kis",
            symbol=symbol,
            order_ref=str(row["order_id"]),
            severity=_severity(event_type, status),
            summary=" ".join(parts),
            payload=_mask_payload(_load_json(row["payload_json"])),
            occurred_at=str(row["created_at"]),
        )

    def _build_ki_fill(self, row: sqlite3.Row) -> OutboxEvent:
        symbol = str(row["symbol"])
        side = str(row["side"]).upper()
        summary = (
            f"[KIS] {symbol} {side} {row['quantity']}주 "
            f"@{row['price']} {row['currency']} 체결"
        )
        return OutboxEvent(
            dedup_key=f"ki:fills:{row['row_id']}",
            source="ki",
            category="FILL",
            event_type="FILLED",
            broker="kis",
            symbol=symbol,
            order_ref=str(row["order_id"]),
            severity="info",
            summary=summary,
            payload={
                "quantity": row["quantity"],
                "price": row["price"],
                "currency": row["currency"],
                "fee": row["fee"],
            },
            occurred_at=str(row["created_at"]),
        )

    def _build_ki_risk_decision(self, row: sqlite3.Row) -> OutboxEvent:
        decision = str(row["decision"]).upper()
        symbol = row["symbol"]
        side = (row["side"] or "").upper()
        reasons = _load_json(row["reasons_json"])
        reason_text = (
            ", ".join(str(reason) for reason in reasons)
            if isinstance(reasons, list) and reasons
            else ""
        )
        parts = ["[KIS 리스크]"]
        if symbol:
            parts.append(f"{symbol} {side} {row['quantity']}주")
        parts.append(decision)
        if reason_text:
            parts.append(reason_text)
        return OutboxEvent(
            dedup_key=f"ki:risk_decisions:{row['row_id']}",
            source="ki",
            category="RISK",
            event_type=f"RISK_{decision}",
            broker="kis",
            symbol=symbol,
            order_ref=str(row["order_intent_id"]),
            severity="warning" if decision != "ALLOW" else "info",
            summary=" ".join(parts),
            payload={"decision": decision, "reasons": reasons},
            occurred_at=str(row["created_at"]),
        )

    def _build_toss_order_event(self, row: sqlite3.Row) -> OutboxEvent:
        symbol = row["symbol"]
        side = (row["side"] or "").upper()
        event_type = str(row["event_type"])
        status = str(row["status"] or "")
        parts = ["[Toss]"]
        if symbol:
            parts.append(f"{symbol} {side} {row['quantity']}주")
        parts.append(event_type)
        if status:
            parts.append(f"({status})")
        return OutboxEvent(
            dedup_key=f"toss:order_events:{row['row_id']}",
            source="toss",
            category="ORDER",
            event_type=event_type,
            broker="toss",
            symbol=symbol,
            order_ref=str(row["order_id"]),
            severity=_severity(event_type, status),
            summary=" ".join(parts),
            payload=_mask_payload(_load_json(row["payload_json"])),
            occurred_at=str(row["created_at"]),
        )


def _severity(event_type: str, status: str) -> str:
    haystack = f"{event_type} {status}".upper()
    if any(marker in haystack for marker in _WARNING_MARKERS):
        return "warning"
    return "info"


def _load_json(raw: Any) -> Any:
    if raw in {None, ""}:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": str(raw)}


def _mask_payload(value: Any) -> Any:
    if isinstance(value, dict):
        masked = {}
        for key, item in value.items():
            if any(
                marker in key.lower() for marker in _SENSITIVE_KEY_MARKERS
            ):
                masked[key] = _MASK
            else:
                masked[key] = _mask_payload(item)
        return masked
    if isinstance(value, list):
        return [_mask_payload(item) for item in value]
    return value
