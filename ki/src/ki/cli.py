from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ki.account import sync_overseas_account
from ki.client import KISClient, KISError
from ki.config import Settings
from ki.dip_watch import DipWatcher, DipWatchOptions
from ki.gateway import KISGateway
from ki.market_watch import (
    MarketWatcher,
    MarketWatchOptions,
    WatchlistConfig,
    validate_watchlist_config,
)
from ki.orderbook_watch import OrderBookWatcher, OrderBookWatchOptions
from ki.reconcile import OrderReconciliationService
from ki.storage import SnapshotStore
from ki.trade_watch import TradeWatcher, TradeWatchOptions
from ki.trader import AutoTrader, ThresholdStrategy
from ki.websocket_client import KISWebSocketClient
from ki.worker import OperationsWorker, WorkerOptions


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ki",
        description="한국투자증권 Open API 주식 자동매매 CLI",
    )
    parser.add_argument("--verbose", action="store_true", help="상세 로그 출력")
    subparsers = parser.add_subparsers(dest="command", required=True)

    quote = subparsers.add_parser("quote", help="현재가 조회")
    quote.add_argument("symbol", help="종목코드 (예: 005930)")
    quote.add_argument("--market", default="J", help="시장 코드 (기본: J/KRX)")

    subparsers.add_parser("balance", help="주식 잔고 조회")

    account_sync = subparsers.add_parser(
        "account-sync",
        help="해외주식 계좌와 매수가능금액을 조회해 SQLite에 저장",
    )
    account_sync.add_argument("symbol", nargs="?", default="AAPL")
    account_sync.add_argument(
        "--quote-exchange",
        default="NAS",
        help="시세 거래소 코드 (기본: NAS)",
    )
    account_sync.add_argument(
        "--account-exchange",
        default="NASD",
        help="계좌 거래소 코드 (기본: NASD/미국전체)",
    )
    account_sync.add_argument("--currency", default="USD")
    account_sync.add_argument("--db", type=Path, help="SQLite 파일 경로")

    gateway_intent = subparsers.add_parser(
        "gateway-intent",
        help="Gateway를 통해 해외주식 주문 후보를 생성하고 리스크 검토",
    )
    gateway_intent.add_argument("side", choices=["buy", "sell"])
    gateway_intent.add_argument("symbol")
    gateway_intent.add_argument("quantity", type=int)
    gateway_intent.add_argument("--price", default="0", help="지정가")
    gateway_intent.add_argument(
        "--order-type",
        choices=["limit", "market"],
        default="limit",
    )
    gateway_intent.add_argument("--exchange", default="NASD")
    gateway_intent.add_argument("--source", default="cli")
    gateway_intent.add_argument("--db", type=Path, help="SQLite 파일 경로")
    gateway_intent.add_argument(
        "--skip-buying-power-check",
        action="store_true",
        help="매수 후보 생성 시 KIS 매수가능수량 조회를 생략",
    )
    gateway_intent.add_argument(
        "--skip-position-check",
        action="store_true",
        help="매도 후보 생성 시 KIS 해외잔고 조회를 생략",
    )

    watchlist = subparsers.add_parser("watchlist", help="관심종목 목록 조회")
    watchlist.add_argument("--enabled-only", action="store_true")
    watchlist.add_argument("--db", type=Path, help="SQLite 파일 경로")

    watchlist_add = subparsers.add_parser(
        "watchlist-add",
        help="관심종목과 핫 시그널 전략 조건 등록",
    )
    watchlist_add.add_argument("symbol")
    watchlist_add.add_argument("--quote-exchange", default="NAS")
    watchlist_add.add_argument("--account-exchange", default="NASD")
    watchlist_add.add_argument("--quantity", type=int, default=1)
    watchlist_add.add_argument("--hot-up-pct", default="1")
    watchlist_add.add_argument("--hot-down-pct", default="1")
    watchlist_add.add_argument(
        "--action-on-hot-up",
        choices=["none", "buy", "sell"],
        default="none",
    )
    watchlist_add.add_argument(
        "--action-on-hot-down",
        choices=["none", "buy", "sell"],
        default="none",
    )
    watchlist_add.add_argument(
        "--limit-offset-pct",
        default="0.02",
        help="자동 주문 후보 지정가 offset percent",
    )
    watchlist_add.add_argument("--disabled", action="store_true")
    watchlist_add.add_argument("--db", type=Path, help="SQLite 파일 경로")

    watchlist_remove = subparsers.add_parser(
        "watchlist-remove",
        help="관심종목 제거",
    )
    watchlist_remove.add_argument("symbol")
    watchlist_remove.add_argument("--db", type=Path, help="SQLite 파일 경로")

    market_watch = subparsers.add_parser(
        "market-watch",
        help="관심종목 시세를 감시하고 핫 시그널/주문 후보 생성",
    )
    market_watch.add_argument("--db", type=Path, help="SQLite 파일 경로")
    market_watch.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="반복 감시 주기(초). 0이면 1회 실행",
    )
    market_watch.add_argument(
        "--max-cycles",
        type=int,
        help="반복 실행 횟수. --interval 사용 시 생략하면 계속 실행",
    )
    market_watch.add_argument(
        "--auto-submit",
        action="store_true",
        help="시그널 주문 후보를 즉시 실전 제출. 반드시 --execute 필요",
    )
    market_watch.add_argument("--execute", action="store_true")

    market_signals = subparsers.add_parser(
        "market-signals",
        help="시장 핫 시그널 조회",
    )
    market_signals.add_argument("--limit", type=int, default=20)
    market_signals.add_argument("--db", type=Path, help="SQLite 파일 경로")

    orderbook_watch = subparsers.add_parser(
        "orderbook-watch",
        help="해외주식 실시간 호가를 저장하고 압력 신호 감지",
    )
    orderbook_watch.add_argument("symbol")
    orderbook_watch.add_argument(
        "--session",
        choices=["regular", "daytime"],
        default="regular",
        help="regular: 미국 정규장, daytime: 미국 주간거래",
    )
    orderbook_watch.add_argument(
        "--max-events",
        type=int,
        help="수신할 최대 호가 이벤트 수. 생략하면 계속 실행",
    )
    orderbook_watch.add_argument(
        "--bid-pressure-threshold",
        default="30",
        help="매수 압력 신호 기준 점수",
    )
    orderbook_watch.add_argument(
        "--ask-pressure-threshold",
        default="-30",
        help="매도 압력 신호 기준 점수",
    )
    orderbook_watch.add_argument(
        "--action-on-bid-pressure",
        choices=["none", "buy", "sell"],
        default="none",
        help="매수 압력 신호 발생 시 주문 후보 액션",
    )
    orderbook_watch.add_argument(
        "--action-on-ask-pressure",
        choices=["none", "buy", "sell"],
        default="none",
        help="매도 압력 신호 발생 시 주문 후보 액션",
    )
    orderbook_watch.add_argument("--quantity", type=int, default=1)
    orderbook_watch.add_argument("--account-exchange", default="NASD")
    orderbook_watch.add_argument(
        "--limit-offset-pct",
        default="0.02",
        help="호가 기반 주문 후보 지정가 offset percent",
    )
    orderbook_watch.add_argument(
        "--order-cooldown-seconds",
        type=float,
        default=60.0,
        help="같은 호가 신호 주문 후보 생성 쿨다운(초)",
    )
    orderbook_watch.add_argument("--db", type=Path, help="SQLite 파일 경로")

    orderbook_snapshots = subparsers.add_parser(
        "orderbook-snapshots",
        help="저장된 실시간 호가 스냅샷 조회",
    )
    orderbook_snapshots.add_argument("--limit", type=int, default=20)
    orderbook_snapshots.add_argument("--db", type=Path, help="SQLite 파일 경로")

    orderbook_signals = subparsers.add_parser(
        "orderbook-signals",
        help="호가 기반 압력 신호 조회",
    )
    orderbook_signals.add_argument("--limit", type=int, default=20)
    orderbook_signals.add_argument("--db", type=Path, help="SQLite 파일 경로")

    minute_candles = subparsers.add_parser(
        "minute-candles",
        help="KIS REST 해외주식 분봉 조회",
    )
    minute_candles.add_argument("symbol")
    minute_candles.add_argument("--exchange", default="NAS")
    minute_candles.add_argument("--interval", type=int, default=1)
    minute_candles.add_argument("--count", type=int, default=120)
    minute_candles.add_argument(
        "--current-day-only",
        action="store_true",
        help="전일 분봉을 포함하지 않음",
    )

    trade_watch = subparsers.add_parser(
        "trade-watch",
        help="해외주식 실시간 체결을 저장하고 분봉·거래량 이상징후 생성",
    )
    trade_watch.add_argument("symbol")
    trade_watch.add_argument(
        "--session",
        choices=["regular", "daytime"],
        default="regular",
    )
    trade_watch.add_argument("--max-events", type=int)
    trade_watch.add_argument("--interval-minutes", type=int, default=1)
    trade_watch.add_argument("--baseline-bars", type=int, default=5)
    trade_watch.add_argument("--volume-surge-multiplier", default="3")
    trade_watch.add_argument("--sell-pressure-ratio", default="1.5")
    trade_watch.add_argument("--db", type=Path, help="SQLite 파일 경로")

    trade_ticks = subparsers.add_parser(
        "trade-ticks",
        help="저장된 실시간 체결 틱 조회",
    )
    trade_ticks.add_argument("--limit", type=int, default=20)
    trade_ticks.add_argument("--db", type=Path, help="SQLite 파일 경로")

    trade_candles = subparsers.add_parser(
        "trade-candles",
        help="실시간 체결로 집계한 로컬 분봉 조회",
    )
    trade_candles.add_argument("--symbol")
    trade_candles.add_argument("--limit", type=int, default=20)
    trade_candles.add_argument("--db", type=Path, help="SQLite 파일 경로")

    dip_watch = subparsers.add_parser(
        "dip-watch",
        help="낙폭·저점 안정·반등을 감시해 수동 승인용 매수 후보 생성",
    )
    dip_watch.add_argument("symbol")
    dip_watch.add_argument("quantity", type=int, help="최대 추가 매수 수량")
    dip_watch.add_argument(
        "--tranches",
        help="차수별 수량. 예: 1,1,2 (합계는 quantity와 같아야 함)",
    )
    dip_watch.add_argument(
        "--next-tranche-drop-pct",
        default="3",
        help="다음 차수 후보에 필요한 직전 후보 저점 대비 추가 하락률",
    )
    dip_watch.add_argument("--exchange", default="NAS")
    dip_watch.add_argument("--interval-minutes", type=int, default=1)
    dip_watch.add_argument("--candle-count", type=int, default=120)
    dip_watch.add_argument("--lookback-bars", type=int, default=60)
    dip_watch.add_argument("--stable-bars", type=int, default=10)
    dip_watch.add_argument("--watch-low")
    dip_watch.add_argument("--watch-high")
    dip_watch.add_argument("--min-drawdown-pct", default="5")
    dip_watch.add_argument("--rebound-pct", default="1.5")
    dip_watch.add_argument(
        "--reference",
        action="append",
        default=[],
        help="동시에 저점 안정을 확인할 기준 종목. 여러 번 지정 가능",
    )
    dip_watch.add_argument("--reference-rebound-pct", default="0")
    dip_watch.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="반복 감시 주기(초). 0이면 1회 실행",
    )
    dip_watch.add_argument("--max-cycles", type=int)
    dip_watch.add_argument("--db", type=Path, help="SQLite 파일 경로")

    orders = subparsers.add_parser("orders", help="Gateway 주문 원장 조회")
    orders.add_argument("--limit", type=int, default=20)
    orders.add_argument("--db", type=Path, help="SQLite 파일 경로")

    fills = subparsers.add_parser("fills", help="체결 원장 조회")
    fills.add_argument("--limit", type=int, default=20)
    fills.add_argument("--db", type=Path, help="SQLite 파일 경로")

    reconcile_orders = subparsers.add_parser(
        "reconcile-orders",
        help="KIS 해외주식 미체결/체결 조회 결과를 주문 원장에 대사",
    )
    reconcile_orders.add_argument("--exchange", default="NASD")
    reconcile_orders.add_argument("--start-date", help="YYYYMMDD")
    reconcile_orders.add_argument("--end-date", help="YYYYMMDD")
    reconcile_orders.add_argument("--days", type=int, default=7)
    reconcile_orders.add_argument("--db", type=Path, help="SQLite 파일 경로")

    submit_order = subparsers.add_parser(
        "submit-order",
        help="Gateway 주문 원장을 KIS 해외주식 지정가 주문으로 전송",
    )
    submit_order.add_argument("order_id", type=int)
    submit_order.add_argument("--execute", action="store_true")
    submit_order.add_argument("--db", type=Path, help="SQLite 파일 경로")

    cancel_order = subparsers.add_parser(
        "cancel-order",
        help="Gateway 주문 원장을 기준으로 KIS 해외주식 주문 취소 요청",
    )
    cancel_order.add_argument("order_id", type=int)
    cancel_order.add_argument("--quantity", type=int, help="취소 수량")
    cancel_order.add_argument("--execute", action="store_true")
    cancel_order.add_argument("--db", type=Path, help="SQLite 파일 경로")

    void_order = subparsers.add_parser(
        "void-order",
        help="KIS에 전송하지 않은 Gateway 주문 후보를 로컬에서 폐기",
    )
    void_order.add_argument("order_id", type=int)
    void_order.add_argument("--reason", default="", help="폐기 사유")
    void_order.add_argument(
        "--confirm-not-placed",
        action="store_true",
        help="결과 불명(SUBMIT_UNKNOWN) 주문이 KIS에 접수되지 않았음을 확인했을 때만 사용",
    )
    void_order.add_argument("--db", type=Path, help="SQLite 파일 경로")

    worker = subparsers.add_parser(
        "worker",
        help="주문 원장 대사를 주기적으로 수행하는 백그라운드 워커",
    )
    worker.add_argument("--interval", type=float, default=60.0, help="반복 주기(초)")
    worker.add_argument("--exchange", default="NASD")
    worker.add_argument("--days", type=int, default=7)
    worker.add_argument("--db", type=Path, help="SQLite 파일 경로")
    worker.add_argument(
        "--max-cycles",
        type=int,
        help="지정한 횟수만 실행 후 종료. 테스트/점검용",
    )
    worker.add_argument(
        "--account-sync",
        action="store_true",
        help="각 cycle마다 계좌 스냅샷도 저장",
    )
    worker.add_argument("--account-symbol", default="AAPL")
    worker.add_argument("--quote-exchange", default="NAS")
    worker.add_argument("--currency", default="USD")
    worker.add_argument(
        "--stop-on-error",
        action="store_true",
        help="cycle 실패 시 계속 돌지 않고 즉시 종료",
    )

    order = subparsers.add_parser("order", help="현금 주문")
    order.add_argument("side", choices=["buy", "sell"])
    order.add_argument("symbol")
    order.add_argument("quantity", type=int)
    order.add_argument("--price", type=int, default=0, help="지정가")
    order.add_argument("--market", action="store_true", help="시장가 주문")
    order.add_argument("--exchange", default="KRX")
    order.add_argument("--execute", action="store_true", help="주문을 실제 API로 전송")

    auto = subparsers.add_parser("auto", help="목표가 기반 자동매매")
    auto.add_argument("symbol")
    auto.add_argument("--quantity", type=int, default=1)
    auto.add_argument("--buy-below", type=int)
    auto.add_argument("--sell-above", type=int)
    auto.add_argument("--interval", type=float, default=30)
    auto.add_argument("--max-orders", type=int, default=1)
    auto.add_argument("--execute", action="store_true", help="주문을 실제 API로 전송")
    return parser


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _watchlist_config_to_dict(config: WatchlistConfig) -> dict[str, Any]:
    return {
        "symbol": config.symbol,
        "quote_exchange": config.quote_exchange,
        "account_exchange": config.account_exchange,
        "enabled": config.enabled,
        "quantity": config.quantity,
        "hot_up_pct": format(config.hot_up_pct, "f"),
        "hot_down_pct": format(config.hot_down_pct, "f"),
        "action_on_hot_up": config.action_on_hot_up,
        "action_on_hot_down": config.action_on_hot_down,
        "limit_offset_pct": format(config.limit_offset_pct, "f"),
    }


