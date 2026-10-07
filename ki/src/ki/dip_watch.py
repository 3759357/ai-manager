from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from ki.client import KISClient
from ki.storage import SnapshotStore


@dataclass(frozen=True)
class DipWatchOptions:
    quantity: int
    tranche_quantities: tuple[int, ...] = ()
    next_tranche_drop_pct: Decimal = Decimal("3")
    exchange: str = "NAS"
    interval_minutes: int = 1
    candle_count: int = 120
    lookback_bars: int = 60
    stable_bars: int = 10
    watch_low: Decimal | None = None
    watch_high: Decimal | None = None
    min_drawdown_pct: Decimal = Decimal("5")
    rebound_pct: Decimal = Decimal("1.5")
    reference_symbols: tuple[str, ...] = ()
    reference_rebound_pct: Decimal = Decimal("0")


@dataclass(frozen=True)
class Candle:
    timestamp: str
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class DipWatcher:
    """낙폭 후 저점 안정과 반등을 확인해 수동 승인용 매수 후보를 만든다."""

    def __init__(
        self,
        client: KISClient,
        store: SnapshotStore,
        *,
        options: DipWatchOptions,
    ) -> None:
        validate_dip_watch_options(options)
        self.client = client
        self.store = store
        self.options = options

    def scan_once(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        plan = build_tranche_plan(symbol, self.options)
        history = self._candidate_history(symbol, str(plan["plan_id"]))
        next_tranche_index = len(history) + 1
        target = self._load_candles(symbol)
        target_evaluation = evaluate_candles(target, self.options)

        references: dict[str, dict[str, Any]] = {}
        references_ready = True
        for reference_symbol in self.options.reference_symbols:
            normalized = reference_symbol.upper()
            candles = self._load_candles(normalized)
            evaluation = evaluate_reference(
                candles,
                lookback_bars=self.options.lookback_bars,
                stable_bars=self.options.stable_bars,
                rebound_pct=self.options.reference_rebound_pct,
            )
            references[normalized] = evaluation
            references_ready = references_ready and bool(evaluation["ready"])

        market_ready = bool(target_evaluation["ready"]) and references_ready
        tranche_ready, tranche_reason, drop_from_previous_pct = (
            evaluate_next_tranche(
                target_evaluation,
                history,
                tranche_count=len(plan["tranche_quantities"]),
                next_tranche_drop_pct=self.options.next_tranche_drop_pct,
            )
        )
        ready = market_ready and tranche_ready
        candidate = None
        if ready:
            candidate = self._save_candidate(
                symbol,
                target_evaluation,
                references,
                plan=plan,
                tranche_index=next_tranche_index,
                drop_from_previous_pct=drop_from_previous_pct,
            )

        return {
            "symbol": symbol,
            "quantity": self.options.quantity,
            "plan": plan,
            "emitted_tranches": len(history),
            "next_tranche_index": (
                next_tranche_index
                if next_tranche_index <= len(plan["tranche_quantities"])
                else None
            ),
            "market_ready": market_ready,
            "tranche_ready": tranche_ready,
            "tranche_reason": tranche_reason,
            "drop_from_previous_pct": drop_from_previous_pct,
            "ready": ready,
            "target": target_evaluation,
            "references": references,
            "candidate": candidate,
        }

    def _candidate_history(
        self,
        symbol: str,
        plan_id: str,
    ) -> list[dict[str, Any]]:
        history = []
        for signal in self.store.list_market_signals(
            limit=1000,
            symbol=symbol,
            signal_type="BUY_CANDIDATE",
        ):
            payload = signal.get("payload", {})
            if payload.get("source") != "dip-watch":
                continue
            if payload.get("plan_id") != plan_id:
                continue
            history.append(payload)
        history.sort(key=lambda payload: int(payload["tranche_index"]))
        return history

    def _load_candles(self, symbol: str) -> list[Candle]:
        response = self.client.overseas_minute_candles(
            symbol,
            self.options.exchange,
            interval=self.options.interval_minutes,
            count=self.options.candle_count,
            include_previous=False,
        )
        return normalize_candles(response.get("candles", []))

    def _save_candidate(
        self,
        symbol: str,
        target: dict[str, Any],
        references: dict[str, dict[str, Any]],
        *,
        plan: dict[str, Any],
        tranche_index: int,
        drop_from_previous_pct: str | None,
    ) -> dict[str, Any]:
        tranche_quantities = plan["tranche_quantities"]
        tranche_quantity = int(tranche_quantities[tranche_index - 1])
        payload = {
            "source": "dip-watch",
            "action": "buy",
            "approval_required": True,
            "plan_id": plan["plan_id"],
            "total_quantity": self.options.quantity,
            "tranche_index": tranche_index,
            "tranche_count": len(tranche_quantities),
            "tranche_quantity": tranche_quantity,
            "tranche_quantities": tranche_quantities,
            "next_tranche_drop_pct": format(
                self.options.next_tranche_drop_pct,
                "f",
            ),
            "drop_from_previous_pct": drop_from_previous_pct,
            "quantity": tranche_quantity,
            "suggested_limit_price": target["current_price"],
            "watch_low": _format_optional(self.options.watch_low),
            "watch_high": _format_optional(self.options.watch_high),
            "min_drawdown_pct": format(self.options.min_drawdown_pct, "f"),
            "rebound_pct": format(self.options.rebound_pct, "f"),
            "stable_bars": self.options.stable_bars,
            "target": target,
            "references": references,
        }
        signal_id = self.store.save_market_signal(
            symbol=symbol,
            signal_type="BUY_CANDIDATE",
            severity="MEDIUM",
            message=(
                f"{symbol} BUY_CANDIDATE: "
                f"tranche={tranche_index}/{len(tranche_quantities)}, "
                f"low={target['trough_price']}, "
                f"rebound={target['rebound_pct']}%, "
                f"quantity={tranche_quantity}"
            ),
            payload=payload,
        )
        return {
            "id": signal_id,
            "signal_type": "BUY_CANDIDATE",
            **payload,
        }


def build_tranche_plan(
    symbol: str,
    options: DipWatchOptions,
) -> dict[str, Any]:
    quantities = effective_tranche_quantities(options)
    fingerprint = "|".join(
        [
            symbol.upper(),
            ",".join(str(quantity) for quantity in quantities),
            _format_optional(options.watch_low) or "",
            _format_optional(options.watch_high) or "",
            format(options.min_drawdown_pct, "f"),
            format(options.rebound_pct, "f"),
            format(options.next_tranche_drop_pct, "f"),
            options.exchange.upper(),
            str(options.interval_minutes),
            str(options.lookback_bars),
            str(options.stable_bars),
            ",".join(symbol.upper() for symbol in options.reference_symbols),
            format(options.reference_rebound_pct, "f"),
        ]
    )
    return {
        "plan_id": sha256(fingerprint.encode("utf-8")).hexdigest()[:16],
        "total_quantity": options.quantity,
        "tranche_quantities": list(quantities),
        "tranche_count": len(quantities),
        "next_tranche_drop_pct": format(options.next_tranche_drop_pct, "f"),
    }


def effective_tranche_quantities(options: DipWatchOptions) -> tuple[int, ...]:
    return options.tranche_quantities or (options.quantity,)


def evaluate_next_tranche(
    target: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    tranche_count: int,
    next_tranche_drop_pct: Decimal,
) -> tuple[bool, str, str | None]:
    if len(history) >= tranche_count:
        return False, "plan_completed", None
    if not history:
        return True, "first_tranche", None
    if not target.get("enough_bars") or target.get("trough_price") is None:
        return False, "not_enough_bars", None

    previous_target = history[-1].get("target", {})
    previous_trough = _decimal(previous_target["trough_price"])
    current_trough = _decimal(target["trough_price"])
    if target.get("trough_at") == previous_target.get("trough_at"):
        return False, "same_trough", "0.0000"
    drop = (previous_trough - current_trough) / previous_trough * Decimal("100")
    formatted_drop = format(drop, ".4f")
    if drop < next_tranche_drop_pct:
        return False, "tranche_gap_not_reached", formatted_drop
    return True, "new_lower_trough", formatted_drop


def evaluate_candles(
    candles: list[Candle],
    options: DipWatchOptions,
) -> dict[str, Any]:
    base = _evaluate_window(
        candles,
        lookback_bars=options.lookback_bars,
        stable_bars=options.stable_bars,
    )
    if not base["enough_bars"]:
        return {
            **base,
            "ready": False,
            "checks": {
                "enough_bars": False,
                "watch_range": False,
                "drawdown": False,
                "stable": False,
                "rebound": False,
            },
        }

    trough = _decimal(base["trough_price"])
    drawdown = _decimal(base["drawdown_pct"])
    rebound = _decimal(base["rebound_pct"])
    watch_range = (
        (options.watch_low is None or trough >= options.watch_low)
        and (options.watch_high is None or trough <= options.watch_high)
    )
    checks = {
        "enough_bars": True,
        "watch_range": watch_range,
        "drawdown": drawdown >= options.min_drawdown_pct,
        "stable": int(base["bars_since_low"]) >= options.stable_bars,
        "rebound": rebound >= options.rebound_pct,
    }
    return {
        **base,
        "ready": all(checks.values()),
        "checks": checks,
    }


def evaluate_reference(
    candles: list[Candle],
    *,
    lookback_bars: int,
    stable_bars: int,
    rebound_pct: Decimal,
) -> dict[str, Any]:
    base = _evaluate_window(
        candles,
        lookback_bars=lookback_bars,
        stable_bars=stable_bars,
    )
    if not base["enough_bars"]:
        return {**base, "ready": False}
    ready = (
        int(base["bars_since_low"]) >= stable_bars
        and _decimal(base["rebound_pct"]) >= rebound_pct
    )
    return {**base, "ready": ready}


def _evaluate_window(
    candles: list[Candle],
    *,
    lookback_bars: int,
    stable_bars: int,
) -> dict[str, Any]:
    minimum_bars = stable_bars + 2
    window = candles[-lookback_bars:]
    if len(window) < minimum_bars:
        return {
            "enough_bars": False,
            "bar_count": len(window),
            "required_bars": minimum_bars,
            "current_price": None,
            "trough_price": None,
            "trough_at": None,
            "bars_since_low": 0,
            "drawdown_pct": None,
            "rebound_pct": None,
        }

    trough = min(candle.low for candle in window)
    trough_index = max(
        index for index, candle in enumerate(window) if candle.low == trough
    )
    pre_trough_high = max(candle.high for candle in window[: trough_index + 1])
    current = window[-1].close
    drawdown = _percent_change(pre_trough_high, trough)
    rebound = _percent_change(trough, current)
    return {
        "enough_bars": True,
        "bar_count": len(window),
        "current_price": format(current, "f"),
        "trough_price": format(trough, "f"),
        "trough_at": window[trough_index].timestamp,
        "bars_since_low": len(window) - trough_index - 1,
        "pre_trough_high": format(pre_trough_high, "f"),
        "drawdown_pct": format(-drawdown, ".4f"),
        "rebound_pct": format(rebound, ".4f"),
        "latest_at": window[-1].timestamp,
    }


def normalize_candles(rows: list[dict[str, Any]]) -> list[Candle]:
    candles = []
    for row in rows:
        date_text = str(row.get("tymd") or row.get("date") or "")
        time_text = str(row.get("xhms") or row.get("time") or "")
        close = _first_decimal(row, "last", "close", "clos")
        high = _first_decimal(row, "high", "hprc", default=close)
        low = _first_decimal(row, "low", "lprc", default=close)
        volume = _first_decimal(row, "evol", "tvol", "volume", default=Decimal("0"))
        if not date_text or not time_text:
            continue
        candles.append(
            Candle(
                timestamp=f"{date_text}T{time_text}",
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    candles.sort(key=lambda candle: candle.timestamp)
    return candles


def validate_dip_watch_options(options: DipWatchOptions) -> None:
    if options.quantity <= 0:
        raise ValueError("--quantity는 1 이상이어야 합니다.")
    tranche_quantities = effective_tranche_quantities(options)
    if any(quantity <= 0 for quantity in tranche_quantities):
        raise ValueError("--tranches의 각 수량은 1 이상이어야 합니다.")
    if sum(tranche_quantities) != options.quantity:
        raise ValueError("--tranches 수량 합계는 quantity와 같아야 합니다.")
    if options.next_tranche_drop_pct <= 0:
        raise ValueError("--next-tranche-drop-pct는 0보다 커야 합니다.")
    if options.interval_minutes <= 0:
        raise ValueError("--interval-minutes는 1 이상이어야 합니다.")
    if options.candle_count < 1 or options.candle_count > 120:
        raise ValueError("--candle-count는 1~120이어야 합니다.")
    if options.lookback_bars <= 0:
        raise ValueError("--lookback-bars는 1 이상이어야 합니다.")
    if options.stable_bars <= 0:
        raise ValueError("--stable-bars는 1 이상이어야 합니다.")
    if options.lookback_bars < options.stable_bars + 2:
        raise ValueError("--lookback-bars는 --stable-bars보다 최소 2 커야 합니다.")
    if options.watch_low is not None and options.watch_low <= 0:
        raise ValueError("--watch-low는 0보다 커야 합니다.")
    if options.watch_high is not None and options.watch_high <= 0:
        raise ValueError("--watch-high는 0보다 커야 합니다.")
    if (
        options.watch_low is not None
        and options.watch_high is not None
        and options.watch_low > options.watch_high
    ):
        raise ValueError("--watch-low는 --watch-high 이하여야 합니다.")
    if options.min_drawdown_pct < 0:
        raise ValueError("--min-drawdown-pct는 0 이상이어야 합니다.")
    if options.rebound_pct < 0 or options.reference_rebound_pct < 0:
        raise ValueError("반등률 기준은 0 이상이어야 합니다.")


def _percent_change(start: Decimal, end: Decimal) -> Decimal:
    if start <= 0:
        return Decimal("0")
    return (end - start) / start * Decimal("100")


def _first_decimal(
    row: dict[str, Any],
    *keys: str,
    default: Decimal | None = None,
) -> Decimal:
    for key in keys:
        value = row.get(key)
        if value not in {None, ""}:
            return _decimal(value)
    if default is not None:
        return default
    raise ValueError(f"분봉 응답에서 숫자 필드를 찾지 못했습니다: {keys}")


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"숫자 변환에 실패했습니다: {value}") from exc


def _format_optional(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None
