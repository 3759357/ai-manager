import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import patch

from core.manager_cli import RequestStore
from core.automation import PaperEngine
from core.execution import RiskPolicy, PlanError
from core.pilot import Pilot, ACCOUNT, UNIVERSE, price_rows, candle_features, session_open
from core.portfolio_risk import RiskTracker

NOW = datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc)


class PilotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RequestStore(Path(tmp.name))
        self.engine = PaperEngine(self.store)
        self.pilot = Pilot(self.store)
        self.pilot.control(True)

    def market(self, symbols, signal=True, price='100'):
        return dict(at=NOW.isoformat(), prices={s: dict(price=price, at=NOW.isoformat()) for s in symbols},
                    features={s: dict(buy_signal=signal, sell_signal=signal) for s in symbols})

    def plan(self, *args):
        return dict(orders=[dict(symbol='QCOM', side='buy', quantity=1)], reason='모의 근거')

    def tick(self, signal=True, now=NOW, planner=None):
        with patch('core.pilot.time.time', return_value=now.timestamp()):
            self.pilot.tick(self.engine, planner or self.plan, lambda ss: self.market(ss, signal), now)

    def test_units_preserve_drawdown_across_deposit_withdrawal_and_restart(self):
        tracker = self.pilot.risk
        with self.store.connect() as db:
            tracker.record(db, ACCOUNT, 'a', NOW.isoformat(), '10000')
            a = tracker.record(db, ACCOUNT, 'b', NOW.isoformat(), '9000')
            b = tracker.record(db, ACCOUNT, 'c', NOW.isoformat(), '14000', '5000')
            c = tracker.record(db, ACCOUNT, 'd', NOW.isoformat(), '12000', '-2000')
        self.assertEqual(Decimal(a['drawdown']), Decimal('-0.1'))
        self.assertEqual(Decimal(b['index']), Decimal(c['index']))
        with self.store.connect() as db:
            self.assertEqual(RiskTracker.latest(db, ACCOUNT)['high_water'], '100')
            self.assertEqual(RiskTracker.latest(db, ACCOUNT)['stage'], 'critical')
        self.assertEqual(Pilot(self.store).status()['risk']['sample_id'], 'd')

    def test_invalid_and_conflicting_risk_samples_rejected(self):
        with self.store.connect() as db:
            self.pilot.risk.record(db, ACCOUNT, 'a', NOW.isoformat(), '10000')
            with self.assertRaises(ValueError):
                self.pilot.risk.record(db, ACCOUNT, 'a', NOW.isoformat(), '9000')
            with self.assertRaises(ValueError):
                self.pilot.risk.record(db, ACCOUNT, 'b', (NOW-timedelta(days=1)).isoformat(), '10000')
            with self.assertRaises(ValueError):
                self.pilot.risk.record(db, ACCOUNT, 'b', NOW.isoformat(), '0')

    def test_quote_validation_uses_exchange_timestamp(self):
        good = dict(symbol='QCOM', lastPrice='100', currency='USD', timestamp=NOW.isoformat())
        self.assertEqual(price_rows([good], ['QCOM'], NOW)['QCOM']['price'], '100')
        for rows in ([], [good,good], [dict(good, currency='KRW')], [dict(good, timestamp=(NOW-timedelta(minutes=3)).isoformat())], [dict(good,timestamp=(NOW+timedelta(seconds=1)).isoformat())]):
            with self.assertRaises(ValueError):
                price_rows(rows, ['QCOM'], NOW)

    def test_candles_ignore_open_bar_and_require_continuity(self):
        rows = [dict(timestamp=(NOW-timedelta(minutes=i)).isoformat(), openPrice='100', highPrice='102', lowPrice='99', closePrice='101', volume='10', currency='USD') for i in range(4)]
        result = candle_features(rows, NOW)
        self.assertEqual(result['last_closed'], (NOW-timedelta(minutes=1)).isoformat())
        self.assertFalse(result['buy_signal'])
        with self.assertRaises(ValueError):
            candle_features([rows[1],rows[1],rows[2],rows[3]], NOW)

    def test_session_rejects_weekend_and_after_hours(self):
        self.assertTrue(session_open(NOW))
        self.assertFalse(session_open(NOW.replace(hour=23)))
        self.assertFalse(session_open(NOW + timedelta(days=3)))

    def test_plan_waits_then_submits_once_across_restart(self):
        self.tick(signal=False)
        self.assertEqual(self.pilot.status()['plans'][0]['state'], 'monitoring')
        self.assertEqual(self.engine.inbox.list(), [])
        self.pilot = Pilot(self.store)
        self.tick(now=NOW+timedelta(minutes=1))
        self.tick(now=NOW+timedelta(minutes=2))
        self.assertEqual(len(self.engine.inbox.list()), 1)
        self.assertEqual(self.pilot.status()['plans'][0]['state'], 'submitted')

    def test_no_trade_decision_reaches_inbox(self):
        self.tick(planner=lambda *a: dict(orders=[], reason='자료 부족으로 관망'))
        self.assertEqual(len(self.engine.inbox.list()), 1)
        self.assertIn('관망', self.engine.inbox.list()[0]['message'])

    def test_stop_cancels_waiting_pilot_not_user_instruction(self):
        self.tick()
        self.engine.inbox.submit('사용자 요청', 'manual')
        self.pilot.control(False)
        rows = {r['id']: r for r in self.engine.inbox.list()}
        self.assertEqual(rows['manual']['state'], 'waiting')
        self.assertEqual(next(v for k,v in rows.items() if k.startswith('pilot-'))['state'], 'cancelled')
        self.assertFalse(self.engine.control()['enabled'])

    def test_plan_expires_without_execution(self):
        self.tick(signal=False)
        self.tick(now=NOW+timedelta(minutes=16))
        self.assertEqual(self.pilot.status()['plans'][0]['state'], 'expired')
        self.assertEqual(self.engine.inbox.list(), [])

    def test_stale_collection_blocks_without_fill(self):
        def broken(_):
            raise ValueError('stale')
        self.pilot.tick(self.engine, self.plan, broken, NOW)
        self.assertEqual(self.pilot.status()['plans'], [])
        self.assertIn('보류', self.pilot.status()['note'])

    def test_hard_drawdown_gate_rejects_ai_buy(self):
        with self.store.connect() as db:
            self.pilot.risk.record(db, ACCOUNT, 'peak', NOW.isoformat(), '10600')
        self.tick()
        self.assertEqual(self.pilot.status()['plans'][0]['state'], 'failed')
        self.assertEqual(self.engine.inbox.list(), [])

    def test_disallowed_buy_rejected(self):
        self.tick(planner=lambda *a: dict(orders=[dict(symbol='GGLL',side='buy',quantity=1)],reason='test'))
        self.assertEqual(self.pilot.status()['plans'][0]['state'], 'failed')

    def test_risk_reducing_sale_can_reduce_existing_concentration_violation(self):
        cash, holdings = RiskPolicy().project([('QCOM','sell',1)], {'QCOM':Decimal('100')}, Decimal('0'), {'QCOM':Decimal('10')}, 0, reduction_only=True)
        self.assertEqual(cash, 100)
        self.assertEqual(holdings['QCOM'], 9)
        with self.assertRaises(PlanError):
            RiskPolicy().project([('QCOM','buy',1)], {'QCOM':Decimal('100')}, 1000, {}, 0, reduction_only=True)

    def test_execution_rechecks_drawdown_and_costs(self):
        self.tick()
        # Patch execution wall clock; quote itself is still checked against it.
        with patch('core.paper_execution.datetime') as clock:
            clock.now.return_value = NOW
            clock.fromisoformat.side_effect = datetime.fromisoformat
            result = self.engine.run_once(lambda _: ('100', NOW.isoformat()))
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.engine.control()['cash'], '9899.900')
        self.assertIn('0.10%', result['result'])

    def test_price_fall_between_signal_and_execution_blocks_buy(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO paper_positions VALUES ('INTC','50')")
        self.tick()
        with patch('core.paper_execution.datetime') as clock:
            clock.now.return_value = NOW
            clock.fromisoformat.side_effect = datetime.fromisoformat
            result = self.engine.run_once(lambda s: ('80' if s=='INTC' else '100', NOW.isoformat()))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.engine.control()['fills'], [])


if __name__ == '__main__':
    unittest.main()
