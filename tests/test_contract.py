import json
import tempfile
import unittest

from app import contract
from app.server import Application
from test_system import await_done

PLAN = {'deliverable': '3 London photographers, each with Instagram handle and one credited campaign',
        'steps': [{'do': 'Find 3 London photographers on Instagram and note each one', 'done_when': 'the notebook lists 3 photographers with handle and a credited campaign'},
                  {'do': 'Report the list to the user', 'done_when': 'the report contains the 3 photographers'}],
        'questions': []}
CLICK = {'actions': [{'kind': 'click', 'x': 400, 'y': 180, 'label': 'open profile'}]}


class ParseTests(unittest.TestCase):
    def test_a_plan_needs_steps_and_keeps_what_proves_them(self):
        p = contract.parse_plan(PLAN)
        self.assertEqual(len(p['steps']), 2); self.assertIn('3 photographers', p['steps'][0]['done_when'])
        self.assertIsNone(contract.parse_plan({'goal_done': True, 'actions': []}))       # not a plan at all
        self.assertIsNone(contract.parse_plan({'steps': [{'do': ''}]}))
        self.assertIsNone(contract.parse_plan({'steps': [{'do': 'x', 'done_when': 'y'}] * 13}))  # too long to hold anyone to

    def test_a_check_needs_a_clear_yes_or_no(self):
        self.assertEqual(contract.parse_verdict({'done': False, 'reason': 'only 1 of 3 noted'}), (False, 'only 1 of 3 noted'))
        self.assertEqual(contract.parse_verdict({'goal_done': True})[0], None)            # unusable: the step is accepted

    def test_the_last_step_is_checked_against_the_promised_result(self):
        text = contract.verify_prompt('task', 'Report', 'reported', 'done', [], ['a'], 'screen', deliverable='3 photographers', report='Here they are: ...')
        self.assertIn('THE TASK MUST DELIVER: 3 photographers', text); self.assertIn('Here they are', text)


