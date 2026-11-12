import json
import tempfile
import unittest
from pathlib import Path

from app.models import PRESETS, Settings, cost_for


class LunaTests(unittest.TestCase):
    def test_luna_is_the_gpt_default_and_has_prices(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Settings(Path(d)).snapshot()['openai_model'], 'gpt-6-luna')
        usage = {'input': 10000, 'output': 300, 'cached': 0, 'cache_write': 0}
        self.assertAlmostEqual(cost_for(usage, PRESETS['gpt-6-luna']), 0.00115)

    def test_old_gpt_default_moves_to_luna_but_a_custom_choice_stays(self):
        for old, new in (('gpt-5.4-mini', 'gpt-6-luna'), ('gpt-5-mini', 'gpt-5-mini')):
            with tempfile.TemporaryDirectory() as d:
                Path(d, 'settings.json').write_text(json.dumps({'settings_version': 6, 'openai_model': old}))
                self.assertEqual(Settings(Path(d)).snapshot()['openai_model'], new)


if __name__ == '__main__': unittest.main()


class GptChoiceTests(unittest.TestCase):
    """The GPT model is the user's choice: the cheapest for routine work, a stronger one where judgment counts."""
    def test_the_stronger_gpt_models_have_prices_and_can_be_chosen(self):
        for model, rate in (('gpt-6.1-terra', (1., .05, 5.)), ('gpt-6.1-sol', (2., .10, 10.)), ('gpt-6-astra', (10., 1., 50.))):
            self.assertEqual((PRESETS[model]['input'], PRESETS[model]['cached'], PRESETS[model]['output']), rate)
        with tempfile.TemporaryDirectory() as d:
            s = Settings(Path(d)); s.update({'openai_model': 'gpt-6.1-terra'})
            self.assertEqual(Settings(Path(d)).snapshot()['openai_model'], 'gpt-6.1-terra')   # and the choice is kept

    def test_routine_steps_think_little_and_the_moments_of_judgment_think_more(self):
        from app.server import Application
        tmp = tempfile.TemporaryDirectory(); app = Application(tmp.name, lab=True)
        try:
            app.settings.update({'openai_key': 'fixture', 'openai_model': 'gpt-6.1-terra'})
            sent = []
            def transport(url, payload, headers, timeout):
                sent.append(payload)
                return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"actions":[]}'}]}]}
            app.client.transport = transport
            for purpose in ('execute', 'plan', 'verify', 'review'): app.client.call('openai', 'p', None, 'run', purpose)
            self.assertEqual([p['model'] for p in sent], ['gpt-6.1-terra'] * 4)
            self.assertEqual([p['reasoning']['effort'] for p in sent], ['low', 'medium', 'medium', 'medium'])
            self.assertAlmostEqual(app.ledger.summary('run')['run']['cost_usd'], 4 * (10 * 1. + 5 * 5.) / 1e6)   # billed at Terra's prices
        finally: app.close(); tmp.cleanup()


class RoomToThinkTests(unittest.TestCase):
    """GPT's reasoning counts against the output limit. A plan once came back cut in half (1837 -> 1536 tokens)
    and planning was skipped."""
    def setUp(self):
        from app.server import Application
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'openai_key': 'fixture'}); self.sent = []
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def answer(self, text, **extra):
        return {'usage': {'input_tokens': 1000, 'output_tokens': 500, 'total_tokens': 1500}, 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}], **extra}

    def test_planning_has_room_for_thinking_and_for_the_plan(self):
        def transport(url, payload, headers, timeout): self.sent.append(payload); return self.answer('{"steps":[]}')
        self.app.client.transport = transport
        self.app.client.call('openai', 'p', None, 'r', 'plan'); self.app.client.call('openai', 'p', None, 'r', 'execute')
        self.assertGreaterEqual(self.sent[0]['max_output_tokens'], 9000)       # the plan: medium reasoning plus a long answer
        self.assertGreater(self.sent[1]['max_output_tokens'], 1536)            # a routine step: a little reasoning plus its answer

    def test_an_answer_that_was_still_cut_off_is_asked_for_again_with_more_room(self):
        def transport(url, payload, headers, timeout):
            self.sent.append(json.loads(json.dumps(payload)))
            if len(self.sent) == 1: return self.answer('{"steps":[{"do":"Search Insta', status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
            return self.answer('{"steps":[{"do":"Search Instagram","done_when":"results"}]}')
        self.app.client.transport = transport
        result = self.app.client.call('openai', 'p', None, 'r', 'plan')
        self.assertEqual(result['steps'][0]['do'], 'Search Instagram')
        self.assertEqual(self.sent[1]['max_output_tokens'], 2 * self.sent[0]['max_output_tokens']); self.assertEqual(self.sent[1]['reasoning'], {'effort': 'low'})
        self.assertEqual(self.app.ledger.summary('r')['run']['total_tokens'], 3000)   # both requests are counted
