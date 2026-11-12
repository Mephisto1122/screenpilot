"""The vision: give it a task and it finishes the work. It asks only for what only the user can do (security),
gets itself out of trouble, and always comes back with results. From a real run on 11 Nov 2026 in which it asked
six times in 25 minutes and delivered nothing."""
import json
import tempfile
import unittest
from unittest.mock import patch

from app import agent as agent_module
from app.server import Application
from test_system import await_done

CLICK = {'actions': [{'kind': 'click', 'x': 400, 'y': 180, 'label': 'open profile'}]}
PLAN = {'deliverable': '5 London photographers with handle and a credited campaign',
        'steps': [{'do': 'Search Instagram for London photographers and note 5', 'done_when': 'the notebook lists 5 photographers'},
                  {'do': 'Report the list', 'done_when': 'the report contains the photographers'}], 'assumptions': ['London; fashion and campaign work'], 'questions': []}


class Base(unittest.TestCase):
    mode = 'fast'
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.plan = PLAN; self.replan = None; self.steps = []; self.verdicts = []; self.reviews = []; self.report = 'REPORT'
        self.step_prompts = []; self.plan_prompts = []; self.kinds = []
        def reply(body): return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': body if isinstance(body, str) else json.dumps(body)}]}
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if 'The agent got stuck on a step' in text: self.kinds.append('replan'); self.plan_prompts.append(text); return reply(self.replan or {'nonsense': 1})
            if text.startswith('You prepare a task'): self.kinds.append('plan'); self.plan_prompts.append(text); return reply(self.plan)
            if text.startswith('You check an agent'): self.kinds.append('verify'); return reply(self.verdicts.pop(0) if self.verdicts else {'done': True, 'reason': 'shown'})
            if text.startswith('You supervise'): self.kinds.append('review'); return reply(self.reviews.pop(0) if self.reviews else {'progress': 'yes', 'reason': 'ok', 'advice': '', 'plan': [], 'ask_user': False})
            if text.startswith("Write the agent's final report"): self.kinds.append('report'); self.report_prompt = text; return reply(self.report)
            self.kinds.append('step'); self.step_prompts.append(text)
            return reply(self.steps.pop(0) if self.steps else {'goal_done': True, 'actions': [], 'note': 'Done.'})
        self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def start(self, task='Find London campaign photographers'):
        self.app.start_agent(task, self.mode)
    def finish(self, seconds=15):
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), seconds)
        return self.app.agent
    def never_asked(self):
        return not any(m['kind'] == 'ask' for m in self.app.chat.list())


