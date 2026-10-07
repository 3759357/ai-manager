from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class NotificationError(RuntimeError):
    pass


Poster = Callable[[str, bytes, dict[str, str], float], tuple[int, str]]


@dataclass(frozen=True)
class DiscordMessage:
    content: str
    username: str = "ai-manager"

    def payload(self) -> dict[str, Any]:
        content = self.content.strip()
        if not content:
            raise ValueError("디스코드 메시지가 비어 있습니다.")
        if len(content) > 1900:
            raise ValueError("디스코드 메시지는 1900자 이하로 제한합니다.")
        return {
            "content": content,
            "username": self.username,
            "allowed_mentions": {"parse": []},
        }


class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str,
        *,
        username: str = "ai-manager",
        timeout_seconds: float = 10.0,
        poster: Poster | None = None,
        max_retries: int = 3,
        max_retry_delay_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.webhook_url = webhook_url.strip()
        self.username = username
        self.timeout_seconds = timeout_seconds
        self.poster = poster or _urllib_post
        self.max_retries = max_retries
        self.max_retry_delay_seconds = max_retry_delay_seconds
        self.sleep = sleep

    def send(self, content: str, *, dry_run: bool = False) -> dict[str, Any]:
        message = DiscordMessage(content=content, username=self.username)
        payload = message.payload()
        if dry_run:
            return {
                "mode": "dry-run",
                "channel": "discord",
                "payload": payload,
            }
        if not self.webhook_url:
            raise NotificationError("CORE_DISCORD_WEBHOOK_URL이 필요합니다.")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, body = 0, ""
        for attempt in range(self.max_retries + 1):
            status, body = self.poster(
                self.webhook_url,
                data,
                {"Content-Type": "application/json"},
                self.timeout_seconds,
            )
            if 200 <= status < 300:
                return {
                    "mode": "live",
                    "channel": "discord",
                    "status": status,
                    "attempts": attempt + 1,
                    "payload": payload,
                }
            retryable = status == 429 or status >= 500
            if not retryable or attempt >= self.max_retries:
                break
            self.sleep(self._retry_delay(status, body, attempt))
        raise NotificationError(
            f"디스코드 웹훅 전송 실패: HTTP {status} {body[:200]}"
        )

    def _retry_delay(self, status: int, body: str, attempt: int) -> float:
        """429는 Discord가 알려주는 retry_after를, 그 외에는 지수 백오프를 쓴다."""
        delay = float(2**attempt)
        if status == 429:
            try:
                retry_after = float(json.loads(body).get("retry_after", delay))
                delay = max(retry_after, 0.0)
            except (TypeError, ValueError):
                pass
        return min(delay, self.max_retry_delay_seconds)


def _urllib_post(
    url: str,
    data: bytes,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body
    except urllib.error.URLError as exc:
        raise NotificationError(f"디스코드 웹훅 연결 실패: {exc}") from exc
