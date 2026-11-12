import json
import tempfile
import time
import unittest

from app.server import Application
from test_system import await_done


class SessionUsageTests(unittest.TestCase):
    """The session count adds up every call since Screenpilot started; finishing a task does not reset it."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        def transport(url, payload, headers, timeout):
            return {'usage': {'input_tokens': 1000, 'output_tokens': 50},
                    'content': [{'type': 'text', 'text': json.dumps({'goal_done': True, 'actions': [], 'note': 'ok'})}]}
        self.app.client.transport = transport

    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_task(self, task):
        self.app.start_agent(task, 'anthropic_direct')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)

    def test_counts_add_up_across_tasks_and_reset_only_when_asked(self):
        self.run_task('first')
        one = self.app.ledger.summary()['session']
        self.assertGreater(one['total_tokens'], 0)
        self.run_task('second')
        two = self.app.ledger.summary(self.app.agent.get('run_id'))
        self.assertGreater(two['session']['total_tokens'], one['total_tokens'])        # kept growing
        self.assertLess(two['run']['total_tokens'], two['session']['total_tokens'])     # a task is only part of it
        self.assertEqual(two['session']['by_provider']['anthropic']['total_tokens'], two['session']['total_tokens'])
        total = two['session']['total_tokens']
        # A restart does not reset the count: a new Screenpilot on the same data keeps counting from where it was.
        again = Application(self.tmp.name, lab=True)
        try: self.assertEqual(again.ledger.summary()['session']['total_tokens'], total)
        finally: again.close()
        time.sleep(0.01); self.app.ledger.reset()                                       # only Reset does
        self.assertEqual(self.app.ledger.summary()['session']['total_tokens'], 0)


if __name__ == '__main__': unittest.main()
