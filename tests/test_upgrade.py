import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from unittest.mock import patch

from PIL import Image
from app.core import Desktop,Engine,Store
from app.live import ScreenStream,InputQueue
from app.models import Settings,Ledger,ModelClient,LocalModels,token_usage,cost_for
from app.server import Application,handler_factory
from test_system import await_done

class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.settings=Settings(self.root);self.ledger=Ledger(self.root)
    def tearDown(self):self.tmp.cleanup()

    def test_provider_usage_and_cache_cost_not_double_counted(self):
        u=token_usage('openai',{'usage':{'input_tokens':1000,'output_tokens':200,'input_tokens_details':{'cached_tokens':400},'output_tokens_details':{'reasoning_tokens':100}}})
        self.assertEqual(u['total'],1200)
        self.assertAlmostEqual(cost_for(u,{'input':1,'cached':.1,'cache_write':1.25,'output':5}),.00164)
        a=token_usage('anthropic',{'usage':{'input_tokens':600,'cache_read_input_tokens':400,'cache_creation_input_tokens':200,'output_tokens':100}})
        self.assertEqual(a['input'],1200);self.assertEqual(a['total'],1300)
        self.assertAlmostEqual(cost_for(a,{'input':1,'cached':.1,'cache_write':1.25,'output':5}),.00139)
        self.assertIsNone(token_usage('local',{'message':{'content':'{}'}}))
        self.assertIsNone(token_usage('openai',{}))

    def test_key_redaction_and_budget_blocks_before_network(self):
        self.settings.update({'openai_key':'fixture-secret-never-persist','run_budget_usd':0.001})
        self.assertNotIn('fixture-secret',self.settings.path.read_text())
        self.assertNotIn('fixture-secret',json.dumps(self.settings.public()))
        called=[]
        client=ModelClient(self.settings,self.ledger,lambda *args:called.append(args))
        with self.assertRaisesRegex(ValueError,'budget'):client.call('openai','Return JSON',Image.new('RGB',(20,20)),'run','plan')
        self.assertEqual(called,[]);self.assertEqual(self.ledger.summary()['today']['calls'],0)

    def test_invalid_model_json_still_accounts_reported_usage(self):
        client=ModelClient(self.settings,self.ledger,lambda *args:{'message':{'content':'not JSON'},'prompt_eval_count':10,'eval_count':7})
        with self.assertRaises(ValueError):client.call('local','Return JSON',Image.new('RGB',(20,20)),'run','execute')
        summary=self.ledger.summary()['today']
        self.assertEqual(summary['total_tokens'],17);self.assertEqual(summary['calls'],1)

    def test_unknown_cloud_usage_retains_reservation_and_counts_call(self):
        self.settings.update({'openai_key':'fixture','max_cloud_calls':1})
        client=ModelClient(self.settings,self.ledger,lambda *args:{'output':[{'type':'message','content':[{'type':'output_text','text':'{"goals":["test"]}'}]}]})
        client.call('openai','Return JSON',Image.new('RGB',(20,20)),'run','plan')
        summary=self.ledger.summary('run')
        self.assertGreater(summary['run']['reserved_usd'],0)
        self.assertIsNone(summary['recent'][0]['input_tokens'])
        with self.assertRaisesRegex(ValueError,'call limit'):client.call('openai','Return JSON',Image.new('RGB',(20,20)),'run','replan')

    def test_openai_and_claude_request_shapes_and_usage(self):
        self.settings.update({'openai_key':'fixture-openai','anthropic_key':'fixture-anthropic'})
        observed=[]
        def transport(url,payload,headers,timeout):
            observed.append((url,payload,headers))
            if 'openai' in url:
                return {'usage':{'input_tokens':100,'output_tokens':10},'output':[{'type':'message','content':[{'type':'output_text','text':'{"goals":["one"]}'}]}]}
            return {'usage':{'input_tokens':100,'output_tokens':10,'cache_read_input_tokens':30},'content':[{'type':'text','text':'{"goals":["two"]}'}]}
        client=ModelClient(self.settings,self.ledger,transport)
        im=Image.new('RGB',(1280,800))
        self.assertEqual(client.call('openai','Return JSON',im,'a','plan')['goals'],['one'])
        self.assertEqual(client.call('anthropic','Return JSON',im,'b','plan')['goals'],['two'])
        self.assertFalse(observed[0][1]['store'])
        self.assertEqual(observed[0][1]['input'][0]['content'][1]['type'],'input_image')
        self.assertEqual(observed[1][1]['messages'][0]['content'][0]['source']['type'],'base64')
        self.assertEqual(self.ledger.summary()['today']['total_tokens'],250)

