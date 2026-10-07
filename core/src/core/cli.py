from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from typing import Any

from ki.client import KISError
from toss.client import TossError

from core.adapters import KISBrokerAdapter, TossBrokerAdapter
from core.audit import record_core_error, record_core_event
from core.config import Settings
from core.models import BrokerCapabilities, UnifiedOrderRequest
from core.killswitch import KillSwitch
from core.notify import DiscordNotifier, NotificationError
from core.notify_worker import (
    ConsoleChannel,
    DiscordChannel,
    NotificationWorker,
)
from core.agent import SpoolAgent
from core.outbox import OutboxStore
from core.quota import QuotaLedger, QuotaLimits
from core.relay import OutboxRelay
from core.strategy import StrategyStore
from core.strategy_worker import StrategyWorker
from core.portfolio import build_snapshot
from core.risk import (
    PortfolioPosition,
    PortfolioRiskLimits,
    PortfolioSnapshot,
    evaluate_portfolio_risk,
)
from core.service import BrokerService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="core",
        description="KIS와 토스증권 통합 관제 CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("brokers", help="브로커 기능 비교")

    quote = subparsers.add_parser("quote", help="통합 현재가 조회")
    quote.add_argument("symbol")
    quote.add_argument("--broker", choices=["kis", "toss", "both"], default="both")

    compare = subparsers.add_parser("compare-quotes", help="양쪽 현재가 괴리 비교")
    compare.add_argument("symbol")

    notify_test = subparsers.add_parser(
        "notify-test",
        help="Discord 웹훅 테스트 메시지 전송",
    )
    notify_test.add_argument("message")
    notify_test.add_argument("--dry-run", action="store_true")

    subparsers.add_parser(
        "positions",
        help="KIS·Toss 보유 포지션·현금·예약 노출 통합 조회",
    )

    kill_switch = subparsers.add_parser(
        "kill-switch",
        help="중앙 Kill Switch 켜기/끄기/상태",
    )
    kill_switch.add_argument("action", choices=["on", "off", "status"])
    kill_switch.add_argument("--reason", default="", help="켤 때 남길 사유")

    portfolio_risk = subparsers.add_parser(
        "portfolio-risk",
        help="현금·포지션 스냅샷으로 중앙 리스크 한도 평가",
    )
    portfolio_risk.add_argument(
        "--live",
        action="store_true",
        help="브로커 API로 현금·포지션·예약 노출을 자동 수집",
    )
    portfolio_risk.add_argument("--cash", help="USD 현금 (수동 모드)")
    portfolio_risk.add_argument(
        "--position",
        action="append",
        default=[],
        metavar="SYMBOL:QTY:PRICE[:leveraged]",
        help="포지션. 예: MUU:1:1122:leveraged",
    )
    portfolio_risk.add_argument(
        "--pending-buy",
        default="0",
        help="대기 매수 금액. 반영 후 현금비중을 계산합니다.",
    )

    subparsers.add_parser(
        "outbox-sync",
        help="브로커 원장 이벤트를 공통 아웃박스로 복제",
    )

    outbox = subparsers.add_parser("outbox", help="공통 이벤트 아웃박스 조회")
    outbox.add_argument(
        "--category",
        choices=["ORDER", "FILL", "RISK", "SIGNAL", "STRATEGY", "SYSTEM"],
    )
    outbox.add_argument("--limit", type=int, default=20)
    outbox.add_argument(
        "--pending",
        action="store_true",
        help="전송 대기 delivery만 조회",
    )

    notify_worker = subparsers.add_parser(
        "notify-worker",
        help="아웃박스 PENDING delivery를 소비해 알림 전송",
    )
    notify_worker.add_argument(
        "--interval",
        type=float,
        help="초 단위 반복 주기. 생략하면 한 번만 실행",
    )
    notify_worker.add_argument(
        "--sync",
        action="store_true",
        help="전송 전에 브로커 이벤트를 아웃박스로 복제",
    )

    strategy_worker = subparsers.add_parser(
        "strategy-worker",
        help="매수 후보를 LLM 게이트웨이로 심사",
    )
    strategy_worker.add_argument(
        "--interval",
        type=float,
        help="초 단위 반복 주기. 생략하면 한 번만 실행",
    )
    strategy_worker.add_argument(
        "--broker",
        default=None,
        help="기준 현재가를 조회할 브로커. 생략하면 CORE_DEFAULT_BROKER",
    )
    strategy_worker.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="한 주기에 심사할 최대 후보 수",
    )

    strategy_stats = subparsers.add_parser(
        "strategy-stats",
        help="심사 거절 사유 분포와 호출 예산 사용량",
    )
    strategy_stats.add_argument(
        "--limit",
        type=int,
        default=20,
        help="함께 보여줄 최근 STRATEGY 이벤트 수",
    )

    orderbook = subparsers.add_parser("orderbook", help="정규화된 호가 조회")
    orderbook.add_argument("symbol")
    orderbook.add_argument("--broker", choices=["toss"], default="toss")

    commissions = subparsers.add_parser(
        "commissions",
        help="브로커 계좌 수수료 조회",
    )
    commissions.add_argument("--broker", choices=["toss"], default="toss")

    intent = subparsers.add_parser(
        "intent",
        help="선택 브로커에 주문 후보 생성",
    )
    intent.add_argument("side", choices=["buy", "sell"])
    intent.add_argument("symbol")
    intent.add_argument("quantity", type=int)
    intent.add_argument("--broker", choices=["kis", "toss"])
    intent.add_argument("--price", required=True)
    intent.add_argument("--currency", choices=["USD", "KRW"], default="USD")
    intent.add_argument("--source", default="core-cli")
    intent.add_argument(
        "--skip-risk",
        action="store_true",
        help="주문 후보 생성 전 중앙 리스크 자동 평가 생략",
    )

    orders = subparsers.add_parser("orders", help="브로커별 로컬 주문 원장 조회")
    orders.add_argument("--broker", choices=["kis", "toss", "both"], default="both")
    orders.add_argument("--limit", type=int, default=20)

    submit = subparsers.add_parser("submit", help="브로커 주문 후보 실전 제출")
    submit.add_argument("broker", choices=["kis", "toss"])
    submit.add_argument("order_id", type=int)
    submit.add_argument("--execute", action="store_true")

    cancel = subparsers.add_parser("cancel", help="브로커 주문 취소")
    cancel.add_argument("broker", choices=["kis", "toss"])
    cancel.add_argument("order_id", type=int)
    cancel.add_argument("--execute", action="store_true")
    return parser


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


