"""Durable, read-only SSH requests and a separately supervised report worker."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from core.events import utc_now_iso
from core.notify import DiscordNotifier
from core.reporting import collect_report, markdown, summary, write_artifacts

MESSAGES = {"현재 계좌", "계좌 보고", "계좌", "보고", "report"}


class RequestStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "requests.db"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, message TEXT NOT NULL, broker TEXT NOT NULL,
                channel TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
                report TEXT, paths TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT, delivered_at TEXT
            )""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, message: str, broker="both", channel="discord", request_id=None):
        message = " ".join(message.strip().split())
        if message not in MESSAGES:
            raise ValueError("현재 지원하는 요청: '현재 계좌', '계좌 보고', 'report'")
        if broker not in {"both", "kis", "toss"} or channel not in {"discord", "local"}:
            raise ValueError("지원하지 않는 브로커 또는 채널")
        request_id = request_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request_id):
            raise ValueError("요청 ID는 영문·숫자·밑줄·하이픈 1~64자여야 합니다.")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO requests(id,message,broker,channel,state,created_at) VALUES (?,?,?,?,?,?)",
                       (request_id, message, broker, channel, "pending", utc_now_iso()))
            row = dict(db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())
            if (row["message"], row["broker"], row["channel"]) != (message, broker, channel):
                raise ValueError("같은 요청 ID에 다른 요청을 사용할 수 없습니다.")
        return self.status(request_id)

    def status(self, request_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("요청 ID를 찾을 수 없습니다.")
        result = dict(row)
        report = result.pop("report")
        result["report_summary"] = summary(json.loads(report)) if report else None
        result["paths"] = json.loads(result["paths"]) if result["paths"] else None
        return result

    def list_requests(self, limit=30):
        if not 1 <= limit <= 100:
            raise ValueError("limit은 1~100이어야 합니다.")
        with self.connect() as db:
            rows = db.execute("SELECT id FROM requests ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
        return [self.status(row["id"]) for row in rows]

    def report(self, request_id="latest"):
        with self.connect() as db:
            if request_id == "latest":
                row = db.execute("SELECT report FROM requests WHERE report IS NOT NULL ORDER BY created_at DESC,id DESC LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT report FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None or row["report"] is None:
            raise ValueError("아직 생성된 보고서가 없습니다. ai-manager status로 접수 상태를 확인하세요.")
        return json.loads(row["report"])

    def run_once(self, collector=collect_report, notifier=None):
        # A process lock survives SSH disconnects and is released by the OS on crash.
        # Holding it across collection/delivery prevents two workers sending one job.
        fd = os.open(self.root / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"worker": "busy"}
            with self.connect() as db:
                row = db.execute("SELECT * FROM requests WHERE state IN ('pending','ready') AND next_attempt<=? ORDER BY created_at,id LIMIT 1", (time.time(),)).fetchone()
            if row is None:
                return {"worker": "idle"}
            job = dict(row)
            try:
                if job["report"] is None:
                    report = collector(job["id"], job["broker"])
                    with self.connect() as db:
                        db.execute("UPDATE requests SET report=?,state='ready' WHERE id=?", (json.dumps(report, ensure_ascii=False), job["id"]))
                else:
                    report = json.loads(job["report"])
                paths = write_artifacts(self.root / "reports", report)
                with self.connect() as db:
                    db.execute("UPDATE requests SET paths=? WHERE id=?", (json.dumps(paths), job["id"]))
                if job["channel"] == "discord":
                    sender = notifier or DiscordNotifier(
                        os.getenv("CORE_DISCORD_WEBHOOK_URL", ""),
                        username=os.getenv("CORE_DISCORD_USERNAME", "ai-manager"))
                    sender.send(summary(report))
                with self.connect() as db:
                    db.execute("UPDATE requests SET state='completed',error=NULL,delivered_at=? WHERE id=?",
                               (utc_now_iso() if job["channel"] == "discord" else None, job["id"]))
            except Exception as exc:
                # Never persist exception text: API errors may contain credentials.
                with self.connect() as db:
                    db.execute("UPDATE requests SET attempts=attempts+1,next_attempt=?,error=? WHERE id=?",
                               (time.time() + min(300, 5 * 2 ** min(job["attempts"], 6)), type(exc).__name__, job["id"]))
            return self.status(job["id"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI Manager SSH 계좌 보고 (조회 전용)")
    parser.add_argument("--data-dir", type=Path, help="요청 및 보고서 영속 디렉터리")
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("request")
    request.add_argument("message")
    request.add_argument("--broker", choices=("both", "kis", "toss"), default="both")
    request.add_argument("--channel", choices=("discord", "local"), default="discord")
    request.add_argument("--request-id")
    status = sub.add_parser("status")
    status.add_argument("request_id")
    reports = sub.add_parser("reports", help="최근 보고 요청 목록")
    reports.add_argument("--limit", type=int, default=30)
    reports.add_argument("--json", action="store_true")
    report = sub.add_parser("report", help="저장된 보고 메시지 읽기 (외부 전송 없음)")
    report.add_argument("request_id", nargs="?", default="latest")
    report.add_argument("--format", choices=("summary", "markdown", "json"), default="summary")
    worker = sub.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--interval", type=float, default=5)
    instruction = sub.add_parser("instruct", help="투자 지시 접수 (자동 실행 없음)")
    instruction.add_argument("message")
    instruction.add_argument("--request-id")
    inbox = sub.add_parser("instructions", help="투자 지시함")
    inbox.add_argument("--limit", type=int, default=30)
    inbox.add_argument("--json", action="store_true")
    detail = sub.add_parser("instruction", help="지시 내용·상태·결과")
    detail.add_argument("request_id")
    cancel = sub.add_parser("cancel-instruction")
    cancel.add_argument("request_id")
    update = sub.add_parser("record-instruction", help="처리 상태·결과 기록 (명령 실행 없음)")
    update.add_argument("request_id")
    update.add_argument("state", choices=("running", "completed", "failed"))
    update.add_argument("--result")
    device = sub.add_parser("register-device")
    device.add_argument("device_id")
    device.add_argument("token")
    device.add_argument("--environment", choices=("sandbox", "production"), required=True)
    unregister = sub.add_parser("unregister-device")
    unregister.add_argument("device_id")
    sub.add_parser("push-status")
    thesis = sub.add_parser("thesis", help="투자논리 카드: 추가·수정·승인·관찰·종료")
    tsub = thesis.add_subparsers(dest="thesis_action", required=True)
    t_add = tsub.add_parser("add", help="새 투자논리 (draft로 저장, 승인 전에는 효력 없음)")
    t_add.add_argument("symbol")
    t_add.add_argument("statement", help="내 판단을 그대로. 예: '마소 너무 싸졌다'")
    t_add.add_argument("--conviction", type=int, default=2, choices=(1, 2, 3))
    t_add.add_argument("--watch", action="store_true", help="관찰만 (비중 0)")
    t_add.add_argument("--max-weight", type=float, help="종목 최대 비중 %% (0~15)")
    t_add.add_argument("--id")
    t_add.add_argument("--no-ai", action="store_true", help="AI 초안 요청 안 함")
    t_list = tsub.add_parser("list")
    t_list.add_argument("--status", choices=("draft", "active", "watching", "paused", "closed"))
    t_list.add_argument("--json", action="store_true")
    t_show = tsub.add_parser("show")
    t_show.add_argument("thesis_id")
    t_edit = tsub.add_parser("edit", help="draft 수정 (버전 증가, 다시 승인 필요)")
    t_edit.add_argument("thesis_id")
    t_edit.add_argument("--conviction", type=int, choices=(1, 2, 3))
    t_edit.add_argument("--thesis", dest="thesis_text")
    t_edit.add_argument("--statement")
    t_edit.add_argument("--max-weight", type=float)
    t_edit.add_argument("--add-on-dip", choices=("on", "off"))
    t_edit.add_argument("--file", type=Path, help="수정할 필드를 담은 JSON 파일")
    t_approve = tsub.add_parser("approve", help="지금 draft 버전을 승인 (show로 확인한 버전 번호 필요)")
    t_approve.add_argument("thesis_id")
    t_approve.add_argument("--version", type=int, required=True)
    for name, text in (("pause", "일시 정지 (새 매수 안 함)"), ("resume", "재개")):
        p_ = tsub.add_parser(name, help=text)
        p_.add_argument("thesis_id")
        p_.add_argument("--reason", default="")
    t_close = tsub.add_parser("close", help="투자논리 종료")
    t_close.add_argument("thesis_id")
    t_close.add_argument("--reason", required=True)
    t_note = tsub.add_parser("note", help="메모 추가")
    t_note.add_argument("thesis_id")
    t_note.add_argument("text")
    t_import = tsub.add_parser("import", help="시드 파일에서 draft로 가져오기")
    t_import.add_argument("file", type=Path)
    t_ai = tsub.add_parser("ai", help="현재 draft로 AI 초안 다시 요청")
    t_ai.add_argument("thesis_id")
    judg = sub.add_parser("judgments", help="LLM 판단 원장 (추가 전용)")
    jsub = judg.add_subparsers(dest="judgment_action", required=True)
    j_list = jsub.add_parser("list", help="판단 이력")
    j_list.add_argument("--thesis")
    j_list.add_argument("--limit", type=int, default=30)
    j_list.add_argument("--json", action="store_true")
    j_cur = jsub.add_parser("current", help="지금 유효한 판단 (비중 계산이 읽는 값)")
    j_cur.add_argument("--as-of", help="ISO 시각. 그 시점에 알려져 있던 판단만")
    j_add = jsub.add_parser("add", help="사용자가 직접 판단 기록")
    j_add.add_argument("thesis_id")
    j_add.add_argument("kind", choices=("thesis_health", "tilt", "event_risk"))
    j_add.add_argument("value")
    j_add.add_argument("--rule", help="broken일 때 인용할 승인된 무효화 조건 문장")
    j_add.add_argument("--at", help="event_risk 시각 (ISO)")
    j_add.add_argument("--days", type=float, default=3.0, help="유효기간(일, 최대 7)")
    j_add.add_argument("--why", default="")
    jsub.add_parser("request", help="오늘 LLM 판단 요청 (하루 한 번)")
    alloc = sub.add_parser("allocation", help="비중 규칙 v1 + 투자논리 → 목표 비중 미리보기 (주문 없음)")
    alloc.add_argument("--input", type=Path, required=True,
                       help="JSON: closes(지수 종가), navs(계좌 NAV), vols(종목 변동성), current(현재 비중)")
    args = parser.parse_args(argv)
    if args.command == "worker" and not 0.1 <= args.interval <= 60:
        parser.error("interval은 0.1~60초여야 합니다.")
    # Load env without validating unrelated order/strategy settings.
    from core.config import _load_dotenv
    _load_dotenv()
    root = args.data_dir or Path(os.getenv("AI_MANAGER_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data/manager"))).expanduser()
    os.umask(0o077)
    store = RequestStore(root)
    try:
        if args.command in {"judgments", "allocation"}:
            result = run_judgment_command(store, args)
            if result is None:
                return 0
        elif args.command == "thesis":
            result = run_thesis_command(store, args)
            if result is None:
                return 0
        elif args.command in {"register-device", "unregister-device", "push-status"}:
            from core.push import PushStore
            pushes = PushStore(store)
            if args.command == "register-device":
                result = pushes.register(args.device_id, args.token, args.environment)
            elif args.command == "unregister-device":
                result = pushes.unregister(args.device_id)
            else:
                result = pushes.status()
        elif args.command in {"instruct", "instructions", "instruction", "cancel-instruction", "record-instruction"}:
            from core.instructions import InstructionStore
            inbox_store = InstructionStore(store)
            if args.command == "instruct":
                result = inbox_store.submit(args.message, args.request_id)
            elif args.command == "instructions":
                rows = inbox_store.list(args.limit)
                if not args.json:
                    print("\n".join(f"{r['id']}  {r['state']}  {r['message']!r}" for r in rows) or "접수된 지시가 없습니다.")
                    return 0
                result = {"schema_version": 1, "instructions": rows}
            elif args.command == "instruction":
                result = inbox_store.get(args.request_id)
            elif args.command == "cancel-instruction":
                result = inbox_store.transition(args.request_id, "cancelled")
            else:
                result = inbox_store.transition(args.request_id, args.state, args.result)
        elif args.command == "request":
            result = store.enqueue(args.message, args.broker, args.channel, args.request_id)
        elif args.command == "status":
            result = store.status(args.request_id)
        elif args.command == "reports":
            rows = store.list_requests(args.limit)
            if not args.json:
                print("\n".join(f"{r['id']}  {r['state']}  {r['created_at']}  {r['channel']}" for r in rows) or "접수된 보고 요청이 없습니다.")
                return 0
            result = {"schema_version": 1, "requests": rows}
        elif args.command == "report":
            result = store.report(args.request_id)
            if args.format != "json":
                print(markdown(result) if args.format == "markdown" else summary(result))
                return 0
        else:
            while True:
                try:
                    result = store.run_once()
                    run_background_once(store)
                except Exception as exc:  # noqa: BLE001 - 한 주기 실패로 워커를 멈추지 않는다
                    if args.once:
                        raise
                    # 예외 원문에는 인증 정보가 섞일 수 있어 종류만 남긴다.
                    print(json.dumps({"worker_error": type(exc).__name__}), flush=True)
                    time.sleep(args.interval)
                    continue
                if args.once:
                    break
                # Logs identify jobs, never print the user's account report.
                if "id" in result:
                    print(json.dumps({k: result[k] for k in ("id", "state", "error")}), flush=True)
                time.sleep(args.interval)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ValueError as exc:
        parser.error(str(exc))


def run_thesis_command(store, args):
    from core.theses import ThesisStore
    theses = ThesisStore(store)
    spool = store.root / "spool"
    ai_enabled = os.getenv("AI_MANAGER_PLANNER_ENABLED") == "true"
    action = args.thesis_action
    if action == "add":
        card = {}
        if args.max_weight is not None:
            card["sizing"] = {"maxWeightPct": args.max_weight, "addOnDip": False}
        result = theses.add(args.symbol, args.statement, kind="watch" if args.watch else "position",
                            conviction=args.conviction, card=card or None, thesis_id=args.id)
        if ai_enabled and not args.no_ai:
            result["ai"] = theses.request_ai_draft(result["id"], spool)
        else:
            result["ai"] = {"requested": False, "reason": "AI 계획기 비활성 또는 --no-ai"}
        result["next"] = f"ai-manager thesis show {result['id']} 로 확인 후 ai-manager thesis approve {result['id']} --version N"
        return result
    if action == "list":
        rows = theses.list(args.status)
        if args.json:
            return {"schema_version": 1, "theses": rows}
        for r in rows:
            card = r["draft_card"]
            pending = "*" if r["pending_changes"] else ""
            print(f"{r['id']}  {r['symbol']:<6} {r['status']:<8} {r['health']:<8} 확신{card['conviction']} "
                  f"v{r['version']}{pending}  {card['thesis'][:40] or card['userStatement'][:40]}")
        if not rows:
            print("등록된 투자논리가 없습니다. ai-manager thesis add SYMBOL '내 판단'")
        return None
    if action == "show":
        return theses.get(args.thesis_id)
    if action == "edit":
        patch = json.loads(args.file.read_text(encoding="utf-8")) if args.file else {}
        current = theses.get(args.thesis_id, with_events=False)["draft_card"]
        if args.conviction is not None:
            patch["conviction"] = args.conviction
        if args.thesis_text is not None:
            patch["thesis"] = args.thesis_text
        if args.statement is not None:
            patch["userStatement"] = args.statement
        if args.max_weight is not None or args.add_on_dip is not None:
            sizing = dict(current["sizing"])
            if args.max_weight is not None:
                sizing["maxWeightPct"] = args.max_weight
            if args.add_on_dip is not None:
                sizing["addOnDip"] = args.add_on_dip == "on"
            patch["sizing"] = sizing
        return theses.edit(args.thesis_id, patch)
    if action == "approve":
        return theses.approve(args.thesis_id, args.version)
    if action == "pause":
        return theses.set_status(args.thesis_id, "paused", args.reason)
    if action == "resume":
        return theses.set_status(args.thesis_id, "active", args.reason)
    if action == "close":
        return theses.set_status(args.thesis_id, "closed", args.reason)
    if action == "note":
        return theses.note(args.thesis_id, args.text)
    if action == "import":
        return {"imported": theses.import_file(args.file),
                "next": "ai-manager thesis list 로 확인 후 카드마다 approve 하세요."}
    if action == "ai":
        if not ai_enabled:
            raise ValueError("AI 계획기가 꺼져 있습니다(AI_MANAGER_PLANNER_ENABLED=true 필요).")
        return theses.request_ai_draft(args.thesis_id, spool)
    raise ValueError("알 수 없는 thesis 명령")


def run_judgment_command(store, args):
    from datetime import datetime, timedelta, timezone
    from core.judgments import JudgmentStore
    judgments = JudgmentStore(store)
    if args.command == "allocation":
        from core.allocation import equity_cap, sleeve_targets
        data = json.loads(args.input.read_text(encoding="utf-8"))
        cap = equity_cap(data["closes"], data["navs"])
        sleeve = sleeve_targets(cap["cap"], judgments.theses.effective(), judgments.current(),
                                vols=data.get("vols"), current=data.get("current"),
                                emergency_no_buys=cap["emergency_no_buys"], brake_active=cap["brake_factor"] < 1)
        return {"equity_cap": cap, "sleeve": sleeve.to_dict(), "note": "미리보기이며 주문을 만들지 않습니다."}
    action = args.judgment_action
    if action == "list":
        rows = judgments.history(args.thesis, args.limit)
        if args.json:
            return {"schema_version": 1, "judgments": rows}
        for r in rows:
            print(f"{r['created_at'][:16]}  {r['symbol']:<6} {r['kind']:<13} {str(r['value']):<9} "
                  f"~{r['valid_until'][:10]}  {r['source']}  {r['explanation'][:40]}")
        if not rows:
            print("기록된 판단이 없습니다.")
        return None
    if action == "current":
        as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
        return judgments.current(as_of)
    if action == "add":
        now = datetime.now(timezone.utc)
        value = int(args.value) if args.kind == "tilt" else args.value
        return judgments.record(
            judgment_id=f"user-{now:%Y%m%dT%H%M%S%f}-{args.thesis_id}-{args.kind}", thesis_id=args.thesis_id,
            kind=args.kind, value=value, rule=args.rule, detail={"at": args.at} if args.at else {},
            valid_from=now.isoformat(), valid_until=(now + timedelta(days=args.days)).isoformat(),
            source="user", model="user", explanation=args.why)
    if action == "request":
        if os.getenv("AI_MANAGER_PLANNER_ENABLED") != "true":
            raise ValueError("AI 계획기가 꺼져 있습니다(AI_MANAGER_PLANNER_ENABLED=true 필요).")
        return judgments.request_daily(store.root / "spool")
    raise ValueError("알 수 없는 judgments 명령")


def run_background_once(store):
    """Ingest research and deliver notifications without executing instructions."""
    from core.push import APNsSender, PushStore
    fd = os.open(store.root / "background.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        from core.theses import ThesisStore
        from core.judgments import JudgmentStore
        ThesisStore(store).ingest_ai_drafts(store.root / "spool")
        judgments = JudgmentStore(store)
        judgments.ingest(store.root / "spool")
        if os.getenv('AI_MANAGER_PLANNER_ENABLED') == 'true':
            judgments.request_daily(store.root / "spool")
        pushes = PushStore(store)
        if APNsSender.configured():
            # Reuse the HTTP/2 connection and signing token across polling cycles.
            if not hasattr(run_background_once, 'sender'):
                run_background_once.sender = APNsSender()
            pushes.deliver_once(run_background_once.sender)
        else:
            pushes.collect()


if __name__ == "__main__":
    raise SystemExit(main())
