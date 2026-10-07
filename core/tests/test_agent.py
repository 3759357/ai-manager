import json
import tempfile
import unittest
from pathlib import Path

from core.agent import AgentError, SpoolAgent


class SpoolAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.spool = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _agent(self, **overrides) -> SpoolAgent:
        options = {"spool_dir": self.spool, "timeout_seconds": 5.0}
        options.update(overrides)
        return SpoolAgent(**options)

    def test_writes_request_and_reads_response(self) -> None:
        answered: list[str] = []

        def fake_sleep(_seconds: float) -> None:
            """에이전트 컨테이너가 응답을 쓰는 순간을 흉내낸다."""
            for request in (self.spool / "requests").glob("*.json"):
                call_id = request.stem
                if call_id in answered:
                    continue
                answered.append(call_id)
                (self.spool / "responses" / f"{call_id}.json").write_text(
                    json.dumps({"decision": "REJECT"}), encoding="utf-8"
                )

        agent = self._agent(sleep=fake_sleep)
        raw = agent.review({"symbol": "SOXX"})

        self.assertEqual(json.loads(raw)["decision"], "REJECT")
        written = json.loads(
            next((self.spool / "requests").glob("*.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(written["symbol"], "SOXX")
        self.assertEqual(written["_role"], "reviewer")

    def test_error_file_raises_agent_error(self) -> None:
        def fake_sleep(_seconds: float) -> None:
            for request in (self.spool / "requests").glob("*.json"):
                (self.spool / "responses" / f"{request.stem}.error").write_text(
                    "인증 만료", encoding="utf-8"
                )

        with self.assertRaises(AgentError) as ctx:
            self._agent(sleep=fake_sleep).review({"symbol": "SOXX"})
        self.assertIn("인증 만료", str(ctx.exception))

    def test_timeout_raises_agent_error(self) -> None:
        ticks = iter([0.0, 1.0, 2.0, 99.0])

        agent = self._agent(
            timeout_seconds=3.0,
            sleep=lambda _seconds: None,
            now=lambda: next(ticks),
        )
        with self.assertRaises(AgentError) as ctx:
            agent.review({"symbol": "SOXX"})
        self.assertIn("시간 초과", str(ctx.exception))

    def test_request_is_moved_into_place_atomically(self) -> None:
        """부분 기록된 파일을 에이전트가 읽지 않아야 한다."""
        seen: list[list[str]] = []

        def fake_sleep(_seconds: float) -> None:
            seen.append(
                [path.name for path in (self.spool / "requests").iterdir()]
            )
            for request in (self.spool / "requests").glob("*.json"):
                (self.spool / "responses" / f"{request.stem}.json").write_text(
                    "{}", encoding="utf-8"
                )

        self._agent(sleep=fake_sleep).review({"symbol": "SOXX"})

        for names in seen:
            self.assertFalse([name for name in names if name.startswith(".")])


if __name__ == "__main__":
    unittest.main()
