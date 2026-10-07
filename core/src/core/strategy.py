from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from core.events import utc_now_iso

DECISION_APPROVE = "APPROVE"
DECISION_REJECT = "REJECT"
DECISIONS = frozenset({DECISION_APPROVE, DECISION_REJECT})

STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
STATUS_ERROR = "ERROR"


class ReviewRejected(Exception):
    """에이전트 응답이 검증을 통과하지 못했다.

    예외로 끝내지 않고 사유 코드를 남긴다. 이 코드가 쌓이면
    "이 모델은 어떤 식으로 틀리는가"에 대한 실측 데이터가 된다.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReviewDecision:
    decision: str
    confidence: Decimal
    reasons: tuple[str, ...]
    invalidation_price: Decimal | None
    expires_in_minutes: int

    @property
    def approved(self) -> bool:
        return self.decision == DECISION_APPROVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "confidence": str(self.confidence),
            "reasons": list(self.reasons),
            "invalidation_price": (
                str(self.invalidation_price)
                if self.invalidation_price is not None
                else None
            ),
            "expires_in_minutes": self.expires_in_minutes,
        }


def parse_decision(raw: Any) -> ReviewDecision:
    """1차 검증: 스키마.

    에이전트 출력은 오염된 입력으로 취급한다. 타입과 필수 필드만 본다.
    """
    if not isinstance(raw, dict):
        raise ReviewRejected("SCHEMA_INVALID", "응답이 JSON 객체가 아닙니다.")

    decision = raw.get("decision")
    if decision not in DECISIONS:
        raise ReviewRejected(
            "SCHEMA_INVALID", f"decision은 {sorted(DECISIONS)} 중 하나여야 합니다."
        )

    try:
        confidence = Decimal(str(raw.get("confidence")))
    except (InvalidOperation, TypeError):
        raise ReviewRejected("SCHEMA_INVALID", "confidence가 숫자가 아닙니다.")
    if not (Decimal("0") <= confidence <= Decimal("1")):
        raise ReviewRejected("SCHEMA_INVALID", "confidence는 0과 1 사이여야 합니다.")

    reasons_raw = raw.get("reasons")
    if not isinstance(reasons_raw, list) or not reasons_raw:
        raise ReviewRejected("SCHEMA_INVALID", "reasons가 비어 있습니다.")
    reasons = tuple(str(item)[:200] for item in reasons_raw[:4])

    expires = raw.get("expires_in_minutes")
    if not isinstance(expires, int) or isinstance(expires, bool):
        raise ReviewRejected("SCHEMA_INVALID", "expires_in_minutes가 정수가 아닙니다.")

    invalidation_price: Decimal | None = None
    invalidation = raw.get("invalidation")
    if isinstance(invalidation, dict):
        try:
            invalidation_price = Decimal(str(invalidation.get("price")))
        except (InvalidOperation, TypeError):
            invalidation_price = None

    return ReviewDecision(
        decision=decision,
        confidence=confidence,
        reasons=reasons,
        invalidation_price=invalidation_price,
        expires_in_minutes=expires,
    )


def validate_decision(
    decision: ReviewDecision,
    *,
    lowest_entry_price: Decimal,
    reference_price: Decimal,
    band_pct: Decimal = Decimal("10"),
    min_confidence: Decimal = Decimal("0.6"),
    max_expiry_minutes: int = 1440,
) -> None:
    """2차 검증: 정합성.

    reference_price는 반드시 게이트웨이가 직접 조회한 현재가여야 한다.
    에이전트가 준 가격을 기준으로 삼으면 검증이 아니라 자기참조가 된다.
    """
    if not decision.approved:
        return

    if decision.confidence < min_confidence:
        raise ReviewRejected(
            "LOW_CONFIDENCE",
            f"confidence {decision.confidence} < {min_confidence}",
        )

    if decision.invalidation_price is None:
        raise ReviewRejected(
            "INVALIDATION_MISSING", "승인에는 무효화 가격이 필요합니다."
        )

    if decision.invalidation_price >= lowest_entry_price:
        raise ReviewRejected(
            "INVALIDATION_ABOVE_ENTRY",
            f"무효화 가격 {decision.invalidation_price}이(가) "
            f"최저 진입가 {lowest_entry_price} 이상입니다. 진입 즉시 손절됩니다.",
        )

    if reference_price <= 0:
        raise ReviewRejected("REFERENCE_PRICE_INVALID", "기준 가격이 유효하지 않습니다.")

    band = reference_price * band_pct / Decimal("100")
    if abs(lowest_entry_price - reference_price) > band:
        raise ReviewRejected(
            "BAND_VIOLATION",
            f"진입가 {lowest_entry_price}이(가) 현재가 {reference_price} "
            f"±{band_pct}% 밴드를 벗어났습니다.",
        )

    if not (5 <= decision.expires_in_minutes <= max_expiry_minutes):
        raise ReviewRejected(
            "EXPIRY_INVALID",
            f"유효 시간 {decision.expires_in_minutes}분이 허용 범위를 벗어났습니다.",
        )


def candidate_signature(payload: dict[str, Any]) -> str:
    """같은 상황을 반복 질의하지 않기 위한 서명.

    급락장에서 같은 종목이 30초마다 조건을 다시 만족시켜
    예산을 태우는 것이 가장 흔한 사고다.
    """
    material = {
        "symbol": payload.get("symbol"),
        "stage": payload.get("stage"),
        "regime": payload.get("regime"),
        "drop_bucket": payload.get("drop_bucket"),
        "pressure_bucket": payload.get("pressure_bucket"),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]


class StrategyStore:
    """심사 원장.

    승인·거절·오류를 모두 남긴다. 프롬프트 해시를 함께 저장하지 않으면
    몇 달 뒤 "왜 그때 이런 판단이 나왔는지"를 재현할 수 없다.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def record(
        self,
        *,
        candidate_ref: str,
        signature: str,
        symbol: str,
        role: str,
        cli: str,
        model: str,
        prompt_version: str,
        prompt_digest: str,
        request: dict[str, Any],
        raw_response: str,
        status: str,
        decision: ReviewDecision | None = None,
        reject_code: str | None = None,
        reject_reason: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO strategy_reviews (
                    candidate_ref, signature, symbol, role, cli, model,
                    prompt_version, prompt_digest, request_json, raw_response,
                    status, decision_json, reject_code, reject_reason, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_ref,
                    signature,
                    symbol,
                    role,
                    cli,
                    model,
                    prompt_version,
                    prompt_digest,
                    json.dumps(request, ensure_ascii=False, sort_keys=True, default=str),
                    raw_response[:8000],
                    status,
                    json.dumps(decision.to_dict(), ensure_ascii=False)
                    if decision
                    else None,
                    reject_code,
                    (reject_reason or "")[:500] or None,
                    utc_now_iso(),
                ),
            )
            row_id = cursor.lastrowid
        if row_id is None:
            raise RuntimeError("심사 기록 ID를 생성하지 못했습니다.")
        return int(row_id)

    def find_cached(
        self,
        signature: str,
        *,
        ttl_minutes: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """TTL 안의 같은 서명 결과를 재사용한다."""
        now = now or datetime.now(timezone.utc)
        cutoff = (now - timedelta(minutes=ttl_minutes)).isoformat()
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM strategy_reviews
                WHERE signature = ? AND status = ? AND created_at >= ?
                ORDER BY id DESC LIMIT 1
                """,
                (signature, STATUS_ACCEPTED, cutoff),
            ).fetchone()
        return {key: row[key] for key in row.keys()} if row else None

    def reject_stats(self) -> dict[str, int]:
        """거절 사유 분포. 모델의 실패 패턴을 보는 창이다."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT reject_code, COUNT(*) FROM strategy_reviews
                WHERE reject_code IS NOT NULL
                GROUP BY reject_code ORDER BY COUNT(*) DESC
                """
            ).fetchall()
        return {str(code): int(count) for code, count in rows}

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_ref TEXT NOT NULL,
                signature TEXT NOT NULL,
                symbol TEXT NOT NULL,
                role TEXT NOT NULL,
                cli TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                prompt_digest TEXT NOT NULL,
                request_json TEXT NOT NULL,
                raw_response TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_json TEXT,
                reject_code TEXT,
                reject_reason TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_reviews_signature "
            "ON strategy_reviews(signature, created_at)"
        )
        return connection
