"""Workers: several agents, each with its own desktop, working at the same time.

A worker is one desktop (an X display with a window manager, taskbar and browser) plus the agent,
chat and input queue that use it. Settings, memory, skills, saved logins, the mailbox, Telegram and the
computer itself (home folder, Documents, Downloads, installed apps; see computer.py) are shared by all workers. Worker 1 uses display :99 and the original browser profile, so a
single-worker setup behaves exactly as before.
"""
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from app.activity import ACTIVITY
from app.agent import AgentRunner
from app.chat import Conversation
from app.core import ActionFailed, Desktop, Engine
from app.live import InputQueue, ScreenStream

APPS = ('chromium', 'mousepad', 'pcmanfm', 'soffice', 'xterm', 'libreoffice', 'ristretto', 'xarchiver')  # closed on reset (development)
CONFIG = Path(__file__).parent / 'desktop'
START_PAGE = 'https://duckduckgo.com'
# How open_app starts each app; any other installed program is started by its command name.
LAUNCH = {'notes': ['mousepad'], 'files': ['pcmanfm', '{home}/Documents'], 'documents': ['soffice', '--writer'],
          'spreadsheets': ['soffice', '--calc'], 'presentations': ['soffice', '--impress'],
          'terminal': ['xterm', '-fa', 'DejaVu Sans Mono', '-fs', '11']}