class WorksAloneTests(Base):
    def test_no_question_before_starting_it_decides_and_says_what(self):
        self.plan = {**PLAN, 'questions': ['Please provide the campaign brief and confirm the roles.']}  # what the planner asked in the real run
        self.steps = [CLICK]
        self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent); self.assertTrue(self.never_asked())
        self.assertIn('The user will NOT answer questions', self.plan_prompts[0])
        self.assertIn('decide them yourself', self.step_prompts[0]); self.assertIn('Please provide the campaign brief', self.step_prompts[0])
        plan = next(m for m in self.app.chat.list() if m['kind'] == 'plan')
        self.assertEqual(plan['assumptions'], ['London; fashion and campaign work'])

    def test_a_step_the_check_keeps_rejecting_goes_on_and_is_named_in_the_end(self):
        # In the real run the check rejected step 1 three times over wording and then the user was asked.
        self.steps = [CLICK, {'goal_done': True, 'actions': [], 'note': 'noted'}, {'goal_done': True, 'actions': [], 'note': 'noted'}]
        self.verdicts = [{'done': False, 'reason': 'Only photographers are named as a role.'}] * 2
        self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent); self.assertTrue(self.never_asked())
        self.assertEqual(agent['proof'][0]['proof'], 'not confirmed: Only photographers are named as a role.')
        self.assertEqual(agent['message'], 'Finished; some of it could not be done.')

    def test_stuck_means_another_way_then_a_new_plan_then_the_rest_and_a_report_of_what_there_is(self):
        stuck = {'stuck': True, 'actions': [], 'note': 'Instagram search shows no results.'}
        self.replan = {'deliverable': PLAN['deliverable'], 'steps': [{'do': 'Search the hashtag londonphotographer instead', 'done_when': 'posts are listed'},
                                                                      {'do': 'Report the list', 'done_when': 'reported'}], 'assumptions': [], 'questions': []}
        self.steps = [{'actions': [{'kind': 'jot', 'text': 'Rina Photography @rina_sample_photo: Sample Magazine'}, CLICK['actions'][0]]}] + [stuck] * 40
        self.report = '1 photographer found: Rina Photography (@rina_sample_photo), Sample Magazine. Instagram search returned nothing further.'
        self.start(); agent = self.finish(30)
        self.assertEqual(agent['status'], 'completed', agent); self.assertTrue(self.never_asked())
        told = [p for p in self.step_prompts if 'Nobody will step in' in p]
        self.assertTrue(told)                                              # first: told to take another route
        self.assertIn('replan', self.kinds)                                # then: a new plan for the rest
        first = next(p for p in self.plan_prompts if 'The agent got stuck on a step' in p)
        self.assertIn('THE STEP IT IS STUCK ON: Search Instagram', first); self.assertIn('Rina Photography', first)  # the planner knows what failed and what is known
        self.assertTrue(any('Search the hashtag londonphotographer instead' in p for p in self.step_prompts))
        self.assertEqual(self.kinds.count('replan'), 2)                     # a new plan at most twice, then it stops planning
        self.assertLess(self.kinds.count('step'), 20)                       # and it ends: it does not go round for ever
        self.assertEqual(agent['final'], self.report)                      # in the end: what it found, and what it could not do
        self.assertIn('Rina Photography', self.report_prompt); self.assertIn('could not be done', self.report_prompt)
        self.assertEqual(agent['message'], 'Finished; some of it could not be done.')

    def test_an_empty_result_is_not_finished_and_checked(self):
        # A real run opened unrelated profiles to reach "25", reported no photographers, and ended "Finished and checked."
        nothing = {'goal_done': True, 'actions': [], 'note': 'No qualifying photographers were verified. The 25-profile minimum was met.'}
        short = {'done': False, 'reason': '0 of 5 delivered; Instagram search for Miami agencies was never tried.'}
        self.replan = {'deliverable': PLAN['deliverable'], 'steps': [{'do': 'Search Instagram for Miami model agencies and read who they credit', 'done_when': '5 credited photographers are noted'},
                                                                      {'do': 'Report the list', 'done_when': 'reported'}], 'assumptions': [], 'questions': []}
        self.steps = [CLICK, {'goal_done': True, 'actions': [], 'note': 'looked'}, nothing, nothing, CLICK]
        self.verdicts = [{'done': True, 'reason': 'ok'}, short, short]
        self.start(); agent = self.finish(30)
        self.assertEqual(self.kinds.count('replan'), 1)                                        # twice short: a new plan to get the rest
        again = next(p for p in self.plan_prompts if 'The agent got stuck on a step' in p)
        self.assertIn('The result falls short of what the task asks for: 0 of 5 delivered', again)
        self.assertTrue(any('Search Instagram for Miami model agencies' in p for p in self.step_prompts))   # and it went back to work
        self.assertTrue(self.never_asked())

    def test_a_long_research_run_is_not_saved_as_a_skill_to_replay(self):
        self.steps = [{'actions': [{'kind': 'jot', 'text': '@sam_sample_photo: credited by Sample'}, CLICK['actions'][0]]}]
        self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertEqual(self.app.skills.list(), [])                                           # it kept a notebook: research, not a routine

    def test_writing_the_same_note_again_and_again_is_stopped(self):
        # A real run spent six steps in a row re-writing one note about a Prada post.
        note = lambda words: {'actions': [{'kind': 'jot', 'text': f'{words} https://www.instagram.com/p/DfXBwQwjXXU/ credits pastry direction only'}]}
        self.app.settings.update({'plan_contract': False})
        self.steps = [note('Prada post'), note('Prada campaign post'), note('Inspected Prada post'), CLICK]
        self.start(); self.finish()
        self.assertTrue(any('only written notes' in p for p in self.step_prompts))
        self.assertTrue(any('already had a note on this' in p for p in self.step_prompts))
        self.assertEqual(self.step_prompts[-1].count('https://www.instagram.com/p/DfXBwQwjXXU/ credits'), 1)   # one note, not three

    def test_a_question_about_the_task_is_not_put_to_the_user_but_a_captcha_is(self):
        self.steps = [{'actions': [{'kind': 'ask_user', 'reason': 'Which visual style do you prefer?'}]}, CLICK,
                      {'actions': [{'kind': 'ask_user', 'reason': 'Please solve the CAPTCHA on the screen'}]}]
        self.start()
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        self.assertEqual(self.app.agent['waiting']['reason'], 'Please solve the CAPTCHA on the screen')   # the only thing asked
        self.assertTrue(any('The user will not answer that' in p and 'Which visual style' in p for p in self.step_prompts))
        self.app.runner.reply('continue'); self.assertEqual(self.finish()['status'], 'completed')

    def test_a_passing_problem_is_retried_without_asking(self):
        boom = {'n': 0}
        inner = self.app.client.transport
        def flaky(url, payload, headers, timeout):
            text = ' '.join(c.get('text', '') for c in payload['messages'][0]['content'])
            if 'Overall task' in text and boom['n'] < 2: boom['n'] += 1; raise RuntimeError('boom')
            return inner(url, payload, headers, timeout)
        self.app.client.transport = flaky; self.steps = [CLICK]
        with patch.object(agent_module.threading.Event, 'wait', lambda self, t=None: self.is_set()):  # no real pauses in a test
            self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent); self.assertTrue(self.never_asked()); self.assertEqual(boom['n'], 2)

    def test_a_problem_that_retrying_cannot_fix_ends_with_what_was_found(self):
        inner = self.app.client.transport; n = {'i': 0}
        def broke(url, payload, headers, timeout):
            text = ' '.join(c.get('text', '') for c in payload['messages'][0]['content'])
            if 'Overall task' in text:
                n['i'] += 1
                if n['i'] > 1: raise ValueError("Today's budget is used up (Settings → Models)")
            return inner(url, payload, headers, timeout)
        self.app.client.transport = broke
        self.steps = [{'actions': [{'kind': 'jot', 'text': 'Rina Photography @rina_sample_photo'}, CLICK['actions'][0]]}]
        self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'failed'); self.assertIn('budget', agent['message']); self.assertTrue(self.never_asked())
        self.assertTrue(any('What I had found so far' in m['text'] and 'Rina Photography' in m['text'] for m in self.app.chat.list()))


