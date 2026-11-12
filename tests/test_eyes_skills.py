import json
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from app import vision
from app.server import Application
from test_system import await_done


def item(n, text, box, kind='text'):
    return {'id': n, 'text': text, 'box': box, 'kind': kind, 'center': ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)}


@unittest.skipUnless(vision.available(), 'Tesseract is not installed')
class EyesTests(unittest.TestCase):
    """Real OCR and box detection on a drawn form."""
    def setUp(self): vision._cache.clear()

    def form(self):
        im = Image.new('RGB', (1280, 800), 'white'); d = ImageDraw.Draw(im)
        try: font = ImageFont.load_default(size=18)
        except TypeError: self.skipTest('Pillow without scalable default font')
        d.text((60, 100), 'Your destination', fill='black', font=font)
        d.rectangle((60, 130, 420, 165), outline=(120, 120, 120), width=2)          # empty text field
        d.rounded_rectangle((60, 200, 230, 240), radius=6, fill=(225, 225, 230), outline=(110, 110, 110), width=2)
        d.text((80, 210), 'Search routes', fill='black', font=font)                # button with a label
        d.text((700, 400), 'Forgot your password?', fill=(30, 60, 200), font=font)  # a link
        return im

    def test_finds_button_link_and_field_by_label(self):
        items = vision.elements(self.form())
        button = vision.find(items, 'Search routes button'); self.assertIsNotNone(button)
        self.assertTrue(60 <= button['center'][0] <= 230 and 200 <= button['center'][1] <= 240, button)
        link = vision.find(items, 'Forgot your password link'); self.assertIsNotNone(link)
        self.assertTrue(700 <= link['center'][0] <= 950 and 395 <= link['center'][1] <= 425, link)
        field = vision.find(items, 'destination field'); self.assertIsNotNone(field)
        self.assertTrue(60 <= field['center'][0] <= 420 and 130 <= field['center'][1] <= 165, field)  # the box, not the label
        self.assertIsNone(vision.find(items, 'Checkout'))

    def test_marks_and_anchor(self):
        im = self.form(); items = vision.elements(im)
        self.assertEqual(vision.draw_marks(im, items).size, (1280, 800))
        self.assertEqual(vision.anchor_at(items, 140, 220), 'Search routes')

    def test_anchor_from_a_small_piece_of_the_screen(self):
        im = self.form()
        crop, x0, y0 = vision.click_crop(im, 140, 220)
        self.assertLessEqual(crop.width * crop.height, 480 * 100)
        self.assertEqual(vision.anchor_near(crop, x0, y0, 140, 220), 'Search routes')
        crop, x0, y0 = vision.click_crop(im, 800, 410)
        self.assertEqual(vision.anchor_near(crop, x0, y0, 800, 410), 'Forgot your password?')


