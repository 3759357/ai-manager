"""리스크관리부: 결정론적 주문 의도 검증과 경보.

LLM을 쓰지 않는다. 주문 의도(OrderIntent)·계좌 상태(AccountState)·호가(Quote)·정책(Policy)을
받아 허용 여부와 차단 사유를 돌려준다. 주문을 만들거나 보내지 않는다.

- 한도는 계좌 → 장부 → 전략 → 종목 → 주문 순으로 모두 검사하고, 하나라도 걸리면 차단한다.
- 매도(위험 축소)는 호가·보유 수량·Kill Switch만 확인하고 비중 한도로 막지 않는다.
- 모르는 값(낙폭·비중 상한·트레이딩북 NAV 미제공, 오래된 스냅샷)은 허용이 아니라 매수 차단이다.
- 정책은 버전이 있는 TOML 파일에서만 읽는다. 키가 빠지거나 남으면 로드를 거절한다.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from core.events import OutboxEvent

HUNDRED = Decimal(100)
BOOKS = frozenset({"invest", "trading"})
SIDES = frozenset({"buy", "sell"})
# 시장 경보의 하루 기준. 미국 장 기준일이 바뀌는 시각에는 장이 닫혀 있으므로 고정 UTC−5로 충분하다.
MARKET_DAY_TZ = timezone(timedelta(hours=-5))

_SCHEMA: dict[str, dict[str, str]] = {
    "account": {
        "min_cash_pct": "pct",
        "emergency_drawdown_pct": "neg",
        "max_account_snapshot_age_seconds": "pos",
    },
    "order": {
        "limit_only": "bool",
        "max_quote_age_seconds": "pos",
        "max_mid_deviation_pct": "pos",
        "max_order_usd": "pos",
        "max_daily_buy_usd": "pos",
    },
    "position": {
        "max_single_pct": "pct",
        "max_single_high_vol_pct": "pct",
        "high_vol_annual_pct": "pos",
        "max_single_before_earnings_pct": "pct",
        "max_leveraged_effective_pct": "pct",
    },
    "trading_book": {
        "max_trade_loss_pct": "pct",
        "strategy_daily_loss_pct": "neg",
        "book_daily_loss_pct": "neg",
        "strategy_monthly_loss_pct": "neg",
        "book_drawdown_pct": "neg",
        "max_hold_days": "int",
    },
    "alerts": {
        "market_drop_pct": "neglist",
        "account_daily_drop_pct": "neglist",
        "holding_move_pct": "pos",
        "heartbeat_stale_minutes": "pos",
    },
}


# 호가 시각이 현재보다 이만큼 넘게 미래면 신뢰하지 않는다.
QUOTE_CLOCK_SKEW = timedelta(seconds=5)


class PolicyError(ValueError):
    """정책 파일이 계약과 맞지 않는다."""


@dataclass(frozen=True)
class Policy:
    version: str
    account: Mapping[str, Any]
    order: Mapping[str, Any]
    position: Mapping[str, Any]
    trading_book: Mapping[str, Any]
    alerts: Mapping[str, Any]
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        def plain(value: Any) -> Any:
            if isinstance(value, Decimal):
                return format(value, "f")
            if isinstance(value, (list, tuple)):
                return [plain(v) for v in value]
            return value
        out: dict[str, Any] = {"version": self.version, "source": self.source}
        for section in _SCHEMA:
            out[section] = {k: plain(v) for k, v in getattr(self, section).items()}
        return out


def _number(section: str, key: str, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError(f"{section}.{key}는 숫자여야 합니다.")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - tomllib이 이미 숫자를 보장
        raise PolicyError(f"{section}.{key} 숫자 변환 실패") from exc


def _coerce(section: str, key: str, kind: str, value: Any) -> Any:
    if kind == "bool":
        if not isinstance(value, bool):
            raise PolicyError(f"{section}.{key}는 true/false여야 합니다.")
        return value
    if kind == "neglist":
        if not isinstance(value, list) or not value:
            raise PolicyError(f"{section}.{key}는 비어 있지 않은 목록이어야 합니다.")
        levels = [_number(section, key, v) for v in value]
        if any(v >= 0 for v in levels) or levels != sorted(levels, reverse=True) or len(set(levels)) != len(levels):
            raise PolicyError(f"{section}.{key}는 0보다 작고 점점 깊어지는 값이어야 합니다(예: [-3, -5]).")
        return tuple(levels)
    number = _number(section, key, value)
    if kind == "neg" and not number < 0:
        raise PolicyError(f"{section}.{key}는 음수여야 합니다.")
    if kind == "pos" and not number > 0:
        raise PolicyError(f"{section}.{key}는 0보다 커야 합니다.")
    if kind == "pct" and not 0 < number <= 100:
        raise PolicyError(f"{section}.{key}는 0 초과 100 이하 퍼센트여야 합니다.")
    if kind == "int":
        if number != number.to_integral_value() or number <= 0:
            raise PolicyError(f"{section}.{key}는 양의 정수여야 합니다.")
        return int(number)
    return number


def parse_policy(data: Mapping[str, Any], source: str = "") -> Policy:
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PolicyError("version이 필요합니다.")
    extra = set(data) - set(_SCHEMA) - {"version"}
    if extra:
        raise PolicyError(f"알 수 없는 정책 구역: {sorted(extra)}")
    sections: dict[str, dict[str, Any]] = {}
    for section, keys in _SCHEMA.items():
        raw = data.get(section)
        if not isinstance(raw, dict):
            raise PolicyError(f"[{section}] 구역이 필요합니다.")
        missing, unknown = set(keys) - set(raw), set(raw) - set(keys)
        if missing:
            raise PolicyError(f"[{section}]에 빠진 값: {sorted(missing)}")
        if unknown:
            raise PolicyError(f"[{section}]의 알 수 없는 값: {sorted(unknown)}")
        sections[section] = {k: _coerce(section, k, kind, raw[k]) for k, kind in keys.items()}
    pos = sections["position"]
    if not (pos["max_single_before_earnings_pct"] <= pos["max_single_pct"]
            and pos["max_single_high_vol_pct"] <= pos["max_single_pct"]):
        raise PolicyError("고변동·실적 전날 종목 한도는 기본 종목 한도보다 클 수 없습니다.")
    order = sections["order"]
    if order["max_order_usd"] > order["max_daily_buy_usd"]:
        raise PolicyError("주문당 한도는 일일 매수 한도보다 클 수 없습니다.")
    return Policy(version=version.strip(), source=source, **sections)


def load_policy(path: Path | str | None = None) -> Policy:
    """정책을 읽는다. 순서: 인자 → AI_MANAGER_POLICY_PATH → 패키지 기본 v1."""
    chosen = path or os.getenv("AI_MANAGER_POLICY_PATH")
    if chosen:
        p = Path(chosen)
        return parse_policy(tomllib.loads(p.read_text(encoding="utf-8")), source=str(p))
    text = resources.files("core").joinpath("policy/v1.toml").read_text(encoding="utf-8")
    return parse_policy(tomllib.loads(text), source="package:core/policy/v1.toml")


# ---------------------------------------------------------------- 입력 모델


@dataclass(frozen=True)
class OrderIntent:
    """프론트 부서가 만든 주문 의도. 수량·가격은 Decimal."""

    intent_id: str
    book: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal | None
    strategy: str | None = None
    stop_price: Decimal | None = None
    leverage: Decimal = Decimal(1)  # 레버리지 상품 배수. 일반 주식·ETF는 1

    @property
    def notional(self) -> Decimal:
        return self.quantity * (self.limit_price or Decimal(0))


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    as_of: datetime


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: Decimal
    price: Decimal
    book: str = "invest"
    leverage: Decimal = Decimal(1)

    @property
    def value(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class StrategyState:
    daily_pnl_pct: Decimal = Decimal(0)
    monthly_pnl_pct: Decimal = Decimal(0)
    halted: bool = False


@dataclass(frozen=True)
class AccountState:
    """USD 기준 계좌 상태. 모르는 값은 None으로 두면 매수를 막는다."""

    as_of: datetime
    nav: Decimal
    cash: Decimal
    holdings: tuple[Holding, ...] = ()
    complete: bool = True  # 모든 계좌 조회 성공
    pending_buy_usd: Decimal = Decimal(0)  # 미체결 매수 예약 금액
    bought_today_usd: Decimal = Decimal(0)
    drawdown_pct: Decimal | None = None  # 1년 롤링 고점 대비, 음수
    equity_cap_pct: Decimal | None = None  # 비중 규칙 v1 total_equity_cap
    trading_book_nav: Decimal | None = None
    trading_book_daily_pnl_pct: Decimal = Decimal(0)
    trading_book_drawdown_pct: Decimal = Decimal(0)
    strategies: Mapping[str, StrategyState] = field(default_factory=dict)
    high_vol_symbols: frozenset[str] = frozenset()
    earnings_tomorrow: frozenset[str] = frozenset()
    kill_switch_active: bool = False
    seen_intent_ids: frozenset[str] = frozenset()

    def value(self, *, symbol: str | None = None, book: str | None = None) -> Decimal:
        return sum((h.value for h in self.holdings
                    if (symbol is None or h.symbol == symbol) and (book is None or h.book == book)), Decimal(0))

    def quantity(self, symbol: str, book: str) -> Decimal:
        return sum((h.quantity for h in self.holdings if h.symbol == symbol and h.book == book), Decimal(0))


# ---------------------------------------------------------------- 판정


@dataclass(frozen=True)
class Check:
    code: str
    message: str


@dataclass(frozen=True)
class Decision:
    allowed: bool
    intent_id: str
    policy_version: str
    blocks: tuple[Check, ...]
    notional: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": "ALLOW" if self.allowed else "BLOCK",
            "intent_id": self.intent_id,
            "policy_version": self.policy_version,
            "notional_usd": format(self.notional.quantize(Decimal("0.01")), "f"),
            "blocks": [{"code": c.code, "message": c.message} for c in self.blocks],
        }


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    return part / whole * HUNDRED


def _f(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def evaluate(intent: OrderIntent, account: AccountState, quote: Quote, policy: Policy,
             now: datetime | None = None) -> Decision:
    now = now or datetime.now(timezone.utc)
    blocks: list[Check] = []

    def block(code: str, message: str) -> None:
        blocks.append(Check(code, message))

    def done() -> Decision:
        return Decision(not blocks, intent.intent_id, policy.version, tuple(blocks), intent.notional)

    # 형식 검사: 이상한 의도는 다른 판정 없이 바로 차단
    if intent.side not in SIDES or intent.book not in BOOKS or intent.quantity <= 0 or not intent.intent_id.strip() \
            or (intent.limit_price is not None and intent.limit_price <= 0) or intent.leverage < 1:
        block("invalid_intent", "주문 의도 형식이 잘못됐습니다(방향·장부·수량·가격·배수).")
        return done()
    if quote.symbol != intent.symbol:
        block("invalid_intent", f"호가 종목({quote.symbol})이 주문 종목({intent.symbol})과 다릅니다.")
        return done()

    # 전체 차단과 중복
    if account.kill_switch_active:
        block("kill_switch", "Kill Switch가 켜져 있습니다.")
    if intent.intent_id in account.seen_intent_ids:
        block("duplicate", "이미 처리한 주문 의도 ID입니다.")
    if intent.book == "invest" and intent.strategy:
        block("book_mismatch", "투자북 주문에는 전략 코드를 붙이지 않습니다.")
    if intent.book == "trading" and not intent.strategy:
        block("book_mismatch", "트레이딩북 주문에는 전략 코드가 필요합니다.")

    # 주문 계층: 호가 신선도·지정가·가격 범위 (매수·매도 공통)
    order = policy.order
    if not (0 < quote.bid <= quote.ask):
        block("bad_quote", "호가가 비정상입니다(매수1호가 ≤ 매도1호가가 아님).")
    elif now - quote.as_of > timedelta(seconds=float(order["max_quote_age_seconds"])):
        block("stale_quote", f"호가가 {order['max_quote_age_seconds']}초보다 오래됐습니다.")
    elif quote.as_of - now > QUOTE_CLOCK_SKEW:
        # 미래 시각 호가는 신선도를 증명하지 못한다(시계 오류·잘못된 입력).
        block("future_quote", "호가 시각이 현재보다 미래입니다.")
    if intent.limit_price is None:
        if order["limit_only"]:
            block("not_limit", "지정가 주문만 허용합니다.")
    elif 0 < quote.bid <= quote.ask:
        mid = (quote.bid + quote.ask) / 2
        if intent.side == "buy" and intent.limit_price > quote.ask:
            block("price_through_book", f"매수 지정가 {intent.limit_price}가 매도1호가 {quote.ask}를 넘습니다.")
        if intent.side == "sell" and intent.limit_price < quote.bid:
            block("price_through_book", f"매도 지정가 {intent.limit_price}가 매수1호가 {quote.bid}보다 낮습니다.")
        deviation = abs(_pct(intent.limit_price - mid, mid))
        if deviation > order["max_mid_deviation_pct"]:
            block("price_deviation", f"지정가가 호가 중간값에서 {_f(deviation)}% 벗어납니다.")

    if intent.side == "sell":
        stale = now - account.as_of > timedelta(seconds=float(policy.account["max_account_snapshot_age_seconds"]))
        held = account.quantity(intent.symbol, intent.book)
        if stale:
            block("stale_account", "계좌 스냅샷이 오래돼 보유 수량을 확인할 수 없습니다.")
        elif intent.quantity > held:
            block("oversell", f"{intent.book} 장부 보유 {held}주보다 많이 팔 수 없습니다(공매도 금지).")
        return done()

    if intent.limit_price is None:
        # 지정가가 없으면 주문 금액을 0으로 계산하게 되어 금액 한도가 모두 통과한다.
        # limit_only 정책과 별개로 매수는 금액을 알 수 있을 때만 판정한다.
        block("price_unknown", "매수 금액을 계산할 지정가가 없어 매수하지 않습니다.")
        return done()
    _check_buy(intent, account, policy, now, block)
    return done()


def _check_buy(intent: OrderIntent, account: AccountState, policy: Policy, now: datetime, block: Any) -> None:
    acct, order, pos, tb = policy.account, policy.order, policy.position, policy.trading_book
    notional = intent.notional

    # 계좌 계층
    if not account.complete or now - account.as_of > timedelta(seconds=float(acct["max_account_snapshot_age_seconds"])) \
            or account.nav <= 0:
        block("account_unknown", "계좌 상태를 신뢰할 수 없어 매수하지 않습니다(조회 실패·오래된 스냅샷).")
        return
    nav = account.nav
    if account.drawdown_pct is None:
        block("drawdown_unknown", "계좌 낙폭을 알 수 없어 매수하지 않습니다.")
    elif account.drawdown_pct <= acct["emergency_drawdown_pct"]:
        block("emergency_stop", f"계좌 낙폭 {_f(account.drawdown_pct)}%로 비상 정지 상태입니다.")
    cash_after = account.cash - account.pending_buy_usd - notional
    if _pct(cash_after, nav) < acct["min_cash_pct"]:
        block("cash_floor", f"매수 후 현금 {_f(_pct(cash_after, nav))}%가 하한 {acct['min_cash_pct']}%보다 낮습니다.")

    # 주문 계층: 금액
    if notional > order["max_order_usd"]:
        block("order_size", f"주문 금액 ${_f(notional)}가 주문당 한도 ${order['max_order_usd']}를 넘습니다.")
    daily = account.bought_today_usd + account.pending_buy_usd + notional
    if daily > order["max_daily_buy_usd"]:
        block("daily_buy", f"오늘 매수 합계 ${_f(daily)}가 일일 한도 ${order['max_daily_buy_usd']}를 넘습니다.")

    # 종목 계층: 두 장부 합산
    limit, why = pos["max_single_pct"], "기본"
    if intent.symbol in account.high_vol_symbols and pos["max_single_high_vol_pct"] < limit:
        limit, why = pos["max_single_high_vol_pct"], "고변동"
    if intent.symbol in account.earnings_tomorrow and pos["max_single_before_earnings_pct"] < limit:
        limit, why = pos["max_single_before_earnings_pct"], "실적 전날"
    weight = _pct(account.value(symbol=intent.symbol) + notional, nav)
    if weight > limit:
        block("position_limit", f"{intent.symbol} 비중 {_f(weight)}%가 {why} 한도 {limit}%를 넘습니다.")

    # 레버리지 상품: 트레이딩북에서만, 계좌 대비 실효 노출 한도
    if intent.leverage > 1:
        if intent.book != "trading":
            block("leverage_book", "레버리지 상품은 트레이딩북에서만 살 수 있습니다.")
        effective = sum((h.value * h.leverage for h in account.holdings if h.leverage > 1), Decimal(0))
        effective += notional * intent.leverage
        if _pct(effective, nav) > pos["max_leveraged_effective_pct"]:
            block("leveraged_exposure", f"레버리지 실효 노출 {_f(_pct(effective, nav))}%가 한도를 넘습니다.")

    if intent.book == "invest":
        # 장부 계층: 투자북 주식 비중은 비중 규칙 v1 상한 이내
        if account.equity_cap_pct is None:
            block("equity_cap_unknown", "비중 규칙 v1 주식 비중 상한이 없어 투자북 매수를 하지 않습니다.")
        else:
            equity = _pct(account.value(book="invest") + account.pending_buy_usd + notional, nav)
            if equity > account.equity_cap_pct:
                block("equity_cap", f"투자북 주식 비중 {_f(equity)}%가 상한 {_f(account.equity_cap_pct)}%를 넘습니다.")
        return

    # 트레이딩북: 장부·전략 손실 정지, 사전 손절, 1건 위험, 할당 자본
    book_nav = account.trading_book_nav
    if book_nav is None or book_nav <= 0:
        block("trading_book_unknown", "트레이딩북 NAV를 알 수 없어 매수하지 않습니다.")
        return
    if account.trading_book_drawdown_pct <= tb["book_drawdown_pct"]:
        block("book_drawdown", f"트레이딩북 낙폭 {_f(account.trading_book_drawdown_pct)}%로 데스크 전체 정지입니다.")
    if account.trading_book_daily_pnl_pct <= tb["book_daily_loss_pct"]:
        block("book_daily_loss", "트레이딩북 오늘 손실 한도에 도달했습니다.")
    state = account.strategies.get(intent.strategy or "", StrategyState())
    if state.halted:
        block("strategy_halted", f"전략 {intent.strategy}는 정지 상태입니다(재개는 사용자 결정).")
    if state.daily_pnl_pct <= tb["strategy_daily_loss_pct"]:
        block("strategy_daily_loss", f"전략 {intent.strategy}의 오늘 손실 한도에 도달했습니다.")
    if state.monthly_pnl_pct <= tb["strategy_monthly_loss_pct"]:
        block("strategy_monthly_loss", f"전략 {intent.strategy}의 월 손실 한도에 도달했습니다.")
    if intent.stop_price is None or intent.limit_price is None or not 0 < intent.stop_price < intent.limit_price:
        block("stop_required", "트레이딩북 매수에는 진입가보다 낮은 손절가가 필요합니다.")
    else:
        risk = (intent.limit_price - intent.stop_price) * intent.quantity
        max_risk = book_nav * tb["max_trade_loss_pct"] / HUNDRED
        if risk > max_risk:
            block("trade_risk", f"손절 시 손실 ${_f(risk)}가 1건 한도 ${_f(max_risk)}를 넘습니다.")
    if account.value(book="trading") + notional > book_nav:
        block("book_capital", "트레이딩북 할당 자본을 넘습니다.")


def size_trade(entry: Decimal, stop: Decimal, trading_book_nav: Decimal, policy: Policy, *,
               step: Decimal = Decimal(1)) -> Decimal:
    """손절 거리로 트레이딩북 1건 최대 손실 한도에 맞는 수량을 계산한다. 전략이 수량을 정하지 않는다."""
    if not 0 < stop < entry or trading_book_nav <= 0 or step <= 0:
        raise ValueError("진입가 > 손절가 > 0, 트레이딩북 NAV > 0이어야 합니다.")
    max_risk = trading_book_nav * policy.trading_book["max_trade_loss_pct"] / HUNDRED
    units = (max_risk / (entry - stop) / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


# ---------------------------------------------------------------- 경보


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: str
    message: str
    dedup_key: str
    symbol: str | None = None
    value_pct: Decimal | None = None

    def to_event(self, now: datetime) -> OutboxEvent:
        category = "SYSTEM" if self.kind == "HEARTBEAT_STALE" else "RISK"
        payload: dict[str, Any] = {"kind": self.kind}
        if self.value_pct is not None:
            payload["value_pct"] = _f(self.value_pct)
        return OutboxEvent(dedup_key=self.dedup_key, source="guard", category=category, event_type=self.kind,
                           summary=self.message, occurred_at=now.isoformat(), symbol=self.symbol,
                           severity=self.severity, payload=payload)


def check_alerts(policy: Policy, now: datetime, *, market_symbol: str = "QQQ",
                 market_change_pct: Decimal | None = None, account_daily_pct: Decimal | None = None,
                 holding_moves: Mapping[str, Decimal] | None = None, earnings_tomorrow: frozenset[str] = frozenset(),
                 heartbeat_at: datetime | None = None) -> list[Alert]:
    """급락·급변·실적 예정·서버 이상 경보. 자동으로 팔거나 막지 않고 알리기만 한다.

    dedup_key에 미국 장 기준일과 단계가 들어가므로, 아웃박스에 넣으면 같은 단계는 하루 한 번만 나간다.
    여러 단계를 한꺼번에 넘으면 가장 깊은 단계 하나만 보낸다.
    """
    day = now.astimezone(MARKET_DAY_TZ).date().isoformat()
    alerts: list[Alert] = []
    al = policy.alerts

    def deepest(value: Decimal | None, levels: tuple[Decimal, ...]) -> tuple[int, Decimal] | None:
        hit = [(i, lv) for i, lv in enumerate(levels) if value is not None and value <= lv]
        return hit[-1] if hit else None

    if (hit := deepest(market_change_pct, al["market_drop_pct"])) is not None:
        i, level = hit
        alerts.append(Alert("MARKET_DROP", "warning" if i == 0 else "critical",
                            f"[시장 급락] {market_symbol} 전일 대비 {_f(market_change_pct)}% ({level}% 단계)",
                            f"guard:{day}:market:{level}", market_symbol, market_change_pct))
    if (hit := deepest(account_daily_pct, al["account_daily_drop_pct"])) is not None:
        i, level = hit
        alerts.append(Alert("ACCOUNT_DROP", "warning" if i == 0 else "critical",
                            f"[계좌 급락] 계좌 NAV 오늘 {_f(account_daily_pct)}% ({level}% 단계)",
                            f"guard:{day}:account:{level}", None, account_daily_pct))
    for symbol, move in sorted((holding_moves or {}).items()):
        if abs(move) >= al["holding_move_pct"]:
            alerts.append(Alert("HOLDING_MOVE", "warning", f"[보유 종목 급변] {symbol} 장중 {_f(move)}%",
                                f"guard:{day}:holding:{symbol}", symbol, move))
    for symbol in sorted(earnings_tomorrow):
        alerts.append(Alert("EARNINGS_TOMORROW", "info", f"[실적 발표 예정] {symbol} 내일 실적 발표",
                            f"guard:{day}:earnings:{symbol}", symbol))
    if heartbeat_at is not None and now - heartbeat_at >= timedelta(minutes=float(al["heartbeat_stale_minutes"])):
        minutes = int((now - heartbeat_at).total_seconds() // 60)
        alerts.append(Alert("HEARTBEAT_STALE", "critical", f"[서버 이상] 운용 루프 heartbeat가 {minutes}분째 없습니다",
                            f"guard:{day}:heartbeat"))
    return alerts


def decision_event(decision: Decision, intent: OrderIntent, now: datetime) -> OutboxEvent | None:
    """차단 판정을 아웃박스 이벤트로 만든다. 허용은 이벤트를 만들지 않는다."""
    if decision.allowed:
        return None
    codes = ", ".join(c.code for c in decision.blocks)
    return OutboxEvent(dedup_key=f"guard:block:{intent.intent_id}", source="guard", category="RISK",
                       event_type="BLOCKED", severity="warning", symbol=intent.symbol,
                       summary=f"[차단] {intent.book} {intent.symbol} {intent.side} {intent.quantity}주: {codes}",
                       occurred_at=now.isoformat(), payload=decision.to_dict())


# ---------------------------------------------------------------- JSON 입력 (CLI·테스트용)


def _dec(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name}는 숫자여야 합니다: {value!r}") from exc


def _opt_dec(value: Any, name: str) -> Decimal | None:
    return None if value is None else _dec(value, name)


def _time(value: Any, name: str) -> datetime:
    t = datetime.fromisoformat(str(value))
    if t.tzinfo is None:
        raise ValueError(f"{name}에는 시간대가 있어야 합니다.")
    return t


def intent_from_dict(d: Mapping[str, Any]) -> OrderIntent:
    return OrderIntent(intent_id=str(d["intent_id"]), book=str(d["book"]), symbol=str(d["symbol"]).upper(),
                       side=str(d["side"]), quantity=_dec(d["quantity"], "quantity"),
                       limit_price=_opt_dec(d.get("limit_price"), "limit_price"), strategy=d.get("strategy"),
                       stop_price=_opt_dec(d.get("stop_price"), "stop_price"),
                       leverage=_dec(d.get("leverage", 1), "leverage"))


def quote_from_dict(d: Mapping[str, Any]) -> Quote:
    return Quote(symbol=str(d["symbol"]).upper(), bid=_dec(d["bid"], "bid"), ask=_dec(d["ask"], "ask"),
                 as_of=_time(d["as_of"], "quote.as_of"))


def account_from_dict(d: Mapping[str, Any]) -> AccountState:
    holdings = tuple(Holding(symbol=str(h["symbol"]).upper(), quantity=_dec(h["quantity"], "quantity"),
                             price=_dec(h["price"], "price"), book=str(h.get("book", "invest")),
                             leverage=_dec(h.get("leverage", 1), "leverage")) for h in d.get("holdings", []))
    strategies = {str(k): StrategyState(daily_pnl_pct=_dec(v.get("daily_pnl_pct", 0), "daily_pnl_pct"),
                                        monthly_pnl_pct=_dec(v.get("monthly_pnl_pct", 0), "monthly_pnl_pct"),
                                        halted=bool(v.get("halted", False)))
                  for k, v in d.get("strategies", {}).items()}
    return AccountState(
        as_of=_time(d["as_of"], "account.as_of"), nav=_dec(d["nav"], "nav"), cash=_dec(d["cash"], "cash"),
        holdings=holdings, complete=bool(d.get("complete", True)),
        pending_buy_usd=_dec(d.get("pending_buy_usd", 0), "pending_buy_usd"),
        bought_today_usd=_dec(d.get("bought_today_usd", 0), "bought_today_usd"),
        drawdown_pct=_opt_dec(d.get("drawdown_pct"), "drawdown_pct"),
        equity_cap_pct=_opt_dec(d.get("equity_cap_pct"), "equity_cap_pct"),
        trading_book_nav=_opt_dec(d.get("trading_book_nav"), "trading_book_nav"),
        trading_book_daily_pnl_pct=_dec(d.get("trading_book_daily_pnl_pct", 0), "trading_book_daily_pnl_pct"),
        trading_book_drawdown_pct=_dec(d.get("trading_book_drawdown_pct", 0), "trading_book_drawdown_pct"),
        strategies=strategies,
        high_vol_symbols=frozenset(s.upper() for s in d.get("high_vol_symbols", [])),
        earnings_tomorrow=frozenset(s.upper() for s in d.get("earnings_tomorrow", [])),
        kill_switch_active=bool(d.get("kill_switch_active", False)),
        seen_intent_ids=frozenset(str(s) for s in d.get("seen_intent_ids", [])),
    )
