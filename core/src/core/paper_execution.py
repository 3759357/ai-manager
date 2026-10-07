"""Paper-only execution adapter. Real submission is not imported or implemented."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from decimal import Decimal
from core.events import utc_now_iso
from core.instructions import InstructionStore
from core.execution import amount, PlanError, ExecutionOutcome, OrderJournal, RiskPolicy, parse_plan


class PaperExecutionBackend:
    mode = 'paper'
    account_id = 'paper-usd-default'
    def __init__(self, store):
        self.store = store
        OrderJournal.initialize(store)
        self.inbox = InstructionStore(store)
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS engine (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, cash TEXT NOT NULL);
                INSERT OR IGNORE INTO engine VALUES(1,0,'10000');
                CREATE TABLE IF NOT EXISTS paper_positions (symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_runs (id TEXT PRIMARY KEY, plan TEXT, phase TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS paper_fills (id TEXT PRIMARY KEY, instruction_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    side TEXT NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    def control(self, enabled=None):
        with self.store.connect() as db:
            if enabled is not None:
                db.execute('UPDATE engine SET enabled=? WHERE id=1', (int(enabled),))
            row = dict(db.execute('SELECT * FROM engine WHERE id=1').fetchone())
            positions = [dict(r) for r in db.execute('SELECT * FROM paper_positions ORDER BY symbol')]
            fills = [dict(r) for r in db.execute('SELECT * FROM paper_fills ORDER BY created_at DESC LIMIT 30')]
        return dict(mode='paper', enabled=bool(row['enabled']), cash=row['cash'], currency='USD', positions=positions,
                    fills=fills, initial_cash='10000', max_order_usd='500', max_daily_buy_usd='1500',
                    min_cash_pct='35', max_position_pct='25')

    def execute(self, ident, plan, quote):
        parsed = parse_plan(plan)
        seen = {symbol for symbol, _, _ in parsed}
        portfolio = self.control()
        symbols = seen | {p['symbol'] for p in portfolio['positions'] if amount(p['quantity']) > 0}
        prices = {}
        for symbol in symbols:
            price, timestamp = quote(symbol)
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()
            if not 0 <= age <= 30 or amount(price) <= 0:
                raise PlanError('시세가 누락되었거나 오래되었습니다.')
            prices[symbol] = amount(price)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            engine = db.execute('SELECT * FROM engine WHERE id=1').fetchone()
            if not engine['enabled']:
                raise PlanError('운용 중지 상태입니다.')
            item = db.execute('SELECT * FROM instructions WHERE id=?', (ident,)).fetchone()
            run = db.execute('SELECT * FROM paper_runs WHERE id=?', (ident,)).fetchone()
            if run and run['phase'] == 'completed':
                return ExecutionOutcome('completed', item['result'])
            if item is None or item['state'] != 'running':
                raise PlanError('처리 중인 지시만 실행할 수 있습니다.')
            if run is None:
                db.execute("INSERT INTO paper_runs VALUES (?,NULL,'planning')", (ident,))
            cash = amount(engine['cash'])
            holdings = {r['symbol']: amount(r['quantity']) for r in db.execute('SELECT * FROM paper_positions')}
            has_pilot = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_config'").fetchone()
            cfg = db.execute('SELECT enabled FROM pilot_config WHERE id=1').fetchone() if has_pilot else None
            pilot_mode = bool(cfg and cfg['enabled']) or ident.startswith('pilot-')
            if pilot_mode:
                from core.pilot import ACCOUNT, UNIVERSE, session_open
                from core.portfolio_risk import RiskTracker
                intent = db.execute('SELECT * FROM pilot_plans WHERE id=?', (ident,)).fetchone()
                now_time = datetime.now(timezone.utc)
                if not cfg or not cfg['enabled'] or not session_open(now_time):
                    raise PlanError('모의 감시 계획이 중지·만료되었거나 정규장이 아닙니다.')
                if ident.startswith('pilot-'):
                    if not intent or intent['expires'] <= now_time.timestamp() or json.loads(intent['plan']) != plan:
                        raise PlanError('모의 계획 만료 또는 내용 불일치')
                # Pilot initialized the schema before execution; same transaction as fills.
                equity = cash + sum(q * prices[s] for s, q in holdings.items() if q > 0)
                risk = RiskTracker.record(db, ACCOUNT, ident + '-pre', now_time.isoformat(), equity)
                if any(side == 'buy' and (risk['stage'] != 'normal' or symbol not in UNIVERSE) for symbol, side, _ in parsed):
                    raise PlanError('모의 계좌 위험 단계 또는 허용 종목 제한')
                # 10 bps adverse execution price includes a conservative cost allowance.
                prices = {s: p for s, p in prices.items()}
                for symbol, side, _ in parsed:
                    prices[symbol] *= Decimal('1.001') if side == 'buy' else Decimal('.999')
            today = datetime.now(timezone.utc).date().isoformat()
            daily = sum((amount(r['quantity']) * amount(r['price']) for r in db.execute("SELECT * FROM paper_fills WHERE side='buy' AND substr(created_at,1,10)=?", (today,))), Decimal(0))
            cash, holdings = RiskPolicy().project(parsed, prices, cash, holdings, daily, reduction_only=pilot_mode and bool(parsed) and all(side == 'sell' for _, side, _ in parsed))
            now = utc_now_iso()
            for index, (symbol, side, qty) in enumerate(parsed):
                order_id = OrderJournal.prepare(db, self.mode, self.account_id, ident, index, symbol, side, qty,
                                                order_type='limit', limit_price=prices[symbol])
                OrderJournal.transition(db, order_id, 'submitting')
                OrderJournal.fill(db, order_id, f'paper-{ident}-{index}', qty, prices[symbol])
                OrderJournal.transition(db, order_id, 'filled')
                db.execute('INSERT INTO paper_fills VALUES (?,?,?,?,?,?,?)',
                           (f'{ident}-{index}', ident, symbol, side, str(qty), str(prices[symbol]), now))
            for symbol, qty in holdings.items():
                db.execute('INSERT OR REPLACE INTO paper_positions VALUES (?,?)', (symbol, str(qty)))
            db.execute('UPDATE engine SET cash=? WHERE id=1', (str(cash),))
            details = '\n'.join(f'{symbol} {side} {qty}주 @ {prices[symbol]} USD' for symbol, side, qty in parsed)
            result = f'모의 체결 {len(parsed)}건 · 실제 주문 전송 없음\n' + details + '\n' + plan['reason']
            if pilot_mode:
                result += '\n실전 시세 모의: 불리한 가격 0.10% 비용 가정 · 실제 호가/부분체결 재현 아님'
            events = json.loads(item['events']) + [dict(state='completed', at=now, result=result)]
            db.execute("UPDATE instructions SET state='completed',result=?,updated_at=?,events=? WHERE id=?", (result, now, json.dumps(events, ensure_ascii=False), ident))
            db.execute("UPDATE paper_runs SET phase='completed',plan=? WHERE id=?", (json.dumps(plan), ident))

        return ExecutionOutcome('completed', result)

    def reconcile(self, ident, plan, quote):
        # Paper fills and completion share one DB transaction: absence proves no fill.
        return self.execute(ident, plan, quote)
