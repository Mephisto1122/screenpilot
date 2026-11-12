"""Provider adapters, redacted settings and persisted token/cost accounting."""
import base64
import contextlib
import copy
import io
import json
import math
import re
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.activity import ACTIVITY
from app.core import KINDS, atomic_json

# Standard API rate snapshots (USD / 1M tokens), verified 2026-11-11.
# Editable: provider billing remains authoritative. Unknown model IDs need explicit rates.
PRESETS = {
    'gpt-6-luna': {'input': .10, 'cached': .01, 'output': .50, 'cache_write': .125},
    # GPT-6.1 Terra (26 Oct 2026) is OpenAI's model for computer use and visual reasoning. Its cache-write price is
    # not published where the others are; 1.25x input is assumed, as for its siblings.
    'gpt-6.1-terra': {'input': 1., 'cached': .05, 'output': 5., 'cache_write': 1.25},
    'gpt-6.1-sol': {'input': 2., 'cached': .10, 'output': 10., 'cache_write': 2.5},
    'gpt-6-astra': {'input': 10., 'cached': 1., 'output': 50., 'cache_write': 12.5},
    'gpt-5.4-mini': {'input': .75, 'cached': .075, 'output': 4.5, 'cache_write': .75},
    'gpt-5-mini': {'input': .25, 'cached': .025, 'output': 2., 'cache_write': .25},
    'claude-sonnet-5-5': {'input': 2., 'cached': .10, 'output': 10., 'cache_write': 2.5},
    'claude-haiku-5-5': {'input': .10, 'cached': .01, 'output': .50, 'cache_write': .125},
    'claude-sonnet-4-6': {'input': 3., 'cached': .30, 'output': 15., 'cache_write': 3.75},
}
DEFAULTS = {'planner':'private','cloud_provider':'auto','plan_contract':True,'ask_when_stuck':False,'thrifty':False,'openai_fast_model':'gpt-6-luna','openai_model':'gpt-6-luna','anthropic_model':'claude-sonnet-5-5','fast_model':'claude-haiku-5-5',
            'local_model':'qwen3-vl:8b','uitars_model':'hf.co/Mungert/UI-TARS-1.5-7B-GGUF:Q4_K_M','uitars_coords':'normalized',
            'holo_model':'ahmadwaqar/holo-3.1:4b','use_holo':False,
            'max_steps':0,'batch_actions':1,'local_output_tokens':256,
            'local_image_width':1280,'local_context_tokens':6144,'local_timeout_seconds':600,
            'planner_output_tokens':1536,'max_cloud_calls':0,'max_run_tokens':0,
            'run_budget_usd':0.,'daily_budget_usd':0.,'replan_after':3,
            'cloud_image_detail':'low','cloud_image_width':1024,'mac_eyes':True,
            'rates':{}, 'pricing_as_of':'2026-11-11','settings_version':8}
# For every limit, 0 means no limit: tasks run until they are done or the user presses Stop.
# What the user chooses: three modes. Each becomes one of the exact set-ups below when a task starts,
# from what is installed in Ollama and which API key is set.
MODES=('private','smart','fast')
EXACT=('local','openai','anthropic','openai_direct','anthropic_direct','anthropic_text','openai_text','uitars','anthropic_uitars')
PLANNERS=MODES+EXACT
CLOUD_LABEL={'anthropic':'Claude','openai':'GPT'}
CLOUD_COMPANY={'anthropic':'Anthropic','openai':'OpenAI'}


def cloud_provider(cfg,keys):
    """The cloud model to use: the user's choice, or whichever has a key (Claude first)."""
    choice=cfg.get('cloud_provider','auto')
    if choice in CLOUD_LABEL:return choice
    return 'anthropic' if keys.get('anthropic') or not keys.get('openai') else 'openai'


def resolve_mode(mode,cfg,keys,installed=None):
    """A mode -> the settings a task runs with. installed(model) says whether Ollama has a model (None: unknown).
    Private: everything on this Mac, with UI-TARS when it is installed. Smart: a cloud model decides from a text
    description, this Mac looks and clicks. Fast: a cloud model sees the screenshots. Holo sharpens the clicking
    whenever it is installed."""
    has=lambda model:bool(installed and model and installed(model))
    out={'use_holo':bool(cfg.get('use_holo')) or has(cfg.get('holo_model'))}
    if mode=='private':out['planner']='uitars' if has(cfg.get('uitars_model')) else 'local'
    elif mode in ('smart','fast'):out['planner']=cloud_provider(cfg,keys)+('_text' if mode=='smart' else '_direct')
    return out


def describe_modes(cfg,keys,installed=None):
    """For the dashboard: what each mode will use right now, where the screenshots go, and what is missing."""
    out={}
    for mode in MODES:
        r=resolve_mode(mode,cfg,keys,installed);planner=r['planner'];holo=' Holo does the clicking.' if r['use_holo'] else ''
        if mode=='private':
            uitars=planner=='uitars'
            out[mode]={'planner':planner,'ready':True,'leaves':False,
                       'privacy':('UI-TARS on this Mac.' if uitars else 'On this Mac.'+holo)+' Nothing leaves it.',
                       'hint':'' if uitars else 'For harder tasks install UI-TARS: UITARS=1 ./run.sh'}
        else:
            p=planner.split('_')[0];ready=bool(keys.get(p))
            out[mode]={'planner':planner,'ready':ready,'leaves':mode=='fast',
                       'privacy':(f'{CLOUD_LABEL[p]} decides from a text description; screenshots stay on this Mac.'+holo) if mode=='smart'
                                 else f'Screenshots are sent to {CLOUD_COMPANY[p]}.',
                       'hint':'' if ready else f'Add a {CLOUD_LABEL[p]} API key in Settings → Models'}
    return out

def planner_provider(planner):
    """Provider that writes the plan (None for local) and provider that picks the actions."""
    if planner in MODES:raise ValueError('Resolve the mode first (resolve_mode)')
    if planner=='uitars':return None,'uitars'                 # UI-TARS plans and acts, all on the Mac
    if planner=='anthropic_uitars':return 'anthropic','uitars'  # Claude plans, UI-TARS acts on the Mac
    if planner.endswith('_direct'):return None,planner[:-len('_direct')]
    if planner.endswith('_text'):return None,planner[:-len('_text')]  # cloud model, but sees only a text description
    return (None if planner=='local' else planner),'local'