class DownloadTests(unittest.TestCase):
    def test_selected_local_model_is_automatically_pulled(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(Path(directory));settings.update({'local_model':'fixture-vision:3b'})
            calls=[]
            def fake_open(request,timeout=None):
                url=request.full_url if isinstance(request,Request) else request
                calls.append(url)
                if url.endswith('/api/tags'):return io.BytesIO(b'{"models":[]}')
                self.assertEqual(json.loads(request.data)['model'],'fixture-vision:3b')
                return io.BytesIO(b'{"status":"pulling","total":100,"completed":50}\n{"status":"success"}\n')
            with patch('app.models.urlopen',side_effect=fake_open),patch.dict('os.environ',{'OLLAMA_URL':'http://fixture','OLLAMA_AUTO_PULL':'1'}):
                manager=LocalModels(settings)
                try:
                    await_done(lambda:manager.public()['status']=='ready')
                    self.assertEqual(manager.public()['percent'],100)
                    self.assertEqual(calls,['http://fixture/api/tags','http://fixture/api/pull'])
                finally:manager.close()

class LiveTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory()
    def tearDown(self):self.tmp.cleanup()

    def test_queue_accepts_without_waiting_for_execution_and_preserves_order(self):
        desktop=Desktop(lab=True);engine=Engine(desktop,Store(self.tmp.name));inputs=InputQueue(engine)
        gate=threading.Event();started=threading.Event();original=desktop.execute
        def blocked(action,cancel=None):
            started.set();gate.wait(2);original(action,cancel)
        desktop.execute=blocked
        try:
            one=inputs.submit({'kind':'type','text':'one'});self.assertEqual(one['status'],'queued')
            self.assertTrue(started.wait(1))
            two=inputs.submit({'kind':'type','text':'two'})
            self.assertEqual(inputs.metrics()['pending'],2)
            self.assertEqual(desktop.lab_step,0)
            gate.set();await_done(lambda:not inputs.busy())
            self.assertEqual(desktop.lab_text,'two');self.assertEqual(inputs.metrics()['completed'],2)
        finally:gate.set();inputs.close()

    def test_stream_updates_while_action_lock_is_held(self):
        desktop=Desktop(lab=True);stream=ScreenStream(desktop,fps=20)
        try:
            seq,jpeg=stream.next_frame(timeout=2);self.assertIsNotNone(jpeg)
            with desktop.lock:
                desktop.lab_step+=1
                next_seq,_=stream.next_frame(seq,timeout=1)
                self.assertGreater(next_seq,seq)
        finally:stream.close()

    def test_stop_cancels_queued_and_running_wait_without_later_actions(self):
        desktop=Desktop(lab=True);engine=Engine(desktop,Store(self.tmp.name));inputs=InputQueue(engine)
        try:
            inputs.submit({'kind':'wait','seconds':10});inputs.submit({'kind':'type','text':'must not type'})
            await_done(lambda:any(j['status']=='running' for j in inputs.metrics()['recent']))
            inputs.cancel();await_done(lambda:not inputs.busy())
            self.assertEqual(desktop.lab_text,'')
        finally:inputs.close()

class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=Application(self.tmp.name,lab=True)
        self.app.settings.update({'local_model':'qwen2.5vl:3b'})  # answers in image pixels; Qwen3-VL is tested below
        # These tests cover the model-only path; the local eyes are tested in test_eyes_skills.
        from app import vision
        p=patch.object(vision,'available',return_value=False);p.start();self.addCleanup(p.stop)
    def tearDown(self):self.app.close();self.tmp.cleanup()

    def fake_ollama(self,thoughts,where=(100,100),fallback=None,openai_goals=None):
        """Fake Ollama: free-text step answers, locate answers, optional schema fallback; optional GPT plan."""
        log={'think':[],'locate':[],'schema':[]}
        def transport(url,payload,headers,timeout):
            if 'openai' in url:
                return {'usage':{'input_tokens':1000,'output_tokens':100},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'goals':openai_goals})}]}]}
            content=payload['messages'][0]['content']
            if 'format' not in payload:
                log['think'].append(content)
                text=thoughts[min(len(log['think']),len(thoughts))-1]
            elif 'Locate the' in content:
                log['locate'].append(content);x,y=where(content) if callable(where) else where
                text=json.dumps({'x':x,'y':y})
            else:
                log['schema'].append(content);text=json.dumps(fallback)
            return {'message':{'content':text},'prompt_eval_count':500,'eval_count':70}
        self.app.client.transport=transport
        return log

    def run_agent(self,instruction,planner='local'):
        self.app.start_agent(instruction,planner)
        await_done(lambda:self.app.agent['status']!='running')
        return self.app.agent

    def test_one_cloud_plan_drives_local_described_steps_and_counts_tokens(self):
        self.app.settings.update({'openai_key':'fixture'})
        log=self.fake_ollama(["Type 'test' into the field and press Enter.","The task is complete."],openai_goals=['Type test in the field'])
        agent=self.run_agent('Fill the field','openai')
        self.assertEqual(agent['status'],'completed',agent)
        self.assertEqual([r['kind'] for r in self.app.desktop.lab_actions[-3:]],['click','type','key'])
        self.assertEqual(self.app.desktop.lab_actions[-1]['key'],'Return')
        u=self.app.ledger.summary(agent['run_id'])
        self.assertEqual(u['by_provider']['openai']['calls'],1)
        self.assertEqual(u['by_provider']['local']['calls'],3)  # step, locate, step
        self.assertEqual(u['run']['total_tokens'],1100+3*570)
        self.assertIn('Current step: Type test in the field',log['think'][0])

    def test_local_step_types_into_located_field(self):
        log=self.fake_ollama(["Type 'Paris' into the destination field.","Press Enter.","The task is complete."],where=(200,300))
        agent=self.run_agent('Type Paris and press Enter')
        self.assertEqual(agent['status'],'completed',agent)
        rows=[r for r in self.app.desktop.lab_actions[-3:]]
        self.assertEqual([a['kind'] for a in rows],['click','type','key'])
        self.assertEqual((rows[0]['x'],rows[0]['y'],rows[1]['text'],rows[2]['key']),(200,300,'Paris','Return'))
        self.assertIn('destination field',log['locate'][0])
        self.assertIn('typed "Paris"',log['think'][1])

    def test_address_bar_uses_shortcut_without_locating(self):
        log=self.fake_ollama(["Press Ctrl + T to open a new tab.","Type 'news' in the address bar and press Enter.","The task is complete."])
        agent=self.run_agent('Open a new tab and search for news')
        self.assertEqual(agent['status'],'completed',agent)
        acts=[(r['kind'],r.get('key') or r.get('text')) for r in self.app.desktop.lab_actions[-4:]]
        self.assertEqual(acts,[('key','ctrl+t'),('key','ctrl+l'),('type','news'),('key','Return')])
        self.assertEqual(log['locate'],[])

    def test_unparseable_step_falls_back_to_schema(self):
        log=self.fake_ollama(["Hmm, the screen is interesting.","The task is complete."],
                             fallback={'screen':'s','next':'n','kind':'click','x':640,'y':400,'text':'','key':'','amount':0})
        agent=self.run_agent('Click the middle')
        self.assertEqual(agent['status'],'completed',agent)
        self.assertEqual(len(log['schema']),1)
        self.assertEqual(self.app.desktop.lab_actions[-1]['x'],640)

    def test_history_uses_model_screenshot_coordinates(self):
        self.app.settings.update({'local_image_width':960})
        log=self.fake_ollama(["Click the OK button.","I am stuck."],where=(480,300))
        self.run_agent('Click OK')
        self.assertEqual(self.app.desktop.lab_actions[-1]['x'],640)  # executed in desktop pixels
        self.assertIn('click at 480,300',log['think'][1])  # shown back in the 960-wide screenshot space

    def test_local_done_before_any_action_is_rejected(self):
        log=self.fake_ollama(["The task is complete.","Click the OK button.","The task is complete."])
        agent=self.run_agent('Click OK')
        self.assertEqual(agent['status'],'completed',agent)
        self.assertEqual(agent['steps'],1)
        self.assertIn('NOT done',log['think'][1])

    def test_identical_repeated_step_is_skipped(self):
        log=self.fake_ollama(["Type 'Paris' into the city field.","Type 'Paris' into the city field.","The task is complete."],where=(200,300))
        agent=self.run_agent('Type Paris')
        self.assertEqual(agent['status'],'completed',agent)
        self.assertEqual(agent['steps'],2)  # click + type once, not twice
        self.assertIn('Choose the NEXT step',log['think'][2])

    def test_qwen3_points_on_a_0_to_1000_grid_are_converted_to_pixels(self):
        self.app.settings.update({'local_model':'qwen3-vl:8b','local_image_width':960})
        log=self.fake_ollama(["<think>The field is at the top.</think>Type 'Paris' into the destination field.","Press Enter.","The task is complete."],where=(250,500),
                             fallback=None)
        agent=self.run_agent('Type Paris')
        self.assertEqual(agent['status'],'completed',agent)
        first=self.app.desktop.lab_actions[-3]
        self.assertEqual((first['kind'],first['x'],first['y']),('click',320,400))  # 250/1000*1280, 500/1000*800
        self.assertIn('0-1000 scale',log['locate'][0])

    def test_qwen3_gets_the_full_screen_and_fallback_points_are_converted(self):
        self.app.settings.update({'local_model':'qwen3-vl:4b','local_image_width':960})
        sizes=[]
        fast=None
        log=self.fake_ollama(["Hmm.","The task is complete."],
                             fallback={'screen':'s','next':'n','kind':'click','x':500,'y':500,'text':'','key':'','amount':0})
        inner=self.app.client.transport
        def spy(url,payload,headers,timeout):
            import base64
            sizes.append(Image.open(io.BytesIO(base64.b64decode(payload['messages'][0]['images'][0]))).size)
            return inner(url,payload,headers,timeout)
        self.app.client.transport=spy
        self.run_agent('Click the middle')
        self.assertEqual(self.app.desktop.lab_actions[-1]['x'],640);self.assertEqual(self.app.desktop.lab_actions[-1]['y'],400)
        self.assertTrue(all(sz==(1280,800) for sz in sizes),sizes)
        self.assertIn('0-1000 scale',log['schema'][0])

    def test_local_tokens_do_not_count_toward_cloud_token_cap(self):
        self.app.settings.update({'max_run_tokens':1000})
        self.fake_ollama(["Press Tab.","Press Enter.","Press Escape.","The task is complete."])
        agent=self.run_agent('Press three keys')
        self.assertEqual(agent['status'],'completed',agent)
        self.assertEqual(agent['steps'],3)

    def test_cloud_goal_done_with_actions_runs_actions_then_rechecks(self):
        self.app.settings.update({'anthropic_key':'fixture'})
        calls=[]
        def transport(url,payload,headers,timeout):
            calls.append(payload)
            body={'goal_done':True,'actions':[{'kind':'click','x':100,'y':100}]} if len(calls)==1 else {'goal_done':True,'actions':[]}
            return {'usage':{'input_tokens':900,'output_tokens':40},'content':[{'type':'text','text':json.dumps(body)}]}
        self.app.client.transport=transport
        agent=self.run_agent('Click it','anthropic_direct')
        self.assertEqual(agent['status'],'completed',agent)
        self.assertEqual(len(calls),2);self.assertEqual(agent['steps'],1)

    def test_done_claim_without_visible_change_is_rejected(self):
        self.app.settings.update({'anthropic_key':'fixture'})
        calls=[]
        def transport(url,payload,headers,timeout):
            calls.append(payload)
            body={'actions':[{'kind':'wait','seconds':0.1}]} if len(calls)==1 else {'goal_done':True,'actions':[]}
            return {'usage':{'input_tokens':900,'output_tokens':40},'content':[{'type':'text','text':json.dumps(body)}]}
        self.app.client.transport=transport
        # With "ask me when it gets stuck" on: instead of giving up, it eventually asks the user to have a look.
        self.app.settings.update({'ask_when_stuck':True})
        self.app.start_agent('Do something visible','anthropic_direct')
        await_done(lambda:self.app.agent['status']=='waiting',10)
        self.assertTrue(any('screen has not changed' in e['message'] for e in self.app.engine.events))
        self.assertIn('screen has not changed',self.app.agent['waiting']['reason'])
        self.app.agent_cancel.set();await_done(lambda:self.app.agent['status']=='stopped',5)

    def test_claude_direct_controls_desktop_with_scaled_clicks(self):
        self.app.settings.update({'anthropic_key':'fixture'})
        calls=[]
        def transport(url,payload,headers,timeout):
            self.assertIn('anthropic',url);calls.append(payload)
            prompt='\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text')
            self.assertIn('1280x800',prompt)
            body={'actions':[{'kind':'click','x':640,'y':400,'label':'centre'}]} if len(calls)==1 else {'goal_done':True,'actions':[]}
            return {'usage':{'input_tokens':900,'output_tokens':40},'content':[{'type':'text','text':'Here you go: '+json.dumps(body)}]}
        self.app.client.transport=transport
        self.app.start_agent('Click the middle','anthropic_direct')
        await_done(lambda:self.app.agent['status']!='running')
        self.assertEqual(self.app.agent['status'],'completed',self.app.agent)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.app.desktop.lab_actions[-1]['x'],640)
        u=self.app.ledger.summary(self.app.agent['run_id'])
        self.assertEqual(u['by_provider']['local']['calls'],0)


    def test_unready_local_worker_blocks_cloud_spend(self):
        from types import SimpleNamespace
        self.app.settings.update({'openai_key':'fixture'})
        self.app.local_models=SimpleNamespace(public=lambda:{'status':'downloading'},close=lambda:None)
        with self.assertRaisesRegex(ValueError,'no cloud call'):
            self.app.start_agent('test','openai')
        self.assertEqual(self.app.ledger.summary()['today']['calls'],0)

    def test_cancellation_after_cloud_response_prevents_execution(self):
        self.app.settings.update({'openai_key':'fixture'})
        entered=threading.Event();release=threading.Event()
        def transport(*args):
            entered.set();release.wait(2)
            return {'usage':{'input_tokens':10,'output_tokens':10},'output':[{'type':'message','content':[{'type':'output_text','text':'{"goals":["test"]}'}]}]}
        self.app.client.transport=transport
        self.app.start_agent('test','openai');self.assertTrue(entered.wait(1))
        self.app.agent_cancel.set();release.set()
        await_done(lambda:self.app.agent['status']!='running')
        self.assertEqual(self.app.agent['status'],'stopped');self.assertEqual(self.app.desktop.lab_step,0)
        self.assertEqual(self.app.ledger.summary()['today']['total_tokens'],20)

class UpgradeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=Application(self.tmp.name,lab=True)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_factory(self.app))
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def tearDown(self):self.app.close();self.server.shutdown();self.server.server_close();self.tmp.cleanup()
    def get(self,path):return urlopen(self.base+path,timeout=3)

    def test_mjpeg_stream_and_lightweight_state(self):
        with self.get('/api/stream') as r:
            self.assertIn('multipart/x-mixed-replace',r.headers['Content-Type'])
            self.assertEqual(r.readline().strip(),b'--screenpilot')
            self.assertIn(b'image/jpeg',r.readline())
        with self.get('/api/state?details=0') as r:s=json.load(r)
        self.assertNotIn('transitions',s);self.assertIn('performance',s)
        with self.get('/api/settings') as r:self.assertIn('keys_configured',json.load(r))


class StepParserTests(unittest.TestCase):
    """Sentences recorded from qwen2.5vl:3b on the practice desktop."""
    def test_real_model_sentences(self):
        from app.localexec import parse_step
        cases={
            "Press Ctrl + T to open a new tab.":[('key','ctrl+t')],
            "Type 'news' in the address bar and press Enter.":[('type','news','address bar'),('key','Return')],
            "Type 'Paris' into the destination field and click 'Search routes'.":[('type','Paris','destination field'),('click','Search routes')],
            "Click on the address bar and type 'DuckDuckGo Israeli news'.":[('click','address bar'),('type','DuckDuckGo Israeli news',None)],
            "Click on the 'Return trip' checkbox.":[('click','Return trip checkbox')],
            "The task is complete.":[('done',)],
            "Go to duckduckgo.com":[('key','ctrl+l'),('type','duckduckgo.com',None),('key','Return')],
            "Scroll down to see more news.":[('scroll',3)],
            "Press the Enter key to search.":[('key','Return')],
            "Open a new tab.":[('key','ctrl+t')],
            "Hmm, the screen is interesting.":[],
        }
        for sentence,expected in cases.items():
            self.assertEqual(parse_step(sentence),expected,sentence)