_ORDER_COMMANDS = {"intent", "submit", "cancel"}


def _record_order_event(
    settings: Settings,
    *,
    category: str = "ORDER",
    **kwargs: Any,
) -> None:
    """주문 관련 Core 이벤트를 기록한다. 기록 실패가 주문을 막지 않는다."""
    try:
        record_core_event(
            OutboxStore(settings.core_database_path),
            category=category,
            channels=settings.notify_channels,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"경고: 이벤트 기록 실패: {exc}", file=sys.stderr)


def _record_command_error(command: str, error: str) -> None:
    try:
        settings = Settings.from_env()
        record_core_error(
            OutboxStore(settings.core_database_path),
            command=command,
            error=error,
            channels=settings.notify_channels,
        )
    except Exception:  # noqa: BLE001 - 오류 기록 실패는 무시한다
        pass


def _channel_handlers(settings: Settings) -> dict[str, Any]:
    handlers: dict[str, Any] = {}
    for channel in settings.notify_channels:
        if channel == "console":
            handlers[channel] = ConsoleChannel()
        elif channel == "discord":
            handlers[channel] = DiscordChannel(
                DiscordNotifier(
                    settings.discord_webhook_url,
                    username=settings.discord_username,
                )
            )
    return handlers


def _quota_ledger(settings: Settings) -> QuotaLedger:
    return QuotaLedger(
        settings.core_database_path,
        QuotaLimits(
            max_calls_per_window=settings.strategy_max_calls_per_window,
            window_hours=settings.strategy_window_hours,
            max_calls_per_day=settings.strategy_max_calls_per_day,
            min_interval_seconds=settings.strategy_min_interval_seconds,
            reserve_calls=settings.strategy_reserve_calls,
        ),
    )


def _strategy_worker(
    settings: Settings,
    service: BrokerService,
    broker: str,
    *,
    batch_size: int,
) -> StrategyWorker:
    """심사 워커를 조립한다.

    기준 현재가는 브로커에서 직접 조회한다. 에이전트가 준 가격으로
    검증하면 자기참조가 되므로 quote_provider를 반드시 주입한다.
    """
    prompt_path = settings.strategy_prompt_path
    prompt_text = (
        prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    )
    agent = SpoolAgent(
        spool_dir=settings.strategy_spool_path,
        role=settings.strategy_role,
        cli=settings.strategy_cli,
        model=settings.strategy_model,
        timeout_seconds=settings.strategy_timeout_seconds,
    )
    return StrategyWorker(
        OutboxStore(settings.core_database_path),
        StrategyStore(settings.core_database_path),
        agent,
        _quota_ledger(settings),
        quote_provider=lambda symbol: service.quotes(symbol, broker)[0].price,
        kill_switch=KillSwitch(settings.kill_switch_path),
        prompt_text=prompt_text,
        prompt_version=settings.strategy_prompt_version,
        cache_ttl_minutes=settings.strategy_cache_ttl_minutes,
        band_pct=settings.strategy_band_pct,
        batch_size=batch_size,
        channels=settings.notify_channels,
    )


