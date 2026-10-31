from __future__ import annotations

import json
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from core.agent import AgentError, StrategyAgent
from core.audit import record_core_event
from core.events import OutboxEvent, utc_now_iso
from core.killswitch import KillSwitch
from core.outbox import OutboxStore
from core.quota import QuotaExhausted, QuotaLedger
from core.strategy import (
    STATUS_ACCEPTED,
    STATUS_ERROR,
    STATUS_REJECTED,
    ReviewRejected,
    StrategyStore,
    candidate_signature,
    parse_decision,
    prompt_hash,
    validate_decision,
)

logger = logging.getLogger(__name__)

CURSOR_SOURCE = "strategy_worker"
CANDIDATE_EVENT_TYPE = "BUY_CANDIDATE"

QuoteProvider = Callable[[str], Decimal]


class QuoteUnavailable(Exception):
    """기준 현재가를 조회하지 못했다(브로커 장애·미지원 종목)."""


class StrategyWorker:
    """매수 후보를 소비해 에이전트 심사를 붙이고 결과를 아웃박스에 남긴다.

    NotificationWorker와 같은 모양이다. 커서로 이벤트를 한 번씩만 읽고,
    실패를 예외로 흘리지 않고 상태로 기록한다.

    이 워커는 전략을 만들지 않는다. 결정론적 규칙이 이미 통과시킨 후보를
    심사만 시킨다. 출력이 작아 검증이 쉽고, 실패해도 후보가 나가지 않을 뿐이다.
    """

    def __init__(
        self,
        store: OutboxStore,
        strategy_store: StrategyStore,
        agent: StrategyAgent,
        quota: QuotaLedger,
        *,
        quote_provider: QuoteProvider,
        kill_switch: KillSwitch | None = None,
        prompt_text: str = "",
        prompt_version: str = "reviewer-v1",
        cache_ttl_minutes: int = 30,
        band_pct: Decimal = Decimal("10"),
        batch_size: int = 10,
        channels: tuple[str, ...] = ("console",),
    ) -> None:
        self.store = store
        self.strategy_store = strategy_store
        self.agent = agent
        self.quota = quota
        self.quote_provider = quote_provider
        self.kill_switch = kill_switch
        self.prompt_text = prompt_text
        self.prompt_version = prompt_version
        self.prompt_digest = prompt_hash(prompt_text)
        self.cache_ttl_minutes = cache_ttl_minutes
        self.band_pct = band_pct
        self.batch_size = batch_size
        self.channels = channels
        # 같은 예산 소진 경고를 주기마다 반복해서 보내지 않기 위해 직전 코드를 기억한다.
        self._last_quota_code: str | None = None

    def run_once(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "reviewed": 0,
            "approved": 0,
            "rejected": 0,
            "errors": 0,
            "cached": 0,
            "skipped": 0,
        }

        # Kill Switch가 켜져 있으면 호출조차 하지 않는다.
        # 멈춰야 할 때 예산을 태우는 것이 가장 나쁘다.
        if self.kill_switch is not None:
            status = self.kill_switch.status()
            if status["active"]:
                result["halted"] = status.get("reason") or "사유 없음"
                return result

        cursor = self.store.get_cursor(CURSOR_SOURCE)
        candidates = self.store.list_events_after(
            after_id=cursor,
            category="SIGNAL",
            event_type=CANDIDATE_EVENT_TYPE,
            limit=self.batch_size,
        )

        for candidate in candidates:
            event_id = int(candidate["id"])
            try:
                outcome = self._review_candidate(candidate)
            except QuotaExhausted as exc:
                # 예산 소진은 후보의 잘못이 아니다. 커서를 전진시키지 않고
                # 다음 주기에 같은 후보부터 다시 본다. 최소 호출 간격(TOO_SOON)은
                # 정상적인 대기이므로 알리지 않고, 소진 경고는 상태가 바뀔 때 한 번만 보낸다.
                result["quota_blocked"] = exc.code
                if exc.code != "TOO_SOON" and exc.code != self._last_quota_code:
                    self._record_system_event(
                        "QUOTA_EXHAUSTED",
                        f"[전략] 호출 예산 소진으로 심사 중단: {exc}",
                        severity="warning",
                        payload={"code": exc.code, "pending_event_id": event_id},
                    )
                if exc.code != "TOO_SOON":
                    self._last_quota_code = exc.code
                break
            except QuoteUnavailable as exc:
                # 기준가가 없으면 검증할 수 없으므로 후보를 내보내지 않는다. 한 종목의
                # 조회 실패가 뒤의 후보를 영원히 막지 않도록 커서는 전진시킨다.
                result["errors"] += 1
                self._record_system_event(
                    "QUOTE_UNAVAILABLE",
                    f"[전략] {candidate.get('symbol') or ''} 기준 현재가 조회 실패로 심사 생략",
                    severity="warning",
                    payload={"event_id": event_id, "error": type(exc.__cause__ or exc).__name__},
                )
            except AgentError as exc:
                # 게이트웨이 장애. 브로커 장애와 같은 취급으로 기록만 하고 넘어간다.
                result["errors"] += 1
                self._record_system_event(
                    "AGENT_ERROR",
                    f"[전략] 에이전트 호출 실패: {exc}",
                    severity="critical",
                    payload={"event_id": event_id, "error": str(exc)[:500]},
                )
            else:
                result["reviewed"] += 1
                result[outcome] += 1
                self._last_quota_code = None

            self.store.set_cursor(CURSOR_SOURCE, event_id)

        return result

    def _review_candidate(self, candidate: dict[str, Any]) -> str:
        payload = candidate.get("payload") or {}
        symbol = candidate.get("symbol") or payload.get("symbol") or ""
        candidate_ref = candidate["dedup_key"]
        signature = candidate_signature({**payload, "symbol": symbol})

        cached = self.strategy_store.find_cached(
            signature, ttl_minutes=self.cache_ttl_minutes
        )
        if cached is not None:
            self._record_strategy_event(
                event_type="REVIEW_CACHED",
                symbol=symbol,
                summary=f"[전략] {symbol} 심사 캐시 재사용",
                payload={"signature": signature, "reused_review_id": cached["id"]},
            )
            return "cached"

        entries = _entries(payload)
        if not entries:
            self.strategy_store.record(
                candidate_ref=candidate_ref,
                signature=signature,
                symbol=symbol,
                role=self.agent.role,
                cli=self.agent.cli,
                model=self.agent.model,
                prompt_version=self.prompt_version,
                prompt_digest=self.prompt_digest,
                request={},
                raw_response="",
                status=STATUS_REJECTED,
                reject_code="NO_ENTRIES",
                reject_reason="후보에 진입 가격이 없습니다.",
            )
            return "rejected"

        lowest_entry = min(entries)

        # 기준 가격은 반드시 우리가 직접 조회한다.
        # 에이전트가 준 가격으로 검증하면 자기참조가 된다.
        try:
            reference_price = self.quote_provider(symbol)
        except Exception as exc:  # noqa: BLE001 - 브로커 장애를 후보 단위로 격리
            self.strategy_store.record(
                candidate_ref=candidate_ref,
                signature=signature,
                symbol=symbol,
                role=self.agent.role,
                cli=self.agent.cli,
                model=self.agent.model,
                prompt_version=self.prompt_version,
                prompt_digest=self.prompt_digest,
                request={},
                raw_response="",
                status=STATUS_ERROR,
                reject_code="QUOTE_UNAVAILABLE",
                reject_reason=type(exc).__name__,
            )
            raise QuoteUnavailable(symbol) from exc

        request = {
            "candidate": payload,
            "symbol": symbol,
            "reference_price": str(reference_price),
            "lowest_entry_price": str(lowest_entry),
            "requested_at": utc_now_iso(),
        }

        self.quota.check()
        raw = ""
        try:
            raw = self.agent.review(request)
        except AgentError:
            self.quota.record(
                role=self.agent.role, cli=self.agent.cli, outcome="ERROR"
            )
            self.strategy_store.record(
                candidate_ref=candidate_ref,
                signature=signature,
                symbol=symbol,
                role=self.agent.role,
                cli=self.agent.cli,
                model=self.agent.model,
                prompt_version=self.prompt_version,
                prompt_digest=self.prompt_digest,
                request=request,
                raw_response="",
                status=STATUS_ERROR,
                reject_code="AGENT_ERROR",
                reject_reason="게이트웨이 호출 실패",
            )
            raise

        # 실패한 호출도 구독 사용량은 소모한다. 성공 여부와 무관하게 기록한다.
        self.quota.record(role=self.agent.role, cli=self.agent.cli, outcome="OK")

        try:
            decision = parse_decision(_as_json(raw))
            validate_decision(
                decision,
                lowest_entry_price=lowest_entry,
                reference_price=reference_price,
                band_pct=self.band_pct,
            )
        except ReviewRejected as exc:
            self.strategy_store.record(
                candidate_ref=candidate_ref,
                signature=signature,
                symbol=symbol,
                role=self.agent.role,
                cli=self.agent.cli,
                model=self.agent.model,
                prompt_version=self.prompt_version,
                prompt_digest=self.prompt_digest,
                request=request,
                raw_response=raw,
                status=STATUS_REJECTED,
                reject_code=exc.code,
                reject_reason=str(exc),
            )
            self._record_strategy_event(
                event_type="REVIEW_REJECTED",
                symbol=symbol,
                summary=f"[전략] {symbol} 심사 거절 ({exc.code})",
                severity="warning",
                payload={"code": exc.code, "reason": str(exc)[:500]},
            )
            return "rejected"

        status = STATUS_ACCEPTED if decision.approved else STATUS_REJECTED
        self.strategy_store.record(
            candidate_ref=candidate_ref,
            signature=signature,
            symbol=symbol,
            role=self.agent.role,
            cli=self.agent.cli,
            model=self.agent.model,
            prompt_version=self.prompt_version,
            prompt_digest=self.prompt_digest,
            request=request,
            raw_response=raw,
            status=status,
            decision=decision,
            reject_code=None if decision.approved else "AGENT_REJECT",
            reject_reason=None if decision.approved else "; ".join(decision.reasons),
        )
        self._record_strategy_event(
            event_type="REVIEW_APPROVED" if decision.approved else "REVIEW_DECLINED",
            symbol=symbol,
            summary=(
                f"[전략] {symbol} 심사 "
                f"{'승인' if decision.approved else '거절'} "
                f"(confidence {decision.confidence})"
            ),
            payload=decision.to_dict(),
        )
        return "approved" if decision.approved else "rejected"

    def _record_strategy_event(
        self,
        *,
        event_type: str,
        symbol: str,
        summary: str,
        payload: dict[str, Any],
        severity: str = "info",
    ) -> None:
        record_core_event(
            self.store,
            category="STRATEGY",
            event_type=event_type,
            severity=severity,
            symbol=symbol or None,
            summary=summary[:500],
            payload=payload,
            channels=self.channels,
        )

    def _record_system_event(
        self,
        event_type: str,
        summary: str,
        *,
        severity: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            record_core_event(
                self.store,
                category="SYSTEM",
                event_type=event_type,
                severity=severity,
                summary=summary[:500],
                payload=payload,
                channels=self.channels,
            )
        except Exception:  # noqa: BLE001 - 기록 실패로 워커를 멈추지 않는다
            pass

    def run_forever(
        self,
        *,
        interval_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        max_cycles: int | None = None,
    ) -> None:
        cycles = 0
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 한 주기 실패로 워커를 멈추지 않는다
                logger.exception("strategy worker cycle failed")
                self._record_system_event(
                    "WORKER_ERROR",
                    f"[전략] 워커 주기 실패: {type(exc).__name__}",
                    severity="critical",
                    payload={"error": type(exc).__name__},
                )
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            sleep(interval_seconds)


def _as_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        raise ReviewRejected("SCHEMA_INVALID", "응답을 JSON으로 읽을 수 없습니다.")


def _entries(payload: dict[str, Any]) -> list[Decimal]:
    prices: list[Decimal] = []
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        try:
            prices.append(Decimal(str(entry.get("price"))))
        except (InvalidOperation, TypeError):
            continue
    return prices
