from __future__ import annotations

import fcntl
import json
import os
import random
import time
from pathlib import Path
from typing import Any

import requests

from toss.config import Settings


class TossError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class TossOutcomeUnknown(TossError):
    """주문 요청이 토스에 도달했는지 알 수 없다. 재전송하지 말고 조회로 확인한다."""


# 서버가 요청을 처리하지 않았음이 확실한 경우에만 쓰기 요청을 다시 보낸다.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class TossClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        token_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.token_path = token_path or (
            Path.home() / ".cache" / "toss" / "token.json"
        )
        self._access_token: str | None = None
        self._expires_at = 0.0

    def accounts(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/accounts").get("result", [])

    def prices(self, symbols: list[str] | str) -> list[dict[str, Any]]:
        value = symbols if isinstance(symbols, str) else ",".join(symbols)
        if not value:
            raise ValueError("조회할 종목이 필요합니다.")
        return self._get("/api/v1/prices", params={"symbols": value}).get(
            "result", []
        )

    def orderbook(self, symbol: str) -> dict[str, Any]:
        return self._get(
            "/api/v1/orderbook",
            params={"symbol": _symbol(symbol)},
        ).get("result", {})

    def trades(self, symbol: str, limit: int = 20) -> Any:
        if limit < 1 or limit > 50:
            raise ValueError("최근 체결 조회 건수는 1~50이어야 합니다.")
        return self._get(
            "/api/v1/trades",
            params={"symbol": _symbol(symbol), "count": limit},
        ).get("result")

    def candles(
        self,
        symbol: str,
        *,
        interval: str = "1m",
        count: int = 100,
        before: str | None = None,
        adjusted: bool = True,
    ) -> dict[str, Any]:
        if interval not in {"1m", "1d"}:
            raise ValueError("interval은 1m 또는 1d여야 합니다.")
        if count < 1 or count > 200:
            raise ValueError("캔들 조회 건수는 1~200이어야 합니다.")
        params: dict[str, Any] = {
            "symbol": _symbol(symbol),
            "interval": interval,
            "count": count,
            "adjusted": adjusted,
        }
        if before:
            params["before"] = before
        return self._get("/api/v1/candles", params=params).get("result", {})

    def holdings(self, symbol: str | None = None) -> dict[str, Any]:
        params = {"symbol": _symbol(symbol)} if symbol else None
        return self._get(
            "/api/v1/holdings",
            params=params,
            account=True,
        ).get("result", {})

    def commissions(self) -> list[dict[str, Any]]:
        return self._get("/api/v1/commissions", account=True).get("result", [])

    def buying_power(self, currency: str) -> dict[str, Any]:
        currency = currency.upper()
        if currency not in {"KRW", "USD"}:
            raise ValueError("currency는 KRW 또는 USD여야 합니다.")
        return self._get(
            "/api/v1/buying-power",
            params={"currency": currency},
            account=True,
        ).get("result", {})

    def sellable_quantity(self, symbol: str) -> dict[str, Any]:
        return self._get(
            "/api/v1/sellable-quantity",
            params={"symbol": _symbol(symbol)},
            account=True,
        ).get("result", {})

    def orders(
        self,
        *,
        status: str = "OPEN",
        symbol: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        status = status.upper()
        if status not in {"OPEN", "CLOSED"}:
            raise ValueError("status는 OPEN 또는 CLOSED여야 합니다.")
        params: dict[str, Any] = {"status": status, "limit": limit}
        if symbol:
            params["symbol"] = _symbol(symbol)
        return self._get(
            "/api/v1/orders",
            params=params,
            account=True,
        ).get("result", {})

    def order(self, order_id: str) -> dict[str, Any]:
        return self._get(
            f"/api/v1/orders/{order_id}",
            account=True,
        ).get("result", {})

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/orders",
            json_body=payload,
            account=True,
        ).get("result", {})

    def modify_order(
        self,
        order_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/orders/{order_id}/modify",
            json_body=payload,
            account=True,
        ).get("result", {})

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/orders/{order_id}/cancel",
            json_body={},
            account=True,
        ).get("result", {})

    def access_token(
        self,
        *,
        force_refresh: bool = False,
        stale_token: str | None = None,
    ) -> str:
        if not force_refresh:
            if self._access_token and time.time() < self._expires_at - 60:
                return self._access_token
            cached = self._read_token_cache()
            if cached is not None:
                self._access_token, self._expires_at = cached
                return self._access_token

        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.token_path.with_suffix(".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            cached = self._read_token_cache()
            if cached is not None:
                cached_token, cached_expires_at = cached
                if not force_refresh or cached_token != stale_token:
                    self._access_token = cached_token
                    self._expires_at = cached_expires_at
                    return cached_token
            return self._issue_access_token()

    def _issue_access_token(self) -> str:
        for attempt in range(4):
            try:
                response = self.session.post(
                    f"{self.settings.base_url}/oauth2/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.client_id,
                        "client_secret": self.settings.client_secret,
                    },
                    timeout=self.settings.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt >= 3:
                    raise TossError(f"토스 액세스 토큰 연결 실패: {exc}") from exc
                time.sleep(_backoff(attempt))
                continue
            body = _json(response)
            if response.status_code == 429 and attempt < 3:
                retry_after = response.headers.get("Retry-After")
                time.sleep(
                    float(retry_after) if retry_after else _backoff(attempt)
                )
                continue
            if response.status_code >= 500 and attempt < 3:
                time.sleep(_backoff(attempt))
                continue
            if response.status_code >= 400:
                raise TossError(
                    body.get("error_description", "토스 액세스 토큰 발급 실패"),
                    status_code=response.status_code,
                    code=body.get("error"),
                )
            token = str(body.get("access_token") or "")
            expires_in = int(body.get("expires_in") or 0)
            if not token or expires_in <= 0:
                raise TossError("토스 액세스 토큰 응답이 올바르지 않습니다.")
            self._access_token = token
            self._expires_at = time.time() + expires_in
            self._write_token_cache(token, self._expires_at)
            return token
        raise TossError("토스 액세스 토큰 재시도 횟수를 초과했습니다.")

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        account: bool = False,
    ) -> dict[str, Any]:
        return self._request("GET", path, params=params, account=account)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        account: bool = False,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        refreshed = False
        unsafe = method.upper() in _UNSAFE_METHODS
        for attempt in range(max_retries + 1):
            token = self.access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            if account:
                headers["X-Tossinvest-Account"] = str(
                    self.settings.require_account()
                )
            try:
                response = self.session.request(
                    method,
                    f"{self.settings.base_url}{path}",
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.settings.timeout_seconds,
                )
            except requests.ConnectTimeout as exc:
                # 연결 자체가 맺어지지 않았으므로 요청은 전송되지 않았다.
                if attempt >= max_retries:
                    raise TossError(f"토스 API 연결 실패: {exc}") from exc
                time.sleep(_backoff(attempt))
                continue
            except requests.RequestException as exc:
                if unsafe:
                    # 응답 대기 중 끊김: 주문이 접수됐을 수 있다. 재전송 금지.
                    raise TossOutcomeUnknown(
                        f"토스 주문 요청 결과를 확인할 수 없습니다: {type(exc).__name__}"
                    ) from exc
                if attempt >= max_retries:
                    raise TossError(f"토스 API 연결 실패: {exc}") from exc
                time.sleep(_backoff(attempt))
                continue

            if unsafe and response.status_code >= 500:
                raise TossOutcomeUnknown(
                    "토스 서버 오류로 주문 처리 여부를 알 수 없습니다.",
                    status_code=response.status_code,
                )
            try:
                body = _json(response)
            except TossError:
                if unsafe and response.status_code < 400:
                    raise TossOutcomeUnknown(
                        "토스 주문 응답을 해석할 수 없어 처리 여부를 알 수 없습니다.",
                        status_code=response.status_code,
                    )
                raise
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            error_code = str(error.get("code") or "")
            if response.status_code == 401 and not refreshed:
                refreshed = True
                self.access_token(force_refresh=True, stale_token=token)
                continue
            if response.status_code == 429 and attempt < max_retries:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else _backoff(attempt)
                time.sleep(wait)
                continue
            if response.status_code == 409 and unsafe and error_code == "request-in-progress":
                raise TossOutcomeUnknown(
                    "같은 clientOrderId 주문이 토스에서 처리 중입니다.",
                    status_code=409,
                    code=error_code,
                )
            if response.status_code >= 500 and attempt < max_retries:
                time.sleep(_backoff(attempt))
                continue
            if response.status_code >= 400:
                request_id = (
                    error.get("requestId")
                    or response.headers.get("X-Request-Id")
                    or response.headers.get("cf-ray")
                )
                raise TossError(
                    str(error.get("message") or "토스 API 요청 실패"),
                    status_code=response.status_code,
                    code=error_code or None,
                    request_id=str(request_id) if request_id else None,
                )
            return body
        raise TossError("토스 API 재시도 횟수를 초과했습니다.")

    def _read_token_cache(self) -> tuple[str, float] | None:
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
            token = str(payload["access_token"])
            expires_at = float(payload["expires_at"])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return None
        if time.time() >= expires_at - 60:
            return None
        return token, expires_at

    def _write_token_cache(self, token: str, expires_at: float) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(
            json.dumps(
                {"access_token": token, "expires_at": expires_at},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chmod(self.token_path, 0o600)


def _symbol(value: str | None) -> str:
    symbol = (value or "").strip().upper()
    if not symbol:
        raise ValueError("종목 심볼이 필요합니다.")
    return symbol


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise TossError(
            "토스 API가 JSON이 아닌 응답을 반환했습니다.",
            status_code=response.status_code,
        ) from exc
    return body if isinstance(body, dict) else {"result": body}


def _backoff(attempt: int) -> float:
    return min(2**attempt, 8) + random.uniform(0, 0.25)
