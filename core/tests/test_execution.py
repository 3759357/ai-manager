import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from core.manager_cli import RequestStore
from core.automation import ExecutionEngine, make_backend
from core.execution import OrderJournal, ExecutionOutcome
from core.events import utc_now_iso


class DeferredBackend:
    mode = 'paper'
    account_id = 'test-account'
    def __init__(self):
        self.enabled = True
        self.submissions = 0
        self.lookups = 0
    def control(self, enabled=None):
        if enabled is not None:
            self.enabled = enabled
        return {'enabled': self.enabled}
    def execute(self, ident, plan, quote):
        self.submissions += 1
        raise TimeoutError('The broker might have received the order')
    def reconcile(self, ident, plan, quote):
        self.lookups += 1
        return ExecutionOutcome('completed', '확인된 처리 결과')


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RequestStore(Path(tmp.name))
        OrderJournal.initialize(self.store)

    def test_live_and_unknown_modes_cannot_be_activated(self):
        for mode in ['live', 'LIVE', 'toss', '', 'kis']:
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                make_backend(self.store, mode)

    def test_submission_timeout_reconciles_after_restart_even_when_stopped(self):
        backend = DeferredBackend()
        engine = ExecutionEngine(self.store, backend)
        engine.inbox.submit(json.dumps({'orders': [], 'reason': 'test'}), 'test')
        first = engine.run_once(None)
        self.assertEqual(first['state'], 'running')
        self.assertIn('확인 대기', first['result'])
        backend.control(False)
        restarted = ExecutionEngine(self.store, backend)
        self.assertEqual(restarted.run_once(None)['state'], 'completed')
        self.assertEqual((backend.submissions, backend.lookups), (1, 1))

    def test_pending_partial_execution_is_not_reported_completed(self):
        backend = DeferredBackend()
        backend.execute = lambda *args: ExecutionOutcome('pending', '부분 체결')
        backend.reconcile = lambda *args: ExecutionOutcome('pending', '부분 체결')
        engine = ExecutionEngine(self.store, backend)
        engine.inbox.submit('{"orders":[],"reason":"test"}', 'partial')
        self.assertEqual(engine.run_once(None)['state'], 'running')
        self.assertEqual(engine.run_once(None)['result'], '부분 체결')

    def test_account_switch_blocks_inflight_replay(self):
        backend = DeferredBackend()
        engine = ExecutionEngine(self.store, backend)
        engine.inbox.submit('{"orders":[],"reason":"test"}', 'test')
        engine.run_once(None)
        backend.account_id = 'different-account'
        self.assertEqual(ExecutionEngine(self.store, backend).run_once(None)['engine'], 'blocked')
        self.assertEqual(backend.lookups, 0)
        self.assertEqual(backend.submissions, 1)

    def test_order_identity_scopes_and_immutable_payload(self):
        with self.store.connect() as db:
            paper = OrderJournal.prepare(db, 'paper', 'one', 'same', 0, 'TEST', 'buy', 2)
            self.assertEqual(OrderJournal.prepare(db, 'paper', 'one', 'same', 0, 'TEST', 'buy', 2), paper)
            live = OrderJournal.prepare(db, 'live', 'one', 'same', 0, 'TEST', 'buy', 2)
            account = OrderJournal.prepare(db, 'paper', 'two', 'same', 0, 'TEST', 'buy', 2)
            self.assertEqual(len({paper, live, account}), 3)
            with self.assertRaises(ValueError):
                OrderJournal.prepare(db, 'paper', 'one', 'same', 0, 'TEST', 'buy', 3)
            OrderJournal.prepare(db, 'paper', 'one', 'limit', 0, 'TEST', 'buy', 2,
                                 order_type='limit', limit_price='100')
            with self.assertRaises(ValueError):
                OrderJournal.prepare(db, 'paper', 'one', 'limit', 0, 'TEST', 'buy', 2,
                                     order_type='limit', limit_price='101')

    def test_fill_dedup_partial_cancel_race_and_unknown_guard(self):
        with self.store.connect() as db:
            order = OrderJournal.prepare(db, 'paper', 'one', 'same', 0, 'TEST', 'buy', 2)
            OrderJournal.transition(db, order, 'submitting')
            OrderJournal.transition(db, order, 'unknown')
            with self.assertRaises(ValueError):
                OrderJournal.transition(db, order, 'submitting')
            OrderJournal.transition(db, order, 'acknowledged', 'broker-123')
            OrderJournal.fill(db, order, 'fill-1', 1, 100)
            OrderJournal.fill(db, order, 'fill-1', 1, 100)
            with self.assertRaises(ValueError):
                OrderJournal.fill(db, order, 'fill-1', 1, 101)
            with self.assertRaises(ValueError):
                OrderJournal.transition(db, order, 'filled')
            OrderJournal.transition(db, order, 'partially_filled')
            OrderJournal.transition(db, order, 'cancel_requested')
            OrderJournal.transition(db, order, 'cancelled')
            OrderJournal.fill(db, order, 'fill-2', 1, 100)
            OrderJournal.transition(db, order, 'filled')
            self.assertEqual(db.execute('SELECT count(*) FROM execution_fills').fetchone()[0], 2)

    def test_paper_backend_records_shared_journal_atomically(self):
        engine = ExecutionEngine(self.store)
        engine.control(True)
        engine.inbox.submit('{"orders":[{"symbol":"TEST","side":"buy","quantity":1}],"reason":"test"}', 'paper')
        engine.run_once(lambda _: ('100', utc_now_iso()))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM execution_orders').fetchone()[0], 'filled')
            self.assertEqual(db.execute('SELECT count(*) FROM execution_fills').fetchone()[0], 1)
        self.assertEqual(engine.control()['cash'], '9900')
