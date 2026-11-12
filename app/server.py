"""Screenpilot server: the workers' screens, their conversations, and settings, on localhost only."""
import argparse
import io
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import __version__, frontdesk, vision
from app.activity import ACTIVITY
from app.chat import Memory
from app.computer import Computer
from app.core import Store, atomic_json
from app.models import Settings, Ledger, ModelClient, LocalModels, cloud_provider, describe_modes
from app.supervisor import _line as action_line
from app.room import Room
from app.skills import Skills
from app.knowhow import Knowhow
from app.telegram import Telegram
from app.workers import Worker

STATIC = Path(__file__).parent / 'static'
FILES = {'/': ('index.html', 'text/html; charset=utf-8'), '/style.css': ('style.css', 'text/css'),
         '/app.js': ('app.js', 'text/javascript')}
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; frame-src 'none'")
MAX_WORKERS = max(1, int(os.getenv('SCREENPILOT_MAX_WORKERS', '6')))
WORKER_PATH = re.compile(r'^/api/w/(\d+)(/.*)$')


class Application:
    """Shared services (settings, models, memory, skills, logins, Telegram) and the workers that use them."""
    def __init__(self, root, lab=False):
        self.lab = lab
        self.store = Store(root)
        self.settings = Settings(self.store.root)
        self.ledger = Ledger(self.store.root)
        self.client = ModelClient(self.settings, self.ledger)
        self.room = Room(self.store.root)
        self.memory = Memory(self.store.root)
        self.skills = Skills(self.store.root)
        self.knowhow = Knowhow(self.store.root)
        self.local_models = LocalModels(self.settings) if not lab else None
        # The agent's computer: its own user, home folder, Documents, Downloads and installed apps (shared by all workers).
        self.computer = Computer(self.store.root)
        if not lab:
            ACTIVITY.configure(self.store.root)
            ACTIVITY.write('server', f'Screenpilot {__version__} starting', worker=None,
                           desktop_user=self.computer.user.pw_name if self.computer.user else 'same user')
            self.computer.prepare()
            threading.Thread(target=self.computer.restore, daemon=True, name='restore-apps').start()
        self.fps = max(2, min(24, int(os.getenv('SCREEN_FPS', '12'))))
        self.workers, self.workers_lock = {}, threading.RLock()
        self.tasks, self.tasks_lock = [], threading.RLock()
        self.closing = threading.Event()
        self.thumbs = {}
        self.telegram = Telegram(self)
        saved = self.store.root / 'workers.json'
        ids = json.loads(saved.read_text()).get('ids', [1]) if saved.exists() else [1]
        for wid in sorted(set([1] + [i for i in ids if isinstance(i, int) and 1 <= i <= MAX_WORKERS])):
            self.add_worker(wid, save=False)
        threading.Thread(target=self._dispatch, daemon=True, name='task-queue').start()

    # ---------- workers ----------
    @property
    def main(self): return self.workers[1]

    def worker(self, wid):
        w = self.workers.get(int(wid))
        if w is None: raise ValueError(f'There is no worker {wid}')
        return w

    def add_worker(self, wid=None, save=True):
        with self.workers_lock:
            if wid is None:
                if len(self.workers) >= MAX_WORKERS: raise ValueError(f'At most {MAX_WORKERS} workers (set SCREENPILOT_MAX_WORKERS to change)')
                wid = next(i for i in range(1, MAX_WORKERS + 1) if i not in self.workers)
            w = Worker(self, wid, lab=self.lab)
            w.chat.listeners.append(self.telegram.on_message)
            self.workers[wid] = w
            if save: self._save_workers()
            return w

    def remove_worker(self, wid):
        wid = int(wid)
        if wid == 1: raise ValueError('Worker 1 is always there')
        with self.workers_lock:
            w = self.worker(wid)
            with self.tasks_lock: self.tasks = [t for t in self.tasks if t['worker'] != wid]
            del self.workers[wid]
            self._save_workers()
        self.thumbs.pop(wid, None)
        w.close(); w.chat.clear()  # a worker added later with this number starts with an empty conversation

    def _save_workers(self):
        atomic_json(self.store.root / 'workers.json', {'ids': sorted(self.workers)})

    def summary(self):
        with self.workers_lock: workers = [w.summary() for _, w in sorted(self.workers.items())]
        return {'workers': workers, 'shared_queue': sum(t['worker'] is None for t in self.tasks), 'max': MAX_WORKERS, 'version': __version__}

    def thumbnail(self, w):
        """A small, recent picture of a worker's screen for the worker rail (cached briefly)."""
        cached = self.thumbs.get(w.id)
        if cached and time.monotonic() - cached[0] < 1.5: return cached[1]
        image = w.desktop.capture(); image.thumbnail((480, 300))  # sharp enough to preview while swiping between workers
        buf = io.BytesIO(); image.save(buf, 'JPEG', quality=65)
        self.thumbs[w.id] = (time.monotonic(), buf.getvalue())
        return buf.getvalue()

    # ---------- tasks: pinned to a worker, or for whichever worker is free first ----------
    def health(self):
        """Is Screenpilot really up: the page answers, the desktop runs and its picture arrives. For run.sh, the
        container's health check and anyone asking why it "loads forever"."""
        w = self.main; problems = []
        if not w.desktop.available(): problems.append((w.session.error if w.session else None) or 'The desktop is not running')
        screen = w.stream.metrics()
        if screen.get('error'): problems.append('Screen: ' + str(screen['error']))
        elif not screen.get('frames_encoded'): problems.append('The desktop has not shown a picture yet')
        local = self.local_models.public() if self.local_models else {}
        return {'ok': not problems, 'version': __version__, 'problems': problems, 'workers': len(self.workers),
                'ollama': local.get('status'), 'models_installed': len(local.get('models') or [])}

    def modes(self):
        """What each of the three modes would use right now (for the dashboard)."""
        return describe_modes(self.settings.snapshot(), {p: bool(self.settings.key(p)) for p in ('anthropic', 'openai')},
                              self.local_models.has if self.local_models else None)

    def queued_for(self, wid):
        return sum(t['worker'] == wid for t in self.tasks)

    def chat_message(self, text, planner=None, source='dashboard', worker=None, queue=False):
        """What the user types in the chat. It is the answer when the agent is waiting for one. While the agent works
        it is a correction or an addition for that task, or a question about the work that is answered here. With
        nothing running it is a remark on the last result (answered or reworked here, the desktop is not touched), more
        work on top of the last task, or a new task: see frontdesk.py. queue=True always makes it a new task."""
        text = str(text).strip()
        if not text or len(text) > 5000: raise ValueError('Write a message (up to 5000 characters)')
        if not queue:
            pinned = self.worker(worker) if worker is not None else None
            candidates = [pinned] if pinned else [w for _, w in sorted(self.workers.items())]
            for w in candidates:  # someone is waiting for the user: this is the answer
                if w.agent.get('status') == 'waiting':
                    msg = w.chat.add('user', text, 'text', source=source)
                    taken = w.runner.answer_text(text)
                    if taken: return {'queued': False, 'worker': w.id, 'message': msg, 'taken_as': taken}
            busy = [w for w in candidates if w.agent.get('status') == 'running']
            if busy and (pinned or len(self.workers) == 1):  # it is working: tell it, or answer a question about the work
                w = busy[0]
                decided = self.front_desk(w, text, w.agent.get('planner'), running=True)
                msg = w.chat.add('user', text, 'text', source=source)
                if decided and decided[0] == 'reply':
                    w.say('text', decided[1], answered=text)
                    return {'queued': False, 'worker': w.id, 'message': msg, 'taken_as': 'reply'}
                w.runner.tell(text)
                return {'queued': False, 'worker': w.id, 'message': msg, 'taken_as': 'instruction'}
            w = pinned or (self.main if len(self.workers) == 1 else None)
            if w and not w.busy() and not self.queued_for(w.id):
                # Nothing is running: is this about the last result (answer it here), more work on top of the last
                # task (continue it), or a new job?
                decided = self.front_desk(w, text, planner, running=False)
                if decided and decided[0] == 'reply':
                    msg = w.chat.add('user', text, 'text', source=source)
                    w.say('text', decided[1], answered=text)
                    return {'queued': False, 'worker': w.id, 'message': msg, 'taken_as': 'reply'}
                past = w.runner.past
                if decided and decided[0] == 'continue' and past:
                    return self.submit_task(frontdesk.continued(text, past['task'], past['result']), planner, source, w.id, shown=text,
                                            carry={'notebook': past['notebook'], 'typed': past['typed']})
        return self.submit_task(text, planner, source, worker)

    def front_desk(self, w, text, planner, running):
        """What a chat message is: ('reply', the answer), ('instruct', ''), ('continue', '') or ('task', ''). None when
        the model is not asked (Private mode, no key, a long brief, nothing to refer to) or its answer cannot be used."""
        cfg = self.settings.snapshot(); mode = planner or cfg['planner']
        provider = cloud_provider(cfg, {p: bool(self.settings.key(p)) for p in ('anthropic', 'openai')}) if mode in ('smart', 'fast') else \
            mode.split('_')[0] if mode.split('_')[0] in ('anthropic', 'openai') else None
        if not provider or not self.settings.key(provider): return None
        run, past, messages = w.runner.current if running else None, w.runner.past, w.chat.list(40)
        has_past = bool(past) or any(m['kind'] == 'result' for m in messages)
        if not frontdesk.worth_asking(text, running, has_past): return None
        if running and run is not None:
            step = f'step {run.goal_index + 1} of {len(run.goals)}: {run.goals[run.goal_index]}' if run.goals and run.goal_index < len(run.goals) else ''
            ask = frontdesk.prompt(text, True, run.asked, step, [action_line(a) for a in run.history[-10:]], list(run.notebook), frontdesk.conversation(messages))
        else:
            ask = frontdesk.prompt(text, running, past['task'] if past else '', notebook=past['notebook'] if past else (), conversation=frontdesk.conversation(messages))
        try:
            decided = frontdesk.parse(self.client.call(provider, ask, None, uuid.uuid4().hex, 'chat', cfg), running)
        except Exception as exc:
            w.engine.event('Could not read the message first, so it is handled the simple way: ' + str(exc)[:120], 'warning'); return None
        if decided: w.engine.event({'reply': 'Answered in the chat, without the desktop', 'instruct': 'Passed to the running task',
                                    'continue': 'Continuing the last task', 'task': 'A new task'}[decided[0]])
        return decided

    def submit_task(self, text, planner=None, source='dashboard', worker=None, shown=None, carry=None):
        """shown: what the chat shows as the user's message when the task text says more (a continued task).
        carry: what the task starts with from the one before."""
        text = str(text).strip()
        if not text or len(text) > 5000: raise ValueError('Write a task (up to 5000 characters)')
        with self.tasks_lock:
            if worker is not None:
                target = self.worker(worker)
            else:
                idle = [w for _, w in sorted(self.workers.items()) if not w.busy() and not self.queued_for(w.id)]
                target = idle[0] if idle else (self.main if len(self.workers) == 1 else None)
            queued = target is None or target.busy() or bool(self.queued_for(target.id))
            msg = target.chat.add('user', shown or text, 'text', source=source) if target else {'text': text, 'source': source}
            self.tasks.append({'text': text, 'planner': planner, 'source': source, 'carry': carry,
                               'worker': target.id if target else None, 'logged': bool(target)})
        if queued and target: target.say('status', 'Queued. I will start this when the current task is finished.')
        return {'queued': queued, 'worker': target.id if target else None, 'message': msg}

    def _dispatch(self):
        while not self.closing.wait(0.25):
            chosen = set()
            with self.tasks_lock:
                for task in list(self.tasks):
                    if task['worker'] is not None:
                        w = self.workers.get(task['worker'])
                        if w is None: self.tasks.remove(task); continue
                        if w.id in chosen or w.busy(): continue
                    else:
                        free = [w for _, w in sorted(self.workers.items())
                                if w.id not in chosen and not w.busy() and not self.queued_for(w.id)]
                        if not free: continue
                        w = free[0]
                    chosen.add(w.id)
                    # Leave the queue only once it is running, so a task is never "neither queued nor running",
                    # also for readers that do not hold the lock.
                    try:
                        if not task['logged']: w.chat.add('user', task['text'], 'text', source=task['source']); task['logged'] = True
                        w.start_agent(task['text'], task['planner'], carry=task.get('carry'))
                    except Exception as exc:
                        w.say('error', str(exc))
                    finally:
                        self.tasks.remove(task)

    def stop_all(self):
        with self.tasks_lock: self.tasks.clear()
        for w in list(self.workers.values()): w.stop()

    def stop_worker(self, w):
        with self.tasks_lock: self.tasks = [t for t in self.tasks if t['worker'] != w.id]
        w.stop()

    def reset_worker(self, w):
        with self.tasks_lock: self.tasks = [t for t in self.tasks if t['worker'] != w.id]
        w.reset()

    # ---------- worker 1, for callers that know only one desktop (CLI, Telegram defaults, tests) ----------
    desktop = property(lambda self: self.main.desktop)
    engine = property(lambda self: self.main.engine)
    inputs = property(lambda self: self.main.inputs)
    stream = property(lambda self: self.main.stream)
    runner = property(lambda self: self.main.runner)
    chat = property(lambda self: self.main.chat)
    agent = property(lambda self: self.main.agent)
    agent_cancel = property(lambda self: self.main.agent_cancel)

    def say(self, kind, text, **extra): return self.main.say(kind, text, **extra)
    def context(self): return self.main.context()
    def start_agent(self, instruction, planner=None, context=None): return self.main.start_agent(instruction, planner, context)
    def reset(self): self.reset_worker(self.main)
    def state(self): return self.main.state()

    def busy(self): return any(w.agent['status'] in ('running', 'waiting') for w in self.workers.values())

    def close(self):
        self.closing.set(); self.telegram.close()
        for w in list(self.workers.values()): w.close()
        if self.local_models: self.local_models.close()


