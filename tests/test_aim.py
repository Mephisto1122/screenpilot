"""Clicks that land. In a real run (11 Nov 2026) 40 of 93 clicks were the same link tried again: the model that sees
the screenshot also guessed the pixel, missed a name inside a line of text, and was never told that nothing happened."""
import json
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from app import vision
from app.agent import TaskRun
from app.server import Application
from test_system import await_done


def caption():
    im = Image.new('RGB', (1280, 800), 'white'); d = ImageDraw.Draw(im)
    try: f = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 15)
    except OSError: f = ImageFont.load_default()
    d.text((700, 250), 'Photographer / Co-creative: Noa @northlight_sample', fill='black', font=f)
    d.text((60, 100), 'Sign in', fill='black', font=f); d.text((900, 600), 'Sign in', fill='black', font=f)
    return im


@unittest.skipUnless(vision.available(), 'needs Tesseract')
class LocateTests(unittest.TestCase):
    def test_a_name_inside_a_line_is_found_where_it_is_not_in_the_middle_of_the_line(self):
        im = caption()
        line = next(i for i in vision.elements(im) if 'northlight_sample' in i['text'].lower())
        x, y = vision.locate_text(im, '@northlight_sample')
        self.assertTrue(970 <= x <= 1082 and 245 <= y <= 275, (x, y))      # on the handle itself
        self.assertLess(line['center'][0], 970)                             # the middle of the line is on another word

    def test_of_two_places_with_the_same_words_the_nearer_one_is_meant(self):
        im = caption()
        self.assertLess(vision.locate_text(im, 'Sign in', near=(70, 90))[0], 200)
        self.assertGreater(vision.locate_text(im, 'Sign in', near=(880, 590))[0], 800)
        self.assertIsNone(vision.locate_text(im, '@nobodyhere'))


class AimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'plan_contract': False})
        self.prompts = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_with(self, replies, mode='fast'):
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if text.startswith('You supervise'):
                return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps({'progress': 'yes', 'reason': 'ok', 'advice': '', 'plan': [], 'ask_user': False})}]}
            self.prompts.append(text)
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(replies[min(len(self.prompts), len(replies)) - 1])}]}
        self.app.client.transport = transport
        self.app.start_agent('Open the photographer', mode)
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return [a for a in self.app.desktop.lab_actions if a['kind'] == 'click']

    DONE = {'goal_done': True, 'actions': [], 'note': 'ok'}

    def test_the_click_goes_to_the_words_not_to_the_models_guess(self):
        with patch.object(vision, 'available', return_value=True), patch.object(vision, 'locate_text', return_value=(1026, 259)) as locate:
            clicks = self.run_with([{'actions': [{'kind': 'click', 'x': 821, 'y': 258, 'words': '@northlight_sample', 'target': 'the photographer credit in the caption'}]}, self.DONE])
        self.assertEqual((clicks[0]['x'], clicks[0]['y']), (1026, 259))
        self.assertEqual(locate.call_args[0][1], '@northlight_sample'); self.assertEqual(locate.call_args[1]['near'], (821, 258))
        self.assertTrue(any('Aimed by its words' in e['message'] for e in self.app.engine.events))
        self.assertIn('"words"', self.prompts[0])                            # the model is asked to say what it clicks

    def test_the_same_words_far_from_where_it_pointed_are_not_what_it_meant(self):
        with patch.object(vision, 'available', return_value=True), patch.object(vision, 'locate_text', return_value=(85, 110)):
            clicks = self.run_with([{'actions': [{'kind': 'click', 'x': 925, 'y': 610, 'words': 'Read more'}]}, self.DONE])
        self.assertEqual((clicks[0]['x'], clicks[0]['y']), (925, 610))

    def test_without_words_holo_points_when_it_is_installed(self):
        self.app.settings.update({'use_holo': True})
        with patch.object(vision, 'available', return_value=True), patch.object(type(self.app.runner), '_holo_point', return_value=(640, 300)) as holo:
            clicks = self.run_with([{'actions': [{'kind': 'click', 'x': 600, 'y': 320, 'target': 'the heart icon under the photo'}]}, self.DONE])
        self.assertEqual((clicks[0]['x'], clicks[0]['y']), (640, 300)); self.assertEqual(holo.call_args[0][0], 'the heart icon under the photo')

    def test_a_click_with_nothing_said_about_it_is_done_as_given(self):
        clicks = self.run_with([{'actions': [{'kind': 'click', 'x': 600, 'y': 320, 'label': 'x'}]}, self.DONE])
        self.assertEqual((clicks[0]['x'], clicks[0]['y']), (600, 320))

    def test_a_click_that_changed_nothing_is_said_at_once(self):
        still = Image.new('RGB', (1280, 800), 'white')
        with patch.object(type(self.app.desktop), 'capture', lambda self: still):
            self.run_with([{'actions': [{'kind': 'click', 'x': 993, 'y': 262, 'label': 'Open credited photographer profile'}]}, self.DONE, self.DONE, self.DONE])
        self.assertIn('Open credited photographer profile', self.prompts[1])
        self.assertIn('changed nothing on the screen', self.prompts[1])     # in the very next prompt, not after five more tries