def _service(
    settings: Settings,
    brokers: set[str],
    *,
    local_only: bool = False,
) -> BrokerService:
    adapters = {}
    if "kis" in brokers:
        adapters["kis"] = (
            KISBrokerAdapter.from_local_store(settings)
            if local_only
            else KISBrokerAdapter.from_core(settings)
        )
    if "toss" in brokers:
        adapters["toss"] = (
            TossBrokerAdapter.from_local_store(settings)
            if local_only
            else TossBrokerAdapter.from_core(settings)
        )
    return BrokerService(adapters)


def _parse_position(value: str) -> PortfolioPosition:
    parts = value.split(":")
    if len(parts) not in {3, 4}:
        raise ValueError(
            "포지션 형식은 SYMBOL:QTY:PRICE 또는 "
            "SYMBOL:QTY:PRICE:leveraged 입니다."
        )
    symbol, quantity, price = parts[:3]
    flag = parts[3].strip().lower() if len(parts) == 4 else ""
    if not symbol.strip():
        raise ValueError("포지션 symbol이 필요합니다.")
    if flag and flag not in {"leveraged", "lev", "2x", "3x"}:
        raise ValueError("포지션 4번째 값은 leveraged/lev/2x/3x만 허용합니다.")
    return PortfolioPosition(
        symbol=symbol.strip().upper(),
        quantity=Decimal(quantity),
        price=Decimal(price),
        leveraged=bool(flag),
    )


def _portfolio_limits(settings: Settings) -> PortfolioRiskLimits:
    return PortfolioRiskLimits(
        min_cash_pct=settings.min_cash_pct,
        max_leveraged_exposure_pct=settings.max_leveraged_exposure_pct,
        max_single_position_pct=settings.max_single_position_pct,
        max_daily_new_buy_usd=settings.max_daily_new_buy_usd,
        panic_buy_block_minutes=settings.panic_buy_block_minutes,
    )


def _live_snapshot(
    settings: Settings,
    *,
    extra_pending_buy: Decimal = Decimal("0"),
) -> tuple[Any, dict[str, Any]]:
    service = _service(settings, {"kis", "toss"})
    portfolios = service.collect_broker_portfolios()
    return build_snapshot(
        portfolios,
        leveraged_symbols=frozenset(settings.leveraged_symbols),
        extra_pending_buy=extra_pending_buy,
    )


def _intent_risk_gate(
    settings: Settings,
    request: UnifiedOrderRequest,
) -> dict[str, Any]:
    """주문 후보 생성 전 통합 리스크를 평가한다.

    브로커 수집 실패는 보수적으로 BLOCK 사유에 포함한다.
    """
    amount = request.price * request.quantity
    snapshot, detail = _live_snapshot(settings, extra_pending_buy=amount)
    result = evaluate_portfolio_risk(snapshot, _portfolio_limits(settings))
    for broker in detail["brokers"]:
        if "error" in broker:
            result["reasons"].append(
                f"{broker['broker']} 포지션 수집 실패: {broker['error']}"
            )
            result["decision"] = "BLOCK"
    result["intent_amount"] = format(amount, "f")
    result["live_detail"] = detail
    return result


