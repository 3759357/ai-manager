import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ki.dip_watch import (
    DipWatcher,
    DipWatchOptions,
    evaluate_candles,
    normalize_candles,
)
from ki.storage import SnapshotStore


class DipWatchTest(unittest.TestCase):
    def test_candidate_requires_drawdown_stability_and_rebound(self) -> None:
        options = DipWatchOptions(
            quantity=1,
            lookback_bars=8,
            stable_bars=2,
            watch_low=Decimal("94"),
            watch_high=Decimal("96"),
            min_drawdown_pct=Decimal("4"),
            rebound_pct=Decimal("2"),
        )
        candles = normalize_candles(
            [
                row("100000", "100", "100", "99"),
                row("100100", "99", "99", "98"),
                row("100200", "97", "97", "95"),
                row("100300", "95.5", "96", "95.2"),
                row("100400", "96", "96.5", "95.4"),
                row("100500", "97", "97.2", "96"),
            ]
        )

        result = evaluate_candles(candles, options)

        self.assertTrue(result["ready"])
        self.assertEqual(result["trough_price"], "95")
        self.assertEqual(result["bars_since_low"], 3)
        self.assertEqual(result["rebound_pct"], "2.1053")

    def test_new_low_blocks_candidate(self) -> None:
        options = DipWatchOptions(
            quantity=1,
            lookback_bars=8,
            stable_bars=2,
            min_drawdown_pct=Decimal("4"),
            rebound_pct=Decimal("1"),
        )
        candles = normalize_candles(
            [
                row("100000", "100", "100", "99"),
                row("100100", "98", "98", "97"),
                row("100200", "96", "96", "95"),
                row("100300", "95", "95.5", "94"),
            ]
        )

        result = evaluate_candles(candles, options)

        self.assertFalse(result["ready"])
        self.assertFalse(result["checks"]["stable"])

    def test_watcher_saves_one_manual_approval_candidate(self) -> None:
        target_rows = [
            row("100000", "100", "100", "99"),
            row("100100", "98", "98", "97"),
            row("100200", "95", "96", "95"),
            row("100300", "95.5", "96", "95.2"),
            row("100400", "96", "96.5", "95.4"),
            row("100500", "97", "97.2", "96"),
        ]
        reference_rows = [
            row("100000", "500", "501", "499"),
            row("100100", "495", "496", "494"),
            row("100200", "490", "491", "489"),
            row("100300", "491", "492", "490"),
            row("100400", "493", "494", "491"),
            row("100500", "495", "496", "493"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "dip.db")
            watcher = DipWatcher(
                FakeClient({"QCOM": target_rows, "SOXX": reference_rows}),
                store,
                options=DipWatchOptions(
                    quantity=1,
                    lookback_bars=6,
                    stable_bars=2,
                    watch_low=Decimal("94"),
                    watch_high=Decimal("96"),
                    min_drawdown_pct=Decimal("4"),
                    rebound_pct=Decimal("2"),
                    reference_symbols=("SOXX",),
                    reference_rebound_pct=Decimal("1"),
                ),
            )

            first = watcher.scan_once("QCOM")
            second = watcher.scan_once("QCOM")
            restarted = DipWatcher(
                FakeClient({"QCOM": target_rows, "SOXX": reference_rows}),
                store,
                options=watcher.options,
            ).scan_once("QCOM")
            signals = store.list_market_signals()

        self.assertTrue(first["ready"])
        self.assertEqual(first["candidate"]["signal_type"], "BUY_CANDIDATE")
        self.assertTrue(first["candidate"]["approval_required"])
        self.assertEqual(first["candidate"]["tranche_index"], 1)
        self.assertEqual(first["candidate"]["tranche_count"], 1)
        self.assertIsNone(second["candidate"])
        self.assertIsNone(restarted["candidate"])
        self.assertEqual(restarted["tranche_reason"], "plan_completed")
        self.assertEqual(len(signals), 1)
        self.assertIsNone(signals[0]["order_id"])

    def test_new_lower_support_opens_next_tranche(self) -> None:
        first_rows = [
            row("100000", "100", "100", "99"),
            row("100100", "98", "98", "97"),
            row("100200", "95", "96", "95"),
            row("100300", "95.5", "96", "95.2"),
            row("100400", "96", "96.5", "95.4"),
            row("100500", "97", "97.2", "96"),
        ]
        second_rows = [
            row("101000", "97", "97", "96"),
            row("101100", "94", "94", "93"),
            row("101200", "91", "92", "91"),
            row("101300", "91.5", "92", "91.2"),
            row("101400", "92", "92.5", "91.4"),
            row("101500", "93", "93.2", "92"),
        ]
        reference_rows = [
            row("100000", "500", "501", "499"),
            row("100100", "495", "496", "494"),
            row("100200", "490", "491", "489"),
            row("100300", "491", "492", "490"),
            row("100400", "493", "494", "491"),
            row("100500", "495", "496", "493"),
        ]
        client = MutableFakeClient(
            {"QCOM": first_rows, "SOXX": reference_rows}
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SnapshotStore(Path(temp_dir) / "dip.db")
            options = DipWatchOptions(
                quantity=3,
                tranche_quantities=(1, 2),
                next_tranche_drop_pct=Decimal("3"),
                lookback_bars=6,
                stable_bars=2,
                watch_low=Decimal("90"),
                watch_high=Decimal("96"),
                min_drawdown_pct=Decimal("4"),
                rebound_pct=Decimal("2"),
                reference_symbols=("SOXX",),
                reference_rebound_pct=Decimal("1"),
            )
            first = DipWatcher(client, store, options=options).scan_once("QCOM")
            same_support = DipWatcher(client, store, options=options).scan_once(
                "QCOM"
            )
            client.rows_by_symbol["QCOM"] = second_rows
            second = DipWatcher(client, store, options=options).scan_once("QCOM")
            completed = DipWatcher(client, store, options=options).scan_once(
                "QCOM"
            )
            signals = store.list_market_signals()

        self.assertEqual(first["candidate"]["tranche_quantity"], 1)
        self.assertEqual(same_support["tranche_reason"], "same_trough")
        self.assertEqual(second["candidate"]["tranche_index"], 2)
        self.assertEqual(second["candidate"]["tranche_quantity"], 2)
        self.assertEqual(second["drop_from_previous_pct"], "4.2105")
        self.assertEqual(completed["tranche_reason"], "plan_completed")
        self.assertEqual(len(signals), 2)


class FakeClient:
    def __init__(self, rows_by_symbol):
        self.rows_by_symbol = rows_by_symbol

    def overseas_minute_candles(self, symbol, exchange, **kwargs):
        return {"summary": {}, "candles": self.rows_by_symbol[symbol]}


class MutableFakeClient(FakeClient):
    pass


def row(time_text: str, close: str, high: str, low: str) -> dict[str, str]:
    return {
        "tymd": "20260623",
        "xhms": time_text,
        "last": close,
        "high": high,
        "low": low,
        "evol": "100",
    }
