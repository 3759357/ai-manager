"""코드 리뷰에서 찾은 결함의 회귀 테스트(core)."""
import dataclasses
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from ki.storage import SnapshotStore

from core import guard
from core.manager_cli import RequestStore
from core.notify_worker import NotificationWorker
from core.outbox import OutboxStore
from core.quota import QuotaExhausted, QuotaLedger, QuotaLimits
from core.relay import OutboxRelay
from core.strategy import StrategyStore
from core.strategy_worker import StrategyWorker
from core.theses import ThesisStore

from test_guard import NOW, POLICY, account, buy, codes, quote
from test_strategy_worker import FakeAgent, _approval, _candidate


class ThesisRetryTest(unittest.TestCase):
    def test_failed_ai_draft_can_be_requested_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            theses = ThesisStore(RequestStore(root / "manager"))
            spool = root / "spool"
            card = theses.add("AMD", "AI 가속기 점유율이 오른다")
            first = theses.request_ai_draft(card["id"], spool)
            # mind 루프가 요청을 처리하다 실패했다.
            (spool / "requests" / f"{first['call_id']}.json").unlink()
            (spool / "responses" / f"{first['call_id']}.error").write_text("timeout")
            theses.ingest_ai_drafts(spool)

            second = theses.request_ai_draft(card["id"], spool)
            again = theses.request_ai_draft(card["id"], spool)
            (spool / "requests" / f"{second['call_id']}.json").unlink()
            (spool / "responses" / f"{second['call_id']}.json").write_text(json.dumps({
                "thesis": "데이터센터 GPU 점유율 확대",
                "checks": [], "counterThesis": [], "invalidationAuto": [], "invalidationAlertOnly": [],
            }))
            ingested = theses.ingest_ai_drafts(spool)
            draft = theses.get(card["id"], with_events=False)["draft_card"]

        self.assertTrue(second["requested"])
        self.assertEqual(second["call_id"], f"{first['call_id']}-r2")
        self.assertFalse(again["requested"])  # 처리 중인 재요청을 또 만들지 않는다
        self.assertEqual(ingested, [{"id": card["id"], "result": "drafted"}])
        self.assertEqual(draft["thesis"], "데이터센터 GPU 점유율 확대")


class StrategyWorkerResilienceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = OutboxStore(root / "core.db")
        self.strategy_store = StrategyStore(root / "core.db")
        self.quota = QuotaLedger(root / "core.db", QuotaLimits(
            max_calls_per_window=10, max_calls_per_day=20, min_interval_seconds=0, reserve_calls=1))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def worker(self, agent, quote_provider=lambda symbol: D("100.00")):
        return StrategyWorker(self.store, self.strategy_store, agent, self.quota,
                              quote_provider=quote_provider)

    def system_events(self, event_type):
        return [e for e in self.store.list_events(limit=50, category="SYSTEM") if e["event_type"] == event_type]

    def test_quote_failure_skips_one_candidate_without_stopping_the_worker(self) -> None:
        self.store.append(_candidate("ki:cand:1", symbol="DELISTED"))
        self.store.append(_candidate("ki:cand:2", symbol="SOXX"))

        def quotes(symbol):
            if symbol == "DELISTED":
                raise ValueError("시세 없음")
            return D("100.00")

        result = self.worker(FakeAgent(_approval()), quotes).run_once()

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["approved"], 1)
        self.assertEqual(len(self.system_events("QUOTE_UNAVAILABLE")), 1)
        self.assertEqual(self.strategy_store.reject_stats().get("QUOTE_UNAVAILABLE"), 1)

    def test_quota_warnings_are_not_repeated_every_cycle(self) -> None:
        self.store.append(_candidate("ki:cand:1"))
        worker = self.worker(FakeAgent())
        with mock.patch.object(self.quota, "check", side_effect=QuotaExhausted("TOO_SOON", "간격")):
            worker.run_once()
        with mock.patch.object(self.quota, "check", side_effect=QuotaExhausted("WINDOW_EXHAUSTED", "소진")):
            worker.run_once()
            worker.run_once()
            worker.run_once()

        self.assertEqual(len(self.system_events("QUOTA_EXHAUSTED")), 1)

    def test_run_forever_survives_a_failing_cycle(self) -> None:
        worker = self.worker(FakeAgent())
        with mock.patch.object(worker, "run_once", side_effect=[RuntimeError("database is locked"), {}]), \
                self.assertLogs("core.strategy_worker", level="ERROR"):
            worker.run_forever(interval_seconds=0, sleep=lambda _: None, max_cycles=2)
        self.assertEqual(len(self.system_events("WORKER_ERROR")), 1)