def _run_market_watch(
    watcher: MarketWatcher,
    *,
    interval_seconds: float,
    max_cycles: int | None,
) -> None:
    if interval_seconds < 0:
        raise ValueError("--interval은 0 이상이어야 합니다.")
    if max_cycles is not None and max_cycles <= 0:
        raise ValueError("--max-cycles는 1 이상이어야 합니다.")
    if interval_seconds == 0:
        _print(watcher.scan_once())
        return

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        _print({"cycle": cycles, **watcher.scan_once()})
        sys.stdout.flush()
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(interval_seconds)


def _run_dip_watch(
    watcher: DipWatcher,
    symbol: str,
    *,
    interval_seconds: float,
    max_cycles: int | None,
) -> None:
    if interval_seconds < 0:
        raise ValueError("--interval은 0 이상이어야 합니다.")
    if max_cycles is not None and max_cycles <= 0:
        raise ValueError("--max-cycles는 1 이상이어야 합니다.")
    if interval_seconds == 0:
        _print(watcher.scan_once(symbol))
        return

    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        _print({"cycle": cycles, **watcher.scan_once(symbol)})
        sys.stdout.flush()
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(interval_seconds)


async def _run_orderbook_watch(watcher: OrderBookWatcher, symbol: str) -> None:
    async for event in watcher.watch(symbol):
        _print(event)
        sys.stdout.flush()


