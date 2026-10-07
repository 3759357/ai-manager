from __future__ import annotations

import uuid
from typing import Any

from core.events import OutboxEvent, utc_now_iso
from core.outbox import OutboxStore


def record_core_event(
    store: OutboxStore,
    *,
    category: str,
    event_type: str,
    summary: str,
    severity: str = "info",
    broker: str | None = None,
    symbol: str | None = None,
    order_ref: str | None = None,
    payload: dict[str, Any] | None = None,
    channels: tuple[str, ...] = ("console",),
) -> dict[str, Any]:
    """Core 자체 이벤트를 아웃박스에 기록한다.

    relay 대상이 아니므로 dedup_key는 uuid로 만든다.
    """
    event = OutboxEvent(
        dedup_key=f"core:{uuid.uuid4()}",
        source="core",
        category=category,
        event_type=event_type,
        broker=broker,
        symbol=symbol,
        order_ref=order_ref,
        severity=severity,
        summary=summary,
        payload=payload or {},
        occurred_at=utc_now_iso(),
    )
    return store.append(event, channels=channels)


def record_core_error(
    store: OutboxStore,
    *,
    command: str,
    error: str,
    broker: str | None = None,
    symbol: str | None = None,
    channels: tuple[str, ...] = ("console",),
) -> dict[str, Any]:
    return record_core_event(
        store,
        category="SYSTEM",
        event_type="COMMAND_ERROR",
        severity="warning",
        broker=broker,
        symbol=symbol,
        summary=f"[Core] {command} 실패: {error}"[:500],
        payload={"command": command, "error": error[:500]},
        channels=channels,
    )
