"""Real run: five search phrases, each made four times, every time showing accounts already visited. The model sees
only its last few actions, so the app remembers what was searched and says when a source is used up. And a wait ends
when the page has arrived, not when its seconds are over."""
import json
import tempfile
import unittest

from PIL import Image, ImageDraw

from app import vision
from app.contract import plan_prompt
from app.server import Application
from app.supervisor import PROMPT as SUPERVISOR
from test_system import await_done


def typing(text): return {'actions': [{'kind': 'type', 'text': text, 'label': 'search'}]}


class SearchMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'plan_contract': False})
        self.steps = []; self.prompts = []
        def reply(body): return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if text.startswith('You supervise'): self.reviews.append(text); return reply({'progress': 'yes', 'reason': 'ok', 'advice': '', 'plan': [], 'ask_user': False})
            self.prompts.append(text)
            return reply(self.steps.pop(0) if self.steps else {'goal_done': True, 'actions': [], 'note': 'Done.'})
        self.reviews = []; self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_task(self):
        self.app.start_agent('Find Miami photographers', 'fast')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 15)
        return self.app.agent

    def test_the_model_is_shown_what_it_already_searched(self):
        self.steps = [typing('miami film photographer'), typing('Miami  Editorial photographer'), typing('miami film photographer')]
        self.assertEqual(self.run_task()['status'], 'completed')
        self.assertNotIn('Already searched in this task', self.prompts[0])
        self.assertIn('Already searched in this task', self.prompts[1]); self.assertIn('"miami film photographer"', self.prompts[1])
        self.assertIn('"miami editorial photographer"; "miami film photographer" x2', self.prompts[3])   # one spelling, counted, latest last

    def test_the_third_time_it_is_told_the_source_is_used_up(self):
        self.steps = [typing('miami film photographer'), typing('miami editorial photographer')] * 2 + [typing('miami film photographer')]
        self.assertEqual(self.run_task()['status'], 'completed')
        self.assertNotIn('3 times in this task', self.prompts[4])
        self.assertIn('You have typed \\"miami film photographer\\" 3 times in this task', self.prompts[5])
        self.assertIn('different KIND of place', self.prompts[5])

    def test_addresses_are_not_listed_as_searches_and_a_front_page_may_be_opened_again(self):
        self.steps = [typing('https://www.instagram.com/'), typing('instagram.com/sample_photographer')] * 3
        self.assertEqual(self.run_task()['status'], 'completed')
        self.assertFalse(any('Already searched in this task' in p for p in self.prompts))
        self.assertFalse(any('You have opened https://www.instagram.com/ ' in p for p in self.prompts))
        self.assertIn('You have opened instagram.com/sample_photographer 3 times', self.prompts[-1])

    def test_everyone_is_told_to_change_the_kind_of_source(self):
        self.run_task()
        self.assertIn('change the KIND\nof place', self.prompts[0])
        self.assertIn('Going round the same few searches is a loop', SUPERVISOR)
        self.assertIn('at least three different KINDS of place', plan_prompt('Find photographers'))


class ArrivalTests(unittest.TestCase):
    def screen(self, *boxes):
        im = Image.new('RGB', (1280, 800), 'white'); d = ImageDraw.Draw(im)
        for box in boxes: d.rectangle(box, fill='black')
        return im

    def test_a_blinking_cursor_is_not_a_change_but_new_results_are(self):
        blank, caret, results = self.screen(), self.screen((500, 120, 501, 138)), self.screen((400, 150, 800, 500))
        small = vision._small
        self.assertFalse(vision.moved(small(blank), small(caret)))
        self.assertTrue(vision.moved(small(blank), small(results)))

    def test_a_wait_ends_when_the_screen_changed_and_then_stood_still(self):
        blank, half, full = self.screen(), self.screen((400, 150, 800, 300)), self.screen((400, 150, 800, 500))
        w = vision.Arrival(blank)
        self.assertFalse(w.arrived(blank))      # nothing yet: keep waiting
        self.assertFalse(w.arrived(self.screen((500, 120, 501, 138))))  # only the cursor blinked
        self.assertFalse(w.arrived(half))       # it started to appear
        self.assertFalse(w.arrived(full))       # still filling in
        self.assertTrue(w.arrived(full))        # there, and still

    def test_a_screen_that_never_changes_is_waited_for_in_full(self):
        w = vision.Arrival(self.screen())
        self.assertFalse(any(w.arrived(self.screen()) for _ in range(10)))


if __name__ == '__main__': unittest.main()
