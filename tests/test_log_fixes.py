"""What a real run's log showed going wrong (Instagram research with GPT, 11 Nov 2026), fixed and kept fixed."""
import json
import tempfile
import unittest
from unittest.mock import patch

from app import models
from app.core import key_name, validate_action
from app.models import Unreachable
from app.server import Application
from app.supervisor import PROMPT as REVIEW_PROMPT
from test_system import await_done


class KeyNameTests(unittest.TestCase):
    def test_keys_are_spelled_the_way_the_desktop_understands(self):
        # "key enter", "key esc" and "key alt+left" did nothing at all, silently: the address never opened.
        for said, pressed in (('enter', 'Return'), ('Enter', 'Return'), ('esc', 'Escape'), ('alt+left', 'alt+Left'),
                              ('Ctrl+L', 'ctrl+l'), ('ctrl l', 'ctrl+l'), ('PageDown', 'Page_Down'), ('backspace', 'BackSpace'),
                              ('Return', 'Return'), ('ctrl+shift+t', 'ctrl+shift+t'), ('f5', 'F5'), ('ctrl+Tab', 'ctrl+Tab')):
            self.assertEqual(validate_action({'kind': 'key', 'key': said})['key'], pressed, said)
        self.assertEqual(key_name('Alt + Left'), 'alt+Left')


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.prompts = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_with(self, replies):
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if text.startswith('You supervise'):
                return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(
                    {'progress': 'yes', 'reason': 'fine', 'advice': '', 'plan': [], 'ask_user': False})}]}
            self.prompts.append(text)
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(replies[min(len(self.prompts), len(replies)) - 1])}]}
        self.app.client.transport = transport
        self.app.start_agent('Find photographers', 'anthropic_direct')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return self.app.agent

    def test_scrolling_again_is_done_not_skipped(self):
        scroll = {'actions': [{'kind': 'scroll', 'amount': 5, 'label': 'scroll'}]}
        agent = self.run_with([scroll, scroll, scroll, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(agent['steps'], 3)  # the second and third used to be "repeated the previous step; skipped it"
        self.assertEqual(sum(a['kind'] == 'scroll' for a in self.app.desktop.lab_actions), 3)

    def test_the_next_arrow_can_be_clicked_again_when_the_first_click_worked(self):
        # Real log: "next post arrow" clicked, the next post appeared, the same arrow clicked again -> "repeated the previous step; skipped it".
        arrow = {'actions': [{'kind': 'click', 'x': 1237, 'y': 422, 'label': 'next post arrow'}]}
        self.run_with([arrow, arrow, arrow, {'goal_done': True, 'actions': [], 'note': 'ok'}])   # the test screen changes at every click
        self.assertEqual(sum(a['kind'] == 'click' for a in self.app.desktop.lab_actions), 3)

    def test_the_same_click_again_after_nothing_happened_is_still_held_back(self):
        from PIL import Image
        from unittest.mock import patch
        still = Image.new('RGB', (1280, 800), 'white')
        arrow = {'actions': [{'kind': 'click', 'x': 1237, 'y': 422, 'label': 'next post arrow'}]}
        with patch.object(type(self.app.desktop), 'capture', lambda self: still):
            self.run_with([arrow, arrow, {'goal_done': True, 'actions': [], 'note': 'ok'}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(sum(a['kind'] == 'click' for a in self.app.desktop.lab_actions), 1)

    def test_typing_the_same_again_is_still_held_back(self):
        typing = {'actions': [{'kind': 'type', 'text': 'London', 'label': 'type'}]}
        agent = self.run_with([typing, typing, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(sum(a['kind'] == 'type' for a in self.app.desktop.lab_actions), 1)

    def test_the_agent_keeps_a_notebook_when_it_sees_screenshots_too(self):
        agent = self.run_with([{'actions': [{'kind': 'jot', 'text': 'Rina Photography: London, credited by Sample Magazine'},
                                            {'kind': 'click', 'x': 400, 'y': 180, 'label': 'next profile'}]},
                               {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertIn('"kind":"jot"', self.prompts[0])                       # it is told about the notebook
        self.assertIn('Your task notebook:\n- Rina Photography: London', self.prompts[1])  # and sees what it wrote
        self.assertEqual(sum(a['kind'] == 'click' for a in self.app.desktop.lab_actions), 1)  # the jot did not cost a step alone

    def test_being_on_a_relevant_page_is_not_called_progress(self):
        self.assertIn('being on a relevant\npage for many actions without writing anything in the notebook is a loop', REVIEW_PROMPT)
        self.assertIn('still loading', REVIEW_PROMPT)  # ... and a page that is loading is not a failure (the over-correction that followed)


class SlowAnswerTests(unittest.TestCase):
    """A GPT answer that hangs is asked for again after 30 s instead of being waited for up to 3 minutes."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def send(self, outcomes, provider='openai', payload=None, timeout=180):
        waits = []
        def transport(url, payload, headers, timeout):
            waits.append(timeout); o = outcomes[min(len(waits), len(outcomes)) - 1]
            if isinstance(o, Exception): raise o
            return o
        self.app.client.transport = transport
        with patch.object(models.time, 'sleep', lambda s: None):
            return self.app.client._send(provider, 'https://x', payload or {}, {}, timeout, None, None), waits

    def test_a_hanging_answer_is_asked_again_at_once(self):
        result, waits = self.send([Unreachable('no answer within 30 s'), {'ok': 1}])
        self.assertEqual(result, {'ok': 1}); self.assertEqual(waits, [30, 180])  # short patience first, then the full time

    def test_a_normal_answer_is_one_request(self):
        result, waits = self.send([{'ok': 1}])
        self.assertEqual(waits, [30])

    def test_streamed_requests_keep_their_own_silence_check(self):
        result, waits = self.send([{'ok': 1}], provider='anthropic', payload={'stream': True}, timeout=60)
        self.assertEqual(waits, [60])

    def test_a_real_outage_still_backs_off_and_gives_up(self):
        with self.assertRaisesRegex(Unreachable, 'Could not reach'):
            self.send([Unreachable('the connection failed')])


if __name__ == '__main__': unittest.main()


class TaskTitleTests(unittest.TestCase):
    """A long brief is shown in one line under the screen, not as its first 300 characters run together."""
    def test_the_sentence_that_says_what_the_task_is(self):
        from app.agent import task_title
        self.assertEqual(task_title("RULES\n- Don't ask.\n- Only Instagram.\n\nGOAL\nFind 5 Miami photographers.\n\nHOW\n1. Search"), 'Find 5 Miami photographers.')
        self.assertEqual(task_title('Compare the pricing of three tools.\n\nRULES\n- x'), 'Compare the pricing of three tools.')
        self.assertEqual(task_title('GOAL: find flats\nmore'), 'find flats')
        self.assertEqual(task_title('RULES\n- a\n- b'), 'RULES'); self.assertEqual(task_title(''), '')
        self.assertTrue(len(task_title('x' * 300)) <= 160 and task_title('x' * 300).endswith('…'))