class LocalEyesTests(unittest.TestCase):
    """The local executor clicks by text when it can, by numbered mark otherwise."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'local_model': 'qwen3-vl:8b'})
        self.items = [item(1, 'Your destination', (60, 100, 220, 120)), item(2, 'Type a city', (60, 130, 420, 165), 'button'),
                      item(3, 'Search routes', (60, 200, 230, 240), 'button'), item(4, '', (1200, 20, 1240, 50), 'box')]
        for p in (patch.object(vision, 'available', return_value=True), patch.object(vision, 'elements', return_value=self.items)):
            p.start(); self.addCleanup(p.stop)
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def fake(self, thoughts, mark=4):
        log = {'think': [], 'mark': [], 'locate': []}
        def transport(url, payload, headers, timeout):
            content = payload['messages'][0]['content']
            if 'format' not in payload:
                log['think'].append(content); text = thoughts[min(len(log['think']), len(thoughts)) - 1]
            elif 'numbered red marks' in content:
                log['mark'].append(content); text = json.dumps({'mark': mark})
            else:
                log['locate'].append(content); text = json.dumps({'x': 500, 'y': 500})
            return {'message': {'content': text}, 'prompt_eval_count': 10, 'eval_count': 5}
        self.app.client.transport = transport
        return log

    def run_task(self, task):
        self.app.start_agent(task, 'local')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting'), 10)

    def test_text_match_needs_no_locate_call(self):
        log = self.fake(["Type 'Paris' into the destination field.", "Click 'Search routes'.", 'The task is complete.'])
        self.run_task('Search routes to Paris')
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        acts = self.app.desktop.lab_actions
        self.assertEqual((acts[0]['x'], acts[0]['y'], acts[1]['text']), (240, 147, 'Paris'))  # the field below the label
        self.assertEqual((acts[2]['x'], acts[2]['y']), (145, 220))
        self.assertEqual(log['mark'], []); self.assertEqual(log['locate'], [])
        self.assertIn('Search routes', log['think'][0])  # the model was told what the screen says

    def test_icon_without_text_is_picked_by_mark(self):
        log = self.fake(['Click the settings gear icon.', 'The task is complete.'], mark=4)
        self.run_task('Open settings')
        self.assertEqual((self.app.desktop.lab_actions[0]['x'], self.app.desktop.lab_actions[0]['y']), (1220, 35))
        self.assertEqual(len(log['mark']), 1); self.assertEqual(log['locate'], [])


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.screen = [item(1, 'Mousepad', (300, 760, 360, 790), 'button'), item(2, 'Save', (900, 40, 950, 60), 'button')]
        anchor_at = vision.anchor_at  # the fake eyes read the whole screen, also for the piece kept around a click
        for p in (patch.object(vision, 'available', return_value=True), patch.object(vision, 'elements', side_effect=lambda im: self.screen),
                  patch.object(vision, 'anchor_near', side_effect=lambda crop, x0, y0, x, y: anchor_at(self.screen, x, y))):
            p.start(); self.addCleanup(p.stop)
        self.calls = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def claude(self, replies):
        def transport(url, payload, headers, timeout):
            self.calls.append('\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text'))
            return {'usage': {'input_tokens': 900, 'output_tokens': 40}, 'content': [{'type': 'text', 'text': json.dumps(replies[min(len(self.calls), len(replies)) - 1])}]}
        self.app.client.transport = transport

    def run_task(self, task):
        self.app.start_agent(task, 'anthropic_direct')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 15)

    def test_finished_task_is_saved_then_replayed_without_model_steps(self):
        self.claude([{'actions': [{'kind': 'click', 'x': 330, 'y': 775, 'label': 'Mousepad'}]},
                     {'actions': [{'kind': 'type', 'text': 'milk', 'label': 'type'}, {'kind': 'key', 'key': 'ctrl+s'}]},
                     {'actions': [{'kind': 'click', 'x': 925, 'y': 50, 'label': 'Save'}]},
                     {'goal_done': True, 'actions': [], 'note': 'Saved.'}])
        self.run_task('Write milk in a note')
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        skill = self.app.skills.match('write milk in a note!')
        self.assertEqual([s.get('anchor') for s in skill['steps'] if s['kind'] == 'click'], ['Mousepad', 'Save'])
        # Second time: the Save button has moved. The replay finds it by its text; the model is asked only to confirm.
        self.screen[1] = item(2, 'Save', (1000, 100, 1050, 120), 'button')
        self.calls.clear(); self.app.desktop.lab_actions.clear()
        self.claude([{'goal_done': True, 'actions': [], 'note': 'Saved again.'}])
        self.run_task('Write milk in a note')
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('saved steps for this task were just replayed', self.calls[0])
        self.assertEqual([a['kind'] for a in self.app.desktop.lab_actions], ['click', 'type', 'key', 'click'])
        self.assertEqual((self.app.desktop.lab_actions[-1]['x'], self.app.desktop.lab_actions[-1]['y']), (1025, 110))
        self.assertEqual(self.app.skills.list()[0]['uses'], 1)

    def test_replay_stops_when_a_button_is_gone_and_the_model_continues(self):
        self.app.skills.save('Open it', [{'kind': 'click', 'x': 1, 'y': 1, 'label': 'x', 'anchor': 'Mousepad'},
                                         {'kind': 'click', 'x': 1, 'y': 1, 'label': 'y', 'anchor': 'Vanished'}])
        self.claude([{'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.app.runner.replay_wait = 0.3
        self.run_task('Open it')
        self.assertIn('could not find', self.calls[0]); self.assertIn('Vanished', self.calls[0])
        self.assertEqual(len(self.app.desktop.lab_actions), 1)

    def test_tasks_that_needed_the_user_are_not_saved(self):
        self.claude([{'actions': [{'kind': 'click', 'x': 330, 'y': 775, 'label': 'Mousepad'}]},
                     {'actions': [{'kind': 'ask_user', 'reason': 'Solve the CAPTCHA'}]},
                     {'goal_done': True, 'actions': [], 'note': 'done'}])
        self.app.start_agent('Sign up', 'anthropic_direct')
        await_done(lambda: self.app.agent['status'] == 'waiting', 5); self.app.runner.reply('continue')
        await_done(lambda: self.app.agent['status'] == 'completed' and not self.app.runner.thread.is_alive(), 10)
        self.assertIsNone(self.app.skills.match('Sign up'))

    def test_reset_stops_and_cleans_the_desktop(self):
        self.app.desktop.lab_step = 5
        self.claude([{'actions': [{'kind': 'ask_user', 'reason': 'Solve the CAPTCHA'}]}])
        self.app.start_agent('Anything', 'anthropic_direct')
        await_done(lambda: self.app.agent['status'] == 'waiting', 5)
        self.app.reset()
        self.assertEqual(self.app.agent['status'], 'idle')
        self.assertEqual(self.app.desktop.lab_step, 0)
        self.assertIn('Desktop reset', self.app.chat.list()[-1]['text'])


if __name__ == '__main__': unittest.main()