async def _run_trade_watch(watcher: TradeWatcher, symbol: str) -> None:
    async for event in watcher.watch(symbol):
        _print(event)
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=(
            logging.INFO
            if args.verbose or args.command in {"auto", "worker"}
            else logging.WARNING
        ),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        settings = Settings.from_env()
        client = KISClient(settings)

        if args.command == "quote":
            _print(client.quote(args.symbol, args.market))
            return
        if args.command == "balance":
            _print(client.balance())
            return
        if args.command == "account-sync":
            store = SnapshotStore(args.db or settings.database_path)
            _print(
                sync_overseas_account(
                    client,
                    settings,
                    store,
                    symbol=args.symbol,
                    quote_exchange=args.quote_exchange,
                    account_exchange=args.account_exchange,
                    currency=args.currency,
                )
            )
            return
        if args.command == "gateway-intent":
            store = SnapshotStore(args.db or settings.database_path)
            gateway = KISGateway(settings, store)
            _print(
                gateway.create_order_intent(
                    side=args.side,
                    symbol=args.symbol,
                    quantity=args.quantity,
                    price=args.price,
                    source=args.source,
                    exchange=args.exchange,
                    order_type=args.order_type,
                    client=client,
                    check_buying_power=not args.skip_buying_power_check,
                    check_position=not args.skip_position_check,
                )
            )
            return
        if args.command == "watchlist":
            store = SnapshotStore(args.db or settings.database_path)
            _print(store.list_watchlist(enabled_only=args.enabled_only))
            return
        if args.command == "watchlist-add":
            store = SnapshotStore(args.db or settings.database_path)
            config = WatchlistConfig(
                symbol=args.symbol.upper(),
                quote_exchange=args.quote_exchange,
                account_exchange=args.account_exchange,
                enabled=not args.disabled,
                quantity=args.quantity,
                hot_up_pct=Decimal(args.hot_up_pct),
                hot_down_pct=Decimal(args.hot_down_pct),
                action_on_hot_up=args.action_on_hot_up,
                action_on_hot_down=args.action_on_hot_down,
                limit_offset_pct=Decimal(args.limit_offset_pct),
            )
            validate_watchlist_config(config)
            item_id = store.upsert_watchlist_item(
                symbol=config.symbol,
                quote_exchange=config.quote_exchange,
                account_exchange=config.account_exchange,
                enabled=config.enabled,
                quantity=config.quantity,
                hot_up_pct=format(config.hot_up_pct, "f"),
                hot_down_pct=format(config.hot_down_pct, "f"),
                action_on_hot_up=config.action_on_hot_up,
                action_on_hot_down=config.action_on_hot_down,
                limit_offset_pct=format(config.limit_offset_pct, "f"),
            )
            _print({"id": item_id, "watchlist_item": _watchlist_config_to_dict(config)})
            return
        if args.command == "watchlist-remove":
            store = SnapshotStore(args.db or settings.database_path)
            _print(
                {
                    "symbol": args.symbol.upper(),
                    "removed": store.remove_watchlist_item(args.symbol),
                }
            )
            return
        if args.command == "market-watch":
            if args.auto_submit and not args.execute:
                raise PermissionError("--auto-submit에는 --execute가 필요합니다.")
            store = SnapshotStore(args.db or settings.database_path)
            watcher = MarketWatcher(
                client,
                settings,
                store,
                options=MarketWatchOptions(
                    auto_submit=args.auto_submit,
                    execute=args.execute,
                ),
            )
            _run_market_watch(
                watcher,
                interval_seconds=args.interval,
                max_cycles=args.max_cycles,
            )
            return
        if args.command == "market-signals":
            store = SnapshotStore(args.db or settings.database_path)
            _print(store.list_market_signals(limit=args.limit))
            return
        if args.command == "orderbook-watch":
            store = SnapshotStore(args.db or settings.database_path)
            watcher = OrderBookWatcher(
                KISWebSocketClient(settings),
                store,
                gateway=KISGateway(settings, store),
                order_client=client,
                options=OrderBookWatchOptions(
                    session=args.session,
                    max_events=args.max_events,
                    bid_pressure_threshold=Decimal(args.bid_pressure_threshold),
                    ask_pressure_threshold=Decimal(args.ask_pressure_threshold),
                    action_on_bid_pressure=args.action_on_bid_pressure,
                    action_on_ask_pressure=args.action_on_ask_pressure,
                    quantity=args.quantity,
                    account_exchange=args.account_exchange,
                    limit_offset_pct=Decimal(args.limit_offset_pct),
                    order_cooldown_seconds=args.order_cooldown_seconds,
                ),
            )
            asyncio.run(_run_orderbook_watch(watcher, args.symbol))
            return
        if args.command == "orderbook-snapshots":
            store = SnapshotStore(args.db or settings.database_path)
            _print(store.list_orderbook_snapshots(limit=args.limit))
            return
        if args.command == "orderbook-signals":
            store = SnapshotStore(args.db or settings.database_path)
            signals = [
                signal
                for signal in store.list_market_signals(limit=args.limit * 5)
                if str(signal["signal_type"]).startswith("ORDERBOOK_")
            ][: args.limit]
            _print(signals)
            return
        if args.command == "minute-candles":
            _print(
                client.overseas_minute_candles(
                    args.symbol,
                    args.exchange,
                    interval=args.interval,
                    count=args.count,
                    include_previous=not args.current_day_only,
                )
            )
            return
        if args.command == "trade-watch":
            store = SnapshotStore(args.db or settings.database_path)
            watcher = TradeWatcher(
                KISWebSocketClient(settings),
                store,
                options=TradeWatchOptions(
                    session=args.session,
                    max_events=args.max_events,
                    interval_minutes=args.interval_minutes,
                    baseline_bars=args.baseline_bars,
                    volume_surge_multiplier=Decimal(
                        args.volume_surge_multiplier
                    ),
                    sell_pressure_ratio=Decimal(args.sell_pressure_ratio),
                ),
            )
            asyncio.run(_run_trade_watch(watcher, args.symbol))
            return
        if args.command == "trade-ticks":
            store = SnapshotStore(args.db or settings.database_path)
            _print(store.list_trade_ticks(limit=args.limit))
            return
        if args.command == "trade-candles":
            store = SnapshotStore(args.db or settings.database_path)
            _print(
                store.list_trade_candles(
                    symbol=args.symbol,
                    limit=args.limit,
                )
            )
            return
        if args.command == "dip-watch":
            store = SnapshotStore(args.db or settings.database_path)
            watcher = DipWatcher(
                client,
                store,
                options=DipWatchOptions(
                    quantity=args.quantity,
                    tranche_quantities=(
                        tuple(
                            int(value.strip())
                            for value in args.tranches.split(",")
                            if value.strip()
                        )
                        if args.tranches
                        else ()
                    ),
                    next_tranche_drop_pct=Decimal(
                        args.next_tranche_drop_pct
                    ),
                    exchange=args.exchange,
                    interval_minutes=args.interval_minutes,
                    candle_count=args.candle_count,
                    lookback_bars=args.lookback_bars,
                    stable_bars=args.stable_bars,
                    watch_low=(
                        Decimal(args.watch_low)
                        if args.watch_low is not None
                        else None
                    ),
                    watch_high=(
                        Decimal(args.watch_high)
                        if args.watch_high is not None
                        else None
                    ),
                    min_drawdown_pct=Decimal(args.min_drawdown_pct),
                    rebound_pct=Decimal(args.rebound_pct),
                    reference_symbols=tuple(args.reference),
                    reference_rebound_pct=Decimal(
                        args.reference_rebound_pct
                    ),
                ),
            )
            _run_dip_watch(
                watcher,
                args.symbol,
                interval_seconds=args.interval,
                max_cycles=args.max_cycles,
            )
            return
        if args.command == "orders":
            store = SnapshotStore(args.db or settings.database_path)
            _print(store.list_broker_orders(limit=args.limit))
            return
        if args.command == "fills":
            store = SnapshotStore(args.db or settings.database_path)
            _print(store.list_fills(limit=args.limit))
            return
        if args.command == "reconcile-orders":
            store = SnapshotStore(args.db or settings.database_path)
            service = OrderReconciliationService(client, store)
            _print(
                service.reconcile_overseas(
                    exchange=args.exchange,
                    start_date=args.start_date,
                    end_date=args.end_date,
                    days=args.days,
                )
            )
            return
        if args.command == "submit-order":
            store = SnapshotStore(args.db or settings.database_path)
            gateway = KISGateway(settings, store)
            _print(
                gateway.submit_order(
                    order_id=args.order_id,
                    client=client,
                    execute=args.execute,
                )
            )
            return
        if args.command == "cancel-order":
            store = SnapshotStore(args.db or settings.database_path)
            gateway = KISGateway(settings, store)
            _print(
                gateway.cancel_order(
                    order_id=args.order_id,
                    client=client,
                    execute=args.execute,
                    quantity=args.quantity,
                )
            )
            return
        if args.command == "void-order":
            store = SnapshotStore(args.db or settings.database_path)
            gateway = KISGateway(settings, store)
            _print(
                gateway.void_order(
                    order_id=args.order_id,
                    reason=args.reason,
                    confirm_not_placed=args.confirm_not_placed,
                )
            )
            return
        if args.command == "worker":
            store = SnapshotStore(args.db or settings.database_path)
            worker = OperationsWorker(
                client,
                settings,
                store,
                options=WorkerOptions(
                    interval_seconds=args.interval,
                    exchange=args.exchange,
                    days=args.days,
                    sync_account=args.account_sync,
                    account_symbol=args.account_symbol,
                    quote_exchange=args.quote_exchange,
                    currency=args.currency,
                    max_cycles=args.max_cycles,
                    continue_on_error=not args.stop_on_error,
                ),
            )
            _print(worker.run())
            return
        if args.command == "order":
            order_type = "market" if args.market else "limit"
            if not args.execute:
                _print(
                    {
                        "mode": "dry-run",
                        "side": args.side,
                        "symbol": args.symbol,
                        "quantity": args.quantity,
                        "price": args.price,
                        "order_type": order_type,
                        "message": "--execute가 없어 주문을 전송하지 않았습니다.",
                    }
                )
                return

            settings.require_order_permission(execute=True)
            estimated_price = (
                client.current_price(args.symbol) if args.market else args.price
            )
            estimated_amount = estimated_price * args.quantity
            if estimated_amount > settings.max_order_amount:
                raise ValueError(
                    f"추정 주문금액 {estimated_amount:,}원이 주문 한도 "
                    f"{settings.max_order_amount:,}원을 초과합니다."
                )
            _print(
                client.order(
                    args.side,
                    args.symbol,
                    args.quantity,
                    args.price,
                    order_type,
                    args.exchange,
                )
            )
            return
        if args.command == "auto":
            strategy = ThresholdStrategy(
                symbol=args.symbol,
                quantity=args.quantity,
                buy_below=args.buy_below,
                sell_above=args.sell_above,
            )
            trader = AutoTrader(client, settings)
            _print(
                trader.run(
                    strategy,
                    interval_seconds=args.interval,
                    max_orders=args.max_orders,
                    execute=args.execute,
                )
            )
    except (KISError, ValueError, PermissionError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\n작업을 중지했습니다.", file=sys.stderr)
        else:
            print(f"오류: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
