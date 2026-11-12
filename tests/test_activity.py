"""The activity log: durations, what is in progress, hangs, the file, and nothing secret in it."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import activity
from app.activity import Activity
from app.core import Desktop, Engine, validate_action
from app.models import Ledger, ModelClient, Settings


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.log = Activity()
        self.log.path = Path(self.tmp.name) / 'screenpilot.log'

    def tearDown(self): self.tmp.cleanup()

    def test_spans_have_durations_and_show_while_running(self):
        self.log.bind(2)
        with self.log.span('model', 'Claude · execute') as info:
            self.assertEqual([n['msg'] for n in self.log.now()], ['Claude · execute'])
            time.sleep(0.05); info['tokens'] = '100→20'
        e = self.log.entries()[-1]
        self.assertEqual((e['w'], e['kind'], e['tokens']), (2, 'model', '100→20')); self.assertGreaterEqual(e['ms'], 50)
        self.assertEqual(self.log.now(), [])
        with self.assertRaises(ValueError):
            with self.log.span('action', 'click'): raise ValueError('boom')
        e = self.log.entries()[-1]
        self.assertEqual(e['level'], 'error'); self.assertIn('failed: ValueError: boom', e['msg']); self.assertIn('Traceback', e['trace'])
        from app.core import ActionFailed
        with self.assertRaises(ActionFailed):
            with self.log.span('action', 'switch to window "Tor"'): raise ActionFailed('No open window matches "Tor"')
        e = self.log.entries()[-1]
        self.assertEqual(e['level'], 'warning'); self.assertNotIn('trace', e)  # expected: no traceback

    def test_a_hang_is_reported_while_it_lasts(self):
        with patch.object(activity, 'WARN_AFTER', (0.05, 0.2)):
            with self.log.span('model', 'Ollama · see'):
                time.sleep(0.1); self.log.check(); self.log.check()  # one line per threshold
                time.sleep(0.15); self.log.check()
            with self.log.span('wait', 'Waiting for you', quiet=True):
                time.sleep(0.1); self.log.check()
        hangs = [e['msg'] for e in self.log.entries() if e['kind'] == 'hang']
        self.assertEqual(len(hangs), 2); self.assertTrue(hangs[0].startswith('Still waiting after')); self.assertIn('Ollama · see', hangs[0])

    def test_file_rotates_and_downloads_as_text(self):
        with patch.object(activity, 'ROTATE_BYTES', 300):
            for i in range(30): self.log.write('agent', f'event {i}', worker=1)
        self.assertTrue(self.log.path.with_name('screenpilot.log.1').exists())
        text = self.log.download()
        self.assertIn('W1 INFO    agent: event 29', text)
        self.assertLess(text.index('event 25'), text.index('event 29'))  # oldest first
        json.loads(self.log.path.read_text().splitlines()[-1])  # one JSON object per line

    def test_actions_are_logged_without_what_was_typed(self):
        with patch.object(activity, 'ACTIVITY', self.log), patch('app.core.ACTIVITY', self.log):
            engine = Engine(Desktop(lab=True), None)
            engine.act({'kind': 'type', 'text': 'hunter2', 'secret': True, 'label': 'fill saved password · github'}, 'agent')
            engine.act({'kind': 'click', 'x': 10, 'y': 20, 'label': 'Sign in'}, 'agent')
        text = self.log.download()
        self.assertNotIn('hunter2', text); self.assertIn('type 7 characters (saved login)', text); self.assertIn('click 10,20 "Sign in"', text)

    def test_model_requests_are_logged_with_tokens(self):
        settings = Settings(Path(self.tmp.name)); settings.update({'anthropic_key': 'fixture'})
        def transport(url, payload, headers, timeout):
            return {'usage': {'input_tokens': 900, 'output_tokens': 40}, 'content': [{'type': 'text', 'text': '{"actions":[]}'}]}
        with patch('app.models.ACTIVITY', self.log):
            ModelClient(settings, Ledger(Path(self.tmp.name)), transport).call('anthropic', 'secret prompt text', None, 'r', 'execute')
        e = self.log.entries()[-1]
        self.assertEqual(e['kind'], 'model'); self.assertIn('execute', e['msg']); self.assertEqual(e['tokens'], '900→40')
        self.assertNotIn('secret prompt text', self.log.download())


class LogsEndpointTests(unittest.TestCase):
    def test_logs_and_download(self):
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.request import Request, urlopen
        from app.server import Application, handler_factory
        tmp = tempfile.TemporaryDirectory(); app = Application(tmp.name, lab=True)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(app)); url = f'http://127.0.0.1:{server.server_port}'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            activity.ACTIVITY.write('agent', 'hello from worker 1', worker=1)
            r = json.load(urlopen(Request(url + '/api/w/1/logs'), timeout=5))
            self.assertIn('hello from worker 1', [e['msg'] for e in r['entries']]); self.assertIn('now', r)
            resp = urlopen(Request(url + '/api/logs/download'), timeout=5)
            self.assertIn('attachment', resp.headers['Content-Disposition']); self.assertIn('hello from worker 1', resp.read().decode())
        finally:
            app.close(); server.shutdown(); server.server_close(); tmp.cleanup()


if __name__ == '__main__': unittest.main()


class NotJsonTests(unittest.TestCase):
    def test_what_claude_said_instead_is_logged_and_shown(self):
        from test_system import await_done
        from app.server import Application
        tmp = tempfile.TemporaryDirectory(); app = Application(tmp.name, lab=True)
        try:
            app.settings.update({'anthropic_key': 'fixture', 'mac_eyes': False, 'ask_when_stuck': True})  # the setting where it asks instead of working it out alone
            calls = []
            def transport(url, payload, headers, timeout):
                calls.append(payload)
                return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': 'I would rather not do that, it breaks the site rules.'}]}
            app.client.transport = transport
            app.start_agent('Do the thing', 'anthropic_text')
            await_done(lambda: app.agent['status'] == 'waiting', 10)  # it does not give up: it asks the user
            self.assertEqual(app.agent['waiting']['type'], 'stuck')
            self.assertIn('Its last answer: "I would rather not do that', app.agent['waiting']['reason'])
            self.assertTrue(any('answered without the JSON' in e['msg'] and 'rather not' in e['msg'] for e in activity.ACTIVITY.entries()))
            self.assertIn('\\"stuck\\":true', calls[1]['messages'][0]['content'][-1]['text'])  # told how to say it cannot
        finally:
            app.close(); tmp.cleanup()
