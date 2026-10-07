"""Mode-pinned instruction orchestration, separated from execution adapters."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from core.events import utc_now_iso
from core.instructions import InstructionStore
from core.execution import ExecutionBackend, ExecutionOutcome, PlanError, amount, parse_plan
from core.paper_execution import PaperExecutionBackend


def make_backend(store, mode=None):
    mode = mode if mode is not None else os.getenv('AI_MANAGER_EXECUTION_MODE', 'paper')
    if mode != 'paper':
        raise ValueError('현재 설치본은 paper 실행 어댑터만 제공합니다. 실전 어댑터는 연결되지 않았습니다.')
    return PaperExecutionBackend(store)


class ExecutionEngine:
    def __init__(self, store, backend: ExecutionBackend | None = None):
        self.store = store
        self.inbox = InstructionStore(store)
        self.backend = backend if backend is not None else make_backend(store)
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS execution_runs (id TEXT PRIMARY KEY, mode TEXT NOT NULL, account_id TEXT NOT NULL, phase TEXT NOT NULL, plan TEXT)""")
            # Recover old in-progress paper requests without migrating or reusing balances.
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_runs'").fetchone()
            if exists:
                db.execute("INSERT OR IGNORE INTO execution_runs SELECT id,'paper','paper-usd-default',phase,plan FROM paper_runs")

    def control(self, enabled=None):
        return self.backend.control(enabled)

    def execute(self, ident, plan, quote):
        return self.backend.execute(ident, plan, quote)

    def _settle(self, ident, outcome):
        if not isinstance(outcome, ExecutionOutcome):
            raise ValueError('Execution backend returned invalid outcome')
        terminal = outcome.state in {'completed', 'failed'}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute('SELECT * FROM instructions WHERE id=?', (ident,)).fetchone()
            if item['state'] == 'running':
                state = outcome.state if terminal else 'running'
                events = json.loads(item['events'])
                now = utc_now_iso()
                if item['result'] != outcome.summary or state != item['state']:
                    events.append(dict(state=state, at=now, result=outcome.summary))
                db.execute('UPDATE instructions SET state=?,result=?,updated_at=?,events=? WHERE id=?',
                    (state, outcome.summary, now, json.dumps(events, ensure_ascii=False), ident))
            db.execute('UPDATE execution_runs SET phase=? WHERE id=?', (outcome.state if terminal else 'executing', ident))

    def run_once(self, quote, planner=None):
        enabled = self.control()['enabled']
        with self.store.connect() as db:
            row = db.execute("SELECT i.*,r.phase,r.plan AS saved_plan,r.mode,r.account_id FROM instructions i LEFT JOIN execution_runs r ON i.id=r.id WHERE (? AND i.state='waiting') OR (i.state='running' AND (r.phase='executing' OR (? AND r.phase='planning'))) ORDER BY i.created_at,i.id LIMIT 1", (enabled, enabled)).fetchone()
        if row is None:
            return {'engine': 'idle' if enabled else 'paused'}
        item = dict(row)
        ident = item['id']
        if item['mode'] is not None and (item['mode'], item['account_id']) != (self.backend.mode, self.backend.account_id):
            return {'engine': 'blocked', 'reason': '다른 실행 모드/계좌의 진행 중 지시입니다.', 'id': ident}
        if item['state'] == 'waiting':
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM instructions WHERE id=?', (ident,)).fetchone()
                if row['state'] != 'waiting':
                    return {'engine': 'busy'}
                now = utc_now_iso()
                events = json.loads(row['events']) + [dict(state='running', at=now, result=f'{self.backend.mode} 운용 계획 검토')]
                db.execute("UPDATE instructions SET state='running',updated_at=?,events=? WHERE id=?", (now, json.dumps(events, ensure_ascii=False), ident))
                db.execute("INSERT INTO execution_runs VALUES (?,?,?,'planning',NULL)", (ident, self.backend.mode, self.backend.account_id))
        executing = item['phase'] == 'executing'
        try:
            if executing:
                # Submission may already have reached a broker. Only reconcile on retry.
                outcome = self.backend.reconcile(ident, json.loads(item['saved_plan']), quote)
            else:
                if item['message'].startswith('{'):
                    plan = json.loads(item['message'])
                elif planner:
                    plan = planner(ident, item['message'], self.control())
                    if plan is None:
                        return {'engine': 'planning', 'id': ident}
                else:
                    raise PlanError('AI 계획기가 연결되지 않았습니다.')
                parse_plan(plan)
                with self.store.connect() as db:
                    db.execute("UPDATE execution_runs SET phase='executing',plan=? WHERE id=?", (json.dumps(plan), ident))
                executing = True
                outcome = self.backend.execute(ident, plan, quote)
            self._settle(ident, outcome)
        except PlanError as exc:
            self._settle(ident, ExecutionOutcome('failed', str(exc)))
        except Exception:
            if executing:
                self._settle(ident, ExecutionOutcome('uncertain', '실행 결과 확인 대기 · 중복 제출 없이 조회로 확인합니다.'))
            else:
                self._settle(ident, ExecutionOutcome('failed', '계획을 검증하지 못했습니다.'))
        return self.inbox.get(ident)


# Backward compatible CLI/test import; runtime selection is explicit and fail-closed.
PaperEngine = ExecutionEngine


def spool_planner(root):
    def plan(ident, message, portfolio):
        requests, responses = root / 'requests', root / 'responses'
        requests.mkdir(parents=True, exist_ok=True, mode=0o700)
        responses.mkdir(parents=True, exist_ok=True, mode=0o700)
        filename = f'planner-{ident}'
        target = requests / (filename + '.json')
        response = responses / (filename + '.json')
        if response.exists():
            if response.stat().st_size > 32768:
                raise PlanError('AI 계획 크기 초과')
            return json.loads(response.read_text())
        if (responses / (filename + '.error')).exists():
            raise PlanError('AI 계획기 호출 실패')
        # A separate marker survives the agent claiming/removing the request.
        marker = root / (filename + '.sent')
        if marker.exists():
            import time
            if time.time() - marker.stat().st_mtime > 180:
                raise PlanError('AI 계획 응답 시간 초과')
            return None
        import time
        now = time.time()
        sent = [p.stat().st_mtime for p in root.glob('planner-*.sent')]
        today = datetime.now(timezone.utc).date()
        if sum(datetime.fromtimestamp(t, timezone.utc).date() == today for t in sent) >= 20:
            raise PlanError('AI 계획기 일일 20회 모의 호출 한도에 도달했습니다.')
        if sent and now - max(sent) < 30:
            return None
        marker.touch(mode=0o600)
        temp = requests / ('.' + filename + '.tmp')
        temp.write_text(json.dumps(dict(_role='planner', _call_id=filename, message=message, portfolio=portfolio), ensure_ascii=False))
        temp.replace(target)
        return None
    return plan


def market_quote(symbol):
    # Read-only market access; the timestamp is fetch time, not exchange freshness.
    from core.reporting import make_client
    from core.events import utc_now_iso
    raw = make_client('kis').overseas_quote(symbol, 'NAS')
    return raw.get('last'), utc_now_iso()
