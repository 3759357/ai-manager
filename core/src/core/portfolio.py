from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from ki.order_state import TERMINAL_ORDER_STATUSES

from core.risk import PortfolioPosition, PortfolioSnapshot

# 로컬 원장에서 예약 노출로 보지 않는 종결 상태. KIS는 상태 머신의 정의를 그대로 쓴다.
KIS_TERMINAL_STATUSES = TERMINAL_ORDER_STATUSES
# SUBMITTING·SUBMIT_UNKNOWN은 접수됐을 수 있으므로 종결로 보지 않는다(미체결 노출로 예약).
TOSS_TERMINAL_STATUSES = frozenset(
    {"FILLED", "REJECTED", "BLOCKED", "PREFLIGHT_BLOCKED", "CANCELED", "VOIDED",
     "SUBMIT_REJECTED", "SUBMIT_FAILED", "EXPIRED"}
)


@dataclass
class BrokerPortfolio:
    broker: str
    cash: Decimal = Decimal("0")
    positions: list[dict[str, Any]] = field(default_factory=list)
    pending_buy_amount: Decimal = Decimal("0")
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


def build_snapshot(
    brokers: list[BrokerPortfolio],
    *,
    leveraged_symbols: frozenset[str],
    currency: str = "USD",
    extra_pending_buy: Decimal = Decimal("0"),
) -> tuple[PortfolioSnapshot, dict[str, Any]]:
    """브로커별 수집 결과를 단일 통화 스냅샷으로 합산한다.

    같은 종목은 브로커를 넘어 합산해 단일 포지션 한도를 평가한다.
    """
    merged: dict[str, dict[str, Decimal]] = {}
    detail: dict[str, Any] = {"brokers": [], "skipped": []}
    total_cash = Decimal("0")
    total_pending = extra_pending_buy
    collected = 0
    for broker in brokers:
        broker_detail: dict[str, Any] = {"broker": broker.broker}
        if broker.error is not None:
            broker_detail["error"] = broker.error
            detail["brokers"].append(broker_detail)
            continue
        collected += 1
        total_cash += broker.cash
        total_pending += broker.pending_buy_amount
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
            leveraged=symbol in leveraged_symbols,
        )
        for symbol, bucket in sorted(merged.items())
    )
    snapshot = PortfolioSnapshot(
        cash=total_cash,
        positions=positions,
        currency=currency,
        pending_buy_amount=total_pending,
    )
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
