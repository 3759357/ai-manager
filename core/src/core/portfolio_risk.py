"""Persistent unitized USD drawdown. Never infer real cash from buying power."""
import json
from decimal import Decimal
from datetime import datetime
from core.execution import amount


def risk_stage(drawdown):
    d = Decimal(str(drawdown))
    return 'critical' if d <= Decimal('-.10') else 'reduce' if d <= Decimal('-.07') else 'attention' if d <= Decimal('-.05') else 'normal'


class RiskTracker:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS portfolio_marks (
                account TEXT NOT NULL, sample_id TEXT NOT NULL, at TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(account,sample_id))''')

    @staticmethod
    def latest(db, account):
        row = db.execute('SELECT payload FROM portfolio_marks WHERE account=? ORDER BY rowid DESC LIMIT 1', (account,)).fetchone()
        return json.loads(row[0]) if row else None

    @classmethod
    def record(cls, db, account, sample_id, at, equity, flow='0'):
        equity = amount(equity)
        flow = Decimal(str(flow))
        if not flow.is_finite() or equity <= 0 or equity - flow <= 0:
            raise ValueError('Invalid equity or external cash flow')
        existing = db.execute('SELECT payload FROM portfolio_marks WHERE account=? AND sample_id=?', (account, sample_id)).fetchone()
        if existing:
            value = json.loads(existing[0])
            if (value['equity'], value['flow'], value['at']) != (str(equity), str(flow), at):
                raise ValueError('Conflicting risk sample')
            return value
        previous = cls.latest(db, account)
        timestamp = datetime.fromisoformat(at)
        if timestamp.tzinfo is None:
            raise ValueError('Risk timestamp needs timezone')
        if previous:
            if timestamp < datetime.fromisoformat(previous['at']):
                raise ValueError('Out of order risk sample')
            units = Decimal(previous['units'])
            index = ((equity - flow) / units).quantize(Decimal('0.000000000001'))
            units += flow / index
            high = max(Decimal(previous['high_water']), index)
        else:
            if flow != 0:
                raise ValueError('Initialize before external flows')
            index = high = Decimal('100')
            units = equity / index
        drawdown = index / high - 1
        value = dict(account=account, sample_id=sample_id, at=at, equity=str(equity), flow=str(flow),
                     units=str(units), index=str(index), high_water=str(high), drawdown=str(drawdown),
                     drawdown_pct=str(drawdown * 100), stage=risk_stage(drawdown),
                     tracked_from=previous['tracked_from'] if previous else at,
                     scope='paper USD only; inception before tracking is unknown')
        db.execute('INSERT INTO portfolio_marks VALUES (?,?,?,?)', (account, sample_id, at, json.dumps(value)))
        return value
