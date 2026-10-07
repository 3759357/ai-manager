import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.cli import _parser, _quota_ledger, _selected_brokers, _strategy_worker
from core.config import Settings
from core.events import OutboxEvent
from core.models import NormalizedQuote
from core.outbox import OutboxStore


class FakeService:
    """브로커 호출 없이 기준 현재가만 돌려준다."""

    def __init__(self, price: str = "100.00") -> None:
        self.price = Decimal(price)
        self.asked: list[tuple[str, str]] = []

    def quotes(self, symbol: str, broker: str) -> list[NormalizedQuote]:
        self.asked.append((symbol, broker))
        return [
            NormalizedQuote(
                broker=broker,
                symbol=symbol,
                price=self.price,
                currency="USD",
                timestamp=None,
                raw={},
            )
        ]


class StrategyCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        prompt = root / "reviewer.md"
        prompt.write_text("심사 프롬프트", encoding="utf-8")
        self.settings = Settings(
            core_database_path=root / "core.db",
            kill_switch_path=root / "kill-switch.json",
            strategy_spool_path=root / "spool",
            strategy_prompt_path=prompt,
            strategy_min_interval_seconds=0,
        )
        self.settings.validate()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parser_accepts_strategy_commands(self) -> None:
        parser = _parser()
        args = parser.parse_args(["strategy-worker", "--interval", "30"])
        self.assertEqual(args.interval, 30.0)
        self.assertEqual(args.batch_size, 10)
        self.assertEqual(parser.parse_args(["strategy-stats"]).limit, 20)

    def test_worker_uses_default_broker_when_not_given(self) -> None:
        args = _parser().parse_args(["strategy-worker"])
        self.assertEqual(
            _selected_brokers(args, self.settings), {self.settings.default_broker}
        )

    def test_explicit_broker_overrides_default(self) -> None:
        args = _parser().parse_args(["strategy-worker", "--broker", "kis"])
        self.assertEqual(_selected_brokers(args, self.settings), {"kis"})

    def test_quote_provider_asks_the_broker_directly(self) -> None:
        service = FakeService("123.45")
        worker = _strategy_worker(
            self.settings, service, "toss", batch_size=5
        )

        price = worker.quote_provider("SOXX")

        self.assertEqual(price, Decimal("123.45"))
        self.assertEqual(service.asked, [("SOXX", "toss")])

    def test_prompt_text_and_version_are_loaded(self) -> None:
        worker = _strategy_worker(
            self.settings, FakeService(), "toss", batch_size=5
        )
        self.assertEqual(worker.prompt_text, "심사 프롬프트")
        self.assertEqual(worker.prompt_version, "reviewer-v1")
        self.assertTrue(worker.prompt_digest)

    def test_missing_prompt_file_does_not_crash(self) -> None:
        self.settings = Settings(
            core_database_path=self.settings.core_database_path,
            kill_switch_path=self.settings.kill_switch_path,
            strategy_spool_path=self.settings.strategy_spool_path,
            strategy_prompt_path=Path("/없는/경로/reviewer.md"),
        )
        worker = _strategy_worker(
            self.settings, FakeService(), "toss", batch_size=5
        )
        self.assertEqual(worker.prompt_text, "")

    def test_run_once_writes_request_into_the_spool(self) -> None:
        store = OutboxStore(self.settings.core_database_path)
        store.append(
            OutboxEvent(
                dedup_key="ki:cand:1",
                source="ki",
                category="SIGNAL",
                event_type="BUY_CANDIDATE",
                broker="kis",
                symbol="SOXX",
                summary="SOXX 낙폭 후 분할매수 후보",
                occurred_at="2026-08-29T12:00:00+00:00",
                payload={
                    "symbol": "SOXX",
                    "entries": [{"price": "97.00", "quantity": 1}],
                },
            )
        )
        worker = _strategy_worker(
            self.settings, FakeService(), "toss", batch_size=5
        )
        # 에이전트 컨테이너가 응답을 쓰는 순간을 흉내낸다.
        spool = self.settings.strategy_spool_path

        def fake_sleep(_seconds: float) -> None:
            for request in (spool / "requests").glob("*.json"):
                (spool / "responses" / f"{request.stem}.json").write_text(
                    json.dumps(
                        {
                            "decision": "APPROVE",
                            "confidence": 0.8,
                            "reasons": ["저점 안정"],
                            "invalidation": {
                                "type": "PRICE_BELOW",
                                "price": "94.00",
                            },
                            "expires_in_minutes": 120,
                        }
                    ),
                    encoding="utf-8",
                )

        worker.agent.sleep = fake_sleep
        result = worker.run_once()

        self.assertEqual(result["approved"], 1)
        written = json.loads(
            next((spool / "requests").glob("*.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(written["reference_price"], "100.00")
        self.assertEqual(written["lowest_entry_price"], "97.00")

    def test_quota_limits_come_from_settings(self) -> None:
        ledger = _quota_ledger(self.settings)
        self.assertEqual(
            ledger.limits.max_calls_per_window,
            self.settings.strategy_max_calls_per_window,
        )
        self.assertEqual(ledger.limits.min_interval_seconds, 0)


if __name__ == "__main__":
    unittest.main()
