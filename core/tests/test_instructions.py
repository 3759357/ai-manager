import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from core.manager_cli import RequestStore, main
from core.instructions import InstructionStore


class InstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = InstructionStore(RequestStore(self.root))

    def test_idempotent_persistent_inbox(self):
        first = self.inbox.submit('비중 검토', 'phone-1')
        self.assertEqual(first['state'], 'waiting')
        self.assertEqual(self.inbox.submit('비중 검토', 'phone-1'), first)
        with self.assertRaises(ValueError):
            self.inbox.submit('다른 지시', 'phone-1')
        self.assertEqual(InstructionStore(RequestStore(self.root)).list(), [first])

    def test_transitions_preserve_results_and_history(self):
        item = self.inbox.submit('검토해줘')
        with self.assertRaises(ValueError):
            self.inbox.transition(item['id'], 'completed', '완료')
        self.inbox.transition(item['id'], 'running')
        with self.assertRaises(ValueError):
            self.inbox.transition(item['id'], 'completed')
        final = self.inbox.transition(item['id'], 'completed', '검토 결과')
        self.assertEqual([e['state'] for e in final['events']], ['waiting', 'running', 'completed'])
        self.assertEqual(final['result'], '검토 결과')
        self.assertEqual(self.inbox.transition(item['id'], 'completed', '검토 결과'), final)
        with self.assertRaises(ValueError):
            self.inbox.transition(item['id'], 'cancelled')

    def test_cancellation_cannot_be_executed(self):
        item = self.inbox.submit('검토')
        self.inbox.transition(item['id'], 'cancelled')
        with self.assertRaises(ValueError):
            self.inbox.transition(item['id'], 'running')

    def test_invalid_message(self):
        for message in ['', '  ', 'x' * 4001, 'bad\0input']:
            with self.subTest(message=message[:10]), self.assertRaises(ValueError):
                self.inbox.submit(message)

    def test_cli_contract_and_no_report_worker_execution(self):
        args = ['--data-dir', str(self.root)]
        def invoke(command):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(args + command), 0)
            return json.loads(output.getvalue())
        self.assertEqual(invoke(['instruct', '$(touch /tmp/not-executed)', '--request-id', 'test'])['state'], 'waiting')
        def forbidden(*args):
            self.fail('Instruction must not be processed by report collector')
        self.assertEqual(RequestStore(self.root).run_once(collector=forbidden), {'worker': 'idle'})
        result = invoke(['instructions', '--json'])
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(len(result['instructions']), 1)
        self.assertEqual(invoke(['cancel-instruction', 'test'])['state'], 'cancelled')
