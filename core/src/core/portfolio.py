from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ki.order_state import TERMINAL_ORDER_STATUSES
from toss.gateway import TERMINAL_STATUSES as TOSS_GATEWAY_TERMINAL_STATUSES

from core.risk import PortfolioPosition, PortfolioSnapshot

# 로컬 원장에서 예약 노출로 보지 않는 종결 상태. KIS는 상태 머신의 정의를 그대로 쓴다.
KIS_TERMINAL_STATUSES = TERMINAL_ORDER_STATUSES
# SUBMITTING·SUBMIT_UNKNOWN은 접수됐을 수 있으므로 종결로 보지 않는다(미체결 노출로 예약).
TOSS_TERMINAL_STATUSES = TOSS_GATEWAY_TERMINAL_STATUSES
# 브로커에 나가지 않아 일일 매수 합계에서 빼는 상태. 취소·만료된 주문은 일부 체결됐을 수
# 있으므로 그날 한도에 남긴다(보수적). 토스 EXPIRED는 제출 전 후보 유효시간 만료다.
KIS_NOT_PLACED_STATUSES = frozenset({"REJECTED", "PREFLIGHT_BLOCKED", "VOIDED"})
TOSS_NOT_PLACED_STATUSES = frozenset(
    {"BLOCKED", "PREFLIGHT_BLOCKED", "REJECTED", "VOIDED", "SUBMIT_REJECTED", "SUBMIT_FAILED", "EXPIRED"}
)
NOT_PLACED_STATUSES = {"kis": KIS_NOT_PLACED_STATUSES, "toss": TOSS_NOT_PLACED_STATUSES}
# 미국 주식 기준 하루. 일일 매수 한도는 뉴욕 자정부터 센다.
TRADING_DAY_ZONE = ZoneInfo("America/New_York")


def trading_day_start(now: datetime | None = None) -> datetime:
    local = (now or datetime.now(TRADING_DAY_ZONE)).astimezone(TRADING_DAY_ZONE)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class BrokerPortfolio:
    broker: str
    cash: Decimal = Decimal("0")
    positions: list[dict[str, Any]] = field(default_factory=list)
    pending_buy_amount: Decimal = Decimal("0")
    # 로컬 주문 원장(최근 500건). 일일 매수 합계 계산에 쓴다. None이면 원장을 못 읽었다.
    orders: list[dict[str, Any]] | None = None
    skipped: list[str] = field(default_factory=list)
    error: str | None = None


def parse_kis_positions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """KIS overseas_balance 응답의 output1 행을 정규화한다."""
    results: list[dict[str, Any]] = []
    for row in raw.get("positions", []) or []:
        symbol = str(row.get("ovrs_pdno") or row.get("pdno") or "").upper()
        quantity = _first_decimal(row, "ovrs_cblc_qty", "cblc_qty")
        if not symbol or quantity is None or quantity <= 0:
            continue
        price = _first_decimal(row, "now_pric2", "ovrs_now_pric1")
        if price is None or price <= 0:
            evaluation = _first_decimal(row, "ovrs_stck_evlu_amt")
            if evaluation is not None and evaluation > 0:
                price = evaluation / quantity
        if price is None or price <= 0:
            price = _first_decimal(row, "pchs_avg_pric")
        results.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "currency": str(row.get("tr_crcy_cd") or "USD").upper(),
            }
        )
    return results


def parse_kis_cash(buying_power: dict[str, Any]) -> Decimal:
    """KIS overseas_buying_power 응답에서 주문 가능 외화 금액을 꺼낸다."""
    cash = _first_decimal(
        buying_power,
        "ord_psbl_frcr_amt",
        "frcr_ord_psbl_amt1",
        "ovrs_ord_psbl_amt",
    )
    if cash is None:
        raise ValueError("KIS 매수 가능 금액 필드를 찾지 못했습니다.")
    return cash


def parse_toss_holdings(raw: Any) -> list[dict[str, Any]]:
    """Toss holdings 응답을 정규화한다. dict 래핑과 키 변형에 대응한다."""
    rows: list[Any]
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        for key in ("holdings", "stocks", "items", "result", "positions"):
            value = raw.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            rows = [raw] if raw.get("symbol") else []
    else:
        rows = []
    results: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
        quantity = _first_decimal(
            row,
            "quantity",
            "holdingQuantity",
            "qty",
        )
        if not symbol or quantity is None or quantity <= 0:
            continue
        price = _first_decimal(
            row,
            "currentPrice",
            "lastPrice",
            "price",
        )
        if price is None or price <= 0:
            evaluation = _first_decimal(
                row,
                "evaluationAmount",
                "marketValue",
            )
            if evaluation is not None and evaluation > 0:
                price = evaluation / quantity
        if price is None or price <= 0:
            price = _first_decimal(
                row,
                "averagePurchasePrice",
                "averagePrice",
            )
        results.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "currency": str(row.get("currency") or "USD").upper(),
            }
        )
    return results


def parse_toss_cash(buying_power: dict[str, Any]) -> Decimal:
    cash = _first_decimal(buying_power, "cashBuyingPower", "buyingPower")
    if cash is None:
        raise ValueError("Toss 매수 가능 금액 필드를 찾지 못했습니다.")
    return cash


