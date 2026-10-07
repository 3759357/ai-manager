#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from ki.client import KISError
from ki.config import Settings
from ki.websocket_client import KISWebSocketClient


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="애플(AAPL) 실시간 매수/매도 1호가를 콘솔에 출력합니다."
    )
    command.add_argument(
        "--session",
        choices=["regular", "daytime"],
        default="regular",
        help="regular: 미국 정규장, daytime: 미국 주간거래",
    )
    return command


async def stream(session: str) -> None:
    os.environ.setdefault("KIS_ENV", "real")
    settings = Settings.from_env(ENV_FILE)
    client = KISWebSocketClient(settings)
    tr_key = "DNASAAPL" if session == "regular" else "RBAQAAPL"

    print(f"AAPL 실시간 호가 연결 중 ({session}, {tr_key})")
    print("종료: Ctrl+C")
    print("-" * 84)
    print("한국시간 | BID 가격 x 잔량 | ASK 가격 x 잔량 | 스프레드 | 총매수 / 총매도")

    async for book in client.orderbooks(tr_key):
        korea_time = format_time(book.korea_time)
        print(
            f"{korea_time} | "
            f"BID {book.bid_price:>10} x {book.bid_quantity:<8} | "
            f"ASK {book.ask_price:>10} x {book.ask_quantity:<8} | "
            f"{book.spread:>8} | "
            f"{book.total_bid_quantity} / {book.total_ask_quantity}",
            flush=True,
        )


def format_time(value: str) -> str:
    if len(value) == 6 and value.isdigit():
        return f"{value[:2]}:{value[2:4]}:{value[4:]}"
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        asyncio.run(stream(args.session))
    except KeyboardInterrupt:
        print("\n실시간 호가 조회를 종료했습니다.")
        return 0
    except (KISError, ValueError) as exc:
        print(f"실시간 호가 조회 실패: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
