import io
import json
import tempfile
import unittest

from app.server import Application
from test_system import await_done


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'planner': 'anthropic_direct'})
        self.prompts = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def claude(self, replies):
        def transport(url, payload, headers, timeout):
            self.prompts.append('\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text'))
            body = replies[min(len(self.prompts), len(replies)) - 1]
            return {'usage': {'input_tokens': 900, 'output_tokens': 40}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        self.app.client.transport = transport

    def idle(self):
        await_done(lambda: not self.app.tasks and self.app.agent['status'] not in ('running', 'waiting')
                   and not (self.app.runner.thread and self.app.runner.thread.is_alive()), 10)

    def kinds(self): return [(m['role'], m['kind']) for m in self.app.chat.list()]

    def test_chat_task_runs_with_plan_memory_and_final_report(self):
        self.claude([{'plan': ['Open the editor', 'Write the note'], 'actions': []},
                     {'actions': [{'kind': 'click', 'x': 10, 'y': 10, 'label': 'Mousepad'}]},
                     {'goal_done': True, 'actions': []},
                     {'actions': [{'kind': 'remember', 'text': 'Notes are saved in Documents/notes.txt'}]},
                     {'actions': [{'kind': 'type', 'text': 'hello', 'label': 'type'}]},
                     {'goal_done': True, 'actions': [], 'note': 'Wrote the note and saved it.'}])
        self.app.submit_task('Write a note that says hello')
        self.idle()
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        msgs = self.app.chat.list()
        self.assertEqual(self.kinds(), [('user', 'text'), ('agent', 'plan'), ('agent', 'result')])
        self.assertEqual(msgs[1]['goals'], ['Open the editor', 'Write the note'])
        self.assertEqual(msgs[2]['text'], 'Wrote the note and saved it.')
        self.assertEqual(self.app.agent['plan'], ['Open the editor', 'Write the note'])
        self.assertEqual(self.app.agent['last_action']['kind'], 'type')
        self.assertEqual([i['text'] for i in self.app.memory.list()], ['Notes are saved in Documents/notes.txt'])
        self.assertIn('Current subgoal: Write the note', self.prompts[-1])

    def test_memory_and_conversation_reach_the_next_task(self):
        self.app.memory.add('The user prefers DuckDuckGo', 'user')
        self.claude([{'actions': [{'kind': 'say', 'text': 'Starting now'}]},
                     {'actions': [{'kind': 'click', 'x': 1, 'y': 1, 'label': 'x'}]},
                     {'goal_done': True, 'actions': [], 'note': 'Done searching.'}])
        self.app.submit_task('Search for train times')
        self.idle()
        self.assertIn('The user prefers DuckDuckGo', self.prompts[0])
        self.assertIn('User: Search for train times', self.prompts[0])
        self.assertIn(('agent', 'text'), self.kinds())
        self.prompts.clear()
        self.app.submit_task('Now the same for Haifa')
        self.idle()
        self.assertIn('You: Done searching.', self.prompts[0])

    def test_second_task_is_queued_until_the_first_finishes(self):
        self.claude([{'actions': [{'kind': 'click', 'x': 2, 'y': 2, 'label': 'x'}]},
                     {'goal_done': True, 'actions': [], 'note': 'first done'},
                     {'actions': [{'kind': 'click', 'x': 1, 'y': 1, 'label': 'x'}]},
                     {'goal_done': True, 'actions': [], 'note': 'second done'}])
        fast = self.app.client.transport
        def slow(*args):
            import time; time.sleep(0.2); return fast(*args)
        self.app.client.transport = slow
        self.app.submit_task('first')
        await_done(lambda: self.app.agent['status'] == 'running', 5)
        self.assertTrue(self.app.submit_task('second')['queued'])
        self.idle()
        texts = [m['text'] for m in self.app.chat.list()]
        self.assertIn('first done', texts); self.assertIn('second done', texts)
        self.assertLess(texts.index('first done'), texts.index('second done'))

    def test_errors_are_reported_in_chat(self):
        self.app.settings.update({'planner': 'openai_direct'})  # no OpenAI key configured
        self.app.submit_task('do something')
        await_done(lambda: any(m['kind'] == 'error' for m in self.app.chat.list()), 5)

    def test_stop_all_clears_the_queue(self):
        self.app.tasks.append({'text': 'x', 'planner': None, 'message': 'm'})
        self.app.stop_all()
        self.assertEqual(self.app.tasks, [])


class FakeTelegram:
    """Records Bot API calls and answers them like Telegram would."""
    def __init__(self): self.calls = []
    def __call__(self, req, timeout=None):
        method = req.full_url.rsplit('/', 1)[1]
        body = json.loads(req.data) if req.headers.get('Content-type') == 'application/json' else {}
        self.calls.append((method, body))
        result = {'getMe': {'username': 'pilot_bot'}, 'getUpdates': []}.get(method, True)
        return io.BytesIO(json.dumps({'ok': True, 'result': result}).encode())


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.fake = FakeTelegram(); self.app.telegram.opener = self.fake
        self.app.telegram.start = lambda: None  # no background polling in tests
        self.tg = self.app.telegram
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def update(self, text, chat=42):
        self.tg.handle({'update_id': 1, 'message': {'chat': {'id': chat}, 'text': text}})

    def sent(self, method='sendMessage'): return [b for m, b in self.fake.calls if m == method]

    def test_pairing_then_tasks_and_strangers_ignored(self):
        status = self.tg.connect('123:ABC')
        self.assertEqual(status['bot'], 'pilot_bot'); code = status['pair_code']
        self.assertNotIn('123:ABC', json.dumps(self.app.room.public()) + json.dumps(status))
        self.update('/start 000000')  # wrong code
        self.assertFalse(self.tg.status()['paired'])
        self.update(f'/start {code}')
        self.assertTrue(self.tg.status()['paired'])
        self.update('book a table', chat=99)  # stranger
        self.assertEqual(self.app.tasks, [])
        self.update('book a table')
        self.assertEqual(self.app.chat.list()[-1]['source'], 'telegram')

    def test_agent_messages_and_buttons_go_to_telegram(self):
        self.tg.connect('123:ABC'); self.update(f"/start {self.tg.pair_code}")
        self.fake.calls.clear()
        self.tg._deliver('Solve the CAPTCHA', [[{'text': 'ok', 'callback_data': 'reply:continue'}]], 'ask')
        self.assertEqual(self.sent()[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'reply:continue')

    def test_button_press_answers_waiting_agent(self):
        self.tg.connect('123:ABC'); self.update(f"/start {self.tg.pair_code}")
        replies = []
        self.app.runner.reply = lambda a, ask=None: replies.append(a) or {'ok': True}
        self.tg.handle({'update_id': 2, 'callback_query': {'id': 'q', 'data': 'reply:approve', 'message': {'chat': {'id': 42}}}})
        self.assertEqual(replies, ['approve'])
        self.tg.handle({'update_id': 3, 'callback_query': {'id': 'q', 'data': 'reply:approve', 'message': {'chat': {'id': 7}}}})
        self.assertEqual(replies, ['approve'])  # other chats cannot press buttons

    def test_disconnect_forgets_token(self):
        self.tg.connect('123:ABC'); self.tg.disconnect()
        self.assertFalse(self.tg.status()['connected'])
        self.assertNotIn('telegram_token', self.app.room.vault)


if __name__ == '__main__': unittest.main()


class ReplyParsingTests(unittest.TestCase):
    def test_parse_variants(self):
        from app.models import parse_object, BadReply
        self.assertEqual(parse_object('{"actions":[]}'), {'actions': []})
        self.assertEqual(parse_object('```json\n{"a":1}\n```'), {'a': 1})
        self.assertEqual(parse_object('Sure! {"a":1} hope that helps'), {'a': 1})
        # the reported failure: "Extra data" from two objects in a row
        two = '{"kind":"click","x":5,"y":6}{"kind":"type","text":"hi"}'
        self.assertEqual(parse_object(two)['actions'], [{'kind': 'click', 'x': 5, 'y': 6}, {'kind': 'type', 'text': 'hi'}])
        merged = parse_object('{"actions":[{"kind":"click","x":1,"y":1}],"note":"a"}\n{"actions":[{"kind":"key","key":"Return"}]}')
        self.assertEqual([a['kind'] for a in merged['actions']], ['click', 'key']); self.assertEqual(merged['note'], 'a')
        self.assertEqual(parse_object('{"note":"brace } inside", "actions":[]}')['note'], 'brace } inside')
        with self.assertRaises(BadReply): parse_object('I cannot see the screen.')


class BadReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'ask_when_stuck': True})
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_with(self, texts):
        calls = []
        def transport(url, payload, headers, timeout):
            calls.append('\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text'))
            return {'usage': {'input_tokens': 900, 'output_tokens': 40}, 'content': [{'type': 'text', 'text': texts[min(len(calls), len(texts)) - 1]}]}
        self.app.client.transport = transport
        self.app.start_agent('Click twice', 'anthropic_direct')
        await_done(lambda: self.app.agent['status'] != 'running', 10)
        return calls

    def test_two_objects_in_one_reply_are_both_executed(self):
        self.run_with(['{"kind":"click","x":5,"y":6,"label":"a"}{"kind":"click","x":50,"y":60,"label":"b"}', '{"goal_done":true,"actions":[]}'])
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        self.assertEqual(self.app.agent['steps'], 2)

    def test_non_json_reply_is_retried_not_fatal(self):
        calls = self.run_with(['Let me look at the screen first.', '{"actions":[{"kind":"click","x":5,"y":6}]}', '{"goal_done":true,"actions":[]}'])
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        self.assertIn('not valid JSON', calls[1])

    def test_asks_the_user_after_three_bad_replies(self):
        self.run_with(['nope'])
        self.assertEqual(self.app.agent['status'], 'waiting')  # not failed: the task stays alive and asks
        self.assertEqual(self.app.agent['waiting']['type'], 'stuck')
        self.assertIn('kept replying without valid JSON', self.app.agent['waiting']['reason'])

    def test_too_many_actions_runs_the_first_three(self):
        many = json.dumps({'actions': [{'kind': 'click', 'x': i, 'y': i} for i in range(1, 6)]})
        self.run_with([many, '{"goal_done":true,"actions":[]}'])
        self.assertEqual(self.app.agent['steps'], 3)