def pending_buy_amount(
    orders: list[dict[str, Any]],
    terminal_statuses: frozenset[str],
) -> Decimal:
    """종결되지 않은 매수 주문의 추정 금액 합계 = 예약 노출."""
    total = Decimal("0")
    for order in orders:
        side = str(order.get("side") or "").upper()
        status = str(order.get("status") or "").upper()
        if side != "BUY" or status in terminal_statuses:
            continue
        amount = _first_decimal(order, "estimated_amount", "estimatedAmount")
        if amount is not None and amount > 0:
            total += amount
    return total


def daily_buy_amount(
    orders: list[dict[str, Any]],
    not_placed_statuses: frozenset[str],
    *,
    since: datetime,
    currency: str = "USD",
    exclude_ids: frozenset[int] = frozenset(),
) -> Decimal:
    """since 이후 원장에 만든 매수 주문 금액 합계(브로커에 나가지 않은 주문 제외)."""
    total = Decimal("0")
    for order in orders:
        if str(order.get("side") or "").upper() != "BUY":
            continue
        if str(order.get("status") or "").upper() in not_placed_statuses:
            continue
        if str(order.get("currency") or currency).upper() != currency:
            continue
        if order.get("id") is not None and int(order["id"]) in exclude_ids:
            continue
        try:
            created = datetime.fromisoformat(str(order.get("created_at")))
        except ValueError:
            created = None
        # 생성 시각을 모르면 오늘 주문으로 본다(보수적).
        if created is not None and created.tzinfo is not None and created < since:
            continue
        amount = _first_decimal(order, "estimated_amount", "estimatedAmount")
        if amount is not None and amount > 0:
            total += amount
    return total


def build_snapshot(
    brokers: list[BrokerPortfolio],
    *,
    leveraged_symbols: frozenset[str] = frozenset(),
    leverage_multipliers: Mapping[str, Decimal] | None = None,
    currency: str = "USD",
    extra_pending_buy: Decimal = Decimal("0"),
    day_start: datetime | None = None,
    exclude_order: tuple[str, int] | None = None,
) -> tuple[PortfolioSnapshot, dict[str, Any]]:
    """브로커별 수집 결과를 단일 통화 스냅샷으로 합산한다.

    같은 종목은 브로커를 넘어 합산해 단일 포지션 한도를 평가한다.
    """
    multipliers = dict(leverage_multipliers or {})
    for symbol in leveraged_symbols:
        multipliers.setdefault(symbol, Decimal("3"))
    since = day_start or trading_day_start()
    merged: dict[str, dict[str, Decimal]] = {}
    detail: dict[str, Any] = {"brokers": [], "skipped": []}
    total_cash = Decimal("0")
    total_pending = extra_pending_buy
    bought_today: Decimal | None = Decimal("0")
    collected = 0
    for broker in brokers:
        broker_detail: dict[str, Any] = {"broker": broker.broker}
        if broker.error is not None:
            broker_detail["error"] = broker.error
            detail["brokers"].append(broker_detail)
            bought_today = None  # 이 브로커의 오늘 매수를 알 수 없다.
            continue
        collected += 1
        total_cash += broker.cash
        total_pending += broker.pending_buy_amount
        if broker.orders is None or bought_today is None:
            bought_today = None
        else:
            exclude = (
                frozenset({exclude_order[1]})
                if exclude_order and exclude_order[0] == broker.broker
                else frozenset()
            )
            bought_today += daily_buy_amount(
                broker.orders,
                NOT_PLACED_STATUSES.get(broker.broker, frozenset()),
                since=since,
                currency=currency,
                exclude_ids=exclude,
            )
        broker_detail["cash"] = format(broker.cash, "f")
        broker_detail["pending_buy_amount"] = format(
            broker.pending_buy_amount, "f"
        )
        broker_detail["positions"] = len(broker.positions)
        detail["brokers"].append(broker_detail)
        detail["skipped"].extend(broker.skipped)
        for row in broker.positions:
            if row.get("price") is None or row["price"] <= 0:
                detail["skipped"].append(
                    f"{broker.broker} {row['symbol']}: 가격 없음"
                )
                continue
            if row["currency"] != currency:
                detail["skipped"].append(
                    f"{broker.broker} {row['symbol']}: "
                    f"{row['currency']} 통화 제외"
                )
                continue
            bucket = merged.setdefault(
                row["symbol"],
                {"quantity": Decimal("0"), "value": Decimal("0")},
            )
            bucket["quantity"] += row["quantity"]
            bucket["value"] += row["quantity"] * row["price"]
    if collected == 0:
        errors = "; ".join(
            f"{broker.broker}: {broker.error}" for broker in brokers
        )
        raise ValueError(f"포지션을 수집한 브로커가 없습니다. {errors}")
    positions = tuple(
        PortfolioPosition(
            symbol=symbol,
            quantity=bucket["quantity"],
            price=bucket["value"] / bucket["quantity"],
            currency=currency,
            leveraged=symbol in multipliers,
            leverage_multiplier=multipliers.get(symbol, Decimal("1")),
        )
        for symbol, bucket in sorted(merged.items())
    )
    snapshot = PortfolioSnapshot(
        cash=total_cash,
        positions=positions,
        currency=currency,
        pending_buy_amount=total_pending,
        bought_today=bought_today,
    )
    detail["bought_today"] = format(bought_today, "f") if bought_today is not None else None
    return snapshot, detail


def _first_decimal(data: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = data.get(key)
        if value in {None, ""}:
            continue
        try:
            return Decimal(str(value).replace(",", ""))
        except (InvalidOperation, ValueError):
            continue
    return None