class ChatTalksToTheTaskTests(Base):
    """What you type in the chat goes to the task that is running. In the real run each answer started a new task."""
    def test_an_answer_typed_in_the_chat_answers_the_question(self):
        self.app.settings.update({'ask_when_stuck': True})
        self.plan = {**PLAN, 'questions': ['Which brand is the brief for?']}; self.steps = [CLICK]
        self.start()
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        run = self.app.agent['run_id']
        r = self.app.chat_message('whatever, just not wedding photographers or amateurs')
        self.assertEqual(r['taken_as'], 'answer'); self.assertFalse(r['queued']); self.assertEqual(self.app.tasks, [])
        agent = self.finish()
        self.assertEqual(agent['run_id'], run)                              # the same task went on; no new one was started
        self.assertIn('The user answered: whatever, just not wedding photographers or amateurs', self.step_prompts[0])

    def test_a_message_while_it_works_is_a_correction_for_that_task(self):
        self.steps = [{'actions': [{'kind': 'wait', 'seconds': 1.5, 'label': 'reading'}]}, CLICK]
        self.start()
        await_done(lambda: self.app.agent['status'] == 'running' and self.step_prompts, 10)
        run = self.app.agent['run_id']
        r = self.app.chat_message('no, only via the instagram search')
        self.assertEqual(r['taken_as'], 'instruction'); self.assertEqual(self.app.tasks, [])
        agent = self.finish()
        self.assertEqual(agent['run_id'], run); self.assertEqual(agent['status'], 'completed', agent)
        later = [p for p in self.step_prompts if 'no, only via the instagram search' in p]
        self.assertTrue(later); self.assertIn('The user just told you', later[0]); self.assertIn('The user added while you were working', later[-1])

    def test_yes_and_no_in_the_chat_answer_an_approval(self):
        self.app.room.update({'require_approval': True})
        for said, expected, clicks in (('yes go ahead', 'approve', 1), ('no', 'deny', 0)):
            self.app.desktop.lab_actions.clear(); self.kinds.clear()
            self.steps = [{'actions': [{'kind': 'click', 'x': 400, 'y': 180, 'label': 'click Post'}]}]
            self.app.settings.update({'plan_contract': False})
            self.start('Post the ' + ('comment' if expected == 'approve' else 'reply'))  # two tasks, so the first is not replayed as a skill
            await_done(lambda: self.app.agent['status'] == 'waiting', 10)
            self.assertEqual(self.app.chat_message(said)['taken_as'], expected)
            self.finish()
            self.assertEqual(sum(a['kind'] == 'click' for a in self.app.desktop.lab_actions), clicks, said)

    def test_with_nothing_running_a_message_is_a_new_task(self):
        r = self.app.chat_message('Find tomorrow\'s weather')
        self.assertNotIn('taken_as', r); self.assertEqual(r['worker'], 1)
        self.app.stop_all()


if __name__ == '__main__': unittest.main()
