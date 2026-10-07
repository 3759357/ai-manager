from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

CATEGORIES = frozenset(
    {"ORDER", "FILL", "RISK", "SIGNAL", "STRATEGY", "SYSTEM"}
)
SEVERITIES = frozenset({"info", "warning", "critical"})
CHANNELS = frozenset({"console", "discord"})

DELIVERY_PENDING = "PENDING"
DELIVERY_SENT = "SENT"
DELIVERY_FAILED = "FAILED"
DELIVERY_SKIPPED = "SKIPPED"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OutboxEvent:
    dedup_key: str
    source: str
    category: str
    event_type: str
    summary: str
    occurred_at: str
    broker: str | None = None
    symbol: str | None = None
    order_ref: str | None = None
    severity: str = "info"
    payload: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.dedup_key.strip():
            raise ValueError("이벤트 dedup_key가 필요합니다.")
        if not self.source.strip():
            raise ValueError("이벤트 source가 필요합니다.")
        if self.category not in CATEGORIES:
            raise ValueError(
                f"이벤트 category는 {sorted(CATEGORIES)} 중 하나여야 합니다."
            )
        if not self.event_type.strip():
            raise ValueError("이벤트 event_type이 필요합니다.")
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"이벤트 severity는 {sorted(SEVERITIES)} 중 하나여야 합니다."
            )
        if not self.summary.strip():
            raise ValueError("이벤트 summary가 필요합니다.")
        if not self.occurred_at.strip():
            raise ValueError("이벤트 occurred_at이 필요합니다.")
