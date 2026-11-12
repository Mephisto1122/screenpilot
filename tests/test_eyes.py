"""Claude, text only, with this computer's eyes: the local vision model describes the screen, names icons,
answers questions and finds things by description. Claude never gets the screenshot."""
import json
import unittest

from app import vision
from app.eyes import Eyes, unnamed
from test_text_mode import TextModeBase, item


class EyesAgentTests(TextModeBase):
    def setUp(self):
        super().setUp()
        self.app.settings.update({'mac_eyes': True})
        self.items[:] = [item(1, 'Your destination', (60, 100, 220, 120)), item(2, '', (60, 130, 420, 165), 'box'),
                         item(3, 'Search routes', (60, 200, 230, 240), 'button'), item(4, '', (1200, 20, 1236, 50), 'box')]
        self.local = []  # what the local model was asked
        self.local_answers = {'see': {'screen': 'A train route planner. A cookie banner covers the bottom.', 'icons': [{'mark': 4, 'name': 'shopping cart'}]},
                              'look': 'The Remember me box is ticked.', 'locate': {'x': 500, 'y': 500}}

    def claude(self, replies):
        super().claude(replies)
        cloud = self.app.client.transport
        def transport(url, payload, headers, timeout):
            if not url.endswith('/api/chat'): return cloud(url, payload, headers, timeout)
            content = payload['messages'][0]['content']
            purpose = 'see' if content.startswith('This is a computer screen') else 'look' if content.startswith('Look at this') else 'locate'
            self.local.append((purpose, content, payload['messages'][0]['images']))
            answer = self.local_answers[purpose]
            return {'message': {'content': answer if isinstance(answer, str) else json.dumps(answer)}, 'prompt_eval_count': 10, 'eval_count': 5, 'done': True}
        self.app.client.transport = transport

    def test_claude_gets_what_this_computer_sees_but_never_the_screenshot(self):
        self.claude([{'actions': [{'kind': 'click', 'mark': 4}]}, {'actions': [{'kind': 'scroll', 'amount': 2}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(self.run_task('Open the cart')['status'], 'completed')
        first = self.prompt(0)
        self.assertIn('Seen on this computer: A train route planner. A cookie banner covers the bottom.', first)
        self.assertIn('[4] box "" (1218,35) 36x30 icon: shopping cart', first)
        self.assertIn('"kind":"look"', first)
        for p in self.payloads: self.assertEqual({c['type'] for c in p['messages'][0]['content']}, {'text'})  # still no image to Claude
        self.assertEqual([p for p, _, _ in self.local], ['see'])  # the same screen is described once
        self.assertIn('Numbered icons: 4', self.local[0][1])

    def test_look_and_click_by_description(self):
        self.claude([{'actions': [{'kind': 'look', 'question': 'Is the Remember me box ticked?'}]},
                     {'actions': [{'kind': 'click', 'target': 'the green arrow next to the price'}]},
                     {'actions': [{'kind': 'type', 'target': 'Your destination', 'text': 'Haifa'}]},
                     {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(self.run_task('Book it')['status'], 'completed')
        self.assertIn('This computer sees: The Remember me box is ticked.', self.prompt(1))
        acts = self.app.desktop.lab_actions
        self.assertEqual((acts[0]['kind'], acts[0]['x'], acts[0]['y']), ('click', 640, 400))  # the model pointed (0-1000 grid)
        self.assertEqual((acts[1]['x'], acts[1]['y'], acts[2]['text']), (240, 147, 'Haifa'))  # found by its text: no model call
        self.assertEqual([p for p, _, _ in self.local].count('locate'), 1)

    def test_something_not_on_screen_is_a_hint(self):
        self.local_answers['locate'] = {'x': 0, 'y': 0}
        self.claude([{'actions': [{'kind': 'click', 'target': 'the unicorn'}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(self.run_task('Find the unicorn')['status'], 'completed')
        self.assertIn('Could not find \\"the unicorn\\" on the screen', self.prompt(1))

    def test_without_ollama_it_goes_on_with_the_text(self):
        self.claude([{'goal_done': True, 'actions': [], 'note': 'ok'}])
        cloud = self.app.client.transport
        def transport(url, payload, headers, timeout):
            if url.endswith('/api/chat'): raise RuntimeError('Cannot connect to Ollama')
            return cloud(url, payload, headers, timeout)
        self.app.client.transport = transport
        self.assertEqual(self.run_task('Read it')['status'], 'completed')
        self.assertNotIn('Seen on this computer:', self.prompt(0))

    def test_a_slow_mac_never_holds_claude_up(self):
        import time
        from unittest.mock import patch
        self.claude([{'actions': [{'kind': 'click', 'x': 10 * n, 'y': 10}]} for n in range(1, 5)] + [{'goal_done': True, 'actions': [], 'note': 'ok'}])
        fast = self.app.client.transport
        def slow(url, payload, headers, timeout):
            if url.endswith('/api/chat'): time.sleep(0.5)
            return fast(url, payload, headers, timeout)
        self.app.client.transport = slow
        with patch.object(Eyes, 'BUDGET', 0.05):
            start = time.monotonic(); self.assertEqual(self.run_task('Wait a bit')['status'], 'completed')
        self.assertNotIn('Seen on this computer:', self.prompt(0))  # not ready yet: Claude went on with the text
        self.assertIn('Seen on this computer:', self.prompt(4))     # ready later, on the same page
        self.assertEqual([p for p, _, _ in self.local], ['see'])   # one look at a time, not one per step

    def test_a_mac_too_slow_to_look_is_left_out_for_the_task(self):
        import time
        from unittest.mock import patch
        self.claude([{'actions': [{'kind': 'click', 'x': 10 * n, 'y': 10}]} for n in range(1, 6)] + [{'goal_done': True, 'actions': [], 'note': 'ok'}])
        fast = self.app.client.transport
        def stuck(url, payload, headers, timeout):
            if url.endswith('/api/chat'): time.sleep(3)
            return fast(url, payload, headers, timeout)
        self.app.client.transport = stuck
        with patch.object(Eyes, 'BUDGET', 0.05), patch.object(Eyes, 'GIVE_UP', 0.5):
            self.assertEqual(self.run_task('Click around')['status'], 'completed')
        self.assertNotIn('"kind":"look"', self.prompt(4))  # given up: Claude is not offered the look any more
        from app.activity import ACTIVITY
        self.assertTrue(any('took too long to look' in e['msg'] for e in ACTIVITY.entries()))


class RenumberTests(unittest.TestCase):
    def test_icon_names_follow_the_icon_when_numbers_change(self):
        class Client:
            calls = 0
            def call(self, *a, **kw):
                Client.calls += 1; return {'screen': 'Shop', 'icons': [{'mark': 2, 'name': 'cart'}]}
        eyes = Eyes(Client(), 'r', {'local_model': 'qwen3-vl:8b'})
        from PIL import Image
        im = Image.new('RGB', (1280, 800))
        first = [item(1, 'Shoes', (10, 10, 80, 30)), item(2, '', (1200, 20, 1236, 50), 'box')]
        self.assertEqual(eyes.see(im, first, 'Shop')['icons'], {2: 'cart'})
        later = [item(1, 'Shoes', (10, 10, 80, 30)), item(2, '', (600, 300, 630, 330), 'box'), item(3, '', (1201, 21, 1237, 51), 'box')]
        self.assertEqual(eyes.see(im, later, 'Shop')['icons'], {3: 'cart'})
        self.assertEqual(Client.calls, 1)  # similar screen: described once
        self.assertEqual(len(unnamed([item(1, '', (0, 0, 900, 700), 'box')])), 0)  # a big panel is not an icon


if __name__ == '__main__': unittest.main()
