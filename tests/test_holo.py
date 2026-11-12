"""Holo grounding: when enabled, the local 'This Mac' mode asks Holo for the exact click point."""
import json
import tempfile
import unittest
from unittest.mock import patch

from app import holo, vision
from app.server import Application
from test_system import await_done
from test_eyes_skills import item


class ParsePointTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(holo.parse_point('{"x": 500, "y": 500}'), (640, 400))
        self.assertEqual(holo.parse_point('<think>top right</think> {"x": 1000, "y": 0}'), (1280, 0))
        self.assertEqual(holo.parse_point("point: {'x': 250, 'y': 125}"), (320, 100))
        self.assertIsNone(holo.parse_point('I cannot find it'))
        self.assertIsNone(holo.parse_point('{"x": 2000, "y": 5}'))  # out of the 0-1000 grid


class HoloLocateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'local_model': 'qwen3-vl:8b', 'use_holo': True, 'holo_model': 'holo-test'})
        # An icon with no text, so the locator must point at it (text match can't).
        self.items = [item(1, '', (1200, 20, 1240, 50), 'box')]
        for p in (patch.object(vision, 'available', return_value=True), patch.object(vision, 'elements', return_value=self.items)):
            p.start(); self.addCleanup(p.stop)
        self.models = []

    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_task(self, task):
        self.app.start_agent(task, 'local')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return self.app.agent

    def test_holo_points_at_the_target_in_local_mode(self):
        thoughts = ["Click the settings icon in the top right.", "The task is complete."]
        n = {'i': 0}
        def transport(url, payload, headers, timeout):
            content = payload['messages'][0]['content']; model = payload.get('model', '')
            self.models.append(model)
            if 'format' not in payload:  # the 'describe the step' / Holo raw calls
                if model == 'holo-test':
                    return {'message': {'content': '{"x": 945, "y": 27}'}, 'prompt_eval_count': 10, 'eval_count': 5}
                text = thoughts[min(n['i'], len(thoughts) - 1)]; n['i'] += 1
                return {'message': {'content': text}, 'prompt_eval_count': 10, 'eval_count': 5}
            return {'message': {'content': json.dumps({'mark': 0})}, 'prompt_eval_count': 10, 'eval_count': 5}
        self.app.client.transport = transport
        agent = self.run_task('Open settings')
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertIn('holo-test', self.models)  # Holo was consulted for the point
        click = next(a for a in self.app.desktop.lab_actions if a['kind'] == 'click')
        self.assertEqual((click['x'], click['y']), (1210, 22))  # 945/1000*1280, 27/1000*800 -> Holo's point


    def test_an_empty_local_reply_is_not_fatal(self):
        # The local brain sometimes returns nothing for "describe the step"; that must fall through
        # to the one-action request, not kill the task (the real failure seen on the M3 Max).
        describes = {'i': 0}
        def transport(url, payload, headers, timeout):
            model = payload.get('model', '')
            if 'format' not in payload:  # 'describe the step' (or Holo, unused here)
                if model == 'holo-test': return {'message': {'content': '{"x": 945, "y": 27}'}, 'prompt_eval_count': 10, 'eval_count': 5}
                describes['i'] += 1
                return {'message': {'content': ''}, 'prompt_eval_count': 10, 'eval_count': 5}  # empty, every time
            return {'message': {'content': json.dumps({'goal_done': True, 'actions': [], 'note': 'Done.'})},
                    'prompt_eval_count': 10, 'eval_count': 5}
        self.app.client.transport = transport
        self.app.start_agent('Open settings', 'local')
        await_done(lambda: self.app.agent['status'] != 'running', 10)
        agent = self.app.agent
        self.assertTrue(describes['i'] >= 1)  # the describe really did come back empty
        # The regression: an empty local reply used to crash the task with "kept replying without valid JSON".
        self.assertNotEqual(agent['status'], 'failed', agent)
        self.assertNotIn('without valid JSON', agent.get('message', ''))


if __name__ == '__main__': unittest.main()


class ClaudeHoloTests(unittest.TestCase):
    """Claude, text only + Holo: Claude decides from the screen description (no pixels leave), Holo clicks."""
    def setUp(self):
        from test_text_mode import item as titem
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'mac_eyes': False, 'use_holo': True, 'holo_model': 'holo-test'})
        self.items = [titem(1, 'Your destination', (60, 100, 220, 120)), titem(2, '', (60, 130, 420, 165), 'box')]
        for p in (patch.object(vision, 'available', return_value=True), patch.object(vision, 'elements', return_value=self.items)):
            p.start(); self.addCleanup(p.stop)
        self.saw_image_at_cloud = []

    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def test_claude_plans_holo_clicks_and_claude_never_sees_the_screenshot(self):
        replies = [{'actions': [{'kind': 'click', 'target': 'the green Book button, bottom right'}]},
                   {'goal_done': True, 'actions': [], 'note': 'done'}]
        n = {'i': 0}
        def transport(url, payload, headers, timeout):
            if url.endswith('/api/chat'):  # local Holo
                return {'message': {'content': '{"x": 900, "y": 750}'}, 'prompt_eval_count': 10, 'eval_count': 5}
            # cloud Claude: record whether any image block was sent
            content = payload['messages'][0]['content']
            self.saw_image_at_cloud.append(any(c.get('type') in ('image', 'input_image') for c in content))
            body = replies[min(n['i'], len(replies) - 1)]; n['i'] += 1
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        self.app.client.transport = transport
        self.app.start_agent('Book the flight', 'anthropic_text')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        click = next(a for a in self.app.desktop.lab_actions if a['kind'] == 'click')
        self.assertEqual((click['x'], click['y']), (1152, 600))  # Holo's 900,750 on the 0-1000 grid
        self.assertTrue(self.saw_image_at_cloud and not any(self.saw_image_at_cloud))  # Claude never got a screenshot