class SettleTests(unittest.TestCase):
    def settle(self, frames, before='old page'):
        import hashlib
        looks = iter(frames); seen = []
        def capture():
            f = next(looks); seen.append(f); return types.SimpleNamespace(tobytes=lambda: f.encode())
        run = types.SimpleNamespace(cancel=threading.Event(), restless=0, sig=hashlib.blake2s(before.encode(), digest_size=12).digest(),
                                    app=types.SimpleNamespace(desktop=types.SimpleNamespace(lab=False, capture=capture)))
        with patch('app.agent.time.monotonic', side_effect=[i * 0.3 for i in range(100)]), patch.object(run.cancel, 'wait', lambda t: False):
            TaskRun.settle(run)
        return len(seen), run.restless

    def test_it_waits_until_the_page_stands_still(self):
        self.assertEqual(self.settle(['loading1', 'loading2', 'page', 'page']), (4, 0))   # two looks alike: go on

    def test_a_new_page_that_has_not_started_to_appear_is_waited_for(self):
        # In a real run the address was typed and Return pressed, and the next look still showed the old page:
        # "the address bar shows the new address but the page still displays the old one; waiting" cost a request each time.
        self.assertEqual(self.settle(['old page', 'old page', 'old page', 'new page', 'new page']), (5, 0))

    def test_an_action_that_changed_nothing_is_not_waited_for_long(self):
        looks, restless = self.settle(['old page'] * 40)
        self.assertLess(looks, 10); self.assertEqual(restless, 0)

    def test_a_page_that_never_stands_still_is_not_waited_for_again_and_again(self):
        looks, restless = self.settle([f'frame{i}' for i in range(40)])
        self.assertLess(looks, 12); self.assertEqual(restless, 6)                          # gave up after about 2 s, and remembers



class HoloMissingTests(unittest.TestCase):
    """Holo switched on but not installed in Ollama (HTTP 404): said once, not at every click, and clicks go on."""
    setUp, tearDown, run_with, DONE = AimTests.setUp, AimTests.tearDown, AimTests.run_with, AimTests.DONE

    def test_it_is_said_once_and_not_asked_again(self):
        from urllib.error import HTTPError
        from app import activity, models
        self.app.settings.update({'use_holo': True})
        asked = []
        def no_model(req, timeout=None):
            asked.append(req.full_url); raise HTTPError(req.full_url, 404, 'Not Found', {}, None)
        ollama = self.app.client._http          # requests to Ollama take the real path, so they meet the 404
        start = self.app.start_agent
        def start_with_ollama(*a, **k):
            cloud = self.app.client.transport   # what run_with installed for the cloud model
            self.app.client.transport = lambda url, *rest: ollama(url, *rest) if url.endswith('/api/chat') else cloud(url, *rest)
            return start(*a, **k)
        click = lambda x: {'actions': [{'kind': 'click', 'x': x, 'y': 300, 'target': 'the post with the runner'}]}
        with patch.object(vision, 'available', return_value=False), patch.object(models, 'urlopen', no_model), \
             patch.object(self.app, 'start_agent', start_with_ollama):
            clicks = self.run_with([click(600), click(640), click(680), self.DONE])
        self.assertEqual([c['x'] for c in clicks], [600, 640, 680])                  # every click still happened, where the model pointed
        self.assertEqual(len(asked), 1)                                              # Ollama was asked once, not three times
        said = [m['text'] for m in self.app.chat.list() if 'Holo' in m.get('text', '')]
        self.assertEqual(len(said), 1); self.assertIn('HOLO=1 ./run.sh', said[0])   # and the user was told once what to do
        self.assertFalse(any(e.get('trace') and 'holo' in e['msg'].lower() for e in activity.ACTIVITY.entries()))  # no traceback for it


if __name__ == '__main__': unittest.main()
