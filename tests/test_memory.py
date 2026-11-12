"""The agent must see its own notebook and last actions at every step. A run on 11 Nov 2026 found good results and
never listed them: with a screenshot attached the prompt was cut to 14,000 characters from the end, and the end is
where the notebook, the last actions and all feedback are. The model had no memory at all."""
import json
import tempfile
import unittest

from app.agent import notebook_key, same_note
from app.models import CLOUD_PROMPT_CHARS, fit_prompt
from app.server import Application
from test_system import await_done

LONG_BRIEF = ('1. Turn the brief into what to look for: from the art direction and references, write down visual traits. '
              '2. Find starting points on Instagram, not Google: recent campaign posts from relevant brands. ') * 12  # about 2,400 characters


class FitTests(unittest.TestCase):
    def test_a_prompt_that_is_too_long_keeps_its_end(self):
        prompt = 'INSTRUCTIONS ' + 'x' * 30000 + ' NOTEBOOK: Rina. RECENT ACTIONS: clicked Post. SCREEN NOW: profile'
        cut = fit_prompt(prompt, 14000)
        self.assertLessEqual(len(cut), 14000)
        self.assertTrue(cut.startswith('INSTRUCTIONS')); self.assertTrue(cut.endswith('SCREEN NOW: profile'))
        self.assertIn('RECENT ACTIONS: clicked Post', cut); self.assertIn('left out', cut)
        self.assertEqual(fit_prompt('short', 14000), 'short')


class NotebookTests(unittest.TestCase):
    """A new note either rewrites an old one or is about something else. Getting this wrong loses results."""
    SAME = [('TO VISIT: @anna, @ben, @cleo', 'TO VISIT: @ben, @cleo, @dan'),             # the list, updated
            ('TO VISIT (3 left): @anna, @ben, @cleo', 'TO VISIT (1 left): @cleo'),
            ('KEPT: @anna — Nike', 'KEPT 1 of 5: @anna — Nike, Adidas'),                    # the same photographer, fuller
            ('Profile log — @lily: London', 'Profile check — @lily: London, Sample Label campaign'),
            ('@sam: credited by @arc', '@sam: Miami; credited by @arc and @nike'),
            ('FOUND: Samsonite Omni PC, $129', 'FOUND: Samsonite Omni PC, $119, 4.6 stars'),
            ('SOURCES: @sample_citymag (6 read)', 'SOURCES: @sample_citymag (6 read), @sample_magazine (2)'),
            ('Prada post https://www.instagram.com/p/DfX/ no credit', 'Prada campaign post https://www.instagram.com/p/DfX/ credits pastry only')]
    DIFFERENT = [('KEPT: @anna — Nike', 'KEPT: @ben — Puma'),                              # one note per photographer: both stay
                 ('TO VISIT: @lily', '@lily: kept, Miami'),                                  # a list is not a note about its only entry
                 ('@sam: credited by @arc', '@arc: agency, credits @sam'),
                 ('FOUND: Samsonite Omni PC, $129', 'FOUND: Away Carry-On, $245'),
                 ('DAYS: Day 1: Belém', 'DAYS: Day 2: Alfama'),
                 ('Puma post https://www.instagram.com/p/AAA/ NYC', 'Prada post https://www.instagram.com/p/DfX/ no credit')]

    def test_a_rewrite_replaces_and_another_subject_is_kept_beside_it(self):
        for old, new in self.SAME: self.assertTrue(same_note(old, new), (old, new))
        for old, new in self.DIFFERENT: self.assertFalse(same_note(old, new), (old, new))


class SeesItsMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'plan_contract': False})
        self.prompts = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_with(self, replies, context=''):
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type'] == 'text')
            if text.startswith('You supervise'):
                return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(
                    {'progress': 'yes', 'reason': 'ok', 'advice': '', 'plan': [], 'ask_user': False})}]}
            self.prompts.append(text)
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(replies[min(len(self.prompts), len(replies)) - 1])}]}
        self.app.client.transport = transport
        self.app.runner.start(LONG_BRIEF, 'fast', context=context)
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 15)
        return self.app.agent

    def test_with_a_long_brief_and_a_long_history_it_still_sees_notebook_actions_and_screen(self):
        # The situation of the real run: long instructions, 6,000 characters of earlier conversation, a long brief.
        context = 'Earlier conversation:\n' + ('The user said they want London photographers, not wedding photographers. ' * 80)[:6000]
        agent = self.run_with([{'actions': [{'kind': 'jot', 'text': '@sam_sample_photo: photographer credited by Sample Creative Production'},
                                            {'kind': 'click', 'x': 795, 'y': 214, 'label': 'Open photographer credit'}]},
                               {'actions': [{'kind': 'click', 'x': 320, 'y': 681, 'label': 'Open fashion editorial post'}]},
                               {'goal_done': True, 'actions': [], 'note': '@sam_sample_photo'}], context=context)
        self.assertEqual(agent['status'], 'completed', agent)
        third = self.prompts[2]
        self.assertGreater(len(third), 14000)                                          # longer than the old cut-off ...
        self.assertLess(len(third), CLOUD_PROMPT_CHARS)
        self.assertIn('Your task notebook:\n- @sam_sample_photo: photographer credited by Sample', third)   # ... and nothing it needs is missing
        self.assertIn('Open photographer credit', third); self.assertIn('Open fashion editorial post', third)  # its last actions
        self.assertIn('Desktop now:', third)                                           # the very end of the prompt is there
        self.assertNotIn('left out', third)

    def test_the_same_note_twice_is_one_note_and_a_better_note_replaces_it(self):
        agent = self.run_with([{'actions': [{'kind': 'jot', 'text': 'Profile log — @lena_sample_photography: London fashion photographer'}, {'kind': 'click', 'x': 400, 'y': 180, 'label': 'a'}]},
                               {'actions': [{'kind': 'jot', 'text': 'Profile log — @lena_sample_photography: London fashion photographer'}, {'kind': 'click', 'x': 410, 'y': 180, 'label': 'b'}]},
                               {'actions': [{'kind': 'jot', 'text': 'Profile check — @lena_sample_photography: London fashion and commercial; campaign for Sample Label'}, {'kind': 'click', 'x': 420, 'y': 180, 'label': 'c'}]},
                               {'actions': [{'kind': 'jot', 'text': '@northlight_sample: photographer credited on a campaign BTS post'}, {'kind': 'click', 'x': 430, 'y': 180, 'label': 'd'}]},
                               {'goal_done': True, 'actions': [], 'note': 'two'}])
        last = self.prompts[-1]
        self.assertEqual(last.count('@lena_sample_photography: London'), 1)             # one note about Lena, not four
        self.assertIn('campaign for Sample Label', last); self.assertIn('- @northlight_sample: photographer credited', last)
        self.assertTrue(any('already in your notebook' in p for p in self.prompts))    # and it was told so

    def test_the_same_click_again_and_again_is_called_out(self):
        click = lambda x: {'actions': [{'kind': 'click', 'x': x, 'y': 263, 'label': 'Open credited photographer profile'}]}
        self.run_with([click(993), click(821), click(765), click(713), {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertIn('You clicked \\"Open credited photographer profile\\" 4 times in a row', self.prompts[-1])


if __name__ == '__main__': unittest.main()
