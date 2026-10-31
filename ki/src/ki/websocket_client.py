from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator

import requests
import websockets

from ki.client import KISError
from ki.config import Settings


REAL_WEBSOCKET_URL = "ws://ops.koreainvestment.com:21000/tryitout"
DEMO_WEBSOCKET_URL = "ws://ops.koreainvestment.com:31000/tryitout"
OVERSEAS_ORDERBOOK_TR_ID = "HDFSASP0"
OVERSEAS_TRADE_TR_ID = "HDFSCNT0"


@dataclass(frozen=True)
class OverseasOrderBook:
    symbol: str
    local_date: str
    local_time: str
    korea_date: str
    korea_time: str
    bid_price: str
    ask_price: str
    bid_quantity: str
    ask_quantity: str
    total_bid_quantity: str
    total_ask_quantity: str

    @property
    def spread(self) -> str:
        try:
            return format(
                Decimal(self.ask_price) - Decimal(self.bid_price),
                "f",
            )
        except InvalidOperation:
            return "-"

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "local_date": self.local_date,
            "local_time": self.local_time,
            "korea_date": self.korea_date,
            "korea_time": self.korea_time,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "bid_quantity": self.bid_quantity,
            "ask_quantity": self.ask_quantity,
            "total_bid_quantity": self.total_bid_quantity,
            "total_ask_quantity": self.total_ask_quantity,
            "spread": self.spread,
        }


@dataclass(frozen=True)
class OverseasTrade:
    symbol: str
    local_business_date: str
    local_date: str
    local_time: str
    korea_date: str
    korea_time: str
    open_price: str
    high_price: str
    low_price: str
    last_price: str
    change_sign: str
    change: str
    change_rate: str
    bid_price: str
    ask_price: str
    bid_quantity: str
    ask_quantity: str
    trade_quantity: str
    cumulative_volume: str
    cumulative_amount: str
    sell_trade_quantity: str
    buy_trade_quantity: str
    execution_strength: str
    market_status: str

    def to_dict(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "local_business_date": self.local_business_date,
            "local_date": self.local_date,
            "local_time": self.local_time,
            "korea_date": self.korea_date,
            "korea_time": self.korea_time,
            "open_price": self.open_price,
            "high_price": self.high_price,
            "low_price": self.low_price,
            "last_price": self.last_price,
            "change_sign": self.change_sign,
            "change": self.change,
            "change_rate": self.change_rate,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "bid_quantity": self.bid_quantity,
            "ask_quantity": self.ask_quantity,
            "trade_quantity": self.trade_quantity,
            "cumulative_volume": self.cumulative_volume,
            "cumulative_amount": self.cumulative_amount,
            "sell_trade_quantity": self.sell_trade_quantity,
            "buy_trade_quantity": self.buy_trade_quantity,
            "execution_strength": self.execution_strength,
            "market_status": self.market_status,
        }


