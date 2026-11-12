"""Reset desktop works at once, also when the task is inside a model request that cannot be interrupted: the dashboard
is clean immediately, and whatever that task still reports when the request returns goes nowhere."""
import json
import tempfile
import threading
import time
import unittest

from app.server import Application
from test_system import await_done


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'plan_contract': False})
        self.hold = threading.Event(); self.inside = threading.Event()
        def transport(url, payload, headers, timeout):
            self.inside.set(); self.hold.wait(8)      # a model request that takes its time and cannot be stopped
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps({'goal_done': True, 'actions': [], 'note': 'LATE RESULT'})}]}
        self.app.client.transport = transport
    def tearDown(self): self.hold.set(); self.app.close(); self.tmp.cleanup()

    def test_reset_during_a_model_request_cleans_the_dashboard_at_once(self):
        self.app.start_agent('Find something', 'fast')
        self.assertTrue(self.inside.wait(5))
        began = time.monotonic()
        self.app.reset()
        self.assertLess(time.monotonic() - began, 4)                         # it does not wait for the model
        agent = self.app.agent
        self.assertEqual(agent['status'], 'idle'); self.assertEqual(agent['plan'], []); self.assertNotIn('task', agent)
        self.assertEqual(self.app.chat.list()[-1]['text'][:13], 'Desktop reset')
        self.hold.set()                                                       # now the old request comes back
        await_done(lambda: not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.agent['status'], 'idle')                    # ... and changes nothing
        self.assertFalse(any('LATE RESULT' in m['text'] or m['kind'] in ('result', 'error') for m in self.app.chat.list()))
        self.assertIsNone(self.app.runner.past)

    def test_a_new_task_after_the_reset_runs_normally(self):
        self.app.start_agent('Find something', 'fast'); self.assertTrue(self.inside.wait(5))
        self.app.reset(); self.hold.set()
        await_done(lambda: not self.app.runner.thread.is_alive(), 10)
        self.app.start_agent('Find something else', 'fast')
        await_done(lambda: self.app.agent['status'] == 'completed' and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.chat.list()[-1]['text'], 'LATE RESULT')    # this one is the new task's own result

    def test_stop_says_so_at_once(self):
        self.app.start_agent('Find something', 'fast'); self.assertTrue(self.inside.wait(5))
        self.app.stop_all()
        self.assertEqual(self.app.agent['message'], 'Stopping')
        self.hold.set(); await_done(lambda: self.app.agent['status'] == 'stopped', 10)


if __name__ == '__main__': unittest.main()