class DipWatchToStrategyPipelineTest(unittest.TestCase):
    def test_dip_watch_candidates_reach_the_strategy_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            SnapshotStore(root / "ki.db").save_market_signal(
                symbol="SOXX",
                signal_type="BUY_CANDIDATE",
                severity="MEDIUM",
                message="SOXX BUY_CANDIDATE: tranche=1/3",
                payload={"tranche_index": 1, "tranche_quantity": 1, "suggested_limit_price": "97.00",
                         "plan_id": "p1", "target": {"drawdown_pct": "-8.4210", "rebound_pct": "1.6"}},
            )
            outbox = OutboxStore(root / "core.db")
            relay = OutboxRelay(outbox, kis_database_path=root / "ki.db", toss_database_path=root / "toss.db")
            first = relay.sync()["ki:market_signals"]["relayed"]
            second = relay.sync()["ki:market_signals"]["relayed"]
            event = outbox.list_events(limit=5, category="SIGNAL")[0]

            quota = QuotaLedger(root / "core.db", QuotaLimits(min_interval_seconds=0))
            agent = FakeAgent(_approval())
            result = StrategyWorker(outbox, StrategyStore(root / "core.db"), agent, quota,
                                    quote_provider=lambda symbol: D("100.00")).run_once()

        self.assertEqual((first, second), (1, 0))
        self.assertEqual(event["event_type"], "BUY_CANDIDATE")
        self.assertEqual(event["payload"]["entries"], [{"price": "97.00", "quantity": 1}])
        self.assertEqual(event["payload"]["drop_bucket"], "-8%")
        self.assertEqual(result["approved"], 1)
        self.assertEqual(agent.calls[0]["lowest_entry_price"], "97.00")


class OutboxRaceTest(unittest.TestCase):
    def test_concurrent_appends_of_one_event_never_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "core.db"
            OutboxStore(path).append(_candidate("ki:cand:race"))  # 스키마 준비
            errors, results = [], []
            barrier = threading.Barrier(8)

            def append(index):
                event = dataclasses.replace(_candidate("ki:cand:shared"), summary=f"n{index}")
                try:
                    barrier.wait()
                    results.append(OutboxStore(path).append(event)["created"])
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=append, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(results.count(True), 1)

    def test_insert_conflict_returns_the_existing_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OutboxStore(Path(temp_dir) / "core.db")
            first = store.append(_candidate("ki:cand:1"))
            real_connect = store._connect

            class SkipFirstSelect:
                """SELECT가 아직 행을 못 본 상황(다른 프로세스가 사이에 INSERT)을 흉내 낸다."""

                def __init__(self, connection):
                    self.connection, self.skipped = connection, False

                def execute(self, sql, *args):
                    if not self.skipped and sql.lstrip().startswith("SELECT id FROM event_outbox"):
                        self.skipped = True
                        return self.connection.execute("SELECT NULL WHERE 0")
                    return self.connection.execute(sql, *args)

                def __enter__(self):
                    self.connection.__enter__()
                    return self

                def __exit__(self, *exc):
                    return self.connection.__exit__(*exc)

            with mock.patch.object(store, "_connect", lambda: SkipFirstSelect(real_connect())):
                second = store.append(_candidate("ki:cand:1"))

        self.assertEqual(second, {"event_id": first["event_id"], "created": False})


class GuardFailClosedTest(unittest.TestCase):
    def test_buy_without_limit_price_is_blocked_even_if_policy_allows_market_orders(self) -> None:
        loose = dataclasses.replace(POLICY, order={**POLICY.order, "limit_only": False})
        decision = guard.evaluate(buy(limit_price=None, quantity=D("100000")), account(), quote(), loose, NOW)
        self.assertIn("price_unknown", codes(decision))

    def test_future_dated_quote_is_not_fresh(self) -> None:
        future = dataclasses.replace(quote(), as_of=NOW + timedelta(minutes=10))
        self.assertIn("future_quote", codes(guard.evaluate(buy(), account(), future, POLICY, NOW)))
        slightly = dataclasses.replace(quote(), as_of=NOW + timedelta(seconds=2))
        self.assertNotIn("future_quote", codes(guard.evaluate(buy(), account(), slightly, POLICY, NOW)))


class NotificationWorkerResilienceTest(unittest.TestCase):
    def test_run_forever_survives_a_failing_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OutboxStore(Path(temp_dir) / "core.db")
            worker = NotificationWorker(store, {"console": lambda item: None})
            with mock.patch.object(worker, "run_once", side_effect=[RuntimeError("locked"), {}]), \
                    self.assertLogs("core.notify_worker", level="ERROR"):
                worker.run_forever(interval_seconds=0, sleep=lambda _: None, max_cycles=2)
            types = [e["event_type"] for e in store.list_events(limit=5, category="SYSTEM")]
        self.assertEqual(types, ["WORKER_ERROR"])


if __name__ == "__main__":
    unittest.main()
