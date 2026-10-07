from __future__ import annotations

import sys
import time
from typing import Any, Callable, TextIO

from core.audit import record_core_event
from core.notify import DiscordNotifier
from core.outbox import OutboxStore
from core.relay import OutboxRelay

ChannelHandler = Callable[[dict[str, Any]], None]

_SEVERITY_PREFIX = {
    "info": "[알림]",
    "warning": "[경고]",
    "critical": "[긴급]",
}


def format_message(item: dict[str, Any]) -> str:
    prefix = _SEVERITY_PREFIX.get(item.get("severity", "info"), "[알림]")
    occurred_at = str(item.get("occurred_at", ""))[:19]
    return f"{prefix} {occurred_at} {item['summary']}"


class ConsoleChannel:
    """콘솔 채널. delivery 요약을 표준 출력에 쓴다."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def __call__(self, item: dict[str, Any]) -> None:
        print(format_message(item), file=self.stream)


class DiscordChannel:
    """Discord 채널. delivery 요약을 웹훅으로 보낸다."""

    def __init__(self, notifier: DiscordNotifier) -> None:
        self.notifier = notifier

    def __call__(self, item: dict[str, Any]) -> None:
        self.notifier.send(format_message(item))


class NotificationWorker:
    """PENDING delivery를 소비해 채널로 보내고 전송 상태를 기록한다."""

    def __init__(
        self,
        store: OutboxStore,
        handlers: dict[str, ChannelHandler],
        *,
        relay: OutboxRelay | None = None,
        max_attempts: int = 3,
        batch_size: int = 50,
    ) -> None:
        if not handlers:
            raise ValueError("알림 채널 핸들러가 하나 이상 필요합니다.")
        self.store = store
        self.handlers = handlers
        self.relay = relay
        self.max_attempts = max_attempts
        self.batch_size = batch_size

    def run_once(self) -> dict[str, Any]:
        result: dict[str, Any] = {"sent": 0, "failed": 0, "still_pending": 0}
        if self.relay is not None:
            try:
                result["relayed"] = self.relay.sync()["relayed_total"]
            except Exception as exc:  # noqa: BLE001 - relay 오류도 알림 대상
                result["relay_error"] = str(exc)
                self._record_worker_error("RELAY_ERROR", str(exc))
        for channel, handler in sorted(self.handlers.items()):
            pending = self.store.list_pending_deliveries(
                channel=channel,
                limit=self.batch_size,
            )
            for item in pending:
                delivery_id = item["delivery_id"]
                try:
                    handler(item)
                except Exception as exc:  # noqa: BLE001 - 전송 실패는 기록한다
                    status = self.store.mark_failed(
                        delivery_id,
                        str(exc),
                        max_attempts=self.max_attempts,
                    )
                    if status == "FAILED":
                        result["failed"] += 1
                    else:
                        result["still_pending"] += 1
                else:
                    self.store.mark_sent(delivery_id)
                    result["sent"] += 1
        return result

    def _record_worker_error(self, event_type: str, error: str) -> None:
        try:
            record_core_event(
                self.store,
                category="SYSTEM",
                event_type=event_type,
                severity="critical",
                summary=f"[Worker] {event_type}: {error}"[:500],
                payload={"error": error[:500]},
                channels=tuple(self.handlers),
            )
        except Exception:  # noqa: BLE001 - 오류 기록 실패는 무시한다
            pass

    def run_forever(
        self,
        *,
        interval_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        max_cycles: int | None = None,
    ) -> None:
        cycles = 0
        while True:
            self.run_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            sleep(interval_seconds)
