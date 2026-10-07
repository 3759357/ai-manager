#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ki.client import KISClient, KISError
from ki.config import Settings


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def main() -> int:
    if not ENV_FILE.exists():
        print(f"설정 파일을 찾을 수 없습니다: {ENV_FILE}", file=sys.stderr)
        return 1

    # 이 스크립트는 조회 전용이며 항상 실전투자 도메인을 사용합니다.
    os.environ["KIS_ENV"] = "real"

    try:
        settings = Settings.from_env(ENV_FILE)
        quote = KISClient(settings).overseas_quote("AAPL", "NAS")
    except (KISError, ValueError) as exc:
        print(f"애플 현재가 조회 실패: {exc}", file=sys.stderr)
        return 1

    sign_code = quote.get("sign")
    direction = {
        "1": "상한",
        "2": "상승",
        "3": "보합",
        "4": "하한",
        "5": "하락",
    }.get(sign_code, "알 수 없음")
    change = quote.get("diff")
    if change and sign_code in {"4", "5"}:
        change = f"-{change}"

    result = {
        "symbol": "AAPL",
        "exchange": "NASDAQ",
        "name": "Apple Inc.",
        "currency": "USD",
        "current_price": quote.get("last"),
        "previous_close": quote.get("base"),
        "direction": direction,
        "change": quote.get("diff"),
        "change_rate": quote.get("rate"),
        "signed_change": change,
        "volume": quote.get("tvol"),
        "trading_amount": quote.get("tamt"),
        "order_availability": quote.get("ordy"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