class DesktopSession:
    """The processes behind one worker's desktop. The browser is reopened if it is closed."""
    def __init__(self, wid, computer):
        self.wid = wid
        self.computer = computer
        self.display = f':{98 + wid}'
        self.env = computer.env(self.display)  # the desktop's own environment: no API keys
        self.profile = computer.data / ('chromium-profile' if wid == 1 else f'chromium-profile-{wid}')
        self.log = f'/tmp/screenpilot-desktop-{wid}.log'
        self.procs, self.browser = [], None
        self.fresh = False
        self.stopping = threading.Event()
        self.wake = threading.Event()  # reopen a closed browser now instead of after a pause
        self.shell = {}  # window manager and taskbar, restarted if they stop
        self.error = None

    def _ready(self):
        if shutil.which('xdpyinfo'):
            return subprocess.run(['xdpyinfo', '-display', self.display], capture_output=True, env=self.env).returncode == 0
        return os.path.exists(f'/tmp/.X11-unix/X{self.display[1:]}')

    def _spawn(self, args, **kw):
        if not shutil.which(args[0]): return None
        log = open(self.log, 'ab')
        proc = subprocess.Popen(args, env=self.env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                cwd=self.computer.home, **self.computer.as_user(), **kw)
        log.close()
        return proc

    def say(self, msg, level='info', **fields): ACTIVITY.write('desktop', msg, level, worker=self.wid, **fields)

    def start(self):
        started = time.monotonic()
        if not shutil.which('Xvfb'):
            self.error = 'Xvfb is not installed'; self.say(self.error, 'error'); return
        if not self._ready():
            # A restarted container keeps /tmp: a leftover lock would make Xvfb refuse to start.
            n = self.display[1:]
            for stale in (f'/tmp/.X{n}-lock', f'/tmp/.X11-unix/X{n}'):
                try: os.remove(stale)
                except OSError: pass
            self.procs.append(self._spawn(['Xvfb', self.display, '-screen', '0', '1280x800x24', '-nolisten', 'tcp']))
            deadline = time.monotonic() + 20
            while not self._ready():
                if time.monotonic() > deadline:
                    self.error = 'The virtual screen did not start; see ' + self.log; self.say(self.error + self._log_tail(), 'error'); return
                time.sleep(0.1)
        self.say(f'Virtual screen {self.display} ready', ms=round((time.monotonic() - started) * 1000),
                 user=self.computer.user.pw_name if self.computer.user else None)
        threading.Thread(target=self._keep_shell, daemon=True, name=f'shell{self.display}').start()
        threading.Thread(target=self._browser_loop, daemon=True, name=f'browser{self.display}').start()

    SHELL = (('openbox', '--config-file', str(CONFIG / 'openbox-rc.xml')), ('tint2', '-c', str(CONFIG / 'tint2rc')))

    def _keep_shell(self):
        """Start the window manager and taskbar, and start them again if one of them stops."""
        while not self.stopping.is_set():
            for args in self.SHELL:
                proc = self.shell.get(args[0])
                if proc is None or proc.poll() is not None:
                    if proc is not None: self.say(f'{args[0]} stopped (exit {proc.returncode}); starting it again' + self._log_tail(), 'warning')
                    self.shell[args[0]] = self._spawn(list(args))
                    if self.shell[args[0]] is None and proc is None: self.say(f'{args[0]} is not installed', 'warning')
            self.stopping.wait(3)

    def launch(self, app):
        """Open an app on this desktop (open_app). Raises ActionFailed if it is not installed."""
        if app == 'browser':
            if self.browser is None or self.browser.poll() is not None:
                self.wake.set(); return  # the browser loop opens it again right away
            args = [os.getenv('SCREENPILOT_BROWSER', 'chromium'), f'--user-data-dir={self.profile}', '--no-sandbox', '--new-window', START_PAGE]
        else:
            args = [a.format(home=self.computer.home) for a in LAUNCH.get(app, [app])]
        if not shutil.which(args[0], path=self.env['PATH']):
            raise ActionFailed(f'No app "{app}" is installed. install_app can install it (its Debian package name).')
        self._spawn(args, start_new_session=True)
        self.say(f'Started {args[0]}')

    # The agent's computer, seen from this desktop.
    def open_file(self, path): self.computer.open(path, self.display)
    def run(self, command, seconds=60, cancel=None): return self.computer.run(command, self.display, seconds, cancel)
    def install(self, package): return self.computer.install(package)
    def download_files(self): return self.computer.download_files()

    def _browser_loop(self):
        browser = os.getenv('SCREENPILOT_BROWSER', 'chromium')
        if not shutil.which(browser): return
        self.profile.mkdir(parents=True, exist_ok=True)
        self.computer.give(self.profile)  # the browser runs as the desktop user
        while not self.stopping.is_set():
            for lock in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                try: os.remove(self.profile / lock)
                except OSError: pass
            sessions = self.profile / 'Default' / 'Sessions'
            restore = not self.fresh and sessions.is_dir() and any(sessions.iterdir())
            self.fresh = False
            # Restore the last tabs when there are some; otherwise (first start, or after a reset) open the start page.
            pages = ['--restore-last-session'] if restore else [START_PAGE]
            self.browser = self._spawn([browser, '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
                                        '--no-default-browser-check', f'--user-data-dir={self.profile}', '--password-store=basic',
                                        '--disable-features=Translate', '--hide-crash-restore-bubble', '--test-type',
                                        '--start-maximized', '--window-size=1280,766', '--window-position=0,0',
                                        *pages], start_new_session=True)
            if self.browser is None: self.say(f'{browser} could not start', 'error'); return
            began = time.monotonic()
            self.say('Browser started' + (' (restoring the last tabs)' if restore else ''))
            code = self.browser.wait()
            if not self.stopping.is_set():
                self.say(f'Browser closed (exit {code}) after {time.monotonic() - began:.0f} s; opening it again' +
                         (self._log_tail() if code not in (0, -15) else ''), 'warning' if code not in (0, -15) else 'info')
            self.wake.wait(2); self.wake.clear()

    def _kill_apps(self):
        """Close the apps (and commands) on THIS display only; other workers keep theirs."""
        tag = f'DISPLAY={self.display}'.encode()
        keep = {p.pid for p in self.procs + list(self.shell.values()) if p} | {os.getpid()}
        user = self.computer.user
        for pid in os.listdir('/proc'):
            if not pid.isdigit() or int(pid) in keep: continue
            try:
                if user:
                    # Everything the desktop user runs on this display, apart from the screen, window manager and taskbar.
                    if os.stat(f'/proc/{pid}').st_uid != user.pw_uid: continue
                else:
                    with open(f'/proc/{pid}/cmdline', 'rb') as f: cmd = f.read().split(b'\0')[0].decode('utf-8', 'replace')
                    if os.path.basename(cmd).split('.')[0] not in APPS: continue
                with open(f'/proc/{pid}/environ', 'rb') as f:
                    if tag not in f.read().split(b'\0'): continue
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                continue

    def _kill_browser(self):
        if self.browser and self.browser.poll() is None:
            try: os.killpg(self.browser.pid, signal.SIGTERM)
            except OSError: pass

    def _log_tail(self, lines=6):
        """The last lines the desktop's programs wrote, for the activity log."""
        try:
            with open(self.log, 'rb') as f:
                f.seek(max(0, os.path.getsize(self.log) - 4000))
                tail = [l for l in f.read().decode('utf-8', 'replace').splitlines() if l.strip()][-lines:]
            return (' · last output: ' + ' | '.join(t[:200] for t in tail)) if tail else ''
        except OSError:
            return ''

    def reset(self):
        """Close every app; the browser comes back on a fresh page."""
        self.say('Desktop reset: closing every app')
        self.fresh = True
        self._kill_browser()
        self._kill_apps()

    def stop(self):
        self.stopping.set(); self.wake.set()
        self._kill_browser()
        self._kill_apps()
        for proc in reversed(self.procs + list(self.shell.values())):
            if proc and proc.poll() is None: proc.terminate()
        for proc in self.procs + list(self.shell.values()):
            try: proc and proc.wait(timeout=3)
            except subprocess.TimeoutExpired: proc.kill()


