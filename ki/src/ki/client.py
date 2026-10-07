from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from ki.config import Settings


class KISError(RuntimeError):
    """KIS API가 반환한 오류."""


class KISOutcomeUnknown(KISError):
    """주문 요청이 KIS에 도달했는지 알 수 없다. 재전송하지 말고 대사로 확인한다."""


class KISClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        token_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()
        self.token_dir = token_dir or Path.home() / ".cache" / "ki"
        self._access_token: str | None = None

    def quote(self, symbol: str, market: str = "J") -> dict[str, Any]:
        self._validate_symbol(symbol)
        body = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": symbol,
            },
        )
        return body["output"]

    def overseas_quote(self, symbol: str, exchange: str) -> dict[str, Any]:
        self._validate_overseas_symbol(symbol)
        self._validate_overseas_exchange(exchange)

        body = self._request(
            "GET",
            "/uapi/overseas-price/v1/quotations/price",
            tr_id="HHDFS00000300",
            params={
                "AUTH": "",
                "EXCD": exchange.upper(),
                "SYMB": symbol.upper(),
            },
        )
        return body["output"]

    def overseas_minute_candles(
        self,
        symbol: str,
        exchange: str = "NAS",
        *,
        interval: int = 1,
        count: int = 120,
        include_previous: bool = True,
        next_key: str = "",
    ) -> dict[str, Any]:
        self._validate_overseas_symbol(symbol)
        if exchange.upper() not in {
            "NYS",
            "NAS",
            "AMS",
            "HKS",
            "SHS",
            "SZS",
            "HSX",
            "HNX",
            "TSE",
            "BAY",
            "BAQ",
            "BAA",
        }:
            raise ValueError(f"지원하지 않는 해외 시세 거래소 코드입니다: {exchange}")
        if interval <= 0:
            raise ValueError("분봉 간격은 1 이상이어야 합니다.")
        if count < 1 or count > 120:
            raise ValueError("분봉 조회 건수는 1~120이어야 합니다.")

        body = self._request(
            "GET",
            "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice",
            tr_id="HHDFS76950200",
            params={
                "AUTH": "",
                "EXCD": exchange.upper(),
                "SYMB": symbol.upper(),
                "NMIN": str(interval),
                "PINC": "1" if include_previous else "0",
                "NEXT": "1" if next_key else "",
                "NREC": str(count),
                "FILL": "",
                "KEYB": next_key,
            },
        )
        return {
            "summary": body.get("output1", {}),
            "candles": body.get("output2", []),
        }

    def overseas_balance(
        self,
        exchange: str = "NASD",
        currency: str = "USD",
    ) -> dict[str, Any]:
        self.settings.require_account()
        self._validate_overseas_exchange(exchange)
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("통화 코드는 3자리 영문이어야 합니다. 예: USD")

        body = self._request(
            "GET",
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            tr_id="TTTS3012R" if self.settings.is_real else "VTTS3012R",
            params={
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product_code,
                "OVRS_EXCG_CD": exchange.upper(),
                "TR_CRCY_CD": currency.upper(),
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        return {
            "positions": body.get("output1", []),
            "summary": body.get("output2", []),
            "context": {
                "fk200": body.get("ctx_area_fk200", ""),
                "nk200": body.get("ctx_area_nk200", ""),
            },
        }

    def overseas_buying_power(
        self,
        symbol: str,
        price: str,
        exchange: str = "NASD",
    ) -> dict[str, Any]:
        self.settings.require_account()
        self._validate_overseas_symbol(symbol)
        self._validate_overseas_exchange(exchange)
        try:
            if float(price) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("조회 기준 가격은 0보다 커야 합니다.") from exc

        body = self._request(
            "GET",
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            tr_id="TTTS3007R" if self.settings.is_real else "VTTS3007R",
            params={
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product_code,
                "OVRS_EXCG_CD": exchange.upper(),
                "OVRS_ORD_UNPR": str(price),
                "ITEM_CD": symbol.upper(),
            },
        )
        return body.get("output", {})

    def overseas_pending_orders(
        self,
        exchange: str = "NASD",
        sort_sequence: str = "DS",
    ) -> list[dict[str, Any]]:
        self.settings.require_account()
        self._validate_overseas_exchange(exchange)
        if not sort_sequence:
            raise ValueError("정렬순서가 필요합니다. 예: DS")

        body = self._request(
            "GET",
            "/uapi/overseas-stock/v1/trading/inquire-nccs",
            tr_id="TTTS3018R",
            params={
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product_code,
                "OVRS_EXCG_CD": exchange.upper(),
                "SORT_SQN": sort_sequence,
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        return _as_list(body.get("output", []))

    def overseas_order_history(
        self,
        start_date: str,
        end_date: str,
        symbol: str = "%",
        side: str = "00",
        fill_status: str = "00",
        exchange: str = "NASD",
        sort_sequence: str = "DS",
    ) -> list[dict[str, Any]]:
        self.settings.require_account()
        if not start_date or not end_date:
            raise ValueError("주문 조회 시작일과 종료일이 필요합니다.")
        if side not in {"00", "01", "02"}:
            raise ValueError("매도매수구분은 00, 01, 02 중 하나여야 합니다.")
        if fill_status not in {"00", "01", "02"}:
            raise ValueError("체결미체결구분은 00, 01, 02 중 하나여야 합니다.")
        if not sort_sequence:
            raise ValueError("정렬순서가 필요합니다. 예: DS")

        pdno = symbol.upper()
        ovrs_excg_cd = exchange.upper()
        if not self.settings.is_real and pdno == "%":
            pdno = ""
            ovrs_excg_cd = ""

        body = self._request(
            "GET",
            "/uapi/overseas-stock/v1/trading/inquire-ccnl",
            tr_id="TTTS3035R" if self.settings.is_real else "VTTS3035R",
            params={
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product_code,
                "PDNO": pdno,
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": side,
                "CCLD_NCCS_DVSN": fill_status,
                "OVRS_EXCG_CD": ovrs_excg_cd,
                "SORT_SQN": sort_sequence,
                "ORD_DT": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "CTX_AREA_NK200": "",
                "CTX_AREA_FK200": "",
            },
        )
        return _as_list(body.get("output", []))

    def overseas_order(
        self,
        side: str,
        symbol: str,
        quantity: int,
        price: str,
        exchange: str = "NASD",
        order_type: str = "limit",
    ) -> dict[str, Any]:
        self.settings.require_account()
        self._validate_overseas_symbol(symbol)
        self._validate_overseas_exchange(exchange)
        if side not in {"buy", "sell"}:
            raise ValueError("side는 buy 또는 sell이어야 합니다.")
        if quantity <= 0:
            raise ValueError("주문 수량은 1주 이상이어야 합니다.")
        if order_type != "limit":
            raise ValueError("해외주식 Gateway는 지정가 주문만 지원합니다.")
        try:
            if float(price) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("지정가 주문에는 0보다 큰 가격이 필요합니다.") from exc

        tr_ids = {
            ("real", "buy"): "TTTT1002U",
            ("real", "sell"): "TTTT1006U",
            ("demo", "buy"): "VTTT1002U",
            ("demo", "sell"): "VTTT1006U",
        }
        payload = {
            "CANO": self.settings.account_no,
            "ACNT_PRDT_CD": self.settings.account_product_code,
            "OVRS_EXCG_CD": exchange.upper(),
            "PDNO": symbol.upper(),
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": str(price),
            "CTAC_TLNO": "",
            "MGCO_APTM_ODNO": "",
            "SLL_TYPE": "00" if side == "sell" else "",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",
        }
        return self._request(
            "POST",
            "/uapi/overseas-stock/v1/trading/order",
            tr_id=tr_ids[(self.settings.environment, side)],
            json_body=payload,
            use_hashkey=True,
        )["output"]

    def overseas_cancel_order(
        self,
        *,
        symbol: str,
        original_order_id: str,
        quantity: int,
        exchange: str = "NASD",
    ) -> dict[str, Any]:
        self.settings.require_account()
        self._validate_overseas_symbol(symbol)
        self._validate_overseas_exchange(exchange)
        if not original_order_id:
            raise ValueError("취소할 원주문번호가 필요합니다.")
        if quantity <= 0:
            raise ValueError("취소 수량은 1주 이상이어야 합니다.")

        payload = {
            "CANO": self.settings.account_no,
            "ACNT_PRDT_CD": self.settings.account_product_code,
            "OVRS_EXCG_CD": exchange.upper(),
            "PDNO": symbol.upper(),
            "ORGN_ODNO": str(original_order_id),
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": "0",
            "MGCO_APTM_ODNO": "",
            "ORD_SVR_DVSN_CD": "0",
        }
        return self._request(
            "POST",
            "/uapi/overseas-stock/v1/trading/order-rvsecncl",
            tr_id="TTTT1004U" if self.settings.is_real else "VTTT1004U",
            json_body=payload,
            use_hashkey=True,
        )["output"]

    def balance(self) -> dict[str, Any]:
        self.settings.require_account()
        body = self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id="TTTC8434R" if self.settings.is_real else "VTTC8434R",
            params={
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.account_product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        return {"positions": body.get("output1", []), "summary": body.get("output2", [])}

    def order(
        self,
        side: str,
        symbol: str,
        quantity: int,
        price: int = 0,
        order_type: str = "limit",
        exchange: str = "KRX",
    ) -> dict[str, Any]:
        self.settings.require_account()
        self._validate_symbol(symbol)
        if side not in {"buy", "sell"}:
            raise ValueError("side는 buy 또는 sell이어야 합니다.")
        if quantity <= 0:
            raise ValueError("주문 수량은 1주 이상이어야 합니다.")
        if order_type not in {"limit", "market"}:
            raise ValueError("order_type은 limit 또는 market이어야 합니다.")
        if order_type == "limit" and price <= 0:
            raise ValueError("지정가 주문에는 0보다 큰 가격이 필요합니다.")

        tr_ids = {
            ("real", "buy"): "TTTC0012U",
            ("real", "sell"): "TTTC0011U",
            ("demo", "buy"): "VTTC0012U",
            ("demo", "sell"): "VTTC0011U",
        }
        payload = {
            "CANO": self.settings.account_no,
            "ACNT_PRDT_CD": self.settings.account_product_code,
            "PDNO": symbol,
            "ORD_DVSN": "00" if order_type == "limit" else "01",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price if order_type == "limit" else 0),
            "EXCG_ID_DVSN_CD": exchange,
            "SLL_TYPE": "01" if side == "sell" else "",
            "CNDT_PRIC": "",
        }
        return self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_ids[(self.settings.environment, side)],
            json_body=payload,
            use_hashkey=True,
        )["output"]

    def current_price(self, symbol: str) -> int:
        return int(self.quote(symbol)["stck_prpr"])

    def holding_quantity(self, symbol: str) -> int:
        for position in self.balance()["positions"]:
            if position.get("pdno") == symbol:
                return int(position.get("hldg_qty", "0"))
        return 0

    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, str] | None = None,
        use_hashkey: bool = False,
    ) -> dict[str, Any]:
        headers = self._headers(tr_id)
        if use_hashkey and json_body is not None:
            headers["hashkey"] = self._hashkey(json_body)

        unsafe = method.upper() != "GET"
        max_attempts = 1 if unsafe else 3
        for attempt in range(max_attempts):
            try:
                response = self.session.request(
                    method,
                    f"{self.settings.base_url}{path}",
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.settings.timeout_seconds,
                )
                response.raise_for_status()
                body = response.json()
                break
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in {429, 500, 502, 503, 504} and attempt + 1 < max_attempts:
                    time.sleep(0.25 * (attempt + 1))
                    continue
                if unsafe and (status is None or status >= 500):
                    raise KISOutcomeUnknown(f"KIS 주문 처리 여부를 알 수 없습니다: HTTP {status}") from exc
                raise KISError(f"KIS API 통신 실패: {exc}") from exc
            except requests.ConnectTimeout as exc:
                raise KISError(f"KIS API 연결 실패: {exc}") from exc
            except requests.RequestException as exc:
                if unsafe:
                    raise KISOutcomeUnknown(
                        f"KIS 주문 요청 결과를 확인할 수 없습니다: {type(exc).__name__}"
                    ) from exc
                raise KISError(f"KIS API 통신 실패: {exc}") from exc
            except ValueError as exc:
                if unsafe:
                    raise KISOutcomeUnknown("KIS 주문 응답을 해석할 수 없어 처리 여부를 알 수 없습니다.") from exc
                raise KISError("KIS API 응답이 JSON 형식이 아닙니다.") from exc

        if str(body.get("rt_cd", "0")) != "0":
            raise KISError(
                f"{body.get('msg_cd', 'UNKNOWN')}: {body.get('msg1', 'API 오류')}"
            )
        return body

    def _headers(self, tr_id: str) -> dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token()}",
            "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _token(self) -> str:
        if self._access_token:
            return self._access_token

        cached = self._read_cached_token()
        if cached:
            self._access_token = cached
            return cached

        try:
            response = self.session.post(
                f"{self.settings.base_url}/oauth2/tokenP",
                headers={"content-type": "application/json"},
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.settings.app_key,
                    "appsecret": self.settings.app_secret,
                },
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise KISError(f"접근 토큰 발급 실패: {exc}") from exc

        token = body.get("access_token")
        if not token:
            raise KISError(f"접근 토큰 발급 실패: {body.get('error_description', body)}")
        expires_at = self._expiry_timestamp(body)
        self._write_cached_token(token, expires_at)
        self._access_token = token
        return token

    def _hashkey(self, payload: dict[str, str]) -> str:
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token()}",
            "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
        }
        try:
            response = self.session.post(
                f"{self.settings.base_url}/uapi/hashkey",
                headers=headers,
                json=payload,
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            value = response.json().get("HASH")
        except requests.RequestException as exc:
            raise KISError(f"주문 해시키 발급 실패: {exc}") from exc
        if not value:
            raise KISError("주문 해시키 응답에 HASH가 없습니다.")
        return value

    @property
    def _token_path(self) -> Path:
        key_id = hashlib.sha256(self.settings.app_key.encode()).hexdigest()[:12]
        return self.token_dir / f"token-{self.settings.environment}-{key_id}.json"

    def _read_cached_token(self) -> str | None:
        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
            if float(data["expires_at"]) > time.time() + 60:
                return str(data["access_token"])
        except (FileNotFoundError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return None

    def _write_cached_token(self, token: str, expires_at: float) -> None:
        self.token_dir.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(
            json.dumps({"access_token": token, "expires_at": expires_at}),
            encoding="utf-8",
        )
        os.chmod(self._token_path, 0o600)

    @staticmethod
    def _expiry_timestamp(body: dict[str, Any]) -> float:
        raw_expiry = body.get("access_token_token_expired")
        if raw_expiry:
            try:
                return datetime.strptime(raw_expiry, "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                pass
        return time.time() + int(body.get("expires_in", 86_400))

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if len(symbol) not in {6, 7} or not symbol.isalnum():
            raise ValueError("종목코드는 6자리(ETN은 7자리)여야 합니다.")

    @staticmethod
    def _validate_overseas_symbol(symbol: str) -> None:
        if not symbol or not symbol.isalnum():
            raise ValueError("해외주식 종목코드는 영문/숫자로 입력하세요.")

    @staticmethod
    def _validate_overseas_exchange(exchange: str) -> None:
        if not exchange or not exchange.isalpha():
            raise ValueError("해외 거래소 코드를 입력하세요. 예: NAS 또는 NASD")


def _as_list(value: Any) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    raise KISError(f"예상하지 못한 KIS 응답 형식입니다: {type(value).__name__}")