class ContractRunTests(unittest.TestCase):
    """A run in Fastest mode: the planning call, then steps whose completion is checked independently."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.plan = PLAN; self.steps = []; self.verdicts = []; self.step_prompts = []; self.checks_seen = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def start(self, mode='fast'):
        def reply(body): return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if text.startswith('You prepare a task'): return reply(self.plans.pop(0) if getattr(self, 'plans', None) else self.plan)
            if text.startswith('You check an agent'):
                self.checks_seen.append(text)
                return reply(self.verdicts.pop(0) if self.verdicts else {'done': True, 'reason': 'shown'})
            if text.startswith('You supervise'): return reply({'progress': 'yes', 'reason': 'ok', 'advice': '', 'plan': [], 'ask_user': False})
            self.step_prompts.append(text)
            return reply(self.steps.pop(0) if self.steps else {'goal_done': True, 'actions': [], 'note': 'Done.'})
        self.app.client.transport = transport
        self.app.start_agent('Find London campaign photographers for our brief', mode)

    def finish(self):
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return self.app.agent

    def test_the_plan_names_the_result_and_what_proves_each_step(self):
        self.steps = [CLICK]
        self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertEqual(agent['plan'], [s['do'] for s in PLAN['steps']]); self.assertEqual(agent['deliverable'], PLAN['deliverable'])
        first = self.step_prompts[0]
        self.assertIn('Plan step 1 of 2: Find 3 London photographers', first)
        self.assertIn('This step is finished when: the notebook lists 3 photographers', first)
        self.assertIn('The task must deliver: 3 London photographers', first)
        plan_msg = next(m for m in self.app.chat.list() if m['kind'] == 'plan')
        self.assertIn('done when:', plan_msg['goals'][0]); self.assertEqual(plan_msg['deliverable'], PLAN['deliverable'])

    def test_a_step_is_not_done_because_the_model_says_so(self):
        # The model claims step 1 after noting one photographer; the check sees one of three and sends it back.
        self.steps = [{'actions': [{'kind': 'jot', 'text': 'Rina Photography @rina_sample_photo: Sample Magazine'}, CLICK['actions'][0]]},
                      {'goal_done': True, 'actions': [], 'note': 'Found photographers.'},
                      {'actions': [{'kind': 'jot', 'text': 'two more noted'}, {'kind': 'click', 'x': 420, 'y': 180, 'label': 'next'}]}]
        self.verdicts = [{'done': False, 'reason': 'The notebook lists 1 photographer, not 3.'}]
        self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertIn('Rina Photography', self.checks_seen[0])                      # the check reads the notebook
        told = next(p for p in self.step_prompts if 'is NOT finished' in p)
        self.assertIn('The notebook lists 1 photographer, not 3.', told)             # and the model is told exactly what is missing
        self.assertIn('Plan step 1 of 2', told)                                       # still on step 1
        self.assertEqual(len(agent['proof']), 2)                                      # in the end both steps were confirmed
        self.assertEqual(agent['message'], 'Finished and checked.')

    def test_the_final_report_must_contain_the_result(self):
        self.steps = [CLICK, {'goal_done': True, 'actions': [], 'note': 'noted 3'}, {'goal_done': True, 'actions': [], 'note': 'I have completed the search.'},
                      {'goal_done': True, 'actions': [], 'note': '1. @a - Nike 2. @b - Burberry 3. @c - Zara'}]
        self.verdicts = [{'done': True, 'reason': '3 noted'}, {'done': False, 'reason': 'The report names no photographers.'}]
        self.start(); agent = self.finish()
        self.assertEqual(agent['final'], '1. @a - Nike 2. @b - Burberry 3. @c - Zara')
        self.assertIn('THE TASK MUST DELIVER', self.checks_seen[1]); self.assertIn('I have completed the search.', self.checks_seen[1])

    def test_when_the_agent_and_the_check_keep_disagreeing_the_user_decides(self):
        self.app.settings.update({'ask_when_stuck': True})
        self.steps = [CLICK] + [{'goal_done': True, 'actions': [], 'note': 'done'}] * 3
        self.verdicts = [{'done': False, 'reason': 'Only 2 of 3.'}] * 3
        self.start()
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        self.assertEqual(self.app.agent['waiting']['type'], 'stuck'); self.assertIn('Only 2 of 3.', self.app.agent['waiting']['reason'])
        self.app.runner.reply('continue')                                             # the user accepts the step
        agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertEqual(agent['proof'][0]['proof'], 'accepted by the user')

    def test_what_is_missing_is_asked_before_work_starts(self):
        self.app.settings.update({'ask_when_stuck': True})
        self.plan = {**PLAN, 'questions': ['Which brand is the brief for?']}
        self.start()
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        self.assertEqual(self.app.agent['waiting']['type'], 'question'); self.assertEqual(self.app.agent['waiting']['reason'], 'Which brand is the brief for?')
        self.assertEqual(self.step_prompts, [])                                       # nothing was done yet
        self.app.runner.reply('A sportswear brand, moody outdoor look')
        self.finish()
        self.assertIn('The user answered: A sportswear brand, moody outdoor look', self.step_prompts[0])

    def test_an_unusable_plan_or_check_never_blocks_the_task(self):
        self.plan = {'goal_done': True, 'actions': []}                                # the planning reply is nonsense
        self.steps = [CLICK]
        self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent); self.assertEqual(self.checks_seen, [])
        self.assertIn('Current subgoal: Find London campaign photographers', self.step_prompts[0])

    def test_one_bad_planning_reply_is_tried_again(self):
        self.plans = [{'goal_done': True}, PLAN]            # first not a plan at all, then a good one
        self.steps = [CLICK]
        self.start(); agent = self.finish()
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertEqual(agent['plan'], [s['do'] for s in PLAN['steps']])   # the second reply became the plan
        self.assertTrue(any('trying once more' in e['message'] for e in self.app.engine.events))

    def test_private_mode_and_a_switched_off_contract_work_as_before(self):
        self.app.settings.update({'plan_contract': False})
        self.steps = [CLICK]
        self.start(); agent = self.finish()
        self.assertEqual(agent['plan'], ['Find London campaign photographers for our brief']); self.assertEqual(self.checks_seen, [])


if __name__ == '__main__': unittest.main()
