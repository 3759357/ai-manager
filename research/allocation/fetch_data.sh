#!/usr/bin/env bash
# 백테스트 입력 데이터 내려받기 (GitHub 공개 저장소, 조회 전용)
set -euo pipefail
cd "$(dirname "$0")"; mkdir -p raw; cd raw
get() { # repo path
  d=$(echo "$1" | tr / _)
  [ -d "$d" ] || git clone -q --depth 1 --filter=blob:none --no-checkout "https://github.com/$1" "$d"
  git -C "$d" checkout -q HEAD -- "$2"
}
get nateGeorge/simulate_leveraged_ETFs eod_data/QQQ.csv          # QQQ 수정주가 1999-03 ~ 2019-10
get Chief-rich/nasdaq100-data data/ndx_daily/history.csv          # ^NDX 2019-01 ~ 현재 (매일 갱신)
get SteelCerberus/us-market-data data/us_market_data.csv          # S&P500 총수익·단기국채 1885 ~ 2025-12
