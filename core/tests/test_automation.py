import tempfile
import unittest
from pathlib import Path
from core.manager_cli import RequestStore
from core.automation import PaperEngine, PlanError
from core.events import utc_now_iso
from core.push import PushStore
import json


class AutomationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RequestStore(Path(tmp.name))
        self.engine = PaperEngine(self.store)
        self.engine.control(True)

    def quote(self, symbol):
        return '100', utc_now_iso()

    def submit(self, orders, key='test'):
        return self.engine.inbox.submit(json.dumps({'orders': orders, 'reason': '모의 테스트'}), key)

    def test_paper_buy_is_atomic_and_never_replayed(self):
        plan = {'orders': [{'symbol': 'TEST', 'side': 'buy', 'quantity': 2}], 'reason': '모의 테스트'}
        self.engine.inbox.submit(json.dumps(plan), 'once')
        result = self.engine.run_once(self.quote)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.engine.control()['cash'], '9800')
        self.engine.execute('once', plan, self.quote)
        self.assertEqual(len(self.engine.control()['fills']), 1)
        self.assertEqual(self.engine.run_once(self.quote), {'engine': 'idle'})
        self.assertEqual(PaperEngine(self.store).control()['cash'], '9800')

    def test_invalid_sell_rolls_back_whole_batch(self):
        self.submit([{'symbol': 'AAA', 'side': 'buy', 'quantity': 1}, {'symbol': 'BBB', 'side': 'sell', 'quantity': 1}])
        self.assertEqual(self.engine.run_once(self.quote)['state'], 'failed')
        self.assertEqual(self.engine.control()['cash'], '10000')
        self.assertEqual(self.engine.control()['fills'], [])

    def test_order_and_daily_limits(self):
        self.submit([{'symbol': 'AAA', 'side': 'buy', 'quantity': 6}], 'too-big')
        self.assertEqual(self.engine.run_once(self.quote)['state'], 'failed')
        for i in range(3):
            self.submit([{'symbol': 'AAA', 'side': 'buy', 'quantity': 5}], f'buy-{i}')
            self.assertEqual(self.engine.run_once(self.quote)['state'], 'completed')
        self.submit([{'symbol': 'BBB', 'side': 'buy', 'quantity': 1}], 'daily')
        self.assertEqual(self.engine.run_once(self.quote)['state'], 'failed')
        self.assertEqual(len(self.engine.control()['fills']), 3)

    def test_stop_during_plan_prevents_fill(self):
        self.engine.inbox.submit('검토', 'stop')
        def planner(*args):
            self.engine.control(False)
            return {'orders': [{'symbol': 'AAA', 'side': 'buy', 'quantity': 1}], 'reason': 'test'}
        self.engine.run_once(self.quote, planner)
        self.assertEqual(self.engine.control()['fills'], [])
        self.assertEqual(self.engine.run_once(self.quote), {'engine': 'paused'})

    def test_stale_quote_rejected(self):
        self.submit([{'symbol': 'AAA', 'side': 'buy', 'quantity': 1}])
        result = self.engine.run_once(lambda _: ('100', '2000-01-01T00:00:00+00:00'))
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(self.engine.control()['fills'], [])

    def test_pending_planner_recovers_after_restart(self):
        self.engine.inbox.submit('검토', 'ai')
        self.assertEqual(self.engine.run_once(self.quote, lambda *a: None)['engine'], 'planning')
        restarted = PaperEngine(self.store)
        result = restarted.run_once(self.quote, lambda *a: {'orders': [], 'reason': '관망'})
        self.assertEqual(result['state'], 'completed')

    def test_push_dedup_and_generic_payload(self):
        push = PushStore(self.store)
        push.register('phone', 'ab' * 32, 'sandbox')
        self.submit([{'symbol': 'SECRET', 'side': 'buy', 'quantity': 1}])
        self.engine.run_once(self.quote)
        calls = []
        def send(token, env, payload, event):
            calls.append(payload)
            return 200, ''
        push.deliver_once(send)
        push.deliver_once(send)
        self.assertEqual(push.deliver_once(send), {'push': 'idle'})
        self.assertEqual(len(calls), 2) # Completion and one simulated fill.
        self.assertNotIn('SECRET', json.dumps(calls))
        self.assertNotIn('100', json.dumps(calls))

    def test_invalid_token_deactivated_and_transient_retried(self):
        push = PushStore(self.store)
        push.register('phone', 'ab' * 32, 'sandbox')
        self.submit([])
        self.engine.run_once(self.quote)
        push.deliver_once(lambda *args: (503, 'ServiceUnavailable'))
        self.assertEqual(push.status()['delivery_counts'], {'pending': 1})
        with self.store.connect() as db:
            db.execute('UPDATE push_delivery SET next_attempt=0')
        push.deliver_once(lambda *args: (410, 'Unregistered'))
        self.assertEqual(push.status()['active_devices'], 0)
        self.assertEqual(push.status()['delivery_counts'], {'failed': 1})
