"""Know-how: written playbooks (how to search the web, find people, dig deeper, draw conclusions, find insights, act
on sites, work like a careful person) that the planner picks for a task and the agent then works by."""
import json
import tempfile
import unittest

from app import contract
from app.knowhow import Knowhow
from app.server import Application
from test_system import await_done

PLAN = {'deliverable': '5 photographers with handle and two brands',
        'steps': [{'do': 'Find and note 5 photographers', 'done_when': '5 KEPT notes'}, {'do': 'Report them', 'done_when': 'the report lists 5'}],
        'skills': ['find-people-and-accounts', 'draw-conclusions', 'no-such-thing'], 'assumptions': [], 'questions': []}


class BookTests(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.TemporaryDirectory(); self.book = Knowhow(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_the_built_in_ones_are_there_and_short_enough_to_carry_along(self):
        items = {p['id']: p for p in self.book.list()}
        for pid in ('search-web', 'search-telegram', 'find-people-and-accounts', 'dig-deeper', 'draw-conclusions', 'find-insights',
                    'act-on-sites', 'work-like-a-person', 'understand-goal'):
            self.assertIn(pid, items); self.assertTrue(items[pid]['on'] and items[pid]['builtin'])
            self.assertTrue(200 < len(items[pid]['text']) < 1700, pid)      # a page of know-how, not a manual: it goes into every step
            self.assertTrue(items[pid]['name'] and items[pid]['when'])
        self.assertEqual(items['work-like-a-person']['use'], 'always'); self.assertEqual(items['understand-goal']['use'], 'planning')

    def test_working_like_a_person_is_about_care_not_about_hiding(self):
        text = {p['id']: p['text'] for p in self.book.list()}['work-like-a-person']
        self.assertIn('never about hiding what you are', text); self.assertIn('Never try to get around it', text)
        self.assertIn('captcha', text); self.assertIn('never pretend to be someone', text)
        for banned in ('random delay', 'randomi', 'fingerprint', 'user agent', 'avoid detection', 'undetect', 'look human'):
            self.assertNotIn(banned, text.lower())

    def test_lookups_are_not_for_profiling_private_people(self):
        text = {p['id']: p['text'] for p in self.book.list()}
        self.assertIn('Never use it to locate or profile a private person', text['dig-deeper'])
        self.assertIn("Do not look for a private person's home", text['find-people-and-accounts'])

    def test_the_planner_sees_a_menu_and_the_agent_gets_the_chosen_ones(self):
        menu = self.book.menu()
        self.assertIn('- search-web: Search the web.', menu)
        self.assertNotIn('work-like-a-person', menu); self.assertNotIn('understand-goal', menu)   # those are not a choice
        picked = self.book.pick(['draw-conclusions', 'nope', 'search-web', 'draw-conclusions', 'find-insights', 'dig-deeper'])
        self.assertEqual([p['id'] for p in picked], ['work-like-a-person', 'draw-conclusions', 'search-web', 'find-insights'])  # the always one, then at most three
        self.assertIn('DRAW CONCLUSIONS\n- Keep three things apart', Knowhow.text(picked))
        self.assertIn('what the user is really after', self.book.for_planning())

    def test_without_a_planner_the_words_of_the_task_decide(self):
        self.assertEqual(self.book.guess('Find 5 creative Miami photographers on Instagram')[0], 'find-people-and-accounts')
        self.assertIn('search-telegram', self.book.guess('Which Telegram channels cover AI news?'))
        self.assertEqual(self.book.guess('Open the calculator'), [])

    def test_one_can_be_switched_off_and_the_user_can_add_their_own(self):
        self.book.switch('search-web', False)
        self.assertNotIn('search-web', self.book.menu()); self.assertEqual(self.book.pick(['search-web']), self.book.pick([]))
        mine = self.book.add('Flats in Tel Aviv', 'The task is about renting a flat', '- Start on the two biggest listing sites.\n- Skip agency duplicates.')
        self.assertIn(mine['id'] + ': Flats in Tel Aviv', self.book.menu())
        again = Knowhow(self.tmp.name)                                       # kept across restarts
        self.assertIn(mine['id'], [p['id'] for p in again.pick([mine['id']])]); self.assertNotIn('search-web', again.menu())
        again.delete(mine['id']); self.assertNotIn(mine['id'], again.menu())
        with self.assertRaises(ValueError): again.add('x', '', 'too short')
        with self.assertRaises(ValueError): again.switch('no-such', True)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.plan = PLAN; self.plan_prompts = []; self.step_prompts = []
        def reply(body): return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if text.startswith('You prepare a task'): self.plan_prompts.append(text); return reply(self.plan)
            if text.startswith('You check an agent'): return reply({'done': True, 'reason': 'shown'})
            if text.startswith('You supervise'): return reply({'progress': 'yes', 'reason': 'ok', 'advice': '', 'plan': [], 'ask_user': False})
            self.step_prompts.append(payload['messages'][0]['content'])
            return reply({'goal_done': True, 'actions': [], 'note': 'Done.'})
        self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_task(self, task='Find 5 creative Miami photographers on Instagram', mode='fast'):
        self.app.start_agent(task, mode)
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)

    def test_the_planner_chooses_and_every_step_works_by_it(self):
        self.run_task()
        asked = self.plan_prompts[0]
        self.assertIn('KNOW-HOW the agent can be given', asked); self.assertIn('- dig-deeper: Find what is hard to find.', asked)
        self.assertIn('what the user is really after', asked)               # how to understand the goal goes to the planner
        task_part = self.step_prompts[0][1]                                  # the part that stays the same through the task
        self.assertIn('cache_control', task_part)                            # ... so it is charged at the seen-before rate
        for title in ('WORK THE WAY A CAREFUL PERSON WOULD', 'FIND PEOPLE, ACCOUNTS AND COMPANIES', 'DRAW CONCLUSIONS'):
            self.assertIn(title, task_part['text'])
        self.assertNotIn('SEARCH IN TELEGRAM', task_part['text'])            # not chosen, not carried
        plan = next(m for m in self.app.chat.list() if m['kind'] == 'plan')
        self.assertEqual(plan['knowhow'], ['Work the way a careful person would', 'Find people, accounts and companies', 'Draw conclusions'])

    def test_without_a_plan_the_task_words_choose(self):
        self.app.settings.update({'plan_contract': False})
        self.run_task('Which Telegram channels cover AI news?')
        text = self.step_prompts[0][1]['text']
        self.assertIn('SEARCH IN TELEGRAM', text); self.assertIn('WORK THE WAY A CAREFUL PERSON WOULD', text)

    def test_a_switched_off_one_is_never_given(self):
        self.app.knowhow.switch('draw-conclusions', False); self.app.knowhow.switch('work-like-a-person', False)
        self.run_task()
        text = self.step_prompts[0][1]['text']
        self.assertNotIn('DRAW CONCLUSIONS', text); self.assertNotIn('CAREFUL PERSON', text); self.assertIn('FIND PEOPLE, ACCOUNTS AND COMPANIES', text)

    def test_a_plan_keeps_the_ids_it_chose(self):
        self.assertEqual(contract.parse_plan(PLAN)['skills'], ['find-people-and-accounts', 'draw-conclusions', 'no-such-thing'])
        self.assertEqual(contract.parse_plan({k: v for k, v in PLAN.items() if k != 'skills'})['skills'], [])


if __name__ == '__main__': unittest.main()
