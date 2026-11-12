import json
import tempfile
import unittest
from unittest.mock import patch

from app import vision
from app.server import Application
from test_system import await_done


def item(n, text, box, kind='text'):
    return {'id': n, 'text': text, 'box': box, 'kind': kind, 'center': ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)}


class TextModeBase(unittest.TestCase):
    """Claude, text only: the cloud model gets the local eyes' description, never the screenshot."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'mac_eyes': False})  # the local eyes have their own tests (test_eyes.py)
        self.items = [item(1, 'Your destination', (60, 100, 220, 120)), item(2, '', (60, 130, 420, 165), 'box'),
                      item(3, 'Search routes', (60, 200, 230, 240), 'button')]
        for p in (patch.object(vision, 'available', return_value=True), patch.object(vision, 'elements', return_value=self.items)):
            p.start(); self.addCleanup(p.stop)
        self.payloads = []

    def tearDown(self): self.app.close(); self.tmp.cleanup()

    verdict = {'progress': 'yes', 'reason': 'fine', 'advice': '', 'plan': [], 'ask_user': False}

    def claude(self, replies):
        self.reviews = []
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text')
            if text.startswith('You supervise an agent'):  # progress checks are answered separately
                self.reviews.append(payload)
                verdict = self.verdict(len(self.reviews)) if callable(self.verdict) else self.verdict
                return {'usage': {'input_tokens': 500, 'output_tokens': 40}, 'content': [{'type': 'text', 'text': json.dumps(verdict)}]}
            self.payloads.append(payload)
            reply = replies[min(len(self.payloads), len(replies)) - 1]
            return {'usage': {'input_tokens': 700, 'output_tokens': 40}, 'content': [{'type': 'text', 'text': json.dumps(reply)}]}
        self.app.client.transport = transport

    def prompt(self, i): return '\n'.join(c['text'] for c in self.payloads[i]['messages'][0]['content'])

    def run_task(self, task):
        self.app.start_agent(task, 'anthropic_text')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return self.app.agent


class TextModeTests(TextModeBase):
    def test_no_image_is_sent_and_marks_become_clicks(self):
        self.claude([{'actions': [{'kind': 'type', 'mark': 2, 'text': 'Paris'}, {'kind': 'click', 'mark': 3}]},
                     {'goal_done': True, 'actions': [], 'note': 'Searched.'}])
        agent = self.run_task('Search routes to Paris')
        self.assertEqual(agent['status'], 'completed', agent)
        for p in self.payloads:
            self.assertEqual({c['type'] for c in p['messages'][0]['content']}, {'text'})  # no image block at all
        self.assertIn('SCREEN NOW', self.prompt(0))
        self.assertIn('[3] button "Search routes" (145,220)', self.prompt(0))
        acts = self.app.desktop.lab_actions
        self.assertEqual([(a['kind'], a.get('x'), a.get('text')) for a in acts], [('click', 240, None), ('type', None, 'Paris'), ('click', 145, None)])

    def test_strong_model_plans_first_then_fast_model_steps(self):
        self.claude([{'plan': ['Fill the destination', 'Search'], 'actions': []},
                     {'actions': [{'kind': 'type', 'mark': 2, 'text': 'Haifa'}]}, {'goal_done': True, 'actions': []},
                     {'actions': [{'kind': 'click', 'mark': 3}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.run_task('Find routes to Haifa')
        models = [p['model'] for p in self.payloads]
        self.assertEqual(models[0], 'claude-sonnet-5-5')
        self.assertTrue(all(m == 'claude-haiku-5-5' for m in models[1:]), models)

    def test_struggling_fast_model_hands_over_to_the_strong_one(self):
        self.claude([{'actions': [{'kind': 'click', 'mark': 3}]},
                     {'stuck': True, 'actions': [], 'note': 'cannot find the results'},
                     {'actions': [{'kind': 'scroll', 'amount': 5}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.run_task('Find routes')
        models = [p['model'] for p in self.payloads]
        self.assertEqual(models[:3], ['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-sonnet-5-5'])
        self.assertEqual(self.app.agent['status'], 'completed')

    def test_wrong_element_number_is_corrected_not_fatal(self):
        self.claude([{'actions': [{'kind': 'click', 'mark': 99}]}, {'actions': [{'kind': 'click', 'mark': 3}]},
                     {'goal_done': True, 'actions': [], 'note': 'ok'}])
        agent = self.run_task('Click search')
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertIn('no element number 99', self.prompt(1))

    def test_notebook_keeps_findings_across_steps(self):
        self.claude([{'actions': [{'kind': 'jot', 'text': 'Haifa route: 42 minutes'}, {'kind': 'click', 'mark': 3}]},
                     {'goal_done': True, 'actions': [], 'note': 'The route takes 42 minutes.'}])
        self.run_task('How long is the route to Haifa?')
        self.assertIn('Your task notebook:\n- Haifa route: 42 minutes', self.prompt(1))
        self.assertEqual(self.app.chat.list()[-1]['text'], 'The route takes 42 minutes.')

    def test_openai_text_mode_sends_no_image(self):
        self.app.settings.update({'openai_key': 'fixture'})
        sent = []
        def transport(url, payload, headers, timeout):
            sent.append(payload)
            body = {'goal_done': True, 'actions': [], 'note': 'ok'} if len(sent) > 1 else {'actions': [{'kind': 'click', 'mark': 1}]}
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(body)}]}]}
        self.app.client.transport = transport
        self.app.start_agent('Click the label', 'openai_text')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual({c['type'] for c in sent[0]['input'][0]['content']}, {'input_text'})  # text only (in parts, so the unchanged ones can be cached)


if __name__ == '__main__': unittest.main()


class FocusTests(TextModeBase):
    def test_active_window_is_reported_and_key_spam_is_called_out(self):
        self.app.desktop.active_title = lambda: 'xterm'
        # Every second identical Return is already skipped, so 4 real presses take 7 replies.
        self.claude([{'actions': [{'kind': 'key', 'key': 'Return'}]}] * 7 + [{'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.run_task('Open the article')
        self.assertIn('Desktop now: active window (keys and typing go here): "xterm"', self.prompt(0))
        self.assertEqual(sum(a['kind'] == 'key' for a in self.app.desktop.lab_actions), 4)
        self.assertIn('pressed Return 4 times in a row. Keys go to the active window (xterm)', self.prompt(7))
        self.assertEqual(self.payloads[7]['model'], 'claude-sonnet-5-5')  # the stronger model takes a look


class SupervisorTests(TextModeBase):
    """The progress check runs every few actions and on loop signals, and can change course or call the user."""
    def no(self, n):
        return {'progress': 'no', 'reason': 'Keys are going into a terminal, not the browser.',
                'advice': 'Switch to the browser window in the taskbar.', 'plan': ['Switch to the browser', 'Open the article'],
                'ask_user': False}

    def test_routine_check_after_eight_actions_is_text_only(self):
        self.claude([{'actions': [{'kind': 'scroll', 'amount': i + 1}]} for i in range(9)] + [{'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.run_task('Read the whole page')
        self.assertEqual(len(self.reviews), 1)
        review = self.reviews[0]
        self.assertEqual([c['type'] for c in review['messages'][0]['content']], ['text'])  # no screenshot in text mode
        self.assertEqual(review['model'], 'claude-sonnet-5-5')
        text = review['messages'][0]['content'][-1]['text']
        self.assertIn('GOAL: Read the whole page', text); self.assertIn('Search routes', text)

    def test_no_progress_changes_the_plan_and_tells_the_agent_why(self):
        self.verdict = lambda n: self.no(n) if n == 1 else TextModeBase.verdict
        self.claude([{'actions': [{'kind': 'key', 'key': 'Return'}]}] * 7 + [{'goal_done': True, 'actions': []}] * 3)
        self.run_task('Open the article')
        self.assertGreaterEqual(len(self.reviews), 1)
        later = [self.prompt(i) for i in range(len(self.payloads))]
        self.assertTrue(any('Supervisor: you are NOT getting closer' in p and 'Switch to the browser window' in p for p in later))
        self.assertTrue(any(m['kind'] == 'plan' and m['goals'][0] == 'Switch to the browser' for m in self.app.chat.list()))
        self.assertTrue(any(m['kind'] == 'status' and 'not getting closer' in m['text'] for m in self.app.chat.list()))

    def test_two_failed_checks_in_a_row_ask_the_user(self):
        self.app.settings.update({'ask_when_stuck': True})
        self.verdict = lambda n: {**self.no(n), 'plan': []}
        self.claude([{'actions': [{'kind': 'scroll', 'amount': (i % 5) + 1}]} for i in range(40)])
        self.app.start_agent('Open the article', 'anthropic_text')
        await_done(lambda: self.app.agent['status'] == 'waiting', 15)
        self.assertEqual(len(self.reviews), 2)
        self.assertIn("I'm not getting closer to the goal", self.app.agent['waiting']['reason'])
        self.app.agent_cancel.set(); await_done(lambda: not self.app.runner.thread.is_alive(), 5)

    def test_a_failed_check_does_not_stop_the_task(self):
        self.verdict = None  # unreadable verdict
        self.claude([{'actions': [{'kind': 'scroll', 'amount': i + 1}]} for i in range(9)] + [{'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(self.run_task('Scroll a lot')['status'], 'completed')
