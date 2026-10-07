"""LLM 판단 원장: 추론 결과를 정해진 값으로 적재하고, 코드는 이것만 읽는다.

원칙
- 추가 전용. UPDATE·DELETE는 SQLite 트리거로 막는다. 판단이 바뀌면 새 행을 쓴다.
- 시점 조회: as_of 시각에 "이미 알고 있었고 유효했던" 판단만 돌려준다(백테스트 오염 방지).
- 판단은 승인된 투자논리 버전에 묶인다. 카드가 재승인되면 이전 버전에 대한 판단은 효력을 잃는다.
- broken은 사용자가 승인한 자동 무효화 조건(invalidation.auto)을 그대로 인용할 때만 인정한다.
  인용이 없거나 다르면 weakened로 낮춰 기록한다(LLM이 사용자 논리를 스스로 폐기하지 못하게).
- 이 모듈은 주문을 만들지 않는다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.events import utc_now_iso
from core.theses import ThesisStore

KINDS = {"thesis_health", "tilt", "event_risk"}
HEALTH_VALUES = {"intact", "weakened", "broken"}
EVENT_TYPES = {"earnings", "guidance", "legal", "product", "macro", "other"}
CHECK_STATUS = {"ok", "warning", "unknown"}
MAX_VALIDITY = timedelta(days=7)
PROMPT_VERSION = "judge-v1"


def _ts(value: str) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("시각에는 시간대가 필요합니다.")
    return stamp.astimezone(timezone.utc)


def _short(value: Any, limit: int, name: str) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name}은(는) {limit}자 이하 문자열이어야 합니다.")
    return value.strip()


class JudgmentStore:
    def __init__(self, store):
        self.store = store
        self.theses = ThesisStore(store)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS llm_judgments (
                    id INTEGER PRIMARY KEY,
                    judgment_id TEXT NOT NULL UNIQUE,
                    thesis_id TEXT NOT NULL, thesis_version INTEGER NOT NULL, symbol TEXT NOT NULL,
                    kind TEXT NOT NULL, value TEXT NOT NULL, rule TEXT, detail TEXT NOT NULL,
                    valid_from TEXT NOT NULL, valid_until TEXT NOT NULL,
                    source TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
                    input_refs TEXT NOT NULL, explanation TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS llm_judgments_lookup ON llm_judgments(thesis_id, kind, valid_from);
                CREATE TRIGGER IF NOT EXISTS llm_judgments_no_update BEFORE UPDATE ON llm_judgments
                    BEGIN SELECT RAISE(ABORT, 'llm_judgments is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS llm_judgments_no_delete BEFORE DELETE ON llm_judgments
                    BEGIN SELECT RAISE(ABORT, 'llm_judgments is append-only'); END;
                CREATE TABLE IF NOT EXISTS judgment_requests (
                    call_id TEXT PRIMARY KEY, requested_at TEXT NOT NULL, theses TEXT NOT NULL,
                    state TEXT NOT NULL, note TEXT);
            """)

    # ---- 쓰기 -------------------------------------------------------------
    def record(self, *, judgment_id: str, thesis_id: str, kind: str, value: Any, valid_from: str,
               valid_until: str, source: str, model: str, explanation: str = "", rule: str | None = None,
               detail: dict[str, Any] | None = None, input_refs: list[str] | None = None,
               prompt_version: str = PROMPT_VERSION) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.:\-]{3,160}", judgment_id or ""):
            raise ValueError("judgment_id 형식이 올바르지 않습니다.")
        if kind not in KINDS:
            raise ValueError(f"판단 종류는 {sorted(KINDS)} 중 하나입니다.")
        if source not in {"llm", "user", "system"}:
            raise ValueError("source는 llm, user, system 중 하나입니다.")
        start, end = _ts(valid_from), _ts(valid_until)
        if not start < end <= start + MAX_VALIDITY:
            raise ValueError("유효기간은 시작 이후이고 최대 7일입니다.")
        thesis = self.theses.get(thesis_id, with_events=False)
        card = thesis["approved_card"]
        if card is None:
            raise ValueError("승인되지 않은 투자논리에는 판단을 기록할 수 없습니다.")
        detail = dict(detail or {})
        stored_rule = None
        if kind == "thesis_health":
            if value not in HEALTH_VALUES:
                raise ValueError("thesis_health 값은 intact, weakened, broken입니다.")
            if value == "broken":
                if rule not in card["invalidation"]["auto"]:
                    value = "weakened"
                    detail["downgraded"] = "승인된 자동 무효화 조건을 인용하지 않아 broken을 weakened로 기록"
                else:
                    stored_rule = rule
            checks = detail.get("checks", [])
            known = {c["id"] for c in card["checks"]}
            if not isinstance(checks, list) or any(
                    not isinstance(c, dict) or c.get("id") not in known or c.get("status") not in CHECK_STATUS
                    for c in checks):
                raise ValueError("점검 결과는 카드의 점검 항목 id와 ok/warning/unknown 상태여야 합니다.")
            detail["checks"] = [{"id": c["id"], "status": c["status"],
                                 "evidence": _short(c.get("evidence", ""), 500, "evidence")} for c in checks]
            text_value = value
        elif kind == "tilt":
            if not isinstance(value, int) or isinstance(value, bool) or not -2 <= value <= 2:
                raise ValueError("tilt는 -2~2 정수입니다.")
            text_value = str(value)
        else:
            if value not in EVENT_TYPES:
                raise ValueError(f"event_risk 값은 {sorted(EVENT_TYPES)} 중 하나입니다.")
            if "at" in detail and detail["at"]:
                detail["at"] = _ts(detail["at"]).isoformat()
            text_value = value
        row = (judgment_id, thesis_id, thesis["approved_version"], thesis["symbol"], kind, text_value, stored_rule,
               json.dumps(detail, ensure_ascii=False, sort_keys=True), start.isoformat(), end.isoformat(), source,
               _short(model, 100, "model"), _short(prompt_version, 50, "prompt_version"),
               json.dumps(input_refs or [], ensure_ascii=False), _short(explanation, 2000, "explanation"),
               utc_now_iso())
        with self.store.connect() as db:
            existing = db.execute("SELECT * FROM llm_judgments WHERE judgment_id=?", (judgment_id,)).fetchone()
            if existing:
                return self._decode(existing)
            db.execute("""INSERT INTO llm_judgments(judgment_id,thesis_id,thesis_version,symbol,kind,value,rule,detail,
                          valid_from,valid_until,source,model,prompt_version,input_refs,explanation,created_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row)
            saved = db.execute("SELECT * FROM llm_judgments WHERE judgment_id=?", (judgment_id,)).fetchone()
        result = self._decode(saved)
        if kind == "thesis_health":
            # 사람이 보는 표시용 상태. 비중 계산은 이 원장의 유효한 행만 읽는다.
            self.theses.set_health(thesis_id, text_value, explanation[:500] or text_value, source=source)
        return result

    # ---- 읽기 -------------------------------------------------------------
    @staticmethod
    def _decode(row) -> dict[str, Any]:
        item = dict(row)
        item["detail"] = json.loads(item["detail"])
        item["input_refs"] = json.loads(item["input_refs"])
        if item["kind"] == "tilt":
            item["value"] = int(item["value"])
        return item

    def history(self, thesis_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM llm_judgments" + (" WHERE thesis_id=?" if thesis_id else "") +
                " ORDER BY id DESC LIMIT ?", ((thesis_id, limit) if thesis_id else (limit,))).fetchall()
        return [self._decode(r) for r in rows]

    def current(self, as_of: datetime | None = None) -> dict[str, dict[str, Any]]:
        """as_of 시점에 알려져 있었고 유효한 최신 판단을 투자논리별·종류별로 돌려준다.

        현재 승인 버전과 다른 버전에 대한 판단은 제외한다(과거 시점 조회에서는 그 시점 버전을 알 수 없어
        현재 승인 버전 기준으로 거른다. 백테스트용 재현은 thesis_version을 함께 저장해 두었다).
        """
        as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        stamp = as_of.isoformat()
        approved = {t["id"]: t["version"] for t in self.theses.effective()}
        out: dict[str, dict[str, Any]] = {}
        with self.store.connect() as db:
            rows = db.execute("""SELECT * FROM llm_judgments
                                 WHERE valid_from<=? AND valid_until>? AND created_at<=?
                                 ORDER BY valid_from, id""", (stamp, stamp, stamp)).fetchall()
        for row in rows:
            item = self._decode(row)
            if approved.get(item["thesis_id"]) != item["thesis_version"]:
                continue
            out.setdefault(item["thesis_id"], {})[item["kind"]] = item  # 뒤에 온 행이 이긴다
        return out

    # ---- LLM 요청·적재 (mind 'judge' 역할) --------------------------------
    def request_daily(self, spool: Path, *, now: datetime | None = None, market: dict[str, Any] | None = None,
                      hour_kst: int = 7) -> dict[str, Any]:
        """승인된 카드가 있으면 KST 하루 한 번(기본 07시 이후) 판단을 요청한다. 멱등."""
        now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Seoul"))
        if now.hour < hour_kst:
            return {"requested": False, "reason": "요청 시각 전"}
        cards = self.theses.effective()
        if not cards:
            return {"requested": False, "reason": "승인된 투자논리 없음"}
        call_id = f"judge-{now:%Y%m%d}"
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM judgment_requests WHERE call_id=?", (call_id,)).fetchone():
                return {"requested": False, "reason": "오늘 이미 요청함", "call_id": call_id}
            requests = spool / "requests"
            requests.mkdir(parents=True, exist_ok=True, mode=0o700)
            (spool / "responses").mkdir(parents=True, exist_ok=True, mode=0o700)
            notes = {c["id"]: [e["detail"].get("text") for e in self.theses.get(c["id"])["events"]
                               if e["kind"] == "note"][-5:] for c in cards}
            body = {"_role": "judge", "_call_id": call_id, "as_of": now.isoformat(),
                    "theses": cards, "userNotes": notes, "market": market or {},
                    "instruction": "각 투자논리의 상태·틸트·이벤트 위험을 판단하라. 입력에 근거가 없으면 intact와 unknown 점검 결과를 유지하라."}
            temp = requests / f".{call_id}.tmp"
            temp.write_text(json.dumps(body, ensure_ascii=False))
            temp.replace(requests / f"{call_id}.json")
            db.execute("INSERT INTO judgment_requests VALUES (?,?,?,?,?)",
                       (call_id, utc_now_iso(), json.dumps({c["id"]: c["version"] for c in cards}), "requested", None))
        return {"requested": True, "call_id": call_id}

    def ingest(self, spool: Path, *, model: str = "mind-planner") -> list[dict[str, Any]]:
        responses = spool / "responses"
        results: list[dict[str, Any]] = []
        with self.store.connect() as db:
            pending = [dict(r) for r in db.execute("SELECT * FROM judgment_requests WHERE state='requested'")]
        for req in pending:
            call_id = req["call_id"]
            ok, err = responses / f"{call_id}.json", responses / f"{call_id}.error"
            if err.exists():
                self._close(call_id, "failed", "mind 호출 실패")
                results.append({"call_id": call_id, "result": "failed"})
                continue
            if not ok.exists():
                continue
            versions = json.loads(req["theses"])
            try:
                if ok.stat().st_size > 262144:
                    raise ValueError("응답이 너무 큽니다.")
                body = json.loads(ok.read_text(encoding="utf-8"))
                items = body["judgments"]
                if not isinstance(items, list):
                    raise ValueError("judgments 목록이 필요합니다.")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self._close(call_id, "invalid", type(exc).__name__)
                results.append({"call_id": call_id, "result": "invalid"})
                continue
            valid_from = _ts(req["requested_at"])
            valid_until = valid_from + timedelta(days=3)
            written, skipped = 0, 0
            for item in items:
                try:
                    thesis_id = item["thesisId"]
                    if thesis_id not in versions:
                        raise ValueError("요청하지 않은 투자논리")
                    current = self.theses.get(thesis_id, with_events=False)
                    if current["approved_version"] != versions[thesis_id] or current["status"] not in {"active", "watching"}:
                        raise ValueError("요청 후 카드가 바뀌거나 정지됨")
                    common = dict(thesis_id=thesis_id, valid_from=valid_from.isoformat(),
                                  valid_until=valid_until.isoformat(), source="llm", model=model,
                                  explanation=item.get("explanation", ""), input_refs=[f"spool:{call_id}"])
                    self.record(judgment_id=f"{call_id}:{thesis_id}:health", kind="thesis_health",
                                value=item["health"], rule=item.get("rule") or None,
                                detail={"checks": item.get("checks", [])}, **common)
                    self.record(judgment_id=f"{call_id}:{thesis_id}:tilt", kind="tilt", value=item["tilt"], **common)
                    for n, event in enumerate(item.get("eventRisks", [])[:5]):
                        self.record(judgment_id=f"{call_id}:{thesis_id}:event:{n}", kind="event_risk",
                                    value=event["type"],
                                    detail={"at": event.get("at") or None,
                                            "note": _short(event.get("note", ""), 300, "note")}, **common)
                    written += 1
                except (ValueError, KeyError, TypeError) as exc:
                    skipped += 1
                    try:
                        self.theses.note(item.get("thesisId", ""), f"AI 판단 적재 실패: {type(exc).__name__}", source="system")
                    except (ValueError, AttributeError):
                        pass
            self._close(call_id, "ingested", f"written={written} skipped={skipped}")
            results.append({"call_id": call_id, "result": "ingested", "written": written, "skipped": skipped})
        return results

    def _close(self, call_id: str, state: str, note: str) -> None:
        with self.store.connect() as db:
            db.execute("UPDATE judgment_requests SET state=?,note=? WHERE call_id=?", (state, note, call_id))
