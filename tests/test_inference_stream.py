import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from app.models import Settings,Ledger,ModelClient

class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.settings=Settings(Path(self.tmp.name));self.ledger=Ledger(Path(self.tmp.name))
    def tearDown(self):self.tmp.cleanup()

    def test_local_stream_progress_usage_and_scaled_coordinates(self):
        observations=[];progress=[]
        def open_stream(req,timeout=None):
            data=json.loads(req.data);observations.append((data,timeout))
            chunks=[{'message':{'content':'{"actions":['},'done':False},
                    {'message':{'content':'{"kind":"click","x":480,"y":300}]}'},'done':False},
                    {'message':{'content':''},'done':True,'prompt_eval_count':900,'eval_count':40}]
            return io.BytesIO(('\n'.join(json.dumps(c) for c in chunks)+'\n').encode())
        self.settings.update({'local_image_width':960})
        with patch('app.models.urlopen',side_effect=open_stream):
            result=ModelClient(self.settings,self.ledger).call('local','Control a 1280x800 desktop. Return JSON.',
                     Image.new('RGB',(1280,800)),'test','execute',progress=lambda *a:progress.append(a))
        payload,timeout=observations[0]
        self.assertTrue(payload['stream']);self.assertEqual(timeout,600)
        self.assertEqual(payload['options']['num_ctx'],6144)
        encoded=payload['messages'][0]['images'][0]
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(encoded))).size,(960,600))
        self.assertIn('960x600',payload['messages'][0]['content'])
        self.assertEqual(result['actions'][0]['x'],640);self.assertEqual(result['actions'][0]['y'],400)
        self.assertTrue(progress)
        self.assertEqual(self.ledger.summary()['today']['total_tokens'],940)

    def test_local_timeout_is_configurable_and_error_identifies_inference(self):
        self.settings.update({'local_timeout_seconds':45})
        with patch('app.models.urlopen',side_effect=TimeoutError()):
            with self.assertRaisesRegex(RuntimeError,'Local inference.*45s'):
                ModelClient(self.settings,self.ledger).call('local','Return JSON',Image.new('RGB',(1280,800)),'test','execute')
        self.assertIsNone(self.ledger.summary()['recent'][0]['total_tokens'])

    def test_short_stream_does_not_report_fabricated_usage(self):
        with patch('app.models.urlopen',return_value=io.BytesIO(b'{"message":{"content":"{}"},"done":false}\n')):
            with self.assertRaisesRegex(RuntimeError,'before completion'):
                ModelClient(self.settings,self.ledger).call('local','Return JSON',Image.new('RGB',(1280,800)),'test','execute')
        self.assertEqual(self.ledger.summary()['recent'][0]['status'],'failed')
        self.assertIsNone(self.ledger.summary()['recent'][0]['total_tokens'])


class ThinkingModelTests(unittest.TestCase):
    """Qwen3-VL in Ollama can stream hidden reasoning ('thinking') before, or instead of, its answer."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from app.models import Settings, Ledger
        self.settings = Settings(Path(self.tmp.name)); self.ledger = Ledger(Path(self.tmp.name))
        self.settings.update({'local_model': 'qwen3-vl:8b'})
    def tearDown(self): self.tmp.cleanup()

    @staticmethod
    def stream(content, thinking='', usage=(900, 40)):
        chunks = [{'message': {'thinking': thinking, 'content': ''}, 'done': False}] if thinking else []
        chunks += [{'message': {'content': content}, 'done': False},
                   {'message': {'content': ''}, 'done': True, 'prompt_eval_count': usage[0], 'eval_count': usage[1]}]
        return io.BytesIO(('\n'.join(json.dumps(c) for c in chunks) + '\n').encode())

    def test_thinking_is_switched_off_and_answer_read(self):
        sent = []
        def fake(req, timeout=None):
            sent.append(json.loads(req.data)); return self.stream('{"x":1,"y":2}')
        with patch('app.models.urlopen', side_effect=fake):
            result = ModelClient(self.settings, self.ledger).call('local', 'Return JSON', Image.new('RGB', (1280, 800)), 'r', 'locate')
        self.assertIs(sent[0]['think'], False)
        self.assertEqual(result, {'x': 1, 'y': 2})

    def test_all_thinking_no_answer_is_retried_with_a_bigger_budget(self):
        sent = []
        def fake(req, timeout=None):
            sent.append(json.loads(req.data))
            return self.stream('', thinking='Let me look at the screen carefully...') if len(sent) == 1 else self.stream('{"x":3,"y":4}')
        progress = []
        with patch('app.models.urlopen', side_effect=fake):
            result = ModelClient(self.settings, self.ledger).call('local', 'Return JSON', Image.new('RGB', (1280, 800)), 'r', 'locate',
                                                                  progress=lambda c, t: progress.append(c))
        self.assertEqual(result, {'x': 3, 'y': 4})
        self.assertEqual(len(sent), 2); self.assertGreaterEqual(sent[1]['options']['num_predict'], 1024)
        self.assertTrue(progress and progress[0] > 0)  # thinking shows as activity
        self.assertEqual(self.ledger.summary()['today']['total_tokens'], 2 * 940)  # both attempts counted

    def test_ollama_rejecting_the_think_option_falls_back(self):
        from urllib.error import HTTPError
        sent = []
        def fake(req, timeout=None):
            body = json.loads(req.data); sent.append(body)
            if 'think' in body: raise HTTPError(req.full_url, 400, 'does not support thinking', {}, io.BytesIO(b''))
            return self.stream('{"x":5,"y":6}')
        client = ModelClient(self.settings, self.ledger)
        with patch('app.models.urlopen', side_effect=fake):
            self.assertEqual(client.call('local', 'Return JSON', Image.new('RGB', (1280, 800)), 'r', 'locate'), {'x': 5, 'y': 6})
            client.call('local', 'Return JSON', Image.new('RGB', (1280, 800)), 'r', 'locate')
        self.assertEqual(['think' in b for b in sent], [True, False, False])  # remembered for next time

    def test_inline_think_tags_and_empty_answers(self):
        from app.models import BadReply
        with patch('app.models.urlopen', side_effect=lambda req, timeout=None: self.stream('<think>hmm {not json}</think>{"x":7,"y":8}')):
            self.assertEqual(ModelClient(self.settings, self.ledger).call('local', 'J', Image.new('RGB', (1280, 800)), 'r', 'locate'), {'x': 7, 'y': 8})
        with patch('app.models.urlopen', side_effect=lambda req, timeout=None: self.stream('')):
            with self.assertRaises(BadReply):
                ModelClient(self.settings, self.ledger).call('local', 'J', Image.new('RGB', (1280, 800)), 'r', 'locate')
