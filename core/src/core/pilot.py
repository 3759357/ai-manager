"""Server-owned live-data paper pilot. No broker order API is imported or called."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
import json
import time
import uuid

from core.events import utc_now_iso
from core.execution import amount, parse_plan, PlanError
from core.portfolio_risk import RiskTracker

UNIVERSE = ('GOOGL', 'QCOM', 'AAPL', 'ORCL', 'INTC')
ACCOUNT = 'paper-usd-default'


def session_open(now):
    # Narrow regular-session window; fresh exchange stamps also required. On
    # holidays/early closes stale quotes fail closed instead of simulating fills.
    local = now.astimezone(ZoneInfo('America/New_York'))
    minute = local.hour * 60 + local.minute
    return local.weekday() < 5 and 575 <= minute < 955


def fresh(timestamp, now, seconds=120):
    stamp = datetime.fromisoformat(timestamp)
    return stamp.tzinfo is not None and 0 <= (now - stamp).total_seconds() <= seconds


def price_rows(rows, symbols, now):
    prices = {}
    for row in rows:
        symbol = row.get('symbol')
        if symbol not in symbols or symbol in prices:
            raise ValueError('Unexpected/duplicate quote')
        if row.get('currency') != 'USD' or not fresh(row['timestamp'], now) or amount(row['lastPrice']) <= 0:
            raise ValueError('Stale or invalid quote')
        prices[symbol] = dict(price=str(amount(row['lastPrice'])), at=row['timestamp'])
    if set(prices) != set(symbols):
        raise ValueError('Incomplete prices')
    return prices


def candle_features(rows, now):
    closed = []
    for row in rows:
        stamp = datetime.fromisoformat(row['timestamp'])
        if stamp.tzinfo is None:
            raise ValueError('Candle timezone missing')
        if stamp + timedelta(minutes=1) > now:
            continue
        if row.get('currency') != 'USD':
            raise ValueError('Candle currency mismatch')
        o, h, l, c, v = [amount(row[k]) for k in ('openPrice', 'highPrice', 'lowPrice', 'closePrice', 'volume')]
        if l <= 0 or not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError('Invalid OHLC')
        closed.append((stamp, o, h, l, c, v))
    closed.sort()
    if len(closed) < 3 or not fresh(closed[-1][0].isoformat(), now, 180):
        raise ValueError('Insufficient recent closed candles')
    if any((b[0] - a[0]).total_seconds() != 60 for a, b in zip(closed, closed[1:])):
        raise ValueError('Missing/duplicate candle')
    a, b = closed[-2:]
    return dict(last_closed=b[0].isoformat(), last_close=str(b[4]), previous_close=str(a[4]),
                previous_low=str(a[3]), previous_high=str(a[2]), last_volume=str(b[5]),
                previous_volume=str(a[5]), return_pct=str((b[4] / closed[0][4] - 1) * 100),
                buy_signal=b[4] > a[2] and b[5] >= a[5] and b[5] > 0,
                sell_signal=b[4] < a[3] and b[5] >= a[5] and b[5] > 0)


def live_quote(symbol):
    from core.reporting import make_client
    now = datetime.now(timezone.utc)
    if not session_open(now):
        raise PlanError('모의 체결 허용 정규장 시간이 아닙니다.')
    rows = make_client('toss').prices([symbol])
    item = price_rows(rows, [symbol], datetime.now(timezone.utc))[symbol]
    return item['price'], item['at']


def collect_market(symbols):
    from core.reporting import make_client
    client = make_client('toss')
    rows = client.prices(list(symbols))
    candles = {s: client.candles(s, count=20)['candles'] for s in symbols}
    now = datetime.now(timezone.utc)
    if not session_open(now):
        raise ValueError('Session ended during collection')
    prices = price_rows(rows, symbols, now)
    features = {s: candle_features(rows, now) for s, rows in candles.items()}
    return dict(at=now.isoformat(), prices=prices, features=features,
                source='toss read-only prices + closed 1m candles', universe=list(symbols))


class Pilot:
    def __init__(self, store):
        self.store = store
        self.risk = RiskTracker(store)
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS pilot_config (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
                    last_poll REAL NOT NULL, last_plan REAL NOT NULL, note TEXT NOT NULL);
                INSERT OR IGNORE INTO pilot_config VALUES (1,0,0,0,'실전 시세 모의 운용 미시작');
                CREATE TABLE IF NOT EXISTS pilot_market (id TEXT PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pilot_plans (id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
                    state TEXT NOT NULL, market_id TEXT NOT NULL, plan TEXT, note TEXT NOT NULL);
            ''')

    def status(self):
        with self.store.connect() as db:
            cfg = dict(db.execute('SELECT * FROM pilot_config').fetchone())
            risk = self.risk.latest(db, ACCOUNT)
            plans = [dict(r) for r in db.execute('SELECT * FROM pilot_plans ORDER BY created DESC LIMIT 10')]
            market = db.execute('SELECT at FROM pilot_market ORDER BY rowid DESC LIMIT 1').fetchone()
        return dict(enabled=bool(cfg['enabled']), mode='live-data-paper', note=cfg['note'],
                    risk=risk, last_market_at=market[0] if market else None, plans=plans,
                    real_account_nav_available=False,
                    scope='기존 가상 USD 계좌 · 실제 시세 사용 · 실계좌 전체 낙폭 아님',
                    policy='pilot-v1: 1m breakout/pullback; DD -5% buy block; -7% reduction; 15m expiry')

    def control(self, enabled):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE pilot_config SET enabled=?,last_poll=0,note=? WHERE id=1',
                       (int(enabled), '실전 시세 모의 운용 시작' if enabled else '모의 운용 중지'))
            db.execute('UPDATE engine SET enabled=? WHERE id=1', (int(enabled),))
            if not enabled:
                db.execute("UPDATE pilot_plans SET state='cancelled',note='사용자 중지' WHERE state IN ('planning','monitoring')")
                # Already handed off intents are cancelled only while still waiting.
                rows = db.execute("SELECT id FROM pilot_plans WHERE state='submitted'").fetchall()
                for row in rows:
                    item = db.execute('SELECT * FROM instructions WHERE id=?', (row['id'],)).fetchone()
                    if item and item['state'] == 'waiting':
                        events = json.loads(item['events']) + [dict(state='cancelled', at=utc_now_iso(), result='사용자 중지')]
                        db.execute("UPDATE instructions SET state='cancelled',result='사용자 중지',updated_at=?,events=? WHERE id=?",
                                   (utc_now_iso(), json.dumps(events, ensure_ascii=False), row['id']))
        return self.status()

    def _note(self, note):
        with self.store.connect() as db:
            db.execute('UPDATE pilot_config SET note=? WHERE id=1', (note,))

    def tick(self, engine, planner, collector=collect_market, now=None):
        now = now or datetime.now(timezone.utc)
        epoch = now.timestamp()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cfg = dict(db.execute('SELECT * FROM pilot_config').fetchone())
            if not cfg['enabled'] or not engine.control()['enabled']:
                return
            if epoch - cfg['last_poll'] < 60:
                return
            db.execute('UPDATE pilot_config SET last_poll=? WHERE id=1', (epoch,))
        if not session_open(now):
            self._note('정규장 대기 · 미체결 대사와 보고는 유지')
            return
        try:
            portfolio = engine.control()
            symbols = sorted(set(UNIVERSE) | {p['symbol'] for p in portfolio['positions'] if amount(p['quantity']) > 0})
            market = collector(symbols)
            market_id = uuid.uuid4().hex
            prices = {s: amount(p['price']) for s, p in market['prices'].items()}
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                # Snapshot the current paper ledger after collection.
                cash = amount(db.execute('SELECT cash FROM engine WHERE id=1').fetchone()[0])
                holdings = {r['symbol']: amount(r['quantity']) for r in db.execute('SELECT * FROM paper_positions')}
                equity = cash + sum(q * prices[s] for s, q in holdings.items() if q > 0)
                risk = self.risk.record(db, ACCOUNT, market_id, market['at'], equity)
                db.execute('INSERT INTO pilot_market VALUES (?,?,?)', (market_id, market['at'], json.dumps(market)))
                for p in db.execute("SELECT * FROM pilot_plans WHERE state='submitted'").fetchall():
                    state = db.execute('SELECT state,result FROM instructions WHERE id=?', (p['id'],)).fetchone()
                    if state and state['state'] in {'completed','failed','cancelled'}:
                        db.execute('UPDATE pilot_plans SET state=?,note=? WHERE id=?', (state['state'], state['result'] or '', p['id']))
                db.execute("UPDATE pilot_plans SET state='expired',note='15분 유효 시간 만료' WHERE state IN ('planning','monitoring') AND expires<=?", (epoch,))
                if risk['stage'] in {'reduce','critical'}:
                    for p in db.execute("SELECT * FROM pilot_plans WHERE state IN ('planning','monitoring')").fetchall():
                        plan = json.loads(p['plan']) if p['plan'] else None
                        if plan is None or any(o['side'] == 'buy' for o in plan['orders']):
                            db.execute("UPDATE pilot_plans SET state='cancelled',note='위험 악화: 기존 계획 철회' WHERE id=?", (p['id'],))
                pending = db.execute("SELECT * FROM pilot_plans WHERE state IN ('planning','monitoring','submitted') ORDER BY created LIMIT 1").fetchone()
            self._note('시세·분봉 수집 정상 · 모의 계좌 위험 ' + risk['stage'])
            if pending and pending['state'] == 'submitted':
                return
            if not pending:
                if epoch - cfg['last_plan'] < (300 if risk['stage'] in {'reduce','critical'} else 1800):
                    return
                # Do not compete with a user's pending instruction.
                with self.store.connect() as db:
                    if db.execute("SELECT 1 FROM instructions WHERE state IN ('waiting','running')").fetchone():
                        return
                    ident = 'pilot-' + uuid.uuid4().hex
                    db.execute('INSERT INTO pilot_plans VALUES (?,?,?,?,?,?,?)',
                               (ident, epoch, epoch + 900, 'planning', market_id, None, 'AI 판단 대기'))
                    db.execute('UPDATE pilot_config SET last_plan=? WHERE id=1', (epoch,))
                    pending = db.execute('SELECT * FROM pilot_plans WHERE id=?', (ident,)).fetchone()
            pending = dict(pending)
            if pending['state'] == 'planning':
                if risk['stage'] in {'reduce','critical'}:
                    # Bounded deterministic risk reduction; same execution limits apply.
                    orders = []
                    for symbol, qty in holdings.items():
                        n = min(int(qty), int(Decimal('450') / prices[symbol]))
                        if n:
                            orders.append(dict(symbol=symbol, side='sell', quantity=n))
                    plan = dict(orders=orders[:10], reason='모의 DD 위험 단계: 타점보다 비중 축소 우선')
                elif planner:
                    with self.store.connect() as db:
                        initial = json.loads(db.execute('SELECT payload FROM pilot_market WHERE id=?', (pending['market_id'],)).fetchone()[0])
                    context = dict(portfolio, market=initial, risk=risk, permitted_symbols=list(UNIVERSE),
                                   objective='실전 시세로 모의 운용. 계좌 DD -5%부터 매수 차단, 집중 축소와 제한적 추가 매수 검토. 근거 부족하면 관망.')
                    plan = planner(pending['id'], '서버 정기 모의 운용 검토: 제공된 데이터로 비중·추가매수·관망을 판단하라.', context)
                    if plan is None:
                        return
                else:
                    raise PlanError('AI 계획기 미연결')
                parsed = parse_plan(plan)
                if any(s not in UNIVERSE and side == 'buy' for s, side, _ in parsed):
                    raise PlanError('시험 허용 종목 밖의 매수')
                with self.store.connect() as db:
                    db.execute('UPDATE pilot_plans SET state=?,plan=?,note=? WHERE id=?',
                               ('monitoring' if parsed else 'completed', json.dumps(plan, ensure_ascii=False), plan['reason'], pending['id']))
                if not parsed:
                    # No-trade decisions also appear in the existing instruction inbox.
                    self._submit(engine, pending['id'], plan)
                    return
            else:
                plan = json.loads(pending['plan'])
            parsed = parse_plan(plan)
            if risk['stage'] != 'normal' and any(side == 'buy' for _, side, _ in parsed):
                raise PlanError('계좌 DD 방어 단계: 추가 매수 차단')
            defensive = risk['stage'] in {'reduce','critical'} and all(side == 'sell' for _, side, _ in parsed)
            if not defensive and not all(market['features'][s][side + '_signal'] for s, side, _ in parsed):
                self._note('계획 감시 중 · 확정 1분봉 가격·거래량 조건 대기')
                return
            self._submit(engine, pending['id'], plan)
        except Exception as exc:
            # No raw broker/AI exception strings (can contain account credentials).
            self._note('모의 운용 보류 · ' + type(exc).__name__)
            if isinstance(exc, (PlanError, ValueError)) and 'pending' in locals() and pending:
                with self.store.connect() as db:
                    db.execute("UPDATE pilot_plans SET state='failed',note=? WHERE id=? AND state IN ('planning','monitoring')",
                               ('검증 실패 · ' + type(exc).__name__, pending['id']))

    def _submit(self, engine, ident, plan):
        # Stable ID makes crash between submit and state change recoverable.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT enabled FROM pilot_config').fetchone()[0] or not db.execute('SELECT enabled FROM engine').fetchone()[0]:
                return
            row = db.execute('SELECT state,expires FROM pilot_plans WHERE id=?', (ident,)).fetchone()
            if row['state'] not in {'monitoring','completed'} or row['expires'] <= time.time():
                return
            message = json.dumps(plan, ensure_ascii=False)
            now = utc_now_iso()
            events = json.dumps([dict(state='waiting', at=now, result=None)])
            db.execute('INSERT OR IGNORE INTO instructions VALUES (?,?,?,?,?,?,?)',
                       (ident, message, 'waiting', now, now, None, events))
            db.execute("UPDATE pilot_plans SET state='submitted',note='조건 충족 · 모의 실행 대기' WHERE id=?", (ident,))