def ollama_base():
    """Ollama endpoint. Default is native Ollama on this machine; Compose sets host.docker.internal."""
    return os.getenv('OLLAMA_URL','http://localhost:11434').rstrip('/')

class Settings:
    def __init__(self, root):
        self.path=root/'settings.json'
        self.lock=threading.RLock()
        self.value=copy.deepcopy(DEFAULTS)
        self.value.update(local_model=os.getenv('OLLAMA_MODEL','qwen3-vl:8b'),
                          uitars_model=os.getenv('UITARS_MODEL',DEFAULTS['uitars_model']),
                          openai_model=os.getenv('OPENAI_MODEL',DEFAULTS['openai_model']),
                          anthropic_model=os.getenv('ANTHROPIC_MODEL','claude-sonnet-5-5'))
        if self.path.exists():
            saved=json.loads(self.path.read_text())
            if saved.get('settings_version',1)<2:
                # 0.3.1: one action per local response. Small models batch in the wrong order
                # (type before click); native GPU inference makes extra local calls cheap.
                saved['batch_actions']=1;saved['settings_version']=2
            if saved.get('settings_version',1)<3:
                # 0.3.3: the local vision model places clicks accurately only at full resolution.
                saved['local_image_width']=1280
                saved['local_context_tokens']=max(6144,saved.get('local_context_tokens',0))
                saved['settings_version']=3
            if saved.get('settings_version',1)<4:
                # 0.5: longer, multi-step goals. Only the old default is raised; custom values are kept.
                if saved.get('max_steps')==30:saved['max_steps']=60
                saved['settings_version']=4
            if saved.get('settings_version',1)<5:
                # 0.6: long-running tasks. Every limit is off unless the user sets one again.
                for key in ('max_steps','max_cloud_calls','max_run_tokens','run_budget_usd','daily_budget_usd'):saved[key]=0
                saved['settings_version']=5
            if saved.get('settings_version',1)<6:
                # 0.7: Qwen3-VL replaces the old 3B default (unless the user picked another model).
                if saved.get('local_model')=='qwen2.5vl:3b':saved['local_model']=os.getenv('OLLAMA_MODEL','qwen3-vl:8b')
                saved['settings_version']=6
            if saved.get('settings_version',1)<7:
                # 0.18.1: GPT-6 Luna replaces GPT-5.4 mini as the GPT model: same job, about 7x cheaper.
                if saved.get('openai_model')=='gpt-5.4-mini':saved['openai_model']=DEFAULTS['openai_model']
                saved['settings_version']=7
            if saved.get('settings_version',1)<8:
                # 0.20: three modes. The old default (the general local model) becomes Private, which uses UI-TARS
                # when it is installed. An exact set-up the user chose is kept.
                if saved.get('planner')=='local':saved['planner']='private'
                saved['settings_version']=8
            self.value.update(saved)
        # The model run.sh chose for this Mac's memory (or the one set in .env) wins over an older saved choice.
        if os.getenv('OLLAMA_MODEL'):self.value['local_model']=os.environ['OLLAMA_MODEL']
        if os.getenv('UITARS_MODEL'):self.value['uitars_model']=os.environ['UITARS_MODEL']
        if os.getenv('HOLO_MODEL'):self.value['holo_model']=os.environ['HOLO_MODEL']
        if os.getenv('SCREENPILOT_HOLO')=='1':self.value['use_holo']=True
        if os.getenv('SCREENPILOT_PLANNER') in PLANNERS:self.value['planner']=os.environ['SCREENPILOT_PLANNER']
        self.keys={'openai':os.getenv('OPENAI_API_KEY',''),'anthropic':os.getenv('ANTHROPIC_API_KEY','')}

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.value)

    def public(self):
        v=self.snapshot()
        with self.lock:v['keys_configured']={k:bool(value) for k,value in self.keys.items()}
        v['pricing_presets']=copy.deepcopy(PRESETS)
        return v

    def update(self, data):
        with self.lock:
            value=copy.deepcopy(self.value)
            for key in ('planner','openai_model','anthropic_model','fast_model','local_model','uitars_model','holo_model','cloud_image_detail'):
                if key in data:value[key]=str(data[key]).strip()
            if 'use_holo' in data:value['use_holo']=bool(data['use_holo'])
            if 'plan_contract' in data:value['plan_contract']=bool(data['plan_contract'])  # plan first, and check every step
            if 'thrifty' in data:value['thrifty']=bool(data['thrifty'])  # the cheap model takes the routine steps (GPT)
            if 'ask_when_stuck' in data:value['ask_when_stuck']=bool(data['ask_when_stuck'])  # off: it finds another way by itself
            if 'cloud_provider' in data:
                value['cloud_provider']=str(data['cloud_provider']).strip()
                if value['cloud_provider'] not in ('auto','anthropic','openai'):raise ValueError('cloud_provider must be auto, anthropic or openai')
            if 'uitars_coords' in data:
                value['uitars_coords']=str(data['uitars_coords']).strip()
                if value['uitars_coords'] not in ('normalized','pixels'):raise ValueError('uitars_coords must be normalized or pixels')
            if 'mac_eyes' in data:value['mac_eyes']=bool(data['mac_eyes'])  # text only: let this computer's vision model look too
            if value['planner'] not in PLANNERS:raise ValueError('Unknown planner')
            if value['cloud_image_detail'] not in ('low','auto','high'):raise ValueError('Unknown image detail')
            for k in ('openai_model','anthropic_model','fast_model','local_model','uitars_model','holo_model'):
                if not 1 <= len(value[k]) <= 120:raise ValueError('Invalid model name')
            bounds={'max_steps':(0,100000),'batch_actions':(1,3),'local_output_tokens':(128,1024),
                    'planner_output_tokens':(256,4096),'max_cloud_calls':(0,1000000),'max_run_tokens':(0,10**10),
                    'replan_after':(2,10),'cloud_image_width':(640,1280),
                    'local_image_width':(640,1280),'local_context_tokens':(2048,16384),'local_timeout_seconds':(30,1800)}
            for k,(lo,hi) in bounds.items():
                if k in data:
                    v=int(data[k])
                    if not lo<=v<=hi:raise ValueError(f'{k} must be between {lo} and {hi}')
                    value[k]=v
            for k in ('run_budget_usd','daily_budget_usd'):
                if k in data:
                    v=float(data[k])
                    if not math.isfinite(v) or not 0 <= v <= 100000:raise ValueError('Budget must be 0 (no limit) or a positive amount')
                    value[k]=v
            if 'rates' in data:
                rates=data['rates']
                if not isinstance(rates,dict):raise ValueError('Rates must be a model-to-price map')
                for model,rate in rates.items():
                    if not isinstance(rate,dict) or not set(('input','cached','output','cache_write'))<=set(rate):
                        raise ValueError('Each rate needs input, cached, output and cache_write prices')
                    for n in rate.values():
                        if not isinstance(n,(int,float)) or not math.isfinite(n) or not 0<=n<=1000:raise ValueError('Invalid token price')
                    if len(model)>120:raise ValueError('Invalid pricing model name')
                value['rates']=copy.deepcopy(rates)
            for provider in ('openai','anthropic'):
                key=str(data.get(provider+'_key','')).strip()
                if key:
                    if len(key)>1000:raise ValueError('Key is too long')
                    self.keys[provider]=key
                if data.get('clear_'+provider+'_key'):self.keys[provider]=''
            atomic_json(self.path,value)  # Keys are intentionally never serialized.
            self.value=value
        return self.public()

    def key(self, provider):
        with self.lock:return self.keys.get(provider,'')