class Worker:
    """One agent with its own desktop, chat and input queue."""
    SHARED = ('settings', 'client', 'ledger', 'room', 'memory', 'skills', 'knowhow', 'local_models', 'store')

    def __init__(self, app, wid, lab=False):
        self.app, self.id = app, wid
        self.session = None if lab else DesktopSession(wid, app.computer)
        if self.session: self.session.start()
        self.desktop = Desktop(lab=lab, display=self.session.display if self.session else None)
        self.desktop.session = self.session
        self.engine = Engine(self.desktop, app.store)
        self.inputs = InputQueue(self.engine)
        self.stream = ScreenStream(self.desktop, fps=app.fps)
        self.chat = Conversation(app.store.root, name='chat.json' if wid == 1 else f'chat-{wid}.json')
        self.runner = AgentRunner(self)

    def __getattr__(self, name):
        # Services every worker shares live on the application.
        if name in Worker.SHARED: return getattr(self.app, name)
        raise AttributeError(name)

    @property
    def name(self): return f'Worker {self.id}'

    @property
    def agent(self): return self.runner.public()

    @property
    def agent_cancel(self): return self.runner.cancel

    def busy(self):
        thread = self.runner.thread
        return self.agent['status'] in ('running', 'waiting') or bool(thread and thread.is_alive()) or self.inputs.busy()

    def say(self, kind, text, **extra):
        return self.chat.add('agent', text, kind, worker=self.id, **extra)

    def context(self):
        parts = []
        memory = self.memory.prompt()
        if memory: parts.append('Your memory (facts saved from earlier tasks):\n' + memory)
        convo = self.chat.recent_context()
        if convo: parts.append('Recent conversation with the user:\n' + convo)
        return '\n\n'.join(parts)

    def start_agent(self, instruction, planner=None, context=None, carry=None):
        return self.runner.start(instruction, planner, context=self.context() if context is None else context, carry=carry)

    def stop(self):
        self.runner.cancel.set(); self.inputs.cancel()

    def reset(self):
        """Stop and give this worker a clean desktop: apps closed, browser reopened on a fresh page."""
        self.stop()
        if self.runner.thread is not None: self.runner.thread.join(timeout=5)
        if self.desktop.lab: self.desktop.lab_step = 0; self.desktop.lab_text = ''
        elif self.session: self.session.reset()
        self.runner.reset_state()
        self.engine.event('Desktop reset')
        self.say('status', 'Desktop reset: apps closed and the browser reopened on a fresh page.')

    def state(self):
        return {'worker': self.id, 'desktop': {'available': self.desktop.available(), 'lab': self.desktop.lab,
                                               'width': 1280, 'height': 800, 'error': self.session.error if self.session else None},
                'agent': self.agent, 'modes': self.app.modes(), 'chat_rev': self.chat.rev, 'queued': self.app.queued_for(self.id),
                'events': list(self.engine.events[-20:]), 'ollama': {'model': self.settings.snapshot()['local_model']},
                'local_model': self.local_models.public() if self.local_models else {'status': 'fixture', 'message': 'Test fixture'},
                'performance': {'screen': self.stream.metrics(), 'input': self.inputs.metrics()}}

    def summary(self):
        a = self.agent
        return {'id': self.id, 'name': self.name, 'status': a.get('status', 'idle'), 'message': a.get('message', ''),
                'task': a.get('task', ''), 'steps': a.get('steps', 0), 'planner': a.get('planner'),
                'waiting': a.get('waiting'), 'queued': self.app.queued_for(self.id), 'chat_rev': self.chat.rev}

    def close(self):
        self.runner.close(); self.inputs.close(); self.stream.close()
        if self.session: self.session.stop()
