import json
import tempfile
import unittest

from app.models import CACHE_BREAK, CacheMarksRejected
from app.server import Application
from test_system import await_done

DONE = json.dumps({'goal_done': True, 'actions': [], 'note': 'ok'})


class SavingTests(unittest.TestCase):
    """Paying less for the same work: instructions that repeat are cached, and GPT does not over-think a step."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture', 'openai_key': 'fixture', 'mac_eyes': False, 'plan_contract': False})  # planning has its own tests
        self.sent = []
        def transport(url, payload, headers, timeout):
            self.sent.append((url, payload))
            n = sum(1 for u, _ in self.sent if 'anthropic' in u)
            usage = {'input_tokens': 900, 'output_tokens': 20, 'cache_creation_input_tokens': 1800 if n == 1 else 0, 'cache_read_input_tokens': 0 if n == 1 else 1800}
            first = {'actions': [{'kind': 'click', 'x': 400, 'y': 180, 'label': 'next'}]}
            body = json.dumps(first) if len(self.sent) == 1 else DONE
            return {'usage': usage, 'content': [{'type': 'text', 'text': body}],
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': body}]}]}
        self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def run_mode(self, mode):
        self.app.start_agent('Do it', mode)
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)

    def steps(self):
        return [p['messages'][0]['content'] for u, p in self.sent if 'anthropic' in u and not p['messages'][0]['content'][-1]['text'].startswith('You supervise')]

    def test_claudes_instructions_are_marked_to_be_kept_and_identical_every_step(self):
        self.run_mode('smart')
        one, two = self.steps()[:2]
        self.assertEqual(one[0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(one[0]['text'], two[0]['text'])            # the kept part does not change between steps
        self.assertGreater(len(one[0]['text']), 4000)               # it is the long instructions (about 1,700 tokens) ...
        self.assertIn('SCREEN NOW', one[-1]['text']); self.assertNotIn('SCREEN NOW', one[0]['text'])  # ... not the screen
        self.assertNotIn(CACHE_BREAK.strip(), one[0]['text'] + one[-1]['text'])  # the mark itself is never sent
        self.assertNotIn('image', [c['type'] for c in one])
        # The task itself does not change either: it is the second kept part, so only the step's own lines are paid in full.
        self.assertEqual(one[1]['cache_control'], {'type': 'ephemeral'}); self.assertEqual(one[1]['text'], two[1]['text'])
        self.assertIn('Overall task: Do it', one[1]['text']); self.assertNotIn('Overall task', one[-1]['text'])

    def test_with_screenshots_the_kept_part_comes_before_the_picture(self):
        self.run_mode('fast')
        one = self.steps()[0]
        self.assertEqual([c['type'] for c in one], ['text', 'text', 'image', 'text'])  # a changing picture first would spoil the cache
        self.assertIn('cache_control', one[0]); self.assertIn('Overall task: Do it', one[1]['text']); self.assertIn('Recent actions', one[-1]['text'])

    def test_kept_tokens_are_counted_at_their_lower_price(self):
        self.run_mode('smart')
        run = self.app.ledger.summary(self.app.agent['run_id'])['run']
        self.assertEqual(run['cached_tokens'], 1800)                # the second step read them from the cache
        # Step 1 is the stronger model (Sonnet 5.5): 900 in + 1800 written to the cache + 20 out.
        # Step 2 is the fast one (Haiku 5.5): 900 in + 1800 read from the cache at a tenth of the price + 20 out.
        sonnet = 900 * 2. + 1800 * 2.5 + 20 * 10.
        haiku = 900 * .10 + 1800 * .01 + 20 * .50
        self.assertAlmostEqual(run['cost_usd'], (sonnet + haiku) / 1e6, places=9)

    def gpt(self, model='gpt-6.1-terra', mode='fast'):
        self.app.settings.update({'cloud_provider': 'openai', 'openai_model': model})
        self.run_mode(mode)
        return [p for u, p in self.sent if 'openai' in u]

    def test_gpt_is_told_to_keep_only_the_parts_that_repeat(self):
        # Left alone, GPT-5.6 and later store the whole prompt of every step (screenshot included) at 1.25x and never
        # read it back, because the next step's prompt differs. A real hour on Terra cost $3-4 that way.
        steps = [p for p in self.gpt() if 'SCREEN' in json.dumps(p['input']) or 'Recent actions' in json.dumps(p['input'])]
        one, two = steps[0]['input'][0]['content'], steps[1]['input'][0]['content']
        self.assertEqual(steps[0]['prompt_cache_options'], {'mode': 'explicit'}); self.assertEqual(steps[0]['reasoning'], {'effort': 'low'})
        self.assertEqual([c['type'] for c in one], ['input_text', 'input_text', 'input_text', 'input_image'])
        self.assertEqual(one[0]['prompt_cache_breakpoint'], {'mode': 'explicit'}); self.assertEqual(one[1]['prompt_cache_breakpoint'], {'mode': 'explicit'})
        self.assertNotIn('prompt_cache_breakpoint', one[2]); self.assertNotIn('prompt_cache_breakpoint', one[3])   # what changes is not stored
        self.assertEqual(one[0]['text'], two[0]['text']); self.assertEqual(one[1]['text'], two[1]['text'])
        self.assertIn('Overall task: Do it', one[1]['text']); self.assertIn('Recent actions', one[2]['text'])
        self.assertNotIn(CACHE_BREAK.strip(), ''.join(c.get('text', '') for c in one))

    def test_a_gpt_request_with_nothing_that_repeats_stores_nothing(self):
        self.app.settings.update({'cloud_provider': 'openai', 'openai_model': 'gpt-6.1-terra'})
        self.app.client.call('openai', 'You check an agent. Reply in JSON.', None, 'run', 'verify', self.app.settings.snapshot())
        payload = self.sent[-1][1]
        self.assertEqual(payload['prompt_cache_options'], {'mode': 'explicit'})                     # so the 1.25x storing does not happen
        self.assertEqual(len(payload['input'][0]['content']), 1); self.assertNotIn('prompt_cache_breakpoint', payload['input'][0]['content'][0])

    def test_older_gpt_models_get_the_prompt_as_before(self):
        payload = self.gpt('gpt-5-mini', 'smart')[0]
        self.assertNotIn('prompt_cache_options', payload)
        content = payload['input'][0]['content']
        self.assertEqual(len(content), 1); self.assertIn('SCREEN NOW', content[0]['text']); self.assertNotIn(CACHE_BREAK.strip(), content[0]['text'])

    def test_if_gpt_refuses_the_caching_fields_the_request_goes_again_without_them(self):
        plain = self.app.client.transport; refused = []
        def transport(url, payload, headers, timeout):
            if 'prompt_cache_options' in payload: refused.append(1); raise CacheMarksRejected('unknown parameter prompt_cache_options')
            return plain(url, payload, headers, timeout)
        self.app.client.transport = transport
        sent = self.gpt()
        self.assertEqual(len(refused), 1)                                   # asked once, remembered for the model after that
        self.assertTrue(sent and all('prompt_cache_options' not in p for p in sent))
        self.assertFalse(any('prompt_cache_breakpoint' in c for p in sent for c in p['input'][0]['content']))

    def test_what_gpt_read_from_its_cache_is_counted_at_the_low_price(self):
        plain = self.app.client.transport
        def transport(url, payload, headers, timeout):
            r = plain(url, payload, headers, timeout)
            r['usage'] = {'input_tokens': 6000, 'output_tokens': 80, 'input_tokens_details': {'cached_tokens': 3600}}
            return r
        self.app.client.transport = transport
        self.app.settings.update({'cloud_provider': 'openai', 'openai_model': 'gpt-6.1-terra'})
        self.app.client.call('openai', 'Reply in JSON.', None, 'r1', 'execute', self.app.settings.snapshot())
        run = self.app.ledger.summary('r1')['run']
        self.assertAlmostEqual(run['cost_usd'], (2400 * 1. + 3600 * .05 + 80 * 5.) / 1e6, places=9)   # $0.0030 a step instead of $0.0079


if __name__ == '__main__': unittest.main()
