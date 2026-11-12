import json
import tempfile
import unittest

from app.agent import approval_kind
from app.server import Application
from test_system import await_done


class ApprovalKindTests(unittest.TestCase):
    def test_browsing_never_asks(self):
        for label in ('click the first post', 'open the comments section', 'click the article link', 'click Search',
                      'open cnn.com', 'scroll to the replies', 'click Accept cookies', 'click Download', 'click Next page',
                      'open the Sports tab', 'click the Posts tab', 'click on the send-to-kindle article title'):
            self.assertIsNone(approval_kind(label, click=True), label)
        for reason in ('Open the finance article', 'Go to the next page of results', 'Download the PDF report',
                       'Install Tor Browser', 'Open the file in Downloads'):
            self.assertIsNone(approval_kind(reason), reason)

    def test_login_and_passwords_ask(self):
        for text in ('click Sign in', 'click the Log in button', 'Sign in'):
            self.assertEqual(approval_kind(text, click=True), 'login', text)
        for text in ('About to sign in to Gmail', 'Enter the password', 'Log in with your saved login "gmail"'):
            self.assertEqual(approval_kind(text), 'login', text)
        for text in ('password field', 'click the username box', 'click the Sign in field'):  # clicking into a field is fine
            self.assertIsNone(approval_kind(text, click=True), text)

    def test_publishing_paying_signing_up_deleting_ask(self):
        for label in ('click Post', 'click the Comment button', 'Reply', 'click Send', 'Publish', 'click Submit',
                      'click Buy now', 'click Place order', 'click Pay', 'click Sign up', 'Create an account',
                      'click Delete', 'click Subscribe', 'click the Post button', 'post the comment'):
            self.assertEqual(approval_kind(label, click=True), 'act', label)
        for reason in ('Post the comment on the article', 'Send the email to Dana', 'Buy the ticket',
                       'Create the new Gmail account', 'Delete the old files', 'Reply to the thread'):
            self.assertEqual(approval_kind(reason), 'act', reason)


class ApprovalFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.app.room.update({'require_approval': True})
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_with(self, replies, answer='approve'):
        calls = []
        def transport(url, payload, headers, timeout):
            calls.append(payload)
            r = replies[min(len(calls), len(replies)) - 1]
            return {'usage': {'input_tokens': 100, 'output_tokens': 10}, 'content': [{'type': 'text', 'text': json.dumps(r)}]}
        self.app.client.transport = transport
        asked = []
        self.app.start_agent('task', 'anthropic_direct')
        def done():
            if self.app.agent['status'] == 'waiting':
                asked.append(self.app.agent['waiting']['reason']); self.app.runner.reply(answer)
            return self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive()
        await_done(done, 10)
        return asked

    def test_browsing_runs_without_a_single_question(self):
        asked = self.run_with([{'actions': [{'kind': 'ask_approval', 'reason': 'Open the finance article'}]},
                               {'actions': [{'kind': 'click', 'x': 50, 'y': 60, 'label': 'click the first post'}]},
                               {'actions': [{'kind': 'click', 'x': 50, 'y': 60, 'label': 'click Search'}]},
                               {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(asked, [])
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)

    def test_posting_a_comment_still_asks(self):
        asked = self.run_with([{'actions': [{'kind': 'click', 'x': 50, 'y': 60, 'label': 'click the Comment button'}]},
                               {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(asked, ['click the Comment button'])

    def test_switched_off_never_asks(self):
        self.app.room.update({'require_approval': False})
        asked = self.run_with([{'actions': [{'kind': 'click', 'x': 50, 'y': 60, 'label': 'click Post'}]},
                               {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(asked, [])


if __name__ == '__main__': unittest.main()


class StaleAnswerTests(unittest.TestCase):
    """An answer counts only for the question it was given for."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.app.room.update({'require_approval': True})
        replies = [{'actions': [{'kind': 'ask_approval', 'reason': 'Post the comment'}]},
                   {'actions': [{'kind': 'ask_approval', 'reason': 'Send the email'}]},
                   {'goal_done': True, 'actions': [], 'note': 'ok'}]
        n = {'i': 0}
        def transport(url, payload, headers, timeout):
            n['i'] += 1; r = replies[min(n['i'], len(replies)) - 1]
            return {'usage': {'input_tokens': 100, 'output_tokens': 10}, 'content': [{'type': 'text', 'text': json.dumps(r)}]}
        self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def waiting_for(self, reason):
        await_done(lambda: (self.app.agent.get('waiting') or {}).get('reason') == reason, 10)
        return self.app.agent['waiting']['id']

    def test_a_double_click_cannot_approve_the_next_question(self):
        self.app.start_agent('task', 'anthropic_direct')
        first = self.waiting_for('Post the comment')
        self.assertTrue(self.app.runner.reply('approve', first)['ok'])
        second = self.waiting_for('Send the email')
        self.assertNotEqual(first, second)
        self.assertTrue(self.app.runner.reply('approve', first)['stale'])          # the second click of a double click
        self.assertEqual(self.app.agent['status'], 'waiting')                     # ... left the new question open
        self.assertTrue(self.app.runner.reply('deny', second)['ok'])
        await_done(lambda: not self.app.runner.thread.is_alive(), 10)
        self.assertIsNone(self.app.agent.get('waiting'))

    def test_answer_when_nothing_is_waiting_is_not_an_error(self):
        self.assertTrue(self.app.runner.reply('approve')['stale'])


class RealButtonTests(ApprovalFlowTests):
    """The text really under the pointer counts, not only what the model calls its click."""
    def test_a_vague_label_on_a_post_button_still_asks(self):
        from unittest.mock import patch
        from app import vision
        with patch.object(vision, 'available', return_value=True), patch.object(vision, 'anchor_near', return_value='Post'):
            asked = self.run_with([{'actions': [{'kind': 'click', 'x': 50, 'y': 60, 'label': 'click the blue button'}]},
                                   {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(asked, ['click the blue button (the button says "Post")'])

    def test_an_ordinary_link_under_the_pointer_does_not_ask(self):
        from unittest.mock import patch
        from app import vision
        with patch.object(vision, 'available', return_value=True), patch.object(vision, 'anchor_near', return_value='Read the full article'):
            asked = self.run_with([{'actions': [{'kind': 'click', 'x': 50, 'y': 60, 'label': 'click the blue button'}]},
                                   {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(asked, [])