def token_usage(provider, result):
    """Normalize actual provider-returned counts. Never estimate missing counts as zero."""
    if provider=='local':
        if 'prompt_eval_count' not in result or 'eval_count' not in result:return None
        inp,out=int(result['prompt_eval_count']),int(result['eval_count'])
        return {'input':inp,'output':out,'cached':min(inp,int(result.get('prompt_eval_cached_count',0))),
                'cache_write':0,'reasoning':0,'total':inp+out}
    u=result.get('usage')
    if not isinstance(u,dict) or 'input_tokens' not in u or 'output_tokens' not in u:return None
    if provider=='anthropic':
        uncached=int(u['input_tokens']);cached=int(u.get('cache_read_input_tokens',0));writes=int(u.get('cache_creation_input_tokens',0))
        inp=uncached+cached+writes
        reasoning=int(u.get('output_tokens_details',{}).get('thinking_tokens',0))
    else:
        inp=int(u['input_tokens']);cached=int(u.get('input_tokens_details',{}).get('cached_tokens',0))
        writes=int(u.get('input_tokens_details',{}).get('cache_write_tokens',0))
        reasoning=int(u.get('output_tokens_details',{}).get('reasoning_tokens',0))
    out=int(u['output_tokens'])
    return {'input':inp,'output':out,'cached':cached,'cache_write':writes,'reasoning':reasoning,'total':inp+out}


def cost_for(usage, rate):
    if usage is None or rate is None:return None
    uncached=max(0,usage['input']-usage['cached']-usage['cache_write'])
    return (uncached*rate['input']+usage['cached']*rate['cached']+usage['cache_write']*rate['cache_write']+
            usage['output']*rate['output'])/1_000_000


