"""비중 규칙 v1과 종목 배분. 순수 함수이며 주문을 만들지 않는다.

1) equity_cap: docs/allocation.md의 총 주식 비중 상한. research/allocation/policies.py의 v1과
   같은 입력이면 같은 숫자가 나와야 한다(테스트로 확인).
2) sleeve_targets: 승인된 투자논리 카드와 유효한 LLM 판단(llm_judgments)으로 종목별 목표 비중을 정한다.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence


@dataclass(frozen=True)
class V1Params:
    target_vol: float = 0.14
    wmin: float = 0.10
    wmax: float = 0.80
    trend_cut: float = 0.5
    trend_band: float = 0.02
    brake: tuple[tuple[float, float], ...] = ((-0.06, 0.75), (-0.09, 0.5))
    dip: bool = True
    dip_level: float = -0.20
    hwm_window: int = 252
    recover_days: int = 20
    check_every: int = 5
    vol_span: int = 30
    emergency_dd: float = -0.15


def _ewm_std(returns: Sequence[float], span: int) -> list[float]:
    """pandas Series.ewm(span).std()와 같은 값(adjust=True, bias 보정)."""
    alpha = 2.0 / (span + 1.0)
    decay = 1.0 - alpha
    sw = sw2 = swx = swx2 = 0.0
    out = []
    for x in returns:
        sw, sw2, swx, swx2 = sw * decay + 1.0, sw2 * decay * decay + 1.0, swx * decay + x, swx2 * decay + x * x
        if sw * sw - sw2 <= 0:
            out.append(float("nan"))
            continue
        mean = swx / sw
        var = max(swx2 / sw - mean * mean, 0.0) * (sw * sw) / (sw * sw - sw2)
        out.append(math.sqrt(var))
    return out


def equity_cap(closes: Sequence[float], navs: Sequence[float], params: V1Params = V1Params()) -> dict[str, Any]:
    """총 주식 비중 상한을 계산한다.

    closes: 기준 지수(QQQ 등) 일별 종가, 오래된 순. navs: 같은 날짜의 계좌 NAV(입출금 보정 지수). 길이가 같아야 한다.
    백테스트와 같게, check_every 거래일마다 추세·브레이크 상태를 갱신하며 전체 이력을 다시 계산한다.
    """
    n = len(closes)
    if n < 2 or len(navs) != n:
        raise ValueError("가격과 NAV 이력은 길이가 같고 2일 이상이어야 합니다.")
    if any(not (c > 0) for c in closes) or any(not (v > 0) for v in navs):
        raise ValueError("가격과 NAV는 양수여야 합니다.")
    p = params
    returns = [0.0] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, n)]
    vol = [v * math.sqrt(252) for v in _ewm_std(returns, p.vol_span)]
    on, k, k_day = True, 1.0, 0
    prefix = [0.0]
    for c in closes:
        prefix.append(prefix[-1] + c)
    peaks: deque[int] = deque()  # 최근 252일 최고가 인덱스(단조 감소 큐)
    nav_peaks: deque[int] = deque()
    last: dict[str, Any] = {}
    for t in range(n):
        while peaks and closes[peaks[-1]] <= closes[t]:
            peaks.pop()
        peaks.append(t)
        if peaks[0] <= t - 252:
            peaks.popleft()
        while nav_peaks and navs[nav_peaks[-1]] <= navs[t]:
            nav_peaks.pop()
        nav_peaks.append(t)
        if nav_peaks[0] <= t - p.hwm_window:
            nav_peaks.popleft()
        if not (t % p.check_every == 0 or t < 2):
            continue
        ma200 = (prefix[t + 1] - prefix[t - 199]) / 200.0 if t >= 199 else float("nan")
        ma50 = (prefix[t + 1] - prefix[t - 49]) / 50.0 if t >= 49 else float("nan")
        hi = closes[peaks[0]]
        px = closes[t]
        base = min(p.wmax, p.target_vol / max(vol[t], 1e-6)) if not math.isnan(vol[t]) else p.wmax
        if not math.isnan(ma200):
            if on and px < ma200 * (1 - p.trend_band):
                on = False
            elif not on and px > ma200 * (1 + p.trend_band):
                on = True
        trend = 1.0 if on else p.trend_cut
        market_dd = px / hi - 1.0
        dip_override = (p.dip and not on and market_dd <= p.dip_level and not math.isnan(ma50) and px > ma50)
        if dip_override:
            trend = 1.0
        account_dd = navs[t] / navs[nav_peaks[0]] - 1.0
        target_k = 1.0
        for level, mult in p.brake:
            if account_dd <= level:
                target_k = mult
        if target_k < k:
            k, k_day = target_k, t
        elif target_k > k and t - k_day >= p.recover_days:
            k, k_day = target_k, t
        cap = min(max(base * trend * k, p.wmin), p.wmax)
        last = dict(index=t, cap=cap, base=base, volatility=vol[t], trend_on=on, trend_factor=trend,
                    dip_override=dip_override, market_drawdown=market_dd, account_drawdown=account_dd,
                    brake_factor=k, brake_since_index=k_day, ma200=ma200, ma50=ma50)
    peak = max(navs)
    last["account_drawdown_from_peak"] = navs[-1] / peak - 1.0
    last["emergency_no_buys"] = last["account_drawdown_from_peak"] <= p.emergency_dd
    last["computed_on_index"] = last.pop("index")
    last["stale_days"] = n - 1 - last["computed_on_index"]
    last["reasons"] = _cap_reasons(last, p)
    return last


def _cap_reasons(r: dict[str, Any], p: V1Params) -> list[str]:
    out = [f"변동성 {r['volatility']:.1%} → 기본 비중 {r['base']:.0%}" if not math.isnan(r["volatility"]) else "변동성 계산 불가 → 상한 사용"]
    if not r["trend_on"]:
        out.append("200일선 아래 → 추세 계수 0.5" + (" (폭락 재진입 조건으로 해제)" if r["dip_override"] else ""))
    if r["brake_factor"] < 1:
        out.append(f"계좌 1년 고점 대비 {r['account_drawdown']:.1%} → 낙폭 계수 {r['brake_factor']}")
    if r["emergency_no_buys"]:
        out.append(f"계좌 고점 대비 {r['account_drawdown_from_peak']:.1%} ≤ {p.emergency_dd:.0%} → 신규 매수 중지")
    return out


# ---- 종목 배분 -------------------------------------------------------------

CONVICTION_WEIGHT = {1: 0.5, 2: 1.0, 3: 1.5}
HIGH_VOL = 0.45
HIGH_VOL_CAP = 0.10
EARNINGS_CAP = 0.07
DEFAULT_VOL = 0.35


@dataclass
class SleeveResult:
    cap: float
    targets: dict[str, float] = field(default_factory=dict)
    index_weight: float = 0.0
    cash_weight: float = 0.0
    buy_allowed: dict[str, bool] = field(default_factory=dict)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"cap": self.cap, "targets": self.targets, "index_weight": self.index_weight,
                "cash_weight": self.cash_weight, "buy_allowed": self.buy_allowed, "reasons": self.reasons}


def sleeve_targets(cap: float, cards: list[dict[str, Any]], judgments: dict[str, dict[str, Any]], *,
                   vols: dict[str, float] | None = None, current: dict[str, float] | None = None,
                   emergency_no_buys: bool = False, brake_active: bool = False,
                   now: datetime | None = None, index_symbol: str = "QQQ") -> SleeveResult:
    """cap 안에서 종목 목표 비중을 정한다. 남는 비중은 지수 ETF로 보낸다(분산).

    - 확신도 가중 × 변동성 역수 × 틸트(1 + 0.25·tilt) × 상태(intact/판단 없음 1, weakened 1, broken 0)
    - weakened: 목표가 현재 비중을 넘지 않는다(추가 매수 금지). broken: 목표 0.
    - 종목 한도: 카드 maxWeightPct, 고변동 10%, 36시간 안 실적 발표 7%.
    """
    if not 0 <= cap <= 1:
        raise ValueError("cap은 0~1입니다.")
    vols, current = vols or {}, current or {}
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result = SleeveResult(cap=cap)
    scores: dict[str, float] = {}
    limits: dict[str, float] = {}
    health: dict[str, str] = {}
    for card in cards:
        symbol = card["symbol"]
        reasons = result.reasons.setdefault(symbol, [])
        if card["kind"] != "position" or card["sizing"]["maxWeightPct"] <= 0:
            reasons.append("관찰 카드 → 비중 0")
            continue
        j = judgments.get(card["id"], {})
        h = j.get("thesis_health", {}).get("value", "unknown")
        tilt = j.get("tilt", {}).get("value", 0)
        vol = vols.get(symbol, DEFAULT_VOL)
        health[symbol] = h
        if h == "broken":
            scores[symbol], limits[symbol] = 0.0, 0.0
            reasons.append(f"무효화 조건 충족(broken): {j['thesis_health'].get('rule')} → 목표 0")
            continue
        score = CONVICTION_WEIGHT[card["conviction"]] / max(vol, 0.05) * (1 + 0.25 * tilt)
        scores[symbol] = max(score, 0.0)
        limit = card["sizing"]["maxWeightPct"] / 100.0
        if vol > HIGH_VOL:
            limit = min(limit, HIGH_VOL_CAP)
            reasons.append(f"고변동({vol:.0%}) → 한도 {HIGH_VOL_CAP:.0%}")
        event = j.get("event_risk")
        if event and event["value"] == "earnings" and event["detail"].get("at"):
            at = datetime.fromisoformat(event["detail"]["at"])
            if now <= at <= now + timedelta(hours=36):
                limit = min(limit, EARNINGS_CAP)
                reasons.append(f"실적 발표 임박({at:%m-%d %H:%M} UTC) → 한도 {EARNINGS_CAP:.0%}")
        limits[symbol] = limit
        reasons.append(f"확신 {card['conviction']} · 변동성 {vol:.0%} · 틸트 {tilt:+d} · 상태 {h}")
    total = sum(scores.values())
    for symbol, score in scores.items():
        raw = cap * score / total if total > 0 else 0.0
        target = min(raw, limits[symbol])
        if health[symbol] == "weakened":
            held = current.get(symbol, 0.0)
            if target > held:
                result.reasons[symbol].append("논리 약화(weakened) → 추가 매수 없이 현재 비중 유지")
            target = min(target, held)
        result.targets[symbol] = round(target, 6)
        # 계좌 낙폭 브레이크 중에는 개별 종목 매수를 하지 않는다(비중 규칙 v1 3장 5번).
        result.buy_allowed[symbol] = (not emergency_no_buys and not brake_active
                                      and health[symbol] in {"intact", "unknown"})
    used = sum(result.targets.values())
    result.index_weight = round(max(cap - used, 0.0), 6)
    if result.index_weight > 0:
        result.reasons.setdefault(index_symbol, []).append("종목 한도로 남은 비중을 지수 ETF로 분산")
    result.buy_allowed[index_symbol] = not emergency_no_buys
    result.cash_weight = round(1.0 - cap, 6)
    return result
