import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.quota import QuotaExhausted, QuotaLedger, QuotaLimits

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


class QuotaLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.limits = QuotaLimits(
            max_calls_per_window=5,
            window_hours=5,
            max_calls_per_day=8,
            min_interval_seconds=30,
            reserve_calls=1,
        )
        self.ledger = QuotaLedger(Path(self._tmp.name) / "core.db", self.limits)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_allows_first_call(self) -> None:
        self.ledger.check(now=NOW)

    def test_blocks_when_window_allowance_spent(self) -> None:
        for index in range(4):
            self.ledger.record(
                role="reviewer",
                cli="codex",
                outcome="OK",
                now=NOW - timedelta(minutes=index + 1),
            )
        with self.assertRaises(QuotaExhausted) as ctx:
            self.ledger.check(now=NOW)
        self.assertEqual(ctx.exception.code, "WINDOW_EXHAUSTED")

    def test_urgent_call_may_use_reserve(self) -> None:
        for index in range(4):
            self.ledger.record(
                role="reviewer",
                cli="codex",
                outcome="OK",
                now=NOW - timedelta(minutes=index + 1),
            )
        self.ledger.check(now=NOW, urgent=True)

    def test_blocks_calls_that_are_too_close_together(self) -> None:
        self.ledger.record(
            role="reviewer", cli="codex", outcome="OK", now=NOW - timedelta(seconds=5)
        )
        with self.assertRaises(QuotaExhausted) as ctx:
            self.ledger.check(now=NOW)
        self.assertEqual(ctx.exception.code, "TOO_SOON")

    def test_old_calls_fall_out_of_window(self) -> None:
        for index in range(4):
            self.ledger.record(
                role="reviewer",
                cli="codex",
                outcome="OK",
                now=NOW - timedelta(hours=6, minutes=index),
            )
        self.ledger.check(now=NOW)

    def test_failed_calls_still_consume_budget(self) -> None:
        for index in range(4):
            self.ledger.record(
                role="reviewer",
                cli="codex",
                outcome="ERROR",
                now=NOW - timedelta(minutes=index + 1),
            )
        with self.assertRaises(QuotaExhausted):
            self.ledger.check(now=NOW)

    def test_reserve_must_be_smaller_than_window_limit(self) -> None:
        with self.assertRaises(ValueError):
            QuotaLimits(max_calls_per_window=3, reserve_calls=3).validate()


if __name__ == "__main__":
    unittest.main()