class Ledger:
    def __init__(self, root):
        self.path=root/'usage.sqlite3'
        self.lock=threading.RLock()
        with self.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, at REAL, day TEXT, run TEXT, provider TEXT, model TEXT, purpose TEXT,
                status TEXT, input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                cache_write_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER,
                cost_usd REAL, reserve_usd REAL, latency_ms REAL, error TEXT, rates_json TEXT)''')
            for column in ('at','day','run'):db.execute(f'CREATE INDEX IF NOT EXISTS calls_{column} ON calls ({column})')
            db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
            # The running count: every call since the user last reset it, across tasks, workers and restarts.
            row=db.execute("SELECT value FROM meta WHERE key='counting_since'").fetchone()
            self.started=float(row['value']) if row else time.time()
            if not row:db.execute("INSERT INTO meta VALUES ('counting_since',?)",(str(self.started),))

    @contextlib.contextmanager
    def connection(self):
        """One short transaction: committed (or rolled back on an error) and always closed."""
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def reset(self):
        """Start the running count from zero (the calls themselves are kept)."""
        with self.lock,self.connection() as db:
            self.started=time.time()
            db.execute("INSERT OR REPLACE INTO meta VALUES ('counting_since',?)",(str(self.started),))

    def begin(self,run,provider,model,purpose,settings):
        rate=settings.get('rates',{}).get(model,PRESETS.get(model)) if provider!='local' else None
        reserve=0
        if provider!='local':
            if rate is None:raise ValueError('Set prices for this custom cloud model before using it')
            # Conservative reservation for a bounded prompt and one reduced screenshot.
            # Not an invoice guarantee; missing/ambiguous usage retains the reservation.
            reserve=(24000*max(rate['input'],rate['cached'],rate['cache_write'])+
                     settings['planner_output_tokens']*rate['output'])/1_000_000
        now=time.time();day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        uid=uuid.uuid4().hex
        with self.lock,self.connection() as db:
            if provider!='local':
                row=db.execute("SELECT count(*) n,coalesce(sum(coalesce(cost_usd,reserve_usd)),0) cost FROM calls WHERE run=? AND provider!='local'",(run,)).fetchone()
                daily=db.execute("SELECT coalesce(sum(coalesce(cost_usd,reserve_usd)),0) cost FROM calls WHERE day=? AND provider!='local'",(day,)).fetchone()
                # When the cloud model drives the desktop directly, each step is one call.
                # Each limit applies only when it is set (0 = no limit).
                if settings['max_cloud_calls']:
                    limit=settings['max_cloud_calls']+(settings['max_steps'] if purpose=='execute' else 0)
                    if row['n']>=limit:raise ValueError('Cloud call limit reached for this task')
                if settings['run_budget_usd'] and row['cost']+reserve>settings['run_budget_usd']+1e-12:raise ValueError('The budget per task is used up (Settings → Models)')
                if settings['daily_budget_usd'] and daily['cost']+reserve>settings['daily_budget_usd']+1e-12:raise ValueError("Today's budget is used up (Settings → Models)")
            # The token cap guards paid usage. Local inference is free and already bounded by max_steps.
            if provider!='local':
                total=db.execute("SELECT coalesce(sum(total_tokens),0) total FROM calls WHERE run=? AND provider!='local'",(run,)).fetchone()['total']
                if settings['max_run_tokens'] and total>=settings['max_run_tokens']:raise ValueError('Run token limit reached for cloud calls')
            db.execute('INSERT INTO calls (id,at,day,run,provider,model,purpose,status,reserve_usd,rates_json) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (uid,now,day,run,provider,model,purpose,'pending',reserve,json.dumps(rate)))
        return uid,rate

    def finish(self,uid,usage,rate,latency_ms,error=None):
        amount=0.0 if rate is None and usage is not None else cost_for(usage,rate)
        with self.lock,self.connection() as db:
            db.execute('''UPDATE calls SET status=?,input_tokens=?,output_tokens=?,cached_tokens=?,cache_write_tokens=?,
                       reasoning_tokens=?,total_tokens=?,cost_usd=?,latency_ms=?,error=? WHERE id=?''',
                       ('failed' if error else ('completed' if usage is not None else 'usage_unknown'),
                        *([usage[k] if usage is not None else None for k in ('input','output','cached','cache_write','reasoning','total')]),
                        amount,round(latency_ms,2),error,uid))

    def summary(self,run=None):
        day=datetime.now(timezone.utc).strftime('%Y-%m-%d')
        with self.lock,self.connection() as db:
            def summarize(where='',args=()):
                row=db.execute('''SELECT count(*) calls,coalesce(sum(input_tokens),0) input_tokens,
                      coalesce(sum(output_tokens),0) output_tokens,coalesce(sum(cached_tokens),0) cached_tokens,
                      coalesce(sum(cache_write_tokens),0) cache_write_tokens,coalesce(sum(reasoning_tokens),0) reasoning_tokens,
                      coalesce(sum(total_tokens),0) total_tokens,coalesce(sum(cost_usd),0) cost_usd,
                      coalesce(sum(CASE WHEN cost_usd IS NULL THEN reserve_usd ELSE 0 END),0) reserved_usd,
                      sum(CASE WHEN status IN ('usage_unknown','failed','pending') THEN 1 ELSE 0 END) unresolved_calls
                      FROM calls '''+where,args).fetchone()
                return dict(row)
            recent=[dict(r) for r in db.execute('SELECT id,at,run,provider,model,purpose,status,input_tokens,output_tokens,cached_tokens,cache_write_tokens,reasoning_tokens,total_tokens,cost_usd,reserve_usd,latency_ms,error FROM calls ORDER BY at DESC LIMIT 30')]
            session={'since':self.started,**summarize('WHERE at>=?',(self.started,)),
                     'by_provider':{p:summarize('WHERE at>=? AND provider=?',(self.started,p)) for p in ('local','openai','anthropic')}}
            return {'session':session,'today':summarize('WHERE day=?',(day,)), 'run':summarize('WHERE run=?',(run,)) if run else None,
                    'by_provider':{p:summarize('WHERE day=? AND provider=?',(day,p)) for p in ('local','openai','anthropic')},
                    'recent':recent,'cost_note':'Estimated USD from actual provider token counts and editable rates. Local API cost excludes hardware and electricity. Unknown usage retains a budget reservation.'}


# In a prompt: everything before this mark is the same at every step of a task (the instructions), so the
# provider can keep it and charge a fraction for it. Claude needs the mark; GPT caches a repeated start by itself.
CACHE_BREAK='\n<<same-every-step-above>>\n'


# How long a prompt may be. A cloud model reads far more than any step needs; a small local model has a short memory.
CLOUD_PROMPT_CHARS=80000
LOCAL_PROMPT_CHARS=14000


def fit_prompt(prompt,limit):
    """A prompt that is too long loses its MIDDLE, never its end, and it is logged. The end holds what the model
    needs most for the next step (the task, its notebook, its last actions, the screen); cutting it off, as an
    earlier version did silently, left the model with no memory of what it had just done."""
    if len(prompt)<=limit:return prompt
    gap='\n[... a part in the middle was left out because the prompt was too long ...]\n'
    head=limit*2//5;tail=limit-head-len(gap)
    ACTIVITY.write('model',f'A prompt of {len(prompt)} characters was shortened to {limit}: its middle was left out','warning')
    return prompt[:head]+gap+prompt[-tail:]


class ModelMissing(RuntimeError):
    """Ollama does not have this model installed. An outcome to handle, not a crash: logged without a traceback."""
    expected=True


class ThinkOptionRejected(Exception):
    """Ollama refused the 'think' option for this model."""


class Unreachable(RuntimeError):
    """The cloud model could not be reached, timed out or is overloaded right now: worth trying again."""


RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}  # busy or temporarily down
IMPATIENT_SECONDS = 30  # how long the first try of a request that is not streamed may take before it is asked again
RETRY_WAITS = (3, 10, 30, 60)  # seconds between tries of a cloud request that did not get through
CLOUD_NAMES = {'anthropic': 'Claude (Anthropic)', 'openai': 'GPT (OpenAI)'}


class ReplyToolRejected(Exception):
    """The provider refused a request that forces the reply tool (HTTP 400)."""


class CacheMarksRejected(Exception):
    """OpenAI refused the fields that say what to cache (HTTP 400): the request is sent again without them."""


MARKED_CACHE=re.compile(r'gpt-(5\.[6-9]|[6-9])')  # OpenAI models that take prompt_cache_options and breakpoints (GPT-5.6 and later)
MIN_CACHED_CHARS=4500  # about 1,024 tokens: a shorter beginning cannot be cached, so it is not marked


def without_cache_marks(payload):
    payload.pop('prompt_cache_options',None)
    for message in payload.get('input',[]):
        for block in message.get('content',[]):block.pop('prompt_cache_breakpoint',None)
    return payload


class BadReply(ValueError):
    """The model answered, but not with usable JSON. The agent can tell it and ask again."""


# The reply's structure. Without it the model sometimes fills the reply with junk actions; with it, every
# action must be one of the real kinds. Extra fields (x, y, mark, text, command, ...) stay free.
ACTION_KINDS=[*KINDS,'fill_login','check_email','ask_user','ask_approval','ask_code','remember','say','jot','look','done']
REPLY_TOOL={'name':'reply','description':'Your whole reply: the JSON object described in the instructions. Always answer by using this tool. '
            'Send only the few actions you need now (never more than 8), then stop.',
            'input_schema':{'type':'object','properties':{
                'actions':{'type':'array','description':'The next actions, in order. Usually 1 to 3.',
                           'items':{'type':'object','properties':{'kind':{'type':'string','enum':ACTION_KINDS}},'required':['kind']}},
                'goal_done':{'type':'boolean'},'stuck':{'type':'boolean'},'note':{'type':'string'},
                'plan':{'type':'array','items':{'type':'string'}},'goals':{'type':'array','items':{'type':'string'}},
                # for planning a task and checking a step (contract.py)
                'deliverable':{'type':'string'},'questions':{'type':'array','items':{'type':'string'}},'done':{'type':'boolean'},
                'assumptions':{'type':'array','items':{'type':'string'}},'skills':{'type':'array','items':{'type':'string'}},
                'brief':{'type':'object','properties':{'looking_for':{'type':'string'},**{k:{'type':'array','items':{'type':'string'}} for k in ('signs','reject','where')}}},
                'steps':{'type':'array','items':{'type':'object','properties':{'do':{'type':'string'},'done_when':{'type':'string'}},'required':['do','done_when']}},
                'route':{'type':'string'},'reply':{'type':'string'},  # for the front desk (frontdesk.py)
                'progress':{'type':'string'},'reason':{'type':'string'},'advice':{'type':'string'},'ask_user':{'type':'boolean'}}}}
INVOKE=re.compile(r'<invoke name="([A-Za-z_]+)">(.*?)</invoke>',re.S)
PARAM=re.compile(r'<parameter name="([A-Za-z_]+)">(.*?)</parameter>',re.S)


def invoke_actions(text):
    """Claude sometimes writes its own tool-call markup (<invoke name="click">...) instead of JSON: read it as actions."""
    actions=[]
    for name,body in INVOKE.findall(text):
        a={'kind':name}
        for key,value in PARAM.findall(body):
            value=value.strip()
            try:a[key]=json.loads(value) if re.fullmatch(r'-?\d+(\.\d+)?|true|false|\[.*\]|\{.*\}',value,re.S) else value
            except ValueError:a[key]=value
        actions.append(a)
    return [{'actions':actions}] if actions else []


def parse_object(text):
    """Read the model's JSON reply. Tolerates code fences, prose around the JSON, and several
    objects in a row ({...}{...}, one per action), which are merged into one reply."""
    text=text.strip()
    if text.startswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    decoder=json.JSONDecoder();objects=[];i=0
    while True:
        j=text.find('{',i)
        if j<0:break
        try:obj,end=decoder.raw_decode(text,j)
        except json.JSONDecodeError:i=j+1;continue
        if isinstance(obj,dict):objects.append(obj)
        i=end
    if not objects:objects=invoke_actions(text)
    if not objects:raise BadReply('Model did not return JSON')
    if len(objects)==1:return objects[0]
    merged={}
    for obj in objects:
        if isinstance(obj.get('actions'),list):merged.setdefault('actions',[]).extend(obj['actions'])
        elif obj.get('kind'):merged.setdefault('actions',[]).append(obj)
        for key,value in obj.items():
            if key!='actions' and not (key=='kind' and 'actions' in merged):merged.setdefault(key,value)
    merged.pop('kind',None) if merged.get('actions') else None
    return merged

class ModelClient:
    def __init__(self,settings,ledger,transport=None):
        self.settings,self.ledger=settings,ledger
        self.transport=transport or self._http
        self.no_think_option=set()  # local models whose Ollama rejected the think option
        self.no_reply_tool=set()    # cloud models that refused the forced reply tool: asked for plain JSON instead
        self.no_cache_marks=set()   # OpenAI models that refused the caching fields: asked without them
        self.context=threading.local()

    def _http(self,url,payload,headers,timeout):
        req=Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json',**headers})
        local=url.endswith('/api/chat')
        try:
            with urlopen(req,timeout=timeout) as response:
                if not local and payload.get('stream'):return self._anthropic_stream(response)
                if not (local and payload.get('stream')):return json.load(response)
                chunks=[];thinking=[];last={};started=time.monotonic()
                for line in response:
                    cancel=getattr(self.context,'cancel',None)
                    if cancel is not None and cancel.is_set():raise InterruptedError('Local inference stopped')
                    if not line.strip():continue
                    item=json.loads(line)
                    if item.get('error'):raise RuntimeError('Ollama inference failed; check the Ollama logs (Mac: ~/.ollama/logs/server.log)')
                    message=item.get('message',{})
                    if getattr(self.context,'first',None) is None:self.context.first=time.monotonic()
                    chunks.append(message.get('content',''));thinking.append(message.get('thinking','') or '')
                    callback=getattr(self.context,'progress',None)
                    if callback:callback(sum(map(len,chunks))+sum(map(len,thinking)),time.monotonic()-started)
                    last=item
                    if item.get('done'):
                        return {**last,'message':{'role':'assistant','content':''.join(chunks),'thinking':''.join(thinking)}}
                raise RuntimeError('Ollama stream ended before completion; usage is unknown')
        except HTTPError as exc:
            # Do not surface provider bodies that may echo input or credentials.
            if local and exc.code==400 and 'think' in payload:raise ThinkOptionRejected() from None
            if local and exc.code==404:
                # Ollama says 404 when it does not have the model: say that, and how to get it.
                raise ModelMissing(f'Ollama does not have the model "{payload.get("model","")}". Install it with: ollama pull {payload.get("model","")}') from None
            if local:raise RuntimeError(f'Ollama returned HTTP {exc.code}. Check its runner logs; this is not a cloud API-key error.') from None
            if exc.code in RETRY_STATUS:raise Unreachable(f'busy or temporarily unavailable (HTTP {exc.code})') from None
            # The provider's own reason (its error type and message, not our request) makes a 400 understandable.
            why=''
            try:
                err=json.loads(exc.read(4000).decode('utf-8','replace')).get('error',{})
                why=' '.join(f"{err.get('type','')}: {err.get('message','')}".split())[:300] if isinstance(err,dict) else ''
            except Exception:pass
            if exc.code==400 and payload.get('tools'):raise ReplyToolRejected(why) from None
            if exc.code==400 and 'prompt_cache_options' in payload and (not why or 'cache' in why.lower()):raise CacheMarksRejected(why) from None
            raise RuntimeError(f'Model provider returned HTTP {exc.code}'+(f' ({why})' if why else '')+'; check model access, key and quota') from None
        except (URLError,TimeoutError) as exc:
            timed_out=isinstance(exc,TimeoutError) or isinstance(getattr(exc,'reason',None),TimeoutError)
            if local:
                if timed_out:raise RuntimeError(f'Local inference produced no response data for {timeout}s. Check that Ollama is running natively (GPU), not in Docker, or reduce the local image width. No automatic retry was made.') from None
                raise RuntimeError(f'Cannot connect to Ollama at {ollama_base()}. Open the Ollama app on your Mac (or run: ollama serve) and check OLLAMA_URL.') from None
            raise Unreachable(f'no answer within {timeout:g} s' if timed_out else 'the connection failed') from None

    def _anthropic_stream(self,response):
        """Read Claude's answer as it is written (server-sent events) and return it as one message. The socket
        timeout then limits silence, not the length of an answer: a dead connection is noticed within a minute."""
        text=[];tool=[];usage={};started=time.monotonic();stop=None
        for raw in response:
            cancel=getattr(self.context,'cancel',None)
            if cancel is not None and cancel.is_set():raise InterruptedError('Stopped')
            line=raw.decode('utf-8','replace').strip()
            if not line.startswith('data:'):continue
            try:event=json.loads(line[5:])
            except ValueError:continue
            kind=event.get('type')
            if kind=='message_start':usage.update(event.get('message',{}).get('usage') or {})
            elif kind=='content_block_delta' and event.get('delta',{}).get('type')=='text_delta':
                if getattr(self.context,'first',None) is None:self.context.first=time.monotonic()
                text.append(event['delta'].get('text',''))
                callback=getattr(self.context,'progress',None)
                if callback:callback(sum(map(len,text)),time.monotonic()-started)
            elif kind=='content_block_delta' and event.get('delta',{}).get('type')=='input_json_delta':
                if getattr(self.context,'first',None) is None:self.context.first=time.monotonic()
                tool.append(event['delta'].get('partial_json',''))
                callback=getattr(self.context,'progress',None)
                if callback:callback(sum(map(len,tool)),time.monotonic()-started)
            elif kind=='message_delta':usage.update(event.get('usage') or {});stop=event.get('delta',{}).get('stop_reason')
            elif kind=='error':
                err=event.get('error',{}).get('type','error')
                if err in ('overloaded_error','api_error','rate_limit_error'):raise Unreachable(f'busy or temporarily unavailable ({err})')
                raise RuntimeError(f'Claude returned an error: {err}')
            elif kind=='message_stop':
                content=[{'type':'text','text':''.join(text)}]
                if tool:
                    try:content.append({'type':'tool_use','input':json.loads(''.join(tool))})
                    except ValueError:content=[{'type':'text','text':''.join(text)+''.join(tool)}]  # cut off: parsed as text
                return {'content':content,'usage':usage,'stop_reason':stop}
        raise Unreachable('the answer stopped half way')

    def _send(self,provider,url,payload,headers,timeout,cancel,on_retry):
        """One request. A cloud request that does not get through is tried again a few times, waiting longer
        each time, so a network blip or a busy API does not end a long task. A timed-out try may be billed."""
        if provider=='local':return self.transport(url,payload,headers,timeout)
        # An answer that normally takes a few seconds sometimes hangs for a minute or more at the provider. A request
        # that is not streamed cannot show that it is alive, so the first try gets limited patience; asking again
        # at once is usually answered at normal speed. (Streamed requests notice silence by themselves.)
        patient=payload.get('stream') or timeout<=IMPATIENT_SECONDS
        for pause in (*RETRY_WAITS,None):
            try:
                if patient:return self.transport(url,payload,headers,timeout)
                patient=True
                try:return self.transport(url,payload,headers,IMPATIENT_SECONDS)
                except Unreachable as exc:
                    if 'no answer' not in str(exc):raise
                    name=CLOUD_NAMES.get(provider,provider)
                    ACTIVITY.write('model',f'{name} has not answered in {IMPATIENT_SECONDS} s; asking again','warning')
                    if on_retry:on_retry(f'{name} is slow to answer; asking again')
                    if cancel is not None and cancel.is_set():raise InterruptedError('Stopped')
                    return self.transport(url,payload,headers,timeout)
            except Unreachable as exc:
                name=CLOUD_NAMES.get(provider,provider)
                if pause is None:raise Unreachable(f'Could not reach {name}: {exc}, also after {len(RETRY_WAITS)} more tries. Check the internet connection.') from None
                ACTIVITY.write('model',f'{name}: {exc}. Trying again in {pause} s','warning')
                if on_retry:on_retry(f'{name}: {exc}. Trying again in {pause} s')
                if cancel is not None:
                    if cancel.wait(pause):raise InterruptedError('Stopped')
                else:time.sleep(pause)

    def call(self,provider,prompt,image,run,purpose,settings=None,progress=None,cancel=None,schema=None,raw=False,on_retry=None):
        cfg=settings or self.settings.snapshot()
        cloud=provider!='local'
        model=cfg[provider+'_model'] if cloud else cfg['local_model']
        if not cloud and ('-cloud' in model.lower() or ':cloud' in model.lower()):
            raise ValueError('Choose a locally hosted model for the local worker; Ollama cloud models are not billed as local inference')
        key=self.settings.key(provider)
        if cloud and not key:raise ValueError(f'Configure the {provider} API key in Models & costs')
        if image is None:
            # Text only: the screen is described in words (local eyes); no screenshot leaves this computer.
            if not cloud:raise ValueError('The local model needs a screenshot')
            prompt=fit_prompt(prompt,CLOUD_PROMPT_CHARS);img=None;encoded=None
        else:
            prompt=fit_prompt(prompt,CLOUD_PROMPT_CHARS if cloud else LOCAL_PROMPT_CHARS)
            img=image.copy()
            img.thumbnail((cfg['cloud_image_width'],)*2 if cloud else (cfg['local_image_width'],)*2)
            # The model sees screenshot-pixel coordinates; they are scaled back before execution.
            prompt=prompt.replace('1280x800',f'{img.width}x{img.height}',1)
            b=io.BytesIO();img.save(b,'JPEG',quality=72 if cloud else 80)
            encoded=base64.b64encode(b.getvalue()).decode()
        # The prompt comes in parts: those that stay the same from step to step first (the instructions; then the
        # task), each ended by CACHE_BREAK, and last what changes at every step. The providers charge 5-10% for a
        # beginning they have seen before, so the unchanged parts are marked to be kept.
        *kept,rest=prompt.split(CACHE_BREAK)
        marked=provider=='anthropic' or (provider=='openai' and bool(MARKED_CACHE.match(model)) and model not in self.no_cache_marks)
        if kept and marked:kept=[*kept[:2],'\n'.join(kept[2:])][:3] if len(kept)>3 else kept;prompt=rest
        else:kept=[];prompt=prompt.replace(CACHE_BREAK,'\n')
        fixed=kept
        if provider=='local':
            url=ollama_base()+'/api/chat'
            payload={'model':model,'stream':True,'format':schema or 'json','keep_alive':'15m',
                     'messages':[{'role':'user','content':prompt,'images':[encoded]}],
                     'options':{'temperature':0,'num_predict':cfg['local_output_tokens'],'num_ctx':cfg['local_context_tokens']}}
            # Thinking models (Qwen3-VL) otherwise spend the whole answer budget on hidden reasoning and
            # return nothing. Screen control needs short direct answers, so switch thinking off.
            if model not in self.no_think_option:payload['think']=False
            headers={}
        elif provider=='openai':
            url='https://api.openai.com/v1/responses'
            blocks=[];seen=0
            for part in fixed:
                seen+=len(part);block={'type':'input_text','text':part}
                if seen>=MIN_CACHED_CHARS:block['prompt_cache_breakpoint']={'mode':'explicit'}
                blocks.append(block)
            payload={'model':model,'store':False,'max_output_tokens':cfg['planner_output_tokens'],
                     'input':[{'role':'user','content':blocks+[{'type':'input_text','text':prompt}]+([] if encoded is None else
                               [{'type':'input_image','image_url':'data:image/jpeg;base64,'+encoded,'detail':cfg['cloud_image_detail']}])}],
                     'text':{'format':{'type':'json_object'}}}
            if marked:
                # GPT-5.6 and later: left alone, OpenAI stores the WHOLE prompt of every request, screenshot included,
                # and charges 1.25x for storing it; since the next step's prompt differs, nothing is ever read back.
                # "explicit" stores only up to the marks above (the parts that do repeat) and nothing when there are none.
                payload['prompt_cache_options']={'mode':'explicit'}
            # GPT-5 and GPT-6 models reason before answering ("medium" unless told otherwise). A screen step needs
            # little of it, and that thinking is paid for and waited for at every step.
            # The few moments that need judgment (the plan, checking a step, the progress check, the report) get more.
            if model.startswith(('gpt-5','gpt-6')):
                routine=purpose in ('execute','locate')
                payload['reasoning']={'effort':'low' if routine or purpose=='chat' else 'medium'}  # a chat reply is quick, but may be long
                # OpenAI counts the model's reasoning against max_output_tokens. Without room for it the thinking
                # uses up the allowance and the answer itself is cut off (a plan once came back as half a JSON).
                # This is a ceiling, not a cost: only tokens actually produced are paid for.
                payload['max_output_tokens']=cfg['planner_output_tokens']+1500 if routine else max(3000,cfg['planner_output_tokens'])+6000
            headers={'Authorization':'Bearer '+key}
        elif provider=='anthropic':
            url='https://api.anthropic.com/v1/messages'
            picture=[] if encoded is None else [{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':encoded}}]
            if fixed:
                # The unchanged parts first, marked to be kept: Claude charges 5-10% for them from the second step on.
                content=[{'type':'text','text':part,'cache_control':{'type':'ephemeral'}} for part in fixed]+picture+[{'type':'text','text':prompt}]
            else:content=picture+[{'type':'text','text':prompt}]
            # A plan, with its brief and steps, is longer than one step's reply: it gets room, like on the OpenAI side.
            payload={'model':model,'max_tokens':max(3000,cfg['planner_output_tokens']) if purpose in ('plan','replan','chat') else cfg['planner_output_tokens'],
                     'messages':[{'role':'user','content':content}]}
            payload['stream']=True  # streamed: silence is noticed quickly, long answers are fine
            if not raw and model not in self.no_reply_tool:
                # Claude replies through a tool it must use, so the reply is always one JSON object (not prose, not
                # its own tool-call markup). The prompt still describes what the object holds.
                payload['tools']=[REPLY_TOOL];payload['tool_choice']={'type':'tool','name':REPLY_TOOL['name']}
            headers={'x-api-key':key,'anthropic-version':'2023-06-01'}
        else:raise ValueError('Unknown provider')
        if raw and provider=='local':payload.pop('format',None)  # free-text answer
        uid,rate=self.ledger.begin(run,provider,model,purpose,cfg)
        start=time.monotonic();result=None
        self.context.progress=progress;self.context.cancel=cancel
        # The activity log shows every model request while it runs and how long it took.
        span=ACTIVITY.span('model',f"{'Ollama' if provider=='local' else CLOUD_NAMES.get(provider,provider)} {model} · {purpose}"+(' · with image' if image is not None else ''))
        info=span.__enter__();failure=None;self.context.first=None
        try:
            # Local: how long Ollama may be silent. Claude streams, so this is the longest silence; OpenAI answers in one piece.
            timeout=cfg['local_timeout_seconds'] if not cloud else 60 if provider=='anthropic' else 180
            try:result=self._send(provider,url,payload,headers,timeout,cancel,on_retry)
            except ReplyToolRejected as exc:
                # Ask again the plain way (JSON as text), and do so for this model from now on.
                self.no_reply_tool.add(model);payload.pop('tools',None);payload.pop('tool_choice',None)
                ACTIVITY.write('model',f'{model} refused the reply tool ({exc or "no reason given"}); asking for plain JSON instead','warning')
                result=self._send(provider,url,payload,headers,timeout,cancel,on_retry)
            except CacheMarksRejected as exc:
                self.no_cache_marks.add(model);without_cache_marks(payload)
                ACTIVITY.write('model',f'{model} did not accept the caching fields ({exc or "no reason given"}); asking without them from now on','warning')
                result=self._send(provider,url,payload,headers,timeout,cancel,on_retry)
            except ThinkOptionRejected:
                # This Ollama or model does not accept the think option: remember that and ask again without it.
                self.no_think_option.add(model);payload.pop('think',None)
                result=self.transport(url,payload,headers,timeout)
            if provider=='openai' and result.get('status')=='incomplete' and (result.get('incomplete_details') or {}).get('reason')=='max_output_tokens':
                # Still cut off: once more with twice the room and less thinking. Both requests are counted.
                ACTIVITY.write('model',f'{model} ran out of room before finishing its answer; asking again with more room','warning')
                payload['max_output_tokens']=payload['max_output_tokens']*2
                if 'reasoning' in payload:payload['reasoning']={'effort':'low'}
                retry=self._send(provider,url,payload,headers,timeout,cancel,on_retry)
                first,second=result.get('usage') or {},retry.get('usage')
                if isinstance(second,dict):
                    for k in ('input_tokens','output_tokens','total_tokens'):
                        if isinstance(first.get(k),int) and isinstance(second.get(k),int):second[k]+=first[k]
                result=retry
            if not cloud and not (result.get('message',{}).get('content') or '').strip():
                # Empty answer (the budget went to thinking): one more try with room for a full answer.
                payload['options']={**payload['options'],'num_predict':max(1024,cfg['local_output_tokens']*4)}
                retry=self.transport(url,payload,headers,timeout)
                for k in ('prompt_eval_count','eval_count'):
                    if k in result and k in retry:retry[k]+=result[k]
                result=retry
            usage=token_usage(provider,result)
            if usage:info['tokens']=f"{usage['input']}→{usage['output']}"
            if usage and usage.get('cached'):info['cached']=usage['cached']  # how much of the input was charged at the low, seen-before rate
            if self.context.first:info['first_words_after_s']=round(self.context.first-start,1)
            # Account before parsing; malformed JSON still costs tokens.
            self.ledger.finish(uid,usage,rate,(time.monotonic()-start)*1000)
        except BaseException as exc:
            failure=exc
            self.ledger.finish(uid,None,rate,(time.monotonic()-start)*1000,'Provider request failed; usage may be unknown')
            raise
        finally:
            self.context.progress=None;self.context.cancel=None
            span.__exit__(*((type(failure),failure,failure.__traceback__) if failure else (None,None,None)))
        if provider=='local':text=result.get('message',{}).get('content','')
        elif provider=='anthropic':
            used=[b for b in result.get('content',[]) if b.get('type')=='tool_use']
            text=json.dumps(used[0].get('input',{})) if used and isinstance(used[0].get('input'),dict) else \
                 '\n'.join(b.get('text','') for b in result.get('content',[]) if b.get('type')=='text')
        else:text='\n'.join(c.get('text','') for o in result.get('output',[]) if o.get('type')=='message'
                            for c in o.get('content',[]) if c.get('type')=='output_text')
        if provider=='local':text=re.sub(r'(?s)<think>.*?</think>','',text).strip()
        if not text:raise BadReply('The model returned an empty answer')
        if raw:return {'text':text,'image_size':img.size if img is not None else None}
        try:parsed=parse_object(text)
        except BadReply as exc:
            exc.reply=' '.join(text.split())[:600]  # what the model said instead (for the log and the user)
            raise
        if img is not None and img.size!=image.size:
            actions=parsed.get('actions',[]) if isinstance(parsed.get('actions',[]),list) else []
            if parsed.get('kind'):actions=actions+[parsed]
            for action in actions:
                if isinstance(action,dict) and action.get('kind') in ('click','double_click','type') and \
                   (action.get('kind')!='type' or isinstance(action.get('x'),(int,float))):
                    for key,old_bound,new_bound in (('x',img.width,image.width),('y',img.height,image.height)):
                        value=action.get(key)
                        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<old_bound:
                            raise ValueError('Model returned coordinates outside its screenshot')
                        action[key]=min(new_bound-1,int(value*new_bound/old_bound))
        return parsed

class LocalModels:
    """Download progress and health polling live off the request/render path."""
    def __init__(self,settings):
        self.settings=settings;self.lock=threading.RLock();self.stop=threading.Event()
        self.value={'status':'checking','message':'Checking Ollama','percent':None,'models':[]}
        self.thread=threading.Thread(target=self._loop,daemon=True,name='ollama-health')
        self.thread.start()

    def _set(self,**change):
        with self.lock:
            before=self.value.get('status');self.value.update(change)
        if change.get('status') and change['status']!=before:  # the log keeps each change of Ollama's state
            ACTIVITY.write('ollama',change.get('message',change['status']),'warning' if change['status'] in ('offline','missing','unsupported') else 'info',worker=None)

    def public(self):
        with self.lock:return copy.deepcopy(self.value)

    def has(self,model):
        """Whether Ollama has this model installed (as far as the last check knows)."""
        with self.lock:names=list(self.value.get('models') or [])
        return bool(model) and (model in names or (':' not in model and model+':latest' in names))

    def _loop(self):
        while not self.stop.is_set():
            model=self.settings.snapshot()['local_model']
            base=ollama_base()
            try:  # what is installed is needed whatever the main local model is (UI-TARS, Holo)
                with urlopen(base+'/api/tags',timeout=2) as r:names=[m.get('name','') for m in json.load(r).get('models',[])]
                self._set(models=names)
            except Exception:names=None
            if model.lower() in ('none','off',''):
                self._set(status='off',message='No general local model (OLLAMA_MODEL=none). Private uses UI-TARS; Smart and Fastest use a cloud model.',percent=None)
                self.stop.wait(10);continue
            if '-cloud' in model.lower() or ':cloud' in model.lower():
                self._set(status='unsupported',message='Select a local model; Ollama cloud models are not included in local-cost accounting.',percent=None)
                self.stop.wait(5);continue
            try:
                if names is None:raise OSError('Ollama did not answer')
                found=model in names or (':' not in model and model+':latest' in names)
                self._set(models=names,model=model)
                if found:
                    self._set(status='ready',message='Local model ready',percent=100)
                elif os.getenv('OLLAMA_AUTO_PULL','0')=='1':
                    self._set(status='downloading',message='Downloading '+model,percent=0)
                    req=Request(base+'/api/pull',data=json.dumps({'model':model,'stream':True}).encode(),headers={'Content-Type':'application/json'})
                    with urlopen(req,timeout=120) as response:
                        item={}
                        for line in response:
                            if self.stop.is_set():break
                            item=json.loads(line)
                            if item.get('error'):raise RuntimeError(str(item['error'])[:150])
                            total=item.get('total',0);completed=item.get('completed',0)
                            self._set(message=item.get('status','Downloading'),percent=round(completed/total*100,1) if total else None)
                    if item.get('status')=='success' and not self.stop.is_set():
                        self._set(status='ready',message='Local model ready',percent=100,
                                                        models=sorted(set(names+[model])))
                else:
                    self._set(status='missing',message='Model not installed. Enable automatic pull or install it in Ollama.',percent=None)
            except Exception:
                self._set(status='offline',message=f'Waiting for Ollama at {base}. Open the Ollama app on your Mac (or run: ollama serve). Desktop controls still work.',percent=None)
            self.stop.wait(5)

    def close(self):self.stop.set();self.thread.join(timeout=2)