class KISWebSocketClient:
    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings
        self.session = session or requests.Session()

    def approval_key(self) -> str:
        try:
            response = self.session.post(
                f"{self.settings.base_url}/oauth2/Approval",
                headers={"content-type": "application/json"},
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.settings.app_key,
                    "secretkey": self.settings.app_secret,
                },
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise KISError(f"웹소켓 승인키 발급 실패: {exc}") from exc

        key = body.get("approval_key")
        if not key:
            raise KISError(
                f"웹소켓 승인키 발급 실패: {body.get('error_description', body)}"
            )
        return str(key)

    async def orderbooks(
        self,
        tr_key: str,
        max_retries: int = 3,
    ) -> AsyncIterator[OverseasOrderBook]:
        async for payload, _ in self._stream(
            tr_key,
            tr_id=OVERSEAS_ORDERBOOK_TR_ID,
            max_retries=max_retries,
        ):
            yield parse_overseas_orderbook(payload)

    async def trades(
        self,
        tr_key: str,
        max_retries: int = 3,
    ) -> AsyncIterator[OverseasTrade]:
        async for payload, data_count in self._stream(
            tr_key,
            tr_id=OVERSEAS_TRADE_TR_ID,
            max_retries=max_retries,
        ):
            for trade in parse_overseas_trades(payload, data_count=data_count):
                yield trade

    async def _stream(
        self,
        tr_key: str,
        *,
        tr_id: str,
        max_retries: int,
    ) -> AsyncIterator[tuple[str, int]]:
        async for _, payload, count in self.stream_many(
            [(tr_id, tr_key)], max_retries=max_retries,
        ):
            yield payload, count

    async def stream_many(
        self,
        subscriptions: list[tuple[str, str]],
        *,
        max_retries: int = 3,
    ) -> AsyncIterator[tuple[str, str, int]]:
        """Subscribe to multiple feeds on one connection for one app key."""
        if not subscriptions or any(not key for _, key in subscriptions):
            raise ValueError("실시간 종목 키가 필요합니다.")
        approval_key = self.approval_key()
        messages = [
            self._subscription_message(approval_key, key, tr_id=tr_id)
            for tr_id, key in dict.fromkeys(subscriptions)
        ]
        tr_ids = {tr_id for tr_id, _ in subscriptions}
        websocket_url = (
            REAL_WEBSOCKET_URL if self.settings.is_real else DEMO_WEBSOCKET_URL
        )

        retries = 0
        while retries <= max_retries:
            try:
                async with websockets.connect(
                    websocket_url,
                    ping_interval=None,
                    close_timeout=5,
                ) as websocket:
                    for message in messages:
                        await websocket.send(json.dumps(message))
                    retries = 0

                    async for raw in websocket:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")

                        if raw.startswith(("0|", "1|")):
                            parts = raw.split("|", 3)
                            if len(parts) == 4 and parts[1] in tr_ids:
                                yield parts[1], parts[3], int(parts[2] or "1")
                            continue

                        message = json.loads(raw)
                        header = message.get("header", {})
                        if header.get("tr_id") == "PINGPONG":
                            await websocket.pong(raw)
                            continue

                        body = message.get("body", {})
                        if body.get("rt_cd") == "0":
                            logging.info("KIS subscription accepted: %s", header.get("tr_id"))
                        if body.get("rt_cd") not in {None, "0"}:
                            error = body.get("msg1", "웹소켓 구독 오류")
                            if error != "ALREADY IN SUBSCRIBE":
                                raise KISError(error)
                # 서버가 연결을 정상 종료했다. 바로 다시 붙으면 빈 루프가 되므로 잠깐 쉰다.
                retries += 1
                if retries > max_retries:
                    raise KISError("웹소켓 연결이 반복해서 종료되었습니다.")
                await asyncio.sleep(min(retries * 2, 10))
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException, json.JSONDecodeError) as exc:
                retries += 1
                if retries > max_retries:
                    raise KISError(f"웹소켓 연결 실패: {exc}") from exc
                await asyncio.sleep(min(retries * 2, 10))

    @staticmethod
    def _subscription_message(
        approval_key: str,
        tr_key: str,
        *,
        tr_id: str = OVERSEAS_ORDERBOOK_TR_ID,
    ) -> dict[str, Any]:
        return {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": tr_key,
                }
            },
        }


def parse_overseas_orderbook(payload: str) -> OverseasOrderBook:
    values = payload.split("^")
    if len(values) >= 17:
        values = values[1:17]
    if len(values) < 16:
        raise ValueError(f"해외주식 호가 필드가 부족합니다: {len(values)}개")

    return OverseasOrderBook(
        symbol=values[0],
        local_date=values[2],
        local_time=values[3],
        korea_date=values[4],
        korea_time=values[5],
        total_bid_quantity=values[6],
        total_ask_quantity=values[7],
        bid_price=values[10],
        ask_price=values[11],
        bid_quantity=values[12],
        ask_quantity=values[13],
    )


def parse_overseas_trades(
    payload: str,
    *,
    data_count: int = 1,
) -> list[OverseasTrade]:
    values = payload.split("^")
    field_count = 26
    if data_count <= 0:
        raise ValueError("해외주식 체결 데이터 개수는 1 이상이어야 합니다.")
    expected = data_count * field_count
    if len(values) < expected:
        raise ValueError(
            f"해외주식 체결 필드가 부족합니다: {len(values)}개, 필요 {expected}개"
        )
    return [
        parse_overseas_trade(values[index : index + field_count])
        for index in range(0, expected, field_count)
    ]


def parse_overseas_trade(values: list[str]) -> OverseasTrade:
    if len(values) < 26:
        raise ValueError(f"해외주식 체결 필드가 부족합니다: {len(values)}개")
    return OverseasTrade(
        symbol=values[1],
        local_business_date=values[3],
        local_date=values[4],
        local_time=values[5],
        korea_date=values[6],
        korea_time=values[7],
        open_price=values[8],
        high_price=values[9],
        low_price=values[10],
        last_price=values[11],
        change_sign=values[12],
        change=values[13],
        change_rate=values[14],
        bid_price=values[15],
        ask_price=values[16],
        bid_quantity=values[17],
        ask_quantity=values[18],
        trade_quantity=values[19],
        cumulative_volume=values[20],
        cumulative_amount=values[21],
        sell_trade_quantity=values[22],
        buy_trade_quantity=values[23],
        execution_strength=values[24],
        market_status=values[25],
    )
