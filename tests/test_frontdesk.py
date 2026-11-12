"""The chat is not only for handing work to the desktop: a remark on the last result is answered or reworked in the
chat, a question about running work is answered without disturbing it, "more of these" continues the last task with
its notebook, and only the rest is a new task."""
import json
import tempfile
import unittest

from app import frontdesk
from app.server import Application
from test_system import await_done

JOT = {'actions': [{'kind': 'jot', 'text': 'KEPT: @ana.sample, 12k followers, Nike and Vogue'}]}
DONE = {'goal_done': True, 'actions': [], 'note': '1. Ana: instagram.com/ana.sample (12k) - Nike, Vogue - bold color'}


class ParseTests(unittest.TestCase):
    def test_what_the_model_may_answer(self):
        self.assertEqual(frontdesk.parse({'route': 'reply', 'reply': ' Shorter. '}, False), ('reply', 'Shorter.'))
        self.assertIsNone(frontdesk.parse({'route': 'reply', 'reply': ''}, False))           # an empty answer is no answer
        self.assertIsNone(frontdesk.parse({'goal_done': True}, False)); self.assertIsNone(frontdesk.parse('reply', False))
        self.assertEqual(frontdesk.parse({'route': 'task'}, True), ('instruct', ''))         # while it works, anything but a reply goes to the task
        self.assertEqual(frontdesk.parse({'route': 'instruct'}, False), ('task', ''))        # nothing runs: there is nothing to instruct

    def test_when_the_model_is_asked_at_all(self):
        self.assertFalse(frontdesk.worth_asking('Find tomorrow\'s weather', False, has_past=False))   # the first message is a task
        self.assertTrue(frontdesk.worth_asking('make it shorter', False, has_past=True))
        self.assertFalse(frontdesk.worth_asking('RULES\n' + 'x' * 900, False, has_past=True))           # a long brief is a task
        self.assertTrue(frontdesk.worth_asking('what have you found so far?', True, True))
        self.assertTrue(frontdesk.worth_asking('how is it going', True, True))
        self.assertFalse(frontdesk.worth_asking('no, only via the instagram search', True, True))       # an instruction goes straight in
        self.assertFalse(frontdesk.worth_asking('skip the wedding photographers', True, True))


class FrontDeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'plan_contract': False, 'planner': 'fast'})
        self.steps = []; self.step_prompts = []; self.desk = None; self.desk_prompts = []
        def reply(body): return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if text.startswith('You are the front desk'): self.desk_prompts.append(text); return reply(self.desk)
            if text.startswith('You supervise'): return reply({'progress': 'yes', 'reason': 'ok', 'advice': '', 'plan': [], 'ask_user': False})
            self.step_prompts.append(text)
            return reply(self.steps.pop(0) if self.steps else DONE)
        self.app.client.transport = transport
    def tearDown(self): self.app.stop_all(); self.app.close(); self.tmp.cleanup()

    def idle(self): return self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive()
    def first_task(self):
        self.steps = [JOT]
        self.app.chat_message('Find a creative Miami photographer under 100k')
        await_done(lambda: self.app.agent.get('status') == 'completed' and self.idle(), 10)
        self.assertEqual(self.desk_prompts, [])            # the first message is a task; nobody is asked
        return self.app.agent['run_id']
    def texts(self): return [(m['role'], m['text']) for m in self.app.chat.list()]

    def test_reworking_the_result_happens_in_the_chat_and_the_desktop_is_not_touched(self):
        run = self.first_task(); actions = len(self.app.desktop.lab_actions); steps = len(self.step_prompts)
        self.desk = {'route': 'reply', 'reply': 'Ana (@ana.sample): Nike, Vogue.'}
        r = self.app.chat_message('make it one line')
        self.assertEqual(r['taken_as'], 'reply'); self.assertEqual(self.app.tasks, [])
        self.assertEqual(self.app.agent['run_id'], run); self.assertEqual(len(self.app.desktop.lab_actions), actions); self.assertEqual(len(self.step_prompts), steps)
        self.assertEqual(self.texts()[-2:], [('user', 'make it one line'), ('agent', 'Ana (@ana.sample): Nike, Vogue.')])
        self.assertEqual(self.app.chat.list()[-1]['answered'], 'make it one line')
        asked = self.desk_prompts[0]                        # it was given what it needs to rework: the result and the notebook
        self.assertIn('NO TASK IS RUNNING', asked); self.assertIn('instagram.com/ana.sample (12k) - Nike, Vogue', asked)
        self.assertIn('KEPT: @ana.sample, 12k followers', asked); self.assertIn("THE USER'S NEW MESSAGE:\nmake it one line", asked)

    def test_a_reworked_result_can_be_reworked_again(self):
        self.first_task()
        self.desk = {'route': 'reply', 'reply': 'Ana (@ana.sample): Nike, Vogue.'}; self.app.chat_message('make it one line')
        self.desk = {'route': 'reply', 'reply': 'אנה: נייקי, ווג.'}; self.app.chat_message('now in Hebrew')
        self.assertIn('You: Ana (@ana.sample): Nike, Vogue.', self.desk_prompts[1])

    def test_more_of_the_same_continues_the_last_task_with_its_notebook(self):
        run = self.first_task()
        self.desk = {'route': 'continue', 'reply': ''}
        r = self.app.chat_message('find two more like her')
        self.assertNotIn('taken_as', r)
        await_done(lambda: self.app.agent.get('run_id') != run and self.app.agent.get('status') == 'completed' and self.idle(), 10)
        prompt = self.step_prompts[-1]
        self.assertIn('find two more like her', prompt); self.assertIn('This continues your previous task', prompt)
        self.assertIn('Find a creative Miami photographer under 100k', prompt)        # what it builds on
        self.assertIn('- KEPT: @ana.sample, 12k followers, Nike and Vogue', prompt)   # the notebook came along: she is not looked up again
        self.assertTrue(self.app.agent['task'].startswith('find two more like her'))
        self.assertIn(('user', 'find two more like her'), self.texts())               # the chat shows what the user wrote, not the long form

    def test_something_unrelated_is_a_new_task_from_nothing(self):
        run = self.first_task()
        self.desk = {'route': 'task', 'reply': ''}
        self.app.chat_message('What is the weather in Tel Aviv tomorrow?')
        await_done(lambda: self.app.agent.get('run_id') != run and self.app.agent.get('status') == 'completed' and self.idle(), 10)
        self.assertNotIn('This continues your previous task', self.step_prompts[-1]); self.assertNotIn('- KEPT: @ana.sample', self.step_prompts[-1])

    def test_an_answer_that_cannot_be_used_means_a_new_task_as_before(self):
        run = self.first_task()
        self.desk = {'nonsense': True}
        self.assertNotIn('taken_as', self.app.chat_message('make it shorter'))
        await_done(lambda: self.app.agent.get('run_id') != run and self.idle(), 10)

    def test_a_question_while_it_works_is_answered_and_the_task_is_not_disturbed(self):
        self.steps = [JOT, {'actions': [{'kind': 'wait', 'seconds': 2.5, 'label': 'reading'}]}]
        self.app.chat_message('Find a creative Miami photographer under 100k')
        await_done(lambda: self.app.agent['status'] == 'running' and len(self.step_prompts) >= 2, 10)
        run = self.app.agent['run_id']
        self.desk = {'route': 'reply', 'reply': 'One so far: @ana.sample.'}
        r = self.app.chat_message('what have you found so far?')
        self.assertEqual(r['taken_as'], 'reply'); self.assertEqual(self.app.runner.inbox, [])   # not handed to the task as an instruction
        self.assertIn('A TASK IS RUNNING NOW', self.desk_prompts[0]); self.assertIn('KEPT: @ana.sample', self.desk_prompts[0])
        self.assertEqual(self.texts()[-1], ('agent', 'One so far: @ana.sample.'))
        r = self.app.chat_message('skip anyone who shoots weddings')                             # a plain instruction: nobody is asked
        self.assertEqual(r['taken_as'], 'instruction'); self.assertEqual(len(self.desk_prompts), 1)
        await_done(lambda: self.app.agent.get('status') == 'completed' and self.idle(), 10)
        self.assertEqual(self.app.agent['run_id'], run)
        self.assertFalse(any('what have you found so far' in p for p in self.step_prompts))
        self.assertTrue(any('skip anyone who shoots weddings' in p for p in self.step_prompts))

    def test_a_question_that_is_really_an_instruction_goes_to_the_task(self):
        self.steps = [{'actions': [{'kind': 'wait', 'seconds': 2.5, 'label': 'reading'}]}]
        self.app.chat_message('Find a creative Miami photographer under 100k')
        await_done(lambda: self.app.agent['status'] == 'running' and self.step_prompts, 10)
        self.desk = {'route': 'instruct', 'reply': ''}
        self.assertEqual(self.app.chat_message('can you also check their websites?')['taken_as'], 'instruction')
        await_done(lambda: self.app.agent.get('status') == 'completed' and self.idle(), 10)
        self.assertTrue(any('can you also check their websites?' in p for p in self.step_prompts))

    def test_in_private_mode_nothing_is_sent_anywhere_to_decide(self):
        self.first_task()
        self.assertIsNone(self.app.front_desk(self.app.main, 'make it shorter', 'private', False)); self.assertEqual(self.desk_prompts, [])

    def test_a_queued_task_and_a_long_brief_are_always_tasks(self):
        run = self.first_task()
        self.desk = {'route': 'reply', 'reply': 'should never be used'}
        self.app.chat_message('make it shorter', queue=True)
        await_done(lambda: self.app.agent.get('run_id') != run and self.idle(), 10)
        run = self.app.agent['run_id']
        self.app.chat_message('RULES\n- ' + 'Keep working. ' * 80)
        await_done(lambda: self.app.agent.get('run_id') != run and self.idle(), 10)
        self.assertEqual(self.desk_prompts, [])


if __name__ == '__main__': unittest.main()
