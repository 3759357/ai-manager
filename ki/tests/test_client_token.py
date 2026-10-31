import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ki.client import KISClient, KISError
from ki.config import Settings
from test_client import FakeResponse

EXPIRED = {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}


def settings() -> Settings:
    return Settings(environment="real", app_key="key", app_secret="secret", account_no="12345678")


class ScriptedSession:
    """요청마다 정해 둔 응답을 차례로 돌려준다."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.token_issues = 0

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/oauth2/tokenP"):
            self.token_issues += 1
            return FakeResponse({"access_token": f"new-{self.token_issues}", "expires_in": 86400})
        if url.endswith("/uapi/hashkey"):
            return FakeResponse({"HASH": "hash"})
        raise AssertionError(url)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def api_calls(self):
        return [call for call in self.calls if "/oauth2/" not in call[1] and "/hashkey" not in call[1]]


def seed_cached_token(client: KISClient, token: str = "old") -> None:
    client._write_cached_token(token, time.time() + 3600)


class TokenExpiryTest(unittest.TestCase):
    def _expiry_in(self, tz: str) -> float:
        previous = os.environ.get("TZ")
        os.environ["TZ"] = tz
        time.tzset()
        try:
            return KISClient._expiry_timestamp({"access_token_token_expired": "2026-10-31 09:00:00"})
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

    def test_kst_expiry_string_is_parsed_as_kst_on_any_server_timezone(self) -> None:
        expected = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self._expiry_in("UTC"), expected)
        self.assertEqual(self._expiry_in("Asia/Seoul"), expected)
        self.assertEqual(self._expiry_in("America/New_York"), expected)

    def test_cache_file_is_private_from_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings(), session=ScriptedSession([]), token_dir=Path(temp_dir))
            seed_cached_token(client, "abc")
            mode = os.stat(client._token_path).st_mode & 0o777
            leftovers = [p.name for p in Path(temp_dir).iterdir() if p.name.endswith(".tmp")]
            cached = json.loads(client._token_path.read_text(encoding="utf-8"))

        self.assertEqual(mode, 0o600)
        self.assertEqual(leftovers, [])
        self.assertEqual(cached["access_token"], "abc")


class TokenRejectedTest(unittest.TestCase):
    def test_query_reissues_token_once_and_retries(self) -> None:
        session = ScriptedSession([
            FakeResponse(EXPIRED),
            FakeResponse({"rt_cd": "0", "output": {"last": "200.00"}}),
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings(), session=session, token_dir=Path(temp_dir))
            seed_cached_token(client)
            quote = client.overseas_quote("AAPL", "NAS")
            cached = json.loads(client._token_path.read_text(encoding="utf-8"))

        calls = session.api_calls()
        self.assertEqual(quote["last"], "200.00")
        self.assertEqual(session.token_issues, 1)
        self.assertEqual(calls[0][2]["headers"]["authorization"], "Bearer old")
        self.assertEqual(calls[1][2]["headers"]["authorization"], "Bearer new-1")
        self.assertEqual(cached["access_token"], "new-1")

    def test_http_500_token_error_is_recognized(self) -> None:
        session = ScriptedSession([
            FakeResponse(EXPIRED, status_code=500),
            FakeResponse({"rt_cd": "0", "output": {"last": "200.00"}}),
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings(), session=session, token_dir=Path(temp_dir))
            seed_cached_token(client)
            quote = client.overseas_quote("AAPL", "NAS")

        self.assertEqual(quote["last"], "200.00")
        self.assertEqual(len(session.api_calls()), 2)

    def test_retry_happens_only_once(self) -> None:
        session = ScriptedSession([FakeResponse(EXPIRED), FakeResponse(EXPIRED)])
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings(), session=session, token_dir=Path(temp_dir))
            seed_cached_token(client)
            with self.assertRaises(KISError):
                client.overseas_quote("AAPL", "NAS")

        self.assertEqual(len(session.api_calls()), 2)

    def test_order_is_not_resent_but_reported_as_not_processed(self) -> None:
        session = ScriptedSession([FakeResponse(EXPIRED, status_code=500)])
        with tempfile.TemporaryDirectory() as temp_dir:
            client = KISClient(settings(), session=session, token_dir=Path(temp_dir))
            seed_cached_token(client)
            with self.assertRaises(KISError) as caught:
                client.overseas_order("buy", "AAPL", 1, "250.00")
            cache_exists = client._token_path.exists()

        # 결과 불명(KISOutcomeUnknown)이 아니라 미처리 오류이고, 주문 API는 한 번만 호출됐다.
        self.assertIs(type(caught.exception), KISError)
        self.assertEqual(len(session.api_calls()), 1)
        self.assertFalse(cache_exists)


if __name__ == "__main__":
    unittest.main()
