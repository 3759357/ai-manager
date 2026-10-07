import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from toss.client import TossClient, TossError, TossOutcomeUnknown
from toss.config import Settings


def settings(**kwargs) -> Settings:
    defaults = {
        "client_id": "client",
        "client_secret": "secret",
        "account_seq": 7,
        "base_url": "https://example.test",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class TossClientTest(unittest.TestCase):
    def test_token_is_cached_with_owner_only_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "token.json"
            session = FakeSession(
                token_responses=[
                    FakeResponse(
                        200,
                        {
                            "access_token": "token-one",
                            "token_type": "Bearer",
                            "expires_in": 86400,
                        },
                    )
                ]
            )
            client = TossClient(settings(), session=session, token_path=token_path)

            first = client.access_token()
            second = client.access_token()
            mode = os.stat(token_path).st_mode & 0o777

        self.assertEqual(first, "token-one")
        self.assertEqual(second, "token-one")
        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(mode, 0o600)

    def test_price_uses_symbols_query_and_bearer_token(self) -> None:
        session = FakeSession(
            token_responses=[token_response()],
            request_responses=[
                FakeResponse(
                    200,
                    {"result": [{"symbol": "AAPL", "lastPrice": "200"}]},
                )
            ],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TossClient(
                settings(),
                session=session,
                token_path=Path(temp_dir) / "token.json",
            )
            result = client.prices(["AAPL", "MSFT"])

        call = session.request_calls[0]
        self.assertEqual(result[0]["symbol"], "AAPL")
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].endswith("/api/v1/prices"))
        self.assertEqual(call["params"], {"symbols": "AAPL,MSFT"})
        self.assertEqual(call["headers"]["Authorization"], "Bearer token")
        self.assertNotIn("X-Tossinvest-Account", call["headers"])

    def test_account_api_uses_account_header(self) -> None:
        session = FakeSession(
            token_responses=[token_response()],
            request_responses=[
                FakeResponse(
                    200,
                    {"result": [{"marketCountry": "US", "commissionRate": "0.1"}]},
                )
            ],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TossClient(
                settings(),
                session=session,
                token_path=Path(temp_dir) / "token.json",
            )
            result = client.commissions()

        self.assertEqual(result[0]["commissionRate"], "0.1")
        self.assertEqual(
            session.request_calls[0]["headers"]["X-Tossinvest-Account"],
            "7",
        )

    def test_trades_uses_count_parameter(self) -> None:
        session = FakeSession(
            token_responses=[token_response()],
            request_responses=[FakeResponse(200, {"result": []})],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TossClient(
                settings(),
                session=session,
                token_path=Path(temp_dir) / "token.json",
            )
            client.trades("aapl", 25)

        self.assertEqual(
            session.request_calls[0]["params"],
            {"symbol": "AAPL", "count": 25},
        )

    def test_candles_uses_market_data_parameters(self) -> None:
        session = FakeSession(
            token_responses=[token_response()],
            request_responses=[
                FakeResponse(
                    200,
                    {"result": {"candles": [], "nextBefore": None}},
                )
            ],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TossClient(
                settings(),
                session=session,
                token_path=Path(temp_dir) / "token.json",
            )
            result = client.candles("aapl", interval="1m", count=50)

        self.assertEqual(result["candles"], [])
        self.assertEqual(
            session.request_calls[0]["params"],
            {
                "symbol": "AAPL",
                "interval": "1m",
                "count": 50,
                "adjusted": True,
            },
        )

    def test_create_order_uses_account_header_and_json_payload(self) -> None:
        session = FakeSession(
            token_responses=[token_response()],
            request_responses=[
                FakeResponse(
                    200,
                    {"result": {"orderId": "TOSS-1", "clientOrderId": "tp-1"}},
                )
            ],
        )
        payload = {
            "clientOrderId": "tp-1",
            "symbol": "AAPL",
            "side": "BUY",
            "orderType": "LIMIT",
            "quantity": "1",
            "price": "200.00",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TossClient(
                settings(),
                session=session,
                token_path=Path(temp_dir) / "token.json",
            )
            result = client.create_order(payload)

        call = session.request_calls[0]
        self.assertEqual(result["orderId"], "TOSS-1")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["json"], payload)
        self.assertEqual(call["headers"]["X-Tossinvest-Account"], "7")

    def test_rate_limit_retries_using_retry_after(self) -> None:
        session = FakeSession(
            token_responses=[token_response()],
            request_responses=[
                FakeResponse(429, {"error": {"message": "slow"}}, {"Retry-After": "0"}),
                FakeResponse(200, {"result": []}),
            ],
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "toss.client.time.sleep"
        ) as sleep:
            client = TossClient(
                settings(),
                session=session,
                token_path=Path(temp_dir) / "token.json",
            )
            result = client.accounts()

        self.assertEqual(result, [])
        self.assertEqual(len(session.request_calls), 2)
        sleep.assert_called_once_with(0.0)

    def test_error_exposes_code_and_request_id(self) -> None:
        session = FakeSession(
            token_responses=[token_response()],
            request_responses=[
                FakeResponse(
                    422,
                    {
                        "error": {
                            "code": "insufficient-buying-power",
                            "message": "부족",
                            "requestId": "req-1",
                        }
                    },
                )
            ],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = TossClient(
                settings(),
                session=session,
                token_path=Path(temp_dir) / "token.json",
            )
            with self.assertRaises(TossError) as context:
                client.buying_power("USD")

        self.assertEqual(context.exception.code, "insufficient-buying-power")
        self.assertEqual(context.exception.request_id, "req-1")


class OrderPostSafetyTest(unittest.TestCase):
    """주문 POST는 전송됐을 수 있는 상황에서 절대 재전송하지 않는다."""

    def _client(self, temp_dir, responses):
        session = FakeSession(token_responses=[token_response()], request_responses=responses)
        client = TossClient(settings(), session=session, token_path=Path(temp_dir) / "token.json")
        return client, session

    def _post(self, responses):
        with tempfile.TemporaryDirectory() as temp_dir, patch("toss.client.time.sleep"):
            client, session = self._client(temp_dir, responses)
            try:
                client.create_order({"clientOrderId": "tp-1"})
                error = None
            except TossError as exc:
                error = exc
        return error, session

    def test_read_timeout_is_unknown_and_sent_once(self) -> None:
        error, session = self._post([requests.ReadTimeout("late"), FakeResponse(200, {"result": {"orderId": "X"}})])
        self.assertIsInstance(error, TossOutcomeUnknown)
        self.assertEqual(len(session.request_calls), 1)

    def test_connection_reset_is_unknown_and_sent_once(self) -> None:
        error, session = self._post([requests.ConnectionError("reset"), FakeResponse(200, {"result": {}})])
        self.assertIsInstance(error, TossOutcomeUnknown)
        self.assertEqual(len(session.request_calls), 1)

    def test_server_error_is_unknown_and_sent_once(self) -> None:
        error, session = self._post([FakeResponse(503, {}), FakeResponse(200, {"result": {}})])
        self.assertIsInstance(error, TossOutcomeUnknown)
        self.assertEqual(error.status_code, 503)
        self.assertEqual(len(session.request_calls), 1)

    def test_request_in_progress_conflict_is_unknown(self) -> None:
        error, session = self._post([FakeResponse(409, {"error": {"code": "request-in-progress", "message": "처리 중"}})])
        self.assertIsInstance(error, TossOutcomeUnknown)
        self.assertEqual(len(session.request_calls), 1)

    def test_connect_timeout_was_never_sent_so_it_retries(self) -> None:
        error, session = self._post([requests.ConnectTimeout("no route"), FakeResponse(200, {"result": {"orderId": "X"}})])
        self.assertIsNone(error)
        self.assertEqual(len(session.request_calls), 2)

    def test_rate_limited_post_retries(self) -> None:
        error, session = self._post([FakeResponse(429, {"error": {}}, {"Retry-After": "0"}), FakeResponse(200, {"result": {"orderId": "X"}})])
        self.assertIsNone(error)
        self.assertEqual(len(session.request_calls), 2)

    def test_read_requests_still_retry_server_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch("toss.client.time.sleep"):
            client, session = self._client(temp_dir, [FakeResponse(503, {}), FakeResponse(200, {"result": []})])
            self.assertEqual(client.accounts(), [])
        self.assertEqual(len(session.request_calls), 2)


class FakeSession:
    def __init__(
        self,
        *,
        token_responses: list["FakeResponse"],
        request_responses: list["FakeResponse"] | None = None,
    ) -> None:
        self.token_responses = list(token_responses)
        self.request_responses = list(request_responses or [])
        self.post_calls: list[dict[str, object]] = []
        self.request_calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return self.token_responses.pop(0)

    def request(self, method: str, url: str, **kwargs):
        self.request_calls.append({"method": method, "url": url, **kwargs})
        item = self.request_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: dict,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


def token_response() -> FakeResponse:
    return FakeResponse(
        200,
        {
            "access_token": "token",
            "token_type": "Bearer",
            "expires_in": 86400,
        },
    )


if __name__ == "__main__":
    unittest.main()
