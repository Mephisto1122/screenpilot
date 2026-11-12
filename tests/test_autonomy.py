import json
import tempfile
import unittest

from app.server import Application
from test_system import await_done

CLICK = json.dumps({'actions': [{'kind': 'click', 'x': 5, 'y': 6, 'label': 'next'}]})
DONE = json.dumps({'goal_done': True, 'actions': [], 'note': 'Finished.'})


class AutonomyTests(unittest.TestCase):
    """With "Ask me when it gets stuck" switched on: when the agent cannot go on, it asks; it does not end the task."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'ask_when_stuck': True})
        self.prompts = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def start(self, texts):
        """texts: replies in order; an Exception is raised instead of answered."""
        def transport(url, payload, headers, timeout):
            self.prompts.append('\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text'))
            t = texts[min(len(self.prompts), len(texts)) - 1]
            if isinstance(t, Exception): raise t
            return {'usage': {'input_tokens': 100, 'output_tokens': 10}, 'content': [{'type': 'text', 'text': t}]}
        self.app.client.transport = transport
        self.app.start_agent('Do the thing', 'anthropic_direct')

    def stuck(self):
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        self.assertEqual(self.app.agent['waiting']['type'], 'stuck', self.app.agent)
        return self.app.agent['waiting']['reason']

    def finish(self):
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return self.app.agent

    def test_try_again_continues_the_same_task_to_the_end(self):
        self.start(['no', 'no', 'no', CLICK, DONE])
        self.assertIn('kept replying without valid JSON', self.stuck())
        self.app.runner.reply('continue')
        agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertEqual(agent['steps'], 1)

    def test_the_users_instructions_reach_the_model(self):
        self.start(['no', 'no', 'no', DONE])
        self.stuck()
        self.app.runner.reply('Use the search box at the top instead')
        self.assertEqual(self.finish()['status'], 'completed')
        self.assertIn('The user says: \\"Use the search box at the top instead\\"', self.prompts[-1])

    def test_an_unexpected_error_asks_instead_of_failing(self):
        self.start([CLICK, RuntimeError('boom'), DONE])
        self.assertIn('boom', self.stuck())
        self.app.runner.reply('continue')
        agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent)

    def test_stop_ends_the_task(self):
        self.start(['no'])
        self.stuck()
        self.app.runner.reply('deny')
        agent = self.finish()
        self.assertEqual(agent['status'], 'stopped'); self.assertIsNone(agent.get('waiting'))

    def test_stuck_again_asks_again(self):
        self.start(['no'])
        self.stuck(); one = self.app.agent['waiting']['id']
        self.app.runner.reply('continue')
        await_done(lambda: self.app.agent['status'] == 'waiting' and self.app.agent['waiting']['id'] != one, 10)
        self.assertEqual(self.app.agent['waiting']['type'], 'stuck')


if __name__ == '__main__': unittest.main()
