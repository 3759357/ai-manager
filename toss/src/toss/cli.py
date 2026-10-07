from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from toss.client import TossClient, TossError
from toss.config import Settings
from toss.gateway import TossGateway
from toss.storage import OrderStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toss",
        description="토스증권 Open API 조회 및 안전 주문 CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth-check", help="토큰 발급/캐시 상태 확인")
    subparsers.add_parser("accounts", help="계좌 목록 조회")

    price = subparsers.add_parser("price", help="현재가 조회")
    price.add_argument("symbols", nargs="+")

    orderbook = subparsers.add_parser("orderbook", help="호가 조회")
    orderbook.add_argument("symbol")

    trades = subparsers.add_parser("trades", help="최근 체결 조회")
    trades.add_argument("symbol")
    trades.add_argument("--limit", type=int, default=20)

    candles = subparsers.add_parser("candles", help="분봉 또는 일봉 조회")
    candles.add_argument("symbol")
    candles.add_argument("--interval", choices=["1m", "1d"], default="1m")
    candles.add_argument("--count", type=int, default=100)
    candles.add_argument("--before")
    candles.add_argument("--unadjusted", action="store_true")

    holdings = subparsers.add_parser("holdings", help="보유 주식 조회")
    holdings.add_argument("--symbol")

    subparsers.add_parser("commissions", help="계좌별 수수료 조회")

    buying_power = subparsers.add_parser(
        "buying-power",
        help="현금 매수 가능 금액 조회",
    )
    buying_power.add_argument("currency", choices=["KRW", "USD", "krw", "usd"])

    sellable = subparsers.add_parser(
        "sellable",
        help="판매 가능 수량 조회",
    )
    sellable.add_argument("symbol")

    toss_orders = subparsers.add_parser("toss-orders", help="토스 주문 조회")
    toss_orders.add_argument("--status", choices=["OPEN", "CLOSED"], default="OPEN")
    toss_orders.add_argument("--symbol")
    toss_orders.add_argument("--limit", type=int, default=20)

    intent = subparsers.add_parser(
        "gateway-intent",
        help="토스 지정가 주문 후보 생성 및 리스크 검토",
    )
    intent.add_argument("side", choices=["buy", "sell"])
    intent.add_argument("symbol")
    intent.add_argument("quantity", type=int)
    intent.add_argument("--price", required=True)
    intent.add_argument("--currency", choices=["KRW", "USD"], required=True)
    intent.add_argument("--source", default="cli")
    intent.add_argument("--db", type=Path)

    orders = subparsers.add_parser("orders", help="로컬 주문 원장 조회")
    orders.add_argument("--limit", type=int, default=20)
    orders.add_argument("--db", type=Path)

    submit = subparsers.add_parser("submit-order", help="주문 후보 실전 제출")
    submit.add_argument("order_id", type=int)
    submit.add_argument("--execute", action="store_true")
    submit.add_argument("--db", type=Path)

    modify = subparsers.add_parser("modify-order", help="제출된 지정가 주문 정정")
    modify.add_argument("order_id", type=int)
    modify.add_argument("--price", required=True)
    modify.add_argument("--execute", action="store_true")
    modify.add_argument("--db", type=Path)

    cancel = subparsers.add_parser("cancel-order", help="제출된 주문 취소")
    cancel.add_argument("order_id", type=int)
    cancel.add_argument("--execute", action="store_true")
    cancel.add_argument("--db", type=Path)

    sync_order = subparsers.add_parser(
        "sync-order",
        help="토스 주문 상세를 로컬 원장에 대사",
    )
    sync_order.add_argument("order_id", type=int)
    sync_order.add_argument("--db", type=Path)

    resolve = subparsers.add_parser(
        "resolve-order",
        help="결과 불명 주문을 토스 조회 후 '미생성'으로 확정",
    )
    resolve.add_argument("order_id", type=int)
    resolve.add_argument("--not-placed", action="store_true", required=True)
    resolve.add_argument("--db", type=Path)
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        client = TossClient(settings)

        if args.command == "auth-check":
            client.access_token()
            _print(
                {
                    "ok": True,
                    "message": "토큰 발급 또는 캐시 재사용에 성공했습니다.",
                    "token_printed": False,
                }
            )
            return
        if args.command == "accounts":
            _print(client.accounts())
            return
        if args.command == "price":
            _print(client.prices(args.symbols))
            return
        if args.command == "orderbook":
            _print(client.orderbook(args.symbol))
            return
        if args.command == "trades":
            _print(client.trades(args.symbol, args.limit))
            return
        if args.command == "candles":
            _print(
                client.candles(
                    args.symbol,
                    interval=args.interval,
                    count=args.count,
                    before=args.before,
                    adjusted=not args.unadjusted,
                )
            )
            return
        if args.command == "holdings":
            _print(client.holdings(args.symbol))
            return
        if args.command == "commissions":
            _print(client.commissions())
            return
        if args.command == "buying-power":
            _print(client.buying_power(args.currency))
            return
        if args.command == "sellable":
            _print(client.sellable_quantity(args.symbol))
            return
        if args.command == "toss-orders":
            _print(
                client.orders(
                    status=args.status,
                    symbol=args.symbol,
                    limit=args.limit,
                )
            )
            return

        store = OrderStore(args.db or settings.database_path)
        gateway = TossGateway(settings, client, store)
        if args.command == "gateway-intent":
            _print(
                gateway.create_order_candidate(
                    side=args.side,
                    symbol=args.symbol,
                    quantity=args.quantity,
                    price=args.price,
                    currency=args.currency,
                    source=args.source,
                )
            )
            return
        if args.command == "orders":
            _print(store.list_orders(limit=args.limit))
            return
        if args.command == "submit-order":
            _print(gateway.submit_order(args.order_id, execute=args.execute))
            return
        if args.command == "modify-order":
            _print(
                gateway.modify_order(
                    args.order_id,
                    price=args.price,
                    execute=args.execute,
                )
            )
            return
        if args.command == "cancel-order":
            _print(gateway.cancel_order(args.order_id, execute=args.execute))
            return
        if args.command == "sync-order":
            _print(gateway.sync_order(args.order_id))
            return
        if args.command == "resolve-order":
            _print(gateway.resolve_not_placed(args.order_id))
            return
    except (TossError, ValueError, PermissionError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\n작업을 중지했습니다.", file=sys.stderr)
        elif isinstance(exc, TossError):
            suffix = f" [code={exc.code}]" if exc.code else ""
            request = f" [request_id={exc.request_id}]" if exc.request_id else ""
            print(f"오류: {exc}{suffix}{request}", file=sys.stderr)
        else:
            print(f"오류: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
