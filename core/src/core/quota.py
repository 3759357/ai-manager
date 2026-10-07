from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.events import utc_now_iso


@dataclass(frozen=True)
class QuotaLimits:
    """LLM 호출 예산.

    risk.py가 돈의 예산이라면 이쪽은 호출의 예산이다.
    구독 사용량은 유한하므로 호출권도 리스크와 같은 방식으로 배급한다.
    """

    max_calls_per_window: int = 20
    window_hours: int = 5
    max_calls_per_day: int = 60
    min_interval_seconds: int = 30
    reserve_calls: int = 3

    def validate(self) -> None:
        for name, value in (
            ("max_calls_per_window", self.max_calls_per_window),
            ("window_hours", self.window_hours),
            ("max_calls_per_day", self.max_calls_per_day),
            ("min_interval_seconds", self.min_interval_seconds),
            ("reserve_calls", self.reserve_calls),
        ):
            if value < 0:
                raise ValueError(f"{name}는 0 이상이어야 합니다.")
        if self.reserve_calls >= self.max_calls_per_window:
            raise ValueError(
                "reserve_calls는 max_calls_per_window보다 작아야 합니다."
            )


class QuotaExhausted(Exception):
    """예산이 남지 않아 호출을 거부한다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class QuotaLedger:
    """호출 사용량 원장.

    성공·실패를 모두 기록한다. 실패한 호출도 구독 사용량은 소모하기 때문이다.
    """

    def __init__(self, database_path: Path, limits: QuotaLimits | None = None) -> None:
        self.database_path = database_path
        self.limits = limits or QuotaLimits()
        self.limits.validate()

    def check(self, *, now: datetime | None = None, urgent: bool = False) -> None:
        """호출 가능한지 확인한다. 불가하면 QuotaExhausted를 던진다."""
        now = now or datetime.now(timezone.utc)
        window_start = now - timedelta(hours=self.limits.window_hours)
        day_start = now - timedelta(days=1)

        with self._connect() as connection:
            window_used = self._count_since(connection, window_start)
            day_used = self._count_since(connection, day_start)
            last_at = self._last_called_at(connection)

        allowance = self.limits.max_calls_per_window
        if not urgent:
            allowance -= self.limits.reserve_calls
        if window_used >= allowance:
            raise QuotaExhausted(
                "WINDOW_EXHAUSTED",
                f"{self.limits.window_hours}시간 예산 소진: "
                f"{window_used}/{allowance}회",
            )
        if day_used >= self.limits.max_calls_per_day:
            raise QuotaExhausted(
                "DAILY_EXHAUSTED",
                f"일일 예산 소진: {day_used}/{self.limits.max_calls_per_day}회",
            )
        if last_at is not None:
            elapsed = (now - last_at).total_seconds()
            if elapsed < self.limits.min_interval_seconds:
                raise QuotaExhausted(
                    "TOO_SOON",
                    f"최소 호출 간격 미달: {elapsed:.0f}s < "
                    f"{self.limits.min_interval_seconds}s",
                )

    def record(
        self,
        *,
        role: str,
        cli: str,
        outcome: str,
        cost: int = 1,
        now: datetime | None = None,
    ) -> None:
        called_at = (now or datetime.now(timezone.utc)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO quota_usage (role, cli, outcome, cost, called_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (role, cli, outcome, cost, called_at),
            )

    def summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        window_start = now - timedelta(hours=self.limits.window_hours)
        day_start = now - timedelta(days=1)
        with self._connect() as connection:
            window_used = self._count_since(connection, window_start)
            day_used = self._count_since(connection, day_start)
        return {
            "window_used": window_used,
            "window_limit": self.limits.max_calls_per_window,
            "window_remaining": max(
                0, self.limits.max_calls_per_window - window_used
            ),
            "daily_used": day_used,
            "daily_limit": self.limits.max_calls_per_day,
            "checked_at": utc_now_iso(),
        }

    @staticmethod
    def _count_since(connection: sqlite3.Connection, since: datetime) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM quota_usage WHERE called_at >= ?",
            (since.isoformat(),),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _last_called_at(connection: sqlite3.Connection) -> datetime | None:
        row = connection.execute(
            "SELECT called_at FROM quota_usage ORDER BY called_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except ValueError:
            return None

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                cli TEXT NOT NULL,
                outcome TEXT NOT NULL,
                cost INTEGER NOT NULL DEFAULT 1,
                called_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_called_at "
            "ON quota_usage(called_at)"
        )
        return connection
