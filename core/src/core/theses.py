"""투자논리 원장: 사용자의 큰 판단을 버전 있는 카드로 보관한다.

- 사용자는 언제든 카드를 추가·수정·승인·관찰 전환·종료할 수 있다.
- AI 초안은 draft 카드만 채운다. 효력은 사용자가 승인한 버전(approved_card)에만 있다.
- 이 모듈은 주문을 만들지 않는다. 비중·주문은 별도 계층이 승인본만 읽어 처리한다.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from core.events import utc_now_iso

KINDS = {"position", "watch"}
STATUSES = {"draft", "active", "watching", "paused", "closed"}
HEALTH = {"unknown", "intact", "weakened", "broken"}
MAX_WEIGHT_PCT = 15  # 종목 한도(비중 규칙 v1). 카드가 이보다 크게 요구할 수 없다.
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
THESIS_ID = re.compile(r"[a-z0-9][a-z0-9\-]{2,63}")
EDITABLE = {"conviction", "userStatement", "thesis", "checks", "counterThesis",
            "invalidation", "sizing", "notes", "relatedSymbols"}


def _text(value: Any, name: str, limit: int = 2000, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"{name}은(는) {limit}자 이하 문자열이어야 합니다.")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name}이(가) 비어 있습니다.")
    return value.strip()


def _texts(value: Any, name: str, limit: int = 10) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{name}은(는) 최대 {limit}개 목록이어야 합니다.")
    return [_text(v, name, 500, allow_empty=False) for v in value]


def validate_card(card: dict[str, Any]) -> dict[str, Any]:
    """카드 구조를 검사하고 정규화된 사본을 돌려준다."""
    if not isinstance(card, dict):
        raise ValueError("카드는 객체여야 합니다.")
    unknown = set(card) - EDITABLE - {"symbol", "kind"}
    if unknown:
        raise ValueError(f"알 수 없는 카드 필드: {sorted(unknown)}")
    out: dict[str, Any] = {}
    symbol = card.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("종목 기호가 올바르지 않습니다. 예: AMD, BRK.B")
    out["symbol"] = symbol
    kind = card.get("kind", "position")
    if kind not in KINDS:
        raise ValueError("kind는 position 또는 watch입니다.")
    out["kind"] = kind
    conviction = card.get("conviction", 2)
    if not isinstance(conviction, int) or isinstance(conviction, bool) or not 1 <= conviction <= 3:
        raise ValueError("확신도(conviction)는 1~3 정수입니다.")
    out["conviction"] = conviction
    out["userStatement"] = _text(card.get("userStatement", ""), "userStatement", 4000, allow_empty=False)
    out["thesis"] = _text(card.get("thesis", ""), "thesis")
    out["relatedSymbols"] = _texts(card.get("relatedSymbols", []), "relatedSymbols")
    for s in out["relatedSymbols"]:
        if not SYMBOL.fullmatch(s):
            raise ValueError(f"관련 종목 기호가 올바르지 않습니다: {s}")
    checks = card.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 12:
        raise ValueError("checks는 최대 12개입니다.")
    out["checks"] = []
    seen = set()
    for item in checks:
        if not isinstance(item, dict) or set(item) - {"id", "what", "source", "healthy", "warning"}:
            raise ValueError("점검 항목 형식: id, what, source, healthy, warning")
        cid = _text(item.get("id", ""), "check.id", 64, allow_empty=False)
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,63}", cid) or cid in seen:
            raise ValueError(f"점검 항목 id가 올바르지 않거나 중복입니다: {cid}")
        seen.add(cid)
        out["checks"].append({
            "id": cid,
            "what": _text(item.get("what", ""), "check.what", 300, allow_empty=False),
            "source": _text(item.get("source", ""), "check.source", 200),
            "healthy": _text(item.get("healthy", ""), "check.healthy", 300),
            "warning": _text(item.get("warning", ""), "check.warning", 300),
        })
    out["counterThesis"] = _texts(card.get("counterThesis", []), "counterThesis")
    inv = card.get("invalidation", {"auto": [], "alertOnly": []})
    if not isinstance(inv, dict) or set(inv) - {"auto", "alertOnly"}:
        raise ValueError("invalidation은 auto, alertOnly 목록만 가집니다.")
    out["invalidation"] = {"auto": _texts(inv.get("auto", []), "invalidation.auto"),
                           "alertOnly": _texts(inv.get("alertOnly", []), "invalidation.alertOnly")}
    sizing = card.get("sizing", {})
    if not isinstance(sizing, dict) or set(sizing) - {"maxWeightPct", "addOnDip"}:
        raise ValueError("sizing은 maxWeightPct, addOnDip만 가집니다.")
    max_weight = sizing.get("maxWeightPct", 0 if kind == "watch" else 10)
    if not isinstance(max_weight, (int, float)) or isinstance(max_weight, bool) or not 0 <= max_weight <= MAX_WEIGHT_PCT:
        raise ValueError(f"maxWeightPct는 0~{MAX_WEIGHT_PCT} 사이입니다(종목 한도).")
    if kind == "watch" and max_weight != 0:
        raise ValueError("관찰 카드는 비중을 가질 수 없습니다(maxWeightPct=0).")
    add_on_dip = sizing.get("addOnDip", False)
    if not isinstance(add_on_dip, bool):
        raise ValueError("addOnDip은 true/false입니다.")
    out["sizing"] = {"maxWeightPct": max_weight, "addOnDip": add_on_dip and kind == "position"}
    out["notes"] = _texts(card.get("notes", []), "notes", 20)
    return out


class ThesisStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS theses (
                    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, health TEXT NOT NULL,
                    version INTEGER NOT NULL, draft_card TEXT NOT NULL,
                    approved_version INTEGER, approved_card TEXT, approved_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS thesis_events (
                    id INTEGER PRIMARY KEY, thesis_id TEXT NOT NULL, at TEXT NOT NULL,
                    kind TEXT NOT NULL, source TEXT NOT NULL, detail TEXT NOT NULL);
            """)

    # ---- 조회 -------------------------------------------------------------
    @staticmethod
    def _decode(row) -> dict[str, Any]:
        if row is None:
            raise ValueError("투자논리를 찾을 수 없습니다.")
        item = dict(row)
        item["draft_card"] = json.loads(item["draft_card"])
        item["approved_card"] = json.loads(item["approved_card"]) if item["approved_card"] else None
        item["pending_changes"] = item["approved_version"] != item["version"]
        return item

    def get(self, thesis_id: str, with_events: bool = True) -> dict[str, Any]:
        with self.store.connect() as db:
            item = self._decode(db.execute("SELECT * FROM theses WHERE id=?", (thesis_id,)).fetchone())
            if with_events:
                item["events"] = [dict(r, detail=json.loads(r["detail"])) for r in db.execute(
                    "SELECT at,kind,source,detail FROM thesis_events WHERE thesis_id=? ORDER BY id", (thesis_id,))]
        return item

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"상태는 {sorted(STATUSES)} 중 하나입니다.")
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM theses" + (" WHERE status=?" if status else "") + " ORDER BY symbol, created_at",
                (status,) if status else ()).fetchall()
        return [self._decode(r) for r in rows]

    def effective(self) -> list[dict[str, Any]]:
        """다른 계층이 읽는 유일한 입력: 승인된 버전만, 진행 중인 카드만."""
        out = []
        for item in self.list():
            if item["status"] in {"active", "watching"} and item["approved_card"]:
                out.append({"id": item["id"], "health": item["health"], "version": item["approved_version"],
                            **item["approved_card"]})
        return out

    # ---- 변경 -------------------------------------------------------------
    def _event(self, db, thesis_id: str, kind: str, source: str, detail: dict[str, Any]) -> None:
        db.execute("INSERT INTO thesis_events(thesis_id,at,kind,source,detail) VALUES (?,?,?,?,?)",
                   (thesis_id, utc_now_iso(), kind, source, json.dumps(detail, ensure_ascii=False)))

    def add(self, symbol: str, statement: str, *, kind: str = "position", conviction: int = 2,
            card: dict[str, Any] | None = None, thesis_id: str | None = None, source: str = "user") -> dict[str, Any]:
        base = {"symbol": symbol.strip().upper(), "kind": kind, "conviction": conviction, "userStatement": statement}
        if card:
            base.update({k: v for k, v in card.items() if k not in {"symbol", "kind"}})
        normalized = validate_card(base)
        if thesis_id is None:
            stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")  # 사용자 기준 날짜
            thesis_id = f"{normalized['symbol'].lower().replace('.', '-')}-{stamp}-{uuid.uuid4().hex[:4]}"
        if not THESIS_ID.fullmatch(thesis_id):
            raise ValueError("투자논리 ID는 영문 소문자·숫자·하이픈 3~64자입니다.")
        now = utc_now_iso()
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM theses WHERE id=?", (thesis_id,)).fetchone()
            if existing:
                if existing["draft_card"] != payload and existing["version"] == 1:
                    raise ValueError("같은 ID에 다른 내용의 투자논리가 있습니다.")
                return self._decode(existing)
            db.execute("INSERT INTO theses VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (thesis_id, normalized["symbol"], normalized["kind"], "draft", "unknown", 1, payload,
                        None, None, None, now, now))
            self._event(db, thesis_id, "created", source, {"version": 1})
        return self.get(thesis_id)

    def _get_in(self, db, thesis_id: str) -> dict[str, Any]:
        return self._decode(db.execute("SELECT * FROM theses WHERE id=?", (thesis_id,)).fetchone())

    def edit(self, thesis_id: str, patch: dict[str, Any], *, source: str = "user",
             expected_version: int | None = None) -> dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            raise ValueError("수정할 내용이 없습니다.")
        bad = set(patch) - EDITABLE
        if bad:
            raise ValueError(f"수정할 수 없는 필드: {sorted(bad)} (종목·종류는 새 카드로 만드세요)")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self._get_in(db, thesis_id)
            if item["status"] == "closed":
                raise ValueError("종료된 투자논리는 수정할 수 없습니다. 새 카드를 만드세요.")
            if expected_version is not None and item["version"] != expected_version:
                raise ValueError("그사이 카드가 바뀌었습니다. 최신 버전을 확인하세요.")
            card = copy.deepcopy(item["draft_card"])
            card.update(patch)
            normalized = validate_card(card)
            if normalized == item["draft_card"]:
                return item
            version = item["version"] + 1
            db.execute("UPDATE theses SET draft_card=?,version=?,updated_at=? WHERE id=?",
                       (json.dumps(normalized, ensure_ascii=False, sort_keys=True), version, utc_now_iso(), thesis_id))
            self._event(db, thesis_id, "edited", source, {"version": version, "fields": sorted(patch)})
        return self.get(thesis_id)

    def approve(self, thesis_id: str, version: int) -> dict[str, Any]:
        """사용자가 본 버전 번호를 함께 받아, 그사이 AI가 바꾼 내용을 모르고 승인하는 일을 막는다."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self._get_in(db, thesis_id)
            if item["status"] == "closed":
                raise ValueError("종료된 투자논리는 승인할 수 없습니다.")
            if item["version"] != version:
                raise ValueError(f"현재 버전은 {item['version']}입니다. 내용을 다시 확인하고 그 버전으로 승인하세요.")
            card = item["draft_card"]
            if not card["thesis"]:
                raise ValueError("요약(thesis)이 비어 있습니다. 직접 쓰거나 AI 초안을 기다리세요.")
            status = item["status"]
            if status in {"draft", "active", "watching"}:
                status = "watching" if card["kind"] == "watch" else "active"
            db.execute("UPDATE theses SET approved_card=?,approved_version=?,approved_at=?,status=?,updated_at=? WHERE id=?",
                       (json.dumps(card, ensure_ascii=False, sort_keys=True), version, utc_now_iso(), status,
                        utc_now_iso(), thesis_id))
            self._event(db, thesis_id, "approved", "user", {"version": version})
        return self.get(thesis_id)

    def set_status(self, thesis_id: str, status: str, reason: str = "") -> dict[str, Any]:
        """사용자 명령: paused(일시 정지), active/watching(재개), closed(종료)."""
        if status not in {"paused", "active", "watching", "closed"}:
            raise ValueError("바꿀 수 있는 상태: paused, active, watching, closed")
        if status == "closed" and not reason.strip():
            raise ValueError("종료할 때는 사유를 남겨 주세요.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self._get_in(db, thesis_id)
            if item["status"] == "closed":
                raise ValueError("이미 종료된 투자논리입니다.")
            if status in {"active", "watching"}:
                if not item["approved_card"]:
                    raise ValueError("승인된 버전이 없습니다. 먼저 승인하세요.")
                status = "watching" if item["approved_card"]["kind"] == "watch" else "active"
            db.execute("UPDATE theses SET status=?,updated_at=? WHERE id=?", (status, utc_now_iso(), thesis_id))
            self._event(db, thesis_id, "status", "user", {"status": status, "reason": _text(reason, "reason", 500)})
        return self.get(thesis_id)

    def note(self, thesis_id: str, text: str, source: str = "user") -> dict[str, Any]:
        with self.store.connect() as db:
            self._get_in(db, thesis_id)
            self._event(db, thesis_id, "note", source, {"text": _text(text, "note", 2000, allow_empty=False)})
        return self.get(thesis_id)

    def set_health(self, thesis_id: str, health: str, reason: str, source: str = "system") -> dict[str, Any]:
        """점검 결과 기록. 상태(active 등)나 승인본은 바꾸지 않는다."""
        if health not in HEALTH:
            raise ValueError(f"health는 {sorted(HEALTH)} 중 하나입니다.")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = self._get_in(db, thesis_id)
            if item["health"] != health:
                db.execute("UPDATE theses SET health=?,updated_at=? WHERE id=?", (health, utc_now_iso(), thesis_id))
                self._event(db, thesis_id, "health", source,
                            {"from": item["health"], "to": health, "reason": _text(reason, "reason", 2000)})
        return self.get(thesis_id)

    def import_file(self, path: Path) -> list[dict[str, Any]]:
        """시드 파일의 카드를 draft로 넣는다. 이미 있는 ID는 건너뛴다. 승인은 따로 해야 한다."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cards = data.get("theses") if isinstance(data, dict) else None
        if not isinstance(cards, list):
            raise ValueError("파일에는 {'theses': [...]} 형식이 필요합니다.")
        results = []
        for raw in cards:
            raw = dict(raw)
            thesis_id = raw.pop("id", None)
            if not thesis_id:
                raise ValueError("시드 카드마다 id가 필요합니다.")
            with self.store.connect() as db:
                exists = db.execute("SELECT 1 FROM theses WHERE id=?", (thesis_id,)).fetchone()
            if exists:
                results.append({"id": thesis_id, "result": "skipped"})
                continue
            self.add(raw.get("symbol", ""), raw.get("userStatement", ""), kind=raw.get("kind", "position"),
                     conviction=raw.get("conviction", 2), card=raw, thesis_id=thesis_id, source="import")
            results.append({"id": thesis_id, "result": "created"})
        return results

    # ---- AI 초안 (mind 'thesis' 역할) ---------------------------------------
    def request_ai_draft(self, thesis_id: str, spool: Path) -> dict[str, Any]:
        item = self.get(thesis_id, with_events=False)
        requests, responses = spool / "requests", spool / "responses"
        requests.mkdir(parents=True, exist_ok=True, mode=0o700)
        responses.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 같은 버전의 요청이 실패했으면(.error) 시도 번호를 붙여 새 요청으로 보낸다.
        # mind 루프는 응답(.json/.error)이 이미 있는 요청 ID를 다시 처리하지 않기 때문이다.
        base = f"thesis-{thesis_id}-v{item['version']}"
        attempt = 1
        while True:
            call_id = base if attempt == 1 else f"{base}-r{attempt}"
            if (responses / f"{call_id}.json").exists() or (requests / f"{call_id}.json").exists():
                return {"id": thesis_id, "requested": False, "call_id": call_id}
            if not (responses / f"{call_id}.error").exists():
                break
            attempt += 1
        card = item["draft_card"]
        body = {"_role": "thesis", "_call_id": call_id, "symbol": card["symbol"], "kind": card["kind"],
                "userStatement": card["userStatement"], "currentDraft": card}
        temp = requests / f".{call_id}.tmp"
        temp.write_text(json.dumps(body, ensure_ascii=False))
        temp.replace(requests / f"{call_id}.json")
        with self.store.connect() as db:
            self._event(db, thesis_id, "ai_requested", "system", {"call_id": call_id})
        return {"id": thesis_id, "requested": True, "call_id": call_id}

    def ingest_ai_drafts(self, spool: Path) -> list[dict[str, Any]]:
        """AI 응답을 draft에 반영한다. 요청 이후 사용자가 카드를 바꿨으면 버린다(사용자 우선)."""
        responses = spool / "responses"
        results = []
        if not responses.is_dir():
            return results
        for path in sorted(responses.glob("thesis-*-v*.*")):
            m = re.fullmatch(r"thesis-(.+)-v(\d+)(?:-r\d+)?\.(json|error)", path.name)
            if not m:
                continue
            thesis_id, version, ext = m.group(1), int(m.group(2)), m.group(3)
            done = path.with_suffix(path.suffix + ".done")
            if done.exists():
                continue
            try:
                item = self.get(thesis_id, with_events=False)
            except ValueError:
                done.touch()
                continue
            if ext == "error":
                with self.store.connect() as db:
                    self._event(db, thesis_id, "ai_failed", "system", {"version": version})
                results.append({"id": thesis_id, "result": "failed"})
            elif item["version"] != version:
                with self.store.connect() as db:
                    self._event(db, thesis_id, "ai_discarded", "system",
                                {"version": version, "reason": "요청 후 카드가 수정됨"})
                results.append({"id": thesis_id, "result": "stale"})
            else:
                try:
                    if path.stat().st_size > 65536:
                        raise ValueError("AI 응답이 너무 큽니다.")
                    draft = json.loads(path.read_text(encoding="utf-8"))
                    patch = {
                        "thesis": draft["thesis"],
                        "checks": draft["checks"],
                        "counterThesis": draft["counterThesis"],
                        "invalidation": {"auto": draft["invalidationAuto"], "alertOnly": draft["invalidationAlertOnly"]},
                        "relatedSymbols": draft.get("relatedSymbols", []),
                    }
                    self.edit(thesis_id, patch, source="ai", expected_version=version)
                    results.append({"id": thesis_id, "result": "drafted"})
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    with self.store.connect() as db:
                        self._event(db, thesis_id, "ai_invalid", "system", {"version": version, "error": type(exc).__name__})
                    results.append({"id": thesis_id, "result": "invalid"})
            done.touch()
        return results