def handler_factory(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # never log typed text, tasks or request bodies

        def send(self, status, data, content_type='application/json'):
            if content_type == 'application/json': data = json.dumps(data, ensure_ascii=False).encode()
            elif isinstance(data, str): data = data.encode()
            self.send_response(status)
            for k, v in (('Content-Type', content_type), ('Content-Length', str(len(data))), ('Cache-Control', 'no-store'),
                         ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'), ('Content-Security-Policy', CSP),
                         *getattr(self, 'extra_headers', {}).items()):
                self.send_header(k, v)
            self.extra_headers = {}
            self.end_headers()
            self.wfile.write(data)

        def stream_desktop(self, stream):
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=screenpilot')
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.end_headers()
            seq, first = -1, True
            try:
                while not stream.stopped.is_set():
                    current, jpeg = stream.next_frame(seq, timeout=2)
                    if jpeg is None or current == seq: continue
                    seq = current
                    packet = b'--screenpilot\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n'
                    # Browsers draw a part when the next one starts: send the first frame twice so it shows at once.
                    self.wfile.write(packet * 2 if first else packet); self.wfile.flush(); first = False
                    with stream.condition: stream.bytes_sent += len(packet)
            except (BrokenPipeError, ConnectionResetError, TimeoutError): pass

        def trusted_host(self):
            host = self.headers.get('Host', '').split(':')[0]
            return host in {'localhost', '127.0.0.1', '[', *os.getenv('SCREENPILOT_HOSTS', '').split(',')}

        def route(self):
            """/api/w/3/... addresses worker 3; plain /api/... addresses worker 1 (or everything, for stop)."""
            path = urlparse(self.path).path
            m = WORKER_PATH.match(path)
            return (app.worker(int(m.group(1))), '/api' + m.group(2), True) if m else (app.main, path, False)

        def do_GET(self):
            if not self.trusted_host(): return self.send(403, {'error': 'Untrusted host'})
            try:
                w, path, scoped = self.route()
                if path == '/api/health': return self.send(200, app.health())
                if path == '/api/state': return self.send(200, w.state())
                if path == '/api/workers': return self.send(200, app.summary())
                if path == '/api/settings': return self.send(200, app.settings.public())
                if path == '/api/room': return self.send(200, app.room.public())
                if path == '/api/usage': return self.send(200, app.ledger.summary(w.agent.get('run_id')))
                if path == '/api/chat':
                    return self.send(200, {'messages': w.chat.list(), 'rev': w.chat.rev, 'memory': app.memory.list(),
                                           'skills': app.skills.list(), 'knowhow': app.knowhow.list(), 'telegram': app.telegram.status(), 'queued': app.queued_for(w.id)})
                if path == '/api/marks':
                    # What the local eyes see: numbered text and controls on the current screen.
                    image = w.desktop.capture()
                    items = vision.elements(image) if vision.available() else []
                    buf = io.BytesIO(); vision.draw_marks(image, items).save(buf, 'JPEG', quality=80)
                    return self.send(200, buf.getvalue(), 'image/jpeg')
                if path == '/api/stream': return self.stream_desktop(w.stream)
                if path == '/api/screen':
                    _, jpeg = w.stream.next_frame(timeout=2)
                    if jpeg is None: raise RuntimeError(w.stream.error or 'Waiting for desktop')
                    return self.send(200, jpeg, 'image/jpeg')
                if path == '/api/thumb': return self.send(200, app.thumbnail(w), 'image/jpeg')
                if path == '/api/logs':
                    q = parse_qs(urlparse(self.path).query)
                    after = int(q.get('after', ['0'])[0] or 0)
                    who = None if q.get('all', ['0'])[0] == '1' or not scoped else w.id
                    return self.send(200, {'entries': ACTIVITY.entries(after, who), 'now': ACTIVITY.now(who), 'version': __version__})
                if path == '/api/logs/download':
                    self.extra_headers = {'Content-Disposition': f'attachment; filename="screenpilot-log-{time.strftime("%Y%m%d-%H%M")}.txt"'}
                    return self.send(200, f'Screenpilot {__version__}\n' + ACTIVITY.download(), 'text/plain; charset=utf-8')
                if path in FILES:
                    name, mime = FILES[path]
                    return self.send(200, (STATIC / name).read_bytes(), mime)
                return self.send(404, {'error': 'Not found'})
            except (ValueError, FileNotFoundError) as exc: self.send(404, {'error': str(exc)})
            except Exception as exc:
                ACTIVITY.error('api', f'GET {urlparse(self.path).path} failed: {type(exc).__name__}: {exc}', worker=None)
                self.send(503, {'error': str(exc)})

        def do_POST(self):
            if not self.trusted_host(): return self.send(403, {'error': 'Untrusted host'})
            if self.headers.get('X-Screenpilot') != '1': return self.send(403, {'error': 'Missing same-origin request header'})
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                return self.send(403, {'error': 'Cross-origin control is blocked'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length > 200000: raise ValueError('Request too large')
                body = json.loads(self.rfile.read(length) or b'{}')
                w, path, scoped = self.route()
                # Worker-scoped
                if path == '/api/chat':
                    target = None if body.get('any') or not scoped else w.id
                    return self.send(202, app.chat_message(body.get('text', ''), body.get('planner'), worker=target, queue=bool(body.get('queue'))))
                if path == '/api/chat/clear': w.chat.clear(); return self.send(200, {'ok': True})
                if path == '/api/stop':
                    app.stop_worker(w) if scoped else app.stop_all(); return self.send(200, {'ok': True})
                if path == '/api/reset': app.reset_worker(w); return self.send(200, {'ok': True})
                if path == '/api/agent/reply': return self.send(200, w.runner.reply(str(body.get('answer', '')), body.get('ask')))
                if path == '/api/action':
                    if w.agent['status'] == 'running': raise ValueError('The agent is working. Stop it, or wait for your turn.')
                    return self.send(202, w.inputs.submit(body))
                # Shared
                if path == '/api/workers': return self.send(200, {'id': app.add_worker().id, **app.summary()})
                if path == '/api/workers/remove': app.remove_worker(body.get('id')); return self.send(200, app.summary())
                if path == '/api/usage/reset': app.ledger.reset(); return self.send(200, app.ledger.summary())
                if path == '/api/settings':
                    if app.busy(): raise ValueError('Stop the running tasks before changing model settings')
                    return self.send(200, app.settings.update(body))
                if path == '/api/room':
                    if app.busy(): raise ValueError('Stop the running tasks before changing these settings')
                    return self.send(200, app.room.update(body))
                if path == '/api/room/login': return self.send(200, app.room.save_login(body))
                if path == '/api/room/login/delete': return self.send(200, app.room.delete_login(body.get('name', '')))
                if path == '/api/room/mail-test':
                    mails = app.room.check_email('', limit=3)
                    return self.send(200, {'ok': True, 'count': len(mails), 'subjects': [m['subject'] for m in mails]})
                if path == '/api/memory': return self.send(200, {'item': app.memory.add(body.get('text', ''), 'user'), 'memory': app.memory.list()})
                if path == '/api/memory/delete': app.memory.delete(str(body.get('id', ''))); return self.send(200, {'memory': app.memory.list()})
                if path == '/api/skills/delete': app.skills.delete(str(body.get('id', ''))); return self.send(200, {'skills': app.skills.list()})
                if path == '/api/knowhow': app.knowhow.add(body.get('name', ''), body.get('when', ''), body.get('text', '')); return self.send(200, {'knowhow': app.knowhow.list()})
                if path == '/api/knowhow/switch': app.knowhow.switch(str(body.get('id', '')), bool(body.get('on'))); return self.send(200, {'knowhow': app.knowhow.list()})
                if path == '/api/knowhow/delete': app.knowhow.delete(str(body.get('id', ''))); return self.send(200, {'knowhow': app.knowhow.list()})
                if path == '/api/telegram':
                    return self.send(200, app.telegram.disconnect() if body.get('disconnect') else app.telegram.connect(body.get('token', '')))
                return self.send(404, {'error': 'Not found'})
            except (ValueError, KeyError, TypeError) as exc:
                ACTIVITY.write('api', f'POST {urlparse(self.path).path} refused: {exc}', 'warning', worker=None)
                self.send(400, {'error': str(exc)})
            except Exception as exc:
                ACTIVITY.error('api', f'POST {urlparse(self.path).path} failed: {type(exc).__name__}: {exc}', worker=None)
                self.send(503, {'error': str(exc)})
    return Handler


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--data', default=os.getenv('DATA_DIR', './data'))
    parser.add_argument('--lab', action='store_true', help='Synthetic test screens instead of Linux desktops (for tests)')
    args = parser.parse_args()
    if os.geteuid() == 0: os.umask(0o077)  # Screenpilot's own files stay private; the desktop's processes get their own umask
    app = Application(args.data, args.lab)
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(app))
    print(f'Screenpilot: http://localhost:{args.port}', flush=True)
    try: server.serve_forever()
    finally: app.close(); server.server_close()
