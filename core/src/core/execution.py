"""Execution boundary and persistent order lifecycle, independent of any broker SDK.

A transport timeout means UNKNOWN, never permission to resubmit an order.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol, Callable
import hashlib
import json
import re
from core.events import utc_now_iso


class PlanError(ValueError):
    pass


def amount(value):
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PlanError('유효한 숫자가 필요합니다.')
    if not n.is_finite() or n < 0:
        raise PlanError('음수·무한대 숫자는 사용할 수 없습니다.')
    return n


@dataclass(frozen=True)
class ExecutionOutcome:
    # pending includes acknowledged / partially filled; uncertain requires lookup.
    state: str
    summary: str
    def __post_init__(self):
        if self.state not in {'completed', 'pending', 'uncertain', 'failed'}:
            raise ValueError('Invalid execution outcome')


class ExecutionBackend(Protocol):
    mode: str
    account_id: str
    def control(self, enabled: bool | None = None) -> dict: ...
    def execute(self, instruction_id: str, plan: dict, quote: Callable) -> ExecutionOutcome: ...
    def reconcile(self, instruction_id: str, plan: dict, quote: Callable) -> ExecutionOutcome:
        """Look up persisted intent / broker order first. Never blindly resubmit."""
        ...


@dataclass(frozen=True)
class RiskPolicy:
    max_order: Decimal = Decimal('500')
    max_daily_buy: Decimal = Decimal('1500')
    min_cash_ratio: Decimal = Decimal('.35')
    max_position_ratio: Decimal = Decimal('.25')

    def project(self, orders, prices, cash, holdings, daily_buy, reduction_only=False):
        """Pure preflight shared by backends; inputs must be same-account USD values.

        This is batch target exposure, not a live settlement/available-cash check.
        A future broker adapter must also recheck each order with reserved exposure.
        """
        cash, daily_buy = amount(cash), amount(daily_buy)
        holdings = {s: amount(q) for s, q in holdings.items()}
        original_cash = cash
        original_equity = cash + sum(q * prices[s] for s, q in holdings.items() if q > 0)
        original_max = max((q * prices[s] / original_equity for s, q in holdings.items() if q > 0), default=Decimal(0)) if original_equity > 0 else Decimal(0)
        if reduction_only and (not orders or any(side != 'sell' for _, side, _ in orders)):
            raise PlanError('위험 축소 전용 계획에는 매도만 허용됩니다.')
        for symbol, side, qty in sorted(orders, key=lambda o: o[1] == 'buy'):
            value = qty * prices[symbol]
            if value > self.max_order:
                raise PlanError('주문당 한도를 초과했습니다.')
            current = holdings.get(symbol, Decimal(0))
            if side == 'sell':
                if qty > current:
                    raise PlanError('보유 수량보다 많이 매도할 수 없습니다.')
                holdings[symbol] = current - qty
                cash += value
            else:
                holdings[symbol] = current + qty
                cash -= value
                daily_buy += value
        equity = cash + sum(q * prices[s] for s, q in holdings.items() if q > 0)
        if reduction_only:
            # Existing violations must not prevent a sale that strictly reduces exposure.
            maximum = max((q * prices[s] / equity for s, q in holdings.items() if q > 0), default=Decimal(0)) if equity > 0 else Decimal(0)
            if equity <= 0 or cash <= original_cash or maximum > original_max:
                raise PlanError('위험 축소 조건을 만족하지 못했습니다.')
            return cash, holdings
        if cash < 0 or equity <= 0 or cash / equity < self.min_cash_ratio or daily_buy > self.max_daily_buy:
            raise PlanError('현금·일일 매수 한도를 초과했습니다.')
        if any(q * prices[s] / equity > self.max_position_ratio for s, q in holdings.items() if q > 0):
            raise PlanError('단일 종목 비중 한도를 초과했습니다.')
        return cash, holdings


class OrderJournal:
    """Shared durable intent / fill ledger. Call inside a caller-owned transaction."""
    transitions = {
        'created': {'submitting', 'cancelled'},
        'submitting': {'acknowledged', 'partially_filled', 'filled', 'rejected', 'unknown'},
        'unknown': {'acknowledged', 'partially_filled', 'filled', 'cancelled', 'rejected'},
        'acknowledged': {'partially_filled', 'filled', 'cancel_requested', 'cancelled', 'rejected', 'unknown'},
        'partially_filled': {'filled', 'cancel_requested', 'cancelled', 'unknown'},
        'cancel_requested': {'partially_filled', 'filled', 'cancelled', 'unknown'},
        'cancelled': {'filled'},  # Late confirmed full fill can win a cancellation race.
    }

    @staticmethod
    def initialize(store):
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS execution_orders (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, account_id TEXT NOT NULL,
                    instruction_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, parameters TEXT NOT NULL,
                    state TEXT NOT NULL, broker_order_id TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(mode,account_id,instruction_id,ordinal));
                CREATE TABLE IF NOT EXISTS execution_fills (
                    order_id TEXT NOT NULL, fill_id TEXT NOT NULL, quantity TEXT NOT NULL,
                    price TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(order_id,fill_id));
                CREATE TABLE IF NOT EXISTS execution_order_events (
                    id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, state TEXT NOT NULL, at TEXT NOT NULL);
            ''')

    @staticmethod
    def prepare(db, mode, account_id, instruction_id, ordinal, symbol, side, quantity,
                *, currency='USD', order_type='market', limit_price=None):
        if mode not in {'paper', 'live'} or not account_id or side not in {'buy', 'sell'} or amount(quantity) <= 0:
            raise ValueError('Invalid order intent')
        if currency not in {'USD', 'KRW'} or order_type not in {'market', 'limit'}:
            raise ValueError('Invalid order parameters')
        if order_type == 'limit' and (limit_price is None or amount(limit_price) <= 0):
            raise ValueError('Limit order needs a positive price')
        if order_type == 'market' and limit_price is not None:
            raise ValueError('Market order cannot carry a limit price')
        parameters = json.dumps(dict(currency=currency, order_type=order_type,
                                     limit_price=str(amount(limit_price).normalize()) if limit_price is not None else None), sort_keys=True)
        identity = json.dumps([mode, account_id, instruction_id, ordinal], separators=(',', ':'))
        ident = hashlib.sha256(identity.encode()).hexdigest()
        now = utc_now_iso()
        inserted = db.execute('INSERT OR IGNORE INTO execution_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident, mode, account_id, instruction_id, ordinal, symbol, side, str(quantity), parameters, 'created', None, now)).rowcount
        row = dict(db.execute('SELECT * FROM execution_orders WHERE id=?', (ident,)).fetchone())
        if (row['symbol'], row['side'], amount(row['quantity']), row['parameters']) != (symbol, side, amount(quantity), parameters):
            raise ValueError('Same client order ID cannot describe another order')
        if inserted:
            db.execute('INSERT INTO execution_order_events(order_id,state,at) VALUES (?,?,?)', (ident, 'created', now))
        return ident

    @classmethod
    def transition(cls, db, ident, state, broker_order_id=None):
        row = db.execute('SELECT * FROM execution_orders WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('Order not found')
        if row['broker_order_id'] and broker_order_id and row['broker_order_id'] != broker_order_id:
            raise ValueError('Broker order ID changed')
        if row['state'] == state:
            return
        if state not in cls.transitions.get(row['state'], set()):
            raise ValueError('Invalid order transition; reconcile uncertain orders')
        if state in {'filled', 'partially_filled'}:
            total = sum((amount(r[0]) for r in db.execute('SELECT quantity FROM execution_fills WHERE order_id=?', (ident,))), Decimal(0))
            if state == 'filled' and total != amount(row['quantity']):
                raise ValueError('Filled requires confirmed full quantity')
            if state == 'partially_filled' and not 0 < total < amount(row['quantity']):
                raise ValueError('Partial fill requires a confirmed partial quantity')
        now = utc_now_iso()
        db.execute('UPDATE execution_orders SET state=?,broker_order_id=COALESCE(broker_order_id,?),updated_at=? WHERE id=?', (state, broker_order_id, now, ident))
        db.execute('INSERT INTO execution_order_events(order_id,state,at) VALUES (?,?,?)', (ident, state, now))

    @staticmethod
    def fill(db, ident, fill_id, quantity, price):
        qty, price = amount(quantity), amount(price)
        row = db.execute('SELECT * FROM execution_orders WHERE id=?', (ident,)).fetchone()
        if row is None or qty <= 0 or price <= 0 or not fill_id:
            raise ValueError('Invalid fill')
        existing = db.execute('SELECT * FROM execution_fills WHERE order_id=? AND fill_id=?', (ident, fill_id)).fetchone()
        if existing:
            if (amount(existing['quantity']), amount(existing['price'])) != (qty, price):
                raise ValueError('Fill ID reused with another payload')
            return
        if row['state'] not in {'submitting', 'acknowledged', 'partially_filled', 'unknown', 'cancel_requested', 'cancelled'}:
            raise ValueError('Fill received for an order that was not submitted')
        total = sum((amount(r[0]) for r in db.execute('SELECT quantity FROM execution_fills WHERE order_id=?', (ident,))), Decimal(0))
        if total + qty > amount(row['quantity']):
            raise ValueError('Overfill')
        db.execute('INSERT INTO execution_fills VALUES (?,?,?,?,?)', (ident, fill_id, str(qty), str(price), utc_now_iso()))


def parse_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {'orders', 'reason'} or not isinstance(plan['reason'], str) or len(plan['reason']) > 2000:
        raise PlanError('계획 형식이 올바르지 않습니다.')
    orders = plan['orders']
    if not isinstance(orders, list) or len(orders) > 10:
        raise PlanError('주문은 최대 10개입니다.')
    parsed = []
    seen = set()
    for order in orders:
        if not isinstance(order, dict) or set(order) != {'symbol', 'side', 'quantity'}:
            raise PlanError('주문 형식이 올바르지 않습니다.')
        symbol, side = order['symbol'], order['side']
        if not isinstance(symbol, str) or not re.fullmatch('[A-Z][A-Z0-9.-]{0,9}', symbol) or symbol in seen or side not in {'buy', 'sell'}:
            raise PlanError('종목·방향 또는 중복 주문을 확인하세요.')
        qty = amount(order['quantity'])
        if qty <= 0 or qty != qty.to_integral_value():
            raise PlanError('현재 실행 규약은 양의 정수 수량만 지원합니다.')
        seen.add(symbol)
        parsed.append((symbol, side, qty))
    return parsed
