"""Before it searches, the agent decides what it is looking for: who qualifies, what shows it, what rules a candidate
out, and where such things are found. Real run: asked for creative photographers, it browsed a model agency."""
import json
import tempfile
import unittest

from app import contract
from app.server import Application
from test_system import await_done

BRIEF = {'looking_for': 'Miami photographers with a style of their own and under 100k followers',
         'signs': ['bio says photographer', 'follower count under 100k', 'campaign posts that tag a brand'],
         'reject': ['model agency or model account', 'wedding or event work', 'over 100k followers'],
         'where': ['Instagram search: "miami fashion photographer"', 'photographers tagged by ones already kept']}
PLAN = {'deliverable': '5 photographers, each with handle, followers and two brands',
        'brief': BRIEF,
        'steps': [{'do': 'Search Instagram for "miami fashion photographer" and note 5 that fit the brief', 'done_when': 'the notebook has 5 KEPT notes'},
                  {'do': 'Report the 5', 'done_when': 'the report lists 5 photographers'}],
        'assumptions': ['"Cool" means a recognisable style, not follower numbers'], 'questions': []}


class BriefTextTests(unittest.TestCase):
    def test_the_planner_is_told_to_think_before_searching(self):
        p = contract.plan_prompt('Find creative photographers in Miami')
        self.assertIn('BEFORE the search starts', p); self.assertIn('"brief"', p)
        self.assertIn('Look for the thing itself, not for what surrounds it', p)

    def test_a_plan_keeps_its_brief_and_a_plan_without_one_has_none(self):
        b = contract.parse_plan(PLAN)['brief']
        self.assertEqual(b['looking_for'], BRIEF['looking_for']); self.assertEqual(len(b['reject']), 3)
        self.assertEqual(contract.parse_plan({**PLAN, 'brief': None})['brief'], {})
        self.assertEqual(contract.parse_plan({**PLAN, 'brief': 'photographers'})['brief'], {})      # not the form asked for: ignored, the plan still stands
        self.assertEqual(contract.parse_plan({**PLAN, 'brief': {'signs': 'x', 'where': []}})['brief'], {})

    def test_the_brief_as_text(self):
        text = contract.brief_text(contract.parse_plan(PLAN)['brief'])
        self.assertIn('Looking for: Miami photographers', text); self.assertIn('Rejected at once: model agency or model account; wedding', text)
        self.assertEqual(contract.brief_text({}), '')

    def test_the_check_and_a_new_plan_are_given_the_brief(self):
        text = contract.brief_text(BRIEF)
        v = contract.verify_prompt('task', 'Note 5', '5 KEPT notes', 'done', [], ['KEPT: @sample.mgmt'], 'screen', brief=text)
        self.assertIn('WHAT THE TASK IS LOOKING FOR', v); self.assertIn('model agency or model account', v)
        self.assertNotIn('WHAT THE TASK IS LOOKING FOR', contract.verify_prompt('task', 'Open a page', 'page open', 'done', [], [], 'screen'))
        r = contract.replan_prompt('task', '5 photographers', [], 'Search', 'nothing found', [], brief=text)
        self.assertIn('it still holds', r); self.assertIn('miami fashion photographer', r)


class BriefRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.plan = PLAN; self.step_prompts = []; self.checks = []; self.plan_calls = []
        def reply(body): return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if text.startswith('You prepare a task'): self.plan_calls.append(payload); return reply(self.plan)
            if text.startswith('You check an agent'): self.checks.append(text); return reply({'done': True, 'reason': 'shown'})
            if text.startswith('You supervise'): return reply({'progress': 'yes', 'reason': 'ok', 'advice': '', 'plan': [], 'ask_user': False})
            self.step_prompts.append(text)
            return reply({'goal_done': True, 'actions': [], 'note': 'Done.'})
        self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_task(self):
        self.app.start_agent('Find 5 creative, cool Miami photographers under 100k', 'fast')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return self.app.agent

    def test_every_step_and_every_check_works_from_the_brief(self):
        self.assertEqual(self.run_task()['status'], 'completed')
        self.assertTrue(self.step_prompts)
        for p in self.step_prompts:
            self.assertIn('WHAT YOU ARE LOOKING FOR (decided before the search)', p); self.assertIn('Rejected at once: model agency or model account', p)
        self.assertTrue(self.checks and all('WHAT THE TASK IS LOOKING FOR' in c for c in self.checks))

    def test_the_user_sees_the_brief_with_the_plan_before_work_starts(self):
        self.run_task()
        plan = next(m for m in self.app.chat.list() if m['kind'] == 'plan')
        self.assertEqual(plan['brief']['where'][0], 'Instagram search: "miami fashion photographer"')

    def test_a_task_that_finds_nothing_has_no_brief_in_its_prompts(self):
        self.plan = {k: v for k, v in PLAN.items() if k != 'brief'}
        self.assertEqual(self.run_task()['status'], 'completed')
        self.assertFalse(any('WHAT YOU ARE LOOKING FOR' in p for p in self.step_prompts))
        self.assertEqual(next(m for m in self.app.chat.list() if m['kind'] == 'plan')['brief'], {})

    def test_the_plan_gets_room_for_its_brief(self):
        self.run_task()
        self.assertGreaterEqual(self.plan_calls[0]['max_tokens'], 3000)


if __name__ == '__main__': unittest.main()
