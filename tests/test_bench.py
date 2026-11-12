import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bench
from app.server import Application, handler_factory


class CheckTests(unittest.TestCase):
    def test_an_answer_is_checked_not_believed(self):
        self.assertTrue(bench.passed(r'example\s+domain', 'The heading is "Example Domain".'))
        self.assertFalse(bench.passed(r'example\s+domain', 'Done.'))
        self.assertTrue(bench.passed(bench.TASKS[1][2], 'The result is 108,583'))
        self.assertFalse(bench.passed(bench.TASKS[1][2], 'About 100000'))

    def test_answers_are_not_given_away_in_the_instructions(self):
        for name, text, pattern in bench.TASKS: self.assertFalse(bench.passed(pattern, text), name)


class BenchRunTests(unittest.TestCase):
    """The benchmark against a real (test-screen) Screenpilot over HTTP."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'plan_contract': False})  # this measures the benchmark's own counting; planning has its tests
        n = {'i': 0}
        def transport(url, payload, headers, timeout):
            n['i'] += 1
            body = ({'actions': [{'kind': 'click', 'x': 400 + n['i'], 'y': 180, 'label': 'next'}]} if n['i'] % 2 else
                    {'goal_done': True, 'actions': [], 'note': 'The heading is Example Domain.'})
            return {'usage': {'input_tokens': 1000, 'output_tokens': 50}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        self.app.client.transport = transport
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.app.close(); self.tmp.cleanup()

    def bench(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): code = bench.main([*argv, '--url', self.url])
        return code, out.getvalue()

    def test_right_and_wrong_answers_are_told_apart_and_measured(self):
        code, out = self.bench('fast', '--tasks', '1,2')
        self.assertEqual(code, 1)  # one task failed
        self.assertRegex(out, r'PASS\s+Read a page'); self.assertRegex(out, r'FAIL\s+Use a command.*wrong answer')
        self.assertRegex(out, r'fast\s+1 of 2\s+\d+ s\s+4,200\s+\$0\.01')  # tokens and cost of both tasks, from the ledger
        self.assertEqual(self.app.skills.list(), [])  # what the runs saved was removed again

    def test_a_mode_without_a_key_is_skipped_with_the_reason(self):
        self.app.settings.keys['anthropic'] = ''
        with self.assertRaises(SystemExit) as stop: self.bench('smart', '--tasks', '1')
        self.assertIn('No mode is ready', str(stop.exception))


if __name__ == '__main__': unittest.main()
