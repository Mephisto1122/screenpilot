import json
import tempfile
import unittest
from pathlib import Path

from app.models import Settings, describe_modes, resolve_mode
from app.server import Application
from test_system import await_done

CFG = {'uitars_model': 'ui-tars', 'holo_model': 'holo', 'use_holo': False, 'cloud_provider': 'auto'}


class ResolveTests(unittest.TestCase):
    def test_private_uses_uitars_when_it_is_installed(self):
        self.assertEqual(resolve_mode('private', CFG, {}, lambda m: m == 'ui-tars')['planner'], 'uitars')
        self.assertEqual(resolve_mode('private', CFG, {}, lambda m: False)['planner'], 'local')
        self.assertEqual(resolve_mode('private', CFG, {}, None)['planner'], 'local')  # Ollama not checked yet

    def test_holo_is_used_whenever_it_is_installed(self):
        self.assertTrue(resolve_mode('smart', CFG, {'anthropic': True}, lambda m: m == 'holo')['use_holo'])
        self.assertFalse(resolve_mode('smart', CFG, {'anthropic': True}, lambda m: False)['use_holo'])
        self.assertTrue(resolve_mode('private', {**CFG, 'use_holo': True}, {}, None)['use_holo'])  # switched on by hand

    def test_the_cloud_model_follows_the_key_or_the_users_choice(self):
        self.assertEqual(resolve_mode('smart', CFG, {'anthropic': True, 'openai': True})['planner'], 'anthropic_text')
        self.assertEqual(resolve_mode('smart', CFG, {'openai': True})['planner'], 'openai_text')
        self.assertEqual(resolve_mode('fast', CFG, {'openai': True})['planner'], 'openai_direct')
        self.assertEqual(resolve_mode('fast', {**CFG, 'cloud_provider': 'openai'}, {'anthropic': True, 'openai': True})['planner'], 'openai_direct')

    def test_the_dashboard_is_told_where_screenshots_go_and_what_is_missing(self):
        m = describe_modes(CFG, {'anthropic': True}, lambda m: m in ('ui-tars', 'holo'))
        self.assertFalse(m['private']['leaves']); self.assertFalse(m['smart']['leaves']); self.assertTrue(m['fast']['leaves'])
        self.assertIn('Nothing leaves', m['private']['privacy']); self.assertIn('Holo', m['smart']['privacy'])
        self.assertIn('sent to Anthropic', m['fast']['privacy'])
        none = describe_modes(CFG, {}, lambda m: False)
        self.assertFalse(none['smart']['ready']); self.assertIn('API key', none['smart']['hint']); self.assertIn('UITARS=1', none['private']['hint'])

    def test_old_default_becomes_private_and_an_exact_choice_is_kept(self):
        for old, new in (('local', 'private'), ('anthropic_direct', 'anthropic_direct')):
            with tempfile.TemporaryDirectory() as d:
                Path(d, 'settings.json').write_text(json.dumps({'settings_version': 7, 'planner': old}))
                self.assertEqual(Settings(Path(d)).snapshot()['planner'], new)


class ModeRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.seen = []
        def transport(url, payload, headers, timeout):
            self.seen.append((url, payload))
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps({'goal_done': True, 'actions': [], 'note': 'ok'})}],
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'goal_done': True, 'actions': [], 'note': 'ok'})}]}]}
        self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_mode(self, mode):
        self.app.start_agent('Do it', mode)
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return self.app.agent

    def test_fast_sends_the_screenshot_and_smart_does_not(self):
        self.app.settings.update({'anthropic_key': 'fixture', 'mac_eyes': False})
        agent = self.run_mode('fast')
        self.assertEqual((agent['mode'], agent['planner'], agent['status']), ('fast', 'anthropic_direct', 'completed'))
        self.assertIn('image', [c['type'] for c in self.seen[-1][1]['messages'][0]['content']])
        agent = self.run_mode('smart')
        self.assertEqual((agent['mode'], agent['planner']), ('smart', 'anthropic_text'))
        self.assertNotIn('image', [c['type'] for c in self.seen[-1][1]['messages'][0]['content']])

    def test_a_cloud_mode_without_a_key_says_what_to_do(self):
        with self.assertRaisesRegex(ValueError, 'Add an API key'): self.app.start_agent('Do it', 'smart')

    def test_state_tells_the_dashboard_about_the_modes(self):
        self.assertEqual(set(self.app.main.state()['modes']), {'private', 'smart', 'fast'})


if __name__ == '__main__': unittest.main()
