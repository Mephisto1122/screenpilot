"""Saving with GPT: the cheapest model takes the routine steps; the chosen model makes the first move, takes over
after trouble, and takes every step that records a finding or finishes, so the judgment stays its own."""
import json
import tempfile
import unittest

from app.server import Application
from test_system import await_done

CLICK = {'actions': [{'kind': 'click', 'x': 400, 'y': 180, 'label': 'open profile'}]}
SCROLL = {'actions': [{'kind': 'scroll', 'dy': 3, 'x': 640, 'y': 400, 'label': 'down'}]}
JOT = {'actions': [{'kind': 'jot', 'text': 'KEPT: @ana.sample, Nike and Vogue'}]}
REJECT = {'actions': [{'kind': 'jot', 'text': 'VISITED: @ana.sample, weddings'}]}
DONE = {'goal_done': True, 'actions': [], 'note': 'Ana: instagram.com/ana.sample'}


class ThriftyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'openai_key': 'fixture', 'cloud_provider': 'openai', 'openai_model': 'gpt-6.1-terra', 'plan_contract': False, 'thrifty': True})
        self.cheap = []; self.strong = []; self.asked = []
        def transport(url, payload, headers, timeout):
            model = payload['model']; self.asked.append(model)
            queue = self.cheap if model == 'gpt-6-luna' else self.strong
            body = json.dumps(queue.pop(0) if queue else DONE)
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': body}]}]}
        self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_task(self, mode='fast'):
        self.app.start_agent('Find a creative Miami photographer', mode)
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)

    def test_luna_drives_and_the_chosen_model_decides_what_is_recorded(self):
        self.strong = [CLICK, JOT, DONE]            # the first move; then the two steps it takes over
        self.cheap = [SCROLL, REJECT, CLICK, DONE]  # Luna wanted to reject her and, later, to finish
        self.run_task()
        self.assertEqual(self.asked, ['gpt-6.1-terra',                  # the first move is the chosen model's
                                      'gpt-6-luna',                     # routine: scrolling
                                      'gpt-6-luna', 'gpt-6.1-terra',    # Luna was about to write a note: Terra takes that step
                                      'gpt-6-luna',                     # routine again
                                      'gpt-6-luna', 'gpt-6.1-terra'])   # Luna says it is done: the result is Terra's
        self.assertIn('KEPT: @ana.sample', json.dumps(self.app.runner.past['notebook']))        # Terra's verdict was recorded ...
        self.assertNotIn('weddings', json.dumps(self.app.runner.past['notebook']))              # ... not Luna's
        self.assertTrue(any('the stronger model takes this step' in e['message'] for e in self.app.engine.events))

    def test_it_works_the_same_when_the_model_gets_text_instead_of_screenshots(self):
        self.app.settings.update({'mac_eyes': False})
        self.strong = [CLICK, DONE]; self.cheap = [SCROLL, DONE]
        self.run_task('smart')
        self.assertEqual([m for m in self.asked if m.startswith('gpt')][:2], ['gpt-6.1-terra', 'gpt-6-luna'])

    def test_it_is_off_unless_chosen(self):
        self.app.settings.update({'thrifty': False})
        self.strong = [CLICK, SCROLL, DONE]
        self.run_task()
        self.assertEqual(set(self.asked), {'gpt-6.1-terra'})

    def test_with_luna_itself_chosen_there_is_nothing_to_save(self):
        self.app.settings.update({'openai_model': 'gpt-6-luna'})
        self.cheap = [CLICK, JOT, DONE]
        self.run_task()
        self.assertEqual(self.asked, ['gpt-6-luna'] * 3)   # no step is asked twice


if __name__ == '__main__': unittest.main()
