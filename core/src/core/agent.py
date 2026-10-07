from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class AgentError(Exception):
    """에이전트 호출 자체가 실패했다. 응답 내용 문제와 구분한다."""


class StrategyAgent(Protocol):
    """LLM 게이트웨이 인터페이스.

    브로커 게이트웨이와 같은 취급을 한다. 비결정적이고, 느리고, 죽는다.
    단위 테스트는 이 인터페이스를 가짜로 갈아끼워 실호출 없이 돌린다.
    """

    role: str
    cli: str
    model: str

    def review(self, request: dict[str, Any]) -> str:
        """요청을 보내고 원문 응답을 돌려준다. 파싱은 호출자가 한다."""
        ...


@dataclass
class SpoolAgent:
    """파일 스풀 기반 게이트웨이.

    워커가 요청 파일을 쓰고 에이전트 컨테이너가 응답 파일을 쓴다.
    둘 사이에 네트워크도, docker.sock도 필요 없다.
    core 컨테이너에 도커 권한을 주지 않으려는 것이 이 방식의 이유다.

    디렉터리는 에이전트 컨테이너와 공유 볼륨으로 물린다.
      requests/  : 워커가 쓰고 에이전트가 읽는다
      responses/ : 에이전트가 쓰고 워커가 읽는다
    """

    spool_dir: Path
    role: str = "reviewer"
    cli: str = "codex"
    model: str = "gpt-5-codex"
    timeout_seconds: float = 120.0
    poll_interval: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic

    @property
    def requests_dir(self) -> Path:
        return self.spool_dir / "requests"

    @property
    def responses_dir(self) -> Path:
        return self.spool_dir / "responses"

    def review(self, request: dict[str, Any]) -> str:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)

        call_id = f"{self.role}-{uuid.uuid4().hex[:12]}"
        payload = dict(request)
        payload["_call_id"] = call_id
        payload["_role"] = self.role

        # 부분 기록을 에이전트가 읽지 않도록 임시 파일에 쓰고 원자적으로 옮긴다.
        target = self.requests_dir / f"{call_id}.json"
        staging = self.requests_dir / f".{call_id}.tmp"
        staging.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            encoding="utf-8",
        )
        staging.rename(target)

        response_path = self.responses_dir / f"{call_id}.json"
        error_path = self.responses_dir / f"{call_id}.error"
        deadline = self.now() + self.timeout_seconds

        while self.now() < deadline:
            if error_path.exists():
                message = error_path.read_text(encoding="utf-8")[:500]
                raise AgentError(f"에이전트 오류: {message}")
            if response_path.exists():
                return response_path.read_text(encoding="utf-8")
            self.sleep(self.poll_interval)

        raise AgentError(
            f"에이전트 응답 시간 초과: {self.timeout_seconds}s ({call_id})"
        )