def _selected_brokers(args: argparse.Namespace, settings: Settings) -> set[str]:
    if args.command == "compare-quotes":
        return {"kis", "toss"}
    if args.command == "positions":
        return {"kis", "toss"}
    if args.command == "notify-test":
        return set()
    if args.command == "portfolio-risk":
        return set()
    if args.command in {"outbox-sync", "outbox", "notify-worker", "kill-switch"}:
        return set()
    if args.command == "strategy-stats":
        return set()
    if args.command == "strategy-worker":
        return {args.broker or settings.default_broker}
    if args.command in {"quote", "orders"}:
        return (
            {"kis", "toss"}
            if args.broker == "both"
            else {args.broker}
        )
    if args.command == "intent":
        return {args.broker or settings.default_broker}
    return {args.broker}


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.command == "brokers":
            _print(
                [
                    BrokerCapabilities(
                        broker="kis",
                        rest_quotes=True,
                        rest_orderbook=False,
                        websocket_orderbook=True,
                        commissions=False,
                        orders=True,
                        modify_orders=False,
                        cancel_orders=True,
                    ).to_dict(),
                    BrokerCapabilities(
                        broker="toss",
                        rest_quotes=True,
                        rest_orderbook=True,
                        websocket_orderbook=False,
                        commissions=True,
                        orders=True,
                        modify_orders=True,
                        cancel_orders=True,
                    ).to_dict(),
                ]
            )
            return
        if args.command == "portfolio-risk":
            if args.live:
                snapshot, detail = _live_snapshot(
                    settings,
                    extra_pending_buy=Decimal(args.pending_buy),
                )
                result = evaluate_portfolio_risk(
                    snapshot,
                    _portfolio_limits(settings),
                )
                result["live_detail"] = detail
                _print(result)
                return
            if args.cash is None:
                raise ValueError(
                    "수동 모드에는 --cash가 필요합니다. "
                    "자동 수집은 --live를 사용합니다."
                )
            snapshot = PortfolioSnapshot(
                cash=Decimal(args.cash),
                positions=tuple(
                    _parse_position(position)
                    for position in args.position
                ),
                pending_buy_amount=Decimal(args.pending_buy),
            )
            _print(
                evaluate_portfolio_risk(
                    snapshot,
                    _portfolio_limits(settings),
                )
            )
            return
        if args.command == "kill-switch":
            switch = KillSwitch(settings.kill_switch_path)
            if args.action == "status":
                _print(switch.status())
                return
            if args.action == "on":
                result = switch.activate(args.reason)
                _record_order_event(
                    settings,
                    category="SYSTEM",
                    severity="critical",
                    event_type="KILL_SWITCH_ON",
                    summary=(
                        "[Kill Switch] 주문 차단 활성화"
                        + (f": {args.reason}" if args.reason else "")
                    ),
                    payload=result,
                )
            else:
                result = switch.deactivate()
                _record_order_event(
                    settings,
                    category="SYSTEM",
                    severity="warning",
                    event_type="KILL_SWITCH_OFF",
                    summary="[Kill Switch] 주문 차단 해제",
                    payload=result,
                )
            _print(result)
            return
        if args.command in {"intent", "submit"}:
            KillSwitch(settings.kill_switch_path).assert_allows_orders()
        if args.command == "outbox-sync":
            relay = OutboxRelay(
                OutboxStore(settings.core_database_path),
                kis_database_path=settings.kis_database_path,
                toss_database_path=settings.toss_database_path,
                channels=settings.notify_channels,
            )
            _print(relay.sync())
            return
        if args.command == "outbox":
            store = OutboxStore(settings.core_database_path)
            if args.pending:
                _print(store.list_pending_deliveries(limit=args.limit))
                return
            _print(
                store.list_events(limit=args.limit, category=args.category)
            )
            return
        if args.command == "notify-worker":
            store = OutboxStore(settings.core_database_path)
            relay = (
                OutboxRelay(
                    store,
                    kis_database_path=settings.kis_database_path,
                    toss_database_path=settings.toss_database_path,
                    channels=settings.notify_channels,
                )
                if args.sync
                else None
            )
            worker = NotificationWorker(
                store,
                _channel_handlers(settings),
                relay=relay,
            )
            if args.interval is None:
                _print(worker.run_once())
                return
            worker.run_forever(interval_seconds=args.interval)
            return
        if args.command == "strategy-stats":
            store = OutboxStore(settings.core_database_path)
            _print(
                {
                    "reject_stats": StrategyStore(
                        settings.core_database_path
                    ).reject_stats(),
                    "quota": _quota_ledger(settings).summary(),
                    "recent_events": store.list_events(
                        limit=args.limit,
                        category="STRATEGY",
                    ),
                }
            )
            return
        if args.command == "notify-test":
            notifier = DiscordNotifier(
                settings.discord_webhook_url,
                username=settings.discord_username,
                timeout_seconds=settings.toss_timeout_seconds,
            )
            _print(notifier.send(args.message, dry_run=args.dry_run))
            return
        service = _service(
            settings,
            _selected_brokers(args, settings),
            local_only=args.command == "orders",
        )
        if args.command == "strategy-worker":
            worker = _strategy_worker(
                settings,
                service,
                args.broker or settings.default_broker,
                batch_size=args.batch_size,
            )
            if args.interval is None:
                _print(worker.run_once())
                return
            worker.run_forever(interval_seconds=args.interval)
            return
        if args.command == "positions":
            snapshot, detail = build_snapshot(
                service.collect_broker_portfolios(),
                leveraged_symbols=frozenset(settings.leveraged_symbols),
            )
            _print(
                {
                    "brokers": detail["brokers"],
                    "skipped": detail["skipped"],
                    "cash": format(snapshot.cash, "f"),
                    "pending_buy_amount": format(
                        snapshot.pending_buy_amount, "f"
                    ),
                    "position_value": format(snapshot.position_value, "f"),
                    "total_equity": format(snapshot.total_equity, "f"),
                    "positions": [
                        position.to_dict(snapshot.total_equity)
                        for position in snapshot.positions
                    ],
                }
            )
            return
        if args.command == "quote":
            _print(
                [
                    quote.to_dict()
                    for quote in service.quotes(args.symbol, args.broker)
                ]
            )
            return
        if args.command == "compare-quotes":
            _print(
                service.compare_quotes(
                    args.symbol,
                    max_divergence_pct=settings.max_quote_divergence_pct,
                )
            )
            return
        if args.command == "orderbook":
            adapter = service.adapters[args.broker]
            _print(adapter.orderbook(args.symbol).to_dict())
            return
        if args.command == "commissions":
            _print(service.adapters[args.broker].commissions())
            return
        if args.command == "intent":
            request = UnifiedOrderRequest(
                broker=args.broker or settings.default_broker,
                side=args.side,
                symbol=args.symbol.upper(),
                quantity=args.quantity,
                price=Decimal(args.price),
                currency=args.currency,
                source=args.source,
            )
            if (
                request.side == "buy"
                and request.currency == "USD"
                and not args.skip_risk
            ):
                risk_result = _intent_risk_gate(settings, request)
                if risk_result["decision"] != "ALLOW":
                    _record_order_event(
                        settings,
                        category="RISK",
                        severity="warning",
                        event_type=f"INTENT_RISK_{risk_result['decision']}",
                        summary=(
                            f"[Core 리스크] {request.symbol} BUY "
                            f"{request.quantity}주 "
                            f"{risk_result['decision']}: "
                            + "; ".join(
                                risk_result["reasons"]
                                + risk_result["warnings"]
                            )
                        )[:500],
                        broker=request.broker,
                        symbol=request.symbol,
                        payload={
                            "decision": risk_result["decision"],
                            "reasons": risk_result["reasons"],
                            "warnings": risk_result["warnings"],
                        },
                    )
                if risk_result["decision"] == "BLOCK":
                    _print(risk_result)
                    raise ValueError(
                        "중앙 리스크 평가에서 차단되었습니다. "
                        "사유 확인 후 필요하면 --skip-risk로 우회합니다."
                    )
            result = service.create_order_candidate(request)
            _record_order_event(
                settings,
                event_type="INTENT_CREATED",
                summary=(
                    f"[Core] {request.broker} {request.symbol} "
                    f"{request.side.upper()} {request.quantity}주 "
                    f"후보 생성 ({result.get('status')})"
                ),
                broker=request.broker,
                symbol=request.symbol,
                order_ref=str(result.get("order_id") or ""),
                payload={"status": result.get("status")},
            )
            _print(result)
            return
        if args.command == "orders":
            _print(service.list_orders(args.broker, limit=args.limit))
            return
        if args.command in {"submit", "cancel"}:
            action = (
                service.submit_order
                if args.command == "submit"
                else service.cancel_order
            )
            result = action(args.broker, args.order_id, execute=args.execute)
            mode = "실행" if args.execute else "dry-run"
            mode_token = "EXECUTE" if args.execute else "DRY_RUN"
            label = "제출" if args.command == "submit" else "취소"
            _record_order_event(
                settings,
                event_type=f"{args.command.upper()}_{mode_token}",
                summary=(
                    f"[Core] {args.broker} 주문 {args.order_id} "
                    f"{label} {mode} ({result.get('status')})"
                ),
                broker=args.broker,
                order_ref=str(args.order_id),
                payload={"status": result.get("status")},
            )
            _print(result)
            return
    except (
        KISError,
        TossError,
        ValueError,
        PermissionError,
        NotificationError,
    ) as exc:
        if getattr(args, "command", None) in _ORDER_COMMANDS:
            _record_command_error(args.command, str(exc))
        print(f"오류: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
