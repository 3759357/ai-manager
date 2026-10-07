import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.agent import AgentError
from core.events import OutboxEvent
from core.killswitch import KillSwitch
from core.outbox import OutboxStore
from core.quota import QuotaLedger, QuotaLimits
from core.strategy import StrategyStore
from core.strategy_worker import StrategyWorker


class FakeAgent:
    """실호출을 하지 않는 가짜 게이트웨이.

    단위 테스트에서 실제 CLI를 부르면 예산이 녹고 결과가 매번 달라진다.
    """

    role = "reviewer"
    cli = "fake"
    model = "fake-1"

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def review(self, request: dict) -> str:
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("예상보다 많이 호출되었습니다.")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item, ensure_ascii=False)


def _approval(price: str = "94.00", confidence: float = 0.8) -> dict:
    return {
        "decision": "APPROVE",
        "confidence": confidence,
        "reasons": ["저점 안정 확인"],
        "invalidation": {"type": "PRICE_BELOW", "price": price},
        "expires_in_minutes": 120,
    }


def _candidate(dedup_key: str, symbol: str = "SOXX") -> OutboxEvent:
    return OutboxEvent(
        dedup_key=dedup_key,
        source="ki",
        category="SIGNAL",
        event_type="BUY_CANDIDATE",
        broker="kis",
        symbol=symbol,
        summary=f"{symbol} 낙폭 후 분할매수 후보",
        occurred_at="2026-08-29T12:00:00+00:00",
        payload={
            "symbol": symbol,
            "stage": 1,
            "drop_bucket": "-8%",
            "entries": [{"price": "97.00", "quantity": 1}],
        },
    )


class StrategyWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = OutboxStore(root / "core.db")
        self.strategy_store = StrategyStore(root / "core.db")
        self.quota = QuotaLedger(
            root / "core.db",
            QuotaLimits(
                max_calls_per_window=10,
                max_calls_per_day=20,
                min_interval_seconds=0,
                reserve_calls=1,
            ),
        )
        self.kill_switch = KillSwitch(root / "kill-switch.json")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _worker(self, agent, **overrides) -> StrategyWorker:
        options = {
            "quote_provider": lambda symbol: Decimal("100.00"),
            "kill_switch": self.kill_switch,
            "prompt_text": "심사 프롬프트",
            "cache_ttl_minutes": 30,
        }
        options.update(overrides)
        return StrategyWorker(
            self.store, self.strategy_store, agent, self.quota, **options
        )

    def test_approves_valid_candidate(self) -> None:
        self.store.append(_candidate("ki:cand:1"))
        agent = FakeAgent(_approval())

        result = self._worker(agent).run_once()

        self.assertEqual(result["approved"], 1)
        self.assertEqual(len(agent.calls), 1)
        self.assertEqual(agent.calls[0]["reference_price"], "100.00")
        types = [item["event_type"] for item in self.store.list_events(limit=10)]
        self.assertIn("REVIEW_APPROVED", types)

    def test_rejects_invalidation_above_entry(self) -> None:
        self.store.append(_candidate("ki:cand:1"))
        agent = FakeAgent(_approval(price="99.00"))

        result = self._worker(agent).run_once()

        self.assertEqual(result["rejected"], 1)
        self.assertEqual(
            self.strategy_store.reject_stats().get("INVALIDATION_ABOVE_ENTRY"), 1
        )

    def test_rejects_unparseable_response(self) -> None:
        self.store.append(_candidate("ki:cand:1"))

        class BrokenAgent(FakeAgent):
            def review(self, request: dict) -> str:
                return "네, 이 종목은 좋아 보입니다."

        result = self._worker(BrokenAgent()).run_once()

        self.assertEqual(result["rejected"], 1)
        self.assertEqual(self.strategy_store.reject_stats().get("SCHEMA_INVALID"), 1)

    def test_kill_switch_blocks_before_any_call(self) -> None:
        self.store.append(_candidate("ki:cand:1"))
        self.kill_switch.activate("점검 중")
        agent = FakeAgent()

        result = self._worker(agent).run_once()

        self.assertIn("halted", result)
        self.assertEqual(agent.calls, [])
        self.assertEqual(self.store.get_cursor("strategy_worker"), 0)

    def test_cursor_prevents_double_review(self) -> None:
        self.store.append(_candidate("ki:cand:1"))
        worker = self._worker(FakeAgent(_approval()))
        worker.run_once()

        second = self._worker(FakeAgent())
        result = second.run_once()

        self.assertEqual(result["reviewed"], 0)

    def test_same_signature_uses_cache(self) -> None:
        self.store.append(_candidate("ki:cand:1"))
        self._worker(FakeAgent(_approval())).run_once()
        self.store.append(_candidate("ki:cand:2"))

        agent = FakeAgent()
        result = self._worker(agent).run_once()

        self.assertEqual(result["cached"], 1)
        self.assertEqual(agent.calls, [])

    def test_agent_failure_is_recorded_not_raised(self) -> None:
        self.store.append(_candidate("ki:cand:1"))
        agent = FakeAgent(AgentError("응답 시간 초과"))

        result = self._worker(agent).run_once()

        self.assertEqual(result["errors"], 1)
        types = [item["event_type"] for item in self.store.list_events(limit=10)]
        self.assertIn("AGENT_ERROR", types)

    def test_quota_exhaustion_stops_without_advancing_cursor(self) -> None:
        self.store.append(_candidate("ki:cand:1"))
        tight = QuotaLedger(
            Path(self._tmp.name) / "core.db",
            QuotaLimits(
                max_calls_per_window=2,
                max_calls_per_day=2,
                min_interval_seconds=0,
                reserve_calls=1,
            ),
        )
        tight.record(role="reviewer", cli="fake", outcome="OK")
        agent = FakeAgent()
        worker = StrategyWorker(
            self.store,
            self.strategy_store,
            agent,
            tight,
            quote_provider=lambda symbol: Decimal("100.00"),
            kill_switch=self.kill_switch,
        )

        result = worker.run_once()

        self.assertEqual(result["quota_blocked"], "WINDOW_EXHAUSTED")
        self.assertEqual(agent.calls, [])
        self.assertEqual(self.store.get_cursor("strategy_worker"), 0)


if __name__ == "__main__":
    unittest.main()
