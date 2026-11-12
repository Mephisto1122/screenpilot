import email.message
import json
import os
import tempfile
import threading
import time
import unittest

from app.server import Application
from test_system import await_done

SECRET = 'S3cret-Pa55-do-not-leak'


class FakeIMAP:
    """Minimal IMAP4_SSL stand-in holding a few messages."""
    messages = []
    def __init__(self, host, port, timeout=None): self.host = host
    def login(self, user, password):
        if password != 'app-pass': raise Exception('auth failed')
    def select(self, box, readonly=False): return 'OK', [b'1']
    def search(self, charset, *criteria): return 'OK', [' '.join(str(i + 1) for i in range(len(self.messages))).encode()]
    def fetch(self, mid, what): return 'OK', [(b'1', self.messages[int(mid) - 1].as_bytes())]
    def logout(self): pass


def mail(sender, subject, body, html=False):
    m = email.message.EmailMessage(); m['From'] = sender; m['Subject'] = subject; m['Date'] = 'Mon, 9 Nov 2026 18:00:00 +0200'
    m.set_content(body, subtype='html' if html else 'plain'); return m


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.prompts = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def claude(self, replies):
        """Claude-direct transport; replies may be dicts or callables taking the call index."""
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text'); self.prompts.append(text)
            body = replies[min(len(self.prompts), len(replies)) - 1]
            return {'usage': {'input_tokens': 900, 'output_tokens': 40}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        self.app.client.transport = transport

    def run_agent(self, task, wait=True):
        self.app.start_agent(task, 'anthropic_direct')
        if wait: await_done(lambda: self.app.agent['status'] not in ('running', 'waiting'), 10)
        return self.app.agent

    def wait_for_waiting(self):
        await_done(lambda: self.app.agent['status'] == 'waiting', 5)
        return self.app.agent['waiting']

    def test_saved_password_is_typed_but_never_exposed(self):
        self.app.room.save_login({'name': 'github', 'site': 'github.com', 'username': 'agent@example.com', 'password': SECRET})
        self.claude([{'actions': [{'kind': 'click', 'x': 100, 'y': 100, 'label': 'password field'}]},
                     {'actions': [{'kind': 'fill_login', 'name': 'github', 'field': 'password'}]},
                     {'goal_done': True, 'actions': []}])
        self.run_agent('Log in to GitHub', wait=False)
        # Logging in with a saved password asks first (once per task).
        self.assertEqual(self.wait_for_waiting()['reason'], 'Log in with your saved login "github"')
        self.app.runner.reply('approve')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting'), 10)
        agent = self.app.agent
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertEqual(self.app.desktop.lab_text, SECRET)  # it really was typed
        self.assertIn('github (github.com)', self.prompts[0])  # the model knows the login by name
        for where in (json.dumps(self.prompts), json.dumps(self.app.room.public()), json.dumps(self.app.desktop.lab_actions[:-1]),
                      json.dumps(self.app.engine.events), json.dumps(self.app.agent)):
            self.assertNotIn(SECRET, where)
        self.assertEqual(oct(os.stat(self.app.store.root / 'vault.json').st_mode & 0o777), '0o600')

    def test_handoff_waits_for_user_then_continues(self):
        self.claude([{'actions': [{'kind': 'ask_user', 'reason': 'Solve the CAPTCHA'}]},
                     {'actions': [{'kind': 'click', 'x': 50, 'y': 50, 'label': 'next'}]},
                     {'goal_done': True, 'actions': []}])
        self.run_agent('Sign in', wait=False)
        waiting = self.wait_for_waiting()
        self.assertEqual((waiting['type'], waiting['reason']), ('handoff', 'Solve the CAPTCHA'))
        time.sleep(0.6); self.assertEqual(self.app.agent['status'], 'waiting')  # really paused
        self.app.runner.reply('continue')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting'), 10)
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        self.assertIn('The user finished: Solve the CAPTCHA', self.prompts[1])

    def test_denied_approval_is_reported_to_the_model(self):
        self.claude([{'actions': [{'kind': 'ask_approval', 'reason': 'Submit the signup form'}]},
                     {'goal_done': True, 'actions': [], 'note': 'Not submitted, as you asked.'}])
        self.run_agent('Sign up', wait=False)
        self.assertEqual(self.wait_for_waiting()['type'], 'approval')
        self.app.runner.reply('deny')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting'), 10)
        self.assertIn('The user DENIED, do not do it: Submit the signup form', self.prompts[1])
        self.assertEqual(self.app.desktop.lab_step, 0)

    def test_irreversible_click_needs_approval_even_if_model_does_not_ask(self):
        self.claude([{'actions': [{'kind': 'click', 'x': 200, 'y': 300, 'label': 'Create account button'}]},
                     {'goal_done': True, 'actions': []}])
        self.run_agent('Create the account', wait=False)
        w = self.wait_for_waiting(); self.assertEqual((w['type'], w['reason']), ('approval', 'Create account button'))
        self.assertEqual(self.app.desktop.lab_step, 0)  # not clicked yet
        self.app.runner.reply('approve')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting'), 10)
        self.assertEqual(self.app.desktop.lab_step, 1)

    def test_approval_guard_can_be_turned_off(self):
        self.app.room.update({'require_approval': False})
        self.claude([{'actions': [{'kind': 'click', 'x': 200, 'y': 300, 'label': 'Submit'}]}, {'goal_done': True, 'actions': []}])
        self.assertEqual(self.run_agent('Submit it')['status'], 'completed')

    def test_stop_while_waiting(self):
        self.claude([{'actions': [{'kind': 'ask_user', 'reason': 'Enter the SMS code'}]}])
        self.run_agent('Verify', wait=False); self.wait_for_waiting()
        self.app.agent_cancel.set()
        await_done(lambda: self.app.agent['status'] == 'stopped', 5)

    def test_check_email_reads_codes_and_links(self):
        FakeIMAP.messages = [mail('News <news@site.com>', 'Weekly digest', 'nothing'),
                             mail('GitHub <noreply@github.com>', 'Your GitHub launch code',
                                  '<p>Your code is <b>482913</b>. <a href="https://github.com/verify?t=abc">Verify</a></p>', html=True)]
        self.app.room.imap = FakeIMAP
        self.app.room.update({'mail_address': 'agent@gmail.com', 'mail_password': 'app-pass'})
        self.claude([{'actions': [{'kind': 'check_email', 'query': 'github'}]}, {'goal_done': True, 'actions': []}])
        self.assertEqual(self.run_agent('Get the GitHub code')['status'], 'completed')
        self.assertIn('482913', self.prompts[1]); self.assertIn('https://github.com/verify?t=abc', self.prompts[1])
        self.assertNotIn('Weekly digest', self.prompts[1])
        self.assertIn('Your email address (the agent mailbox): agent@gmail.com', self.prompts[0])
        self.assertNotIn('app-pass', json.dumps(self.prompts) + json.dumps(self.app.room.public()))

    def test_profile_reaches_the_model(self):
        self.app.room.update({'profile': 'Name: David Example'})
        self.claude([{'actions': [{'kind': 'click', 'x': 5, 'y': 5, 'label': 'field'}]}, {'goal_done': True, 'actions': []}])
        self.run_agent('Fill in my name')
        self.assertIn('Name: David Example', self.prompts[0])
        self.assertIn('Never invent names', self.prompts[0])

    def test_reply_when_not_waiting_is_ignored(self):
        self.assertTrue(self.app.runner.reply('continue')['stale'])


if __name__ == '__main__': unittest.main()
