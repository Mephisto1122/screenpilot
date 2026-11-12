"""The agent's Linux desktop: screen capture and mouse/keyboard input only. No DOM or application APIs."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageGrab

from app.activity import ACTIVITY

KINDS = ('click', 'double_click', 'type', 'key', 'scroll', 'wait', 'open_app', 'switch_window', 'open_file', 'run_command', 'install_app')
WIDTH, HEIGHT = 1280, 800

# Apps the agent can open by name. The window class tells an open window of that app.
APPS = {'browser': ('Chromium web browser', 'chromium'), 'notes': ('Mousepad text editor', 'mousepad'),
        'files': ('file manager', 'pcmanfm'), 'documents': ('LibreOffice Writer', 'libreoffice-writer'),
        'spreadsheets': ('LibreOffice Calc', 'libreoffice-calc'), 'presentations': ('LibreOffice Impress', 'libreoffice-impress'),
        'terminal': ('Terminal', 'xterm')}
APP_WORDS = {'browser': 'browser', 'web browser': 'browser', 'web': 'browser', 'chromium': 'browser', 'chrome': 'browser',
             'notes': 'notes', 'note': 'notes', 'mousepad': 'notes', 'text editor': 'notes', 'editor': 'notes', 'notepad': 'notes',
             'files': 'files', 'file manager': 'files', 'pcmanfm': 'files', 'documents': 'documents', 'writer': 'documents',
             'libreoffice writer': 'documents', 'word processor': 'documents', 'word': 'documents', 'document': 'documents',
             'spreadsheets': 'spreadsheets', 'spreadsheet': 'spreadsheets', 'calc': 'spreadsheets',
             'libreoffice calc': 'spreadsheets', 'excel': 'spreadsheets', 'sheets': 'spreadsheets',
             'presentations': 'presentations', 'presentation': 'presentations', 'slides': 'presentations', 'impress': 'presentations',
             'libreoffice impress': 'presentations', 'powerpoint': 'presentations', 'terminal': 'terminal', 'shell': 'terminal',
             'console': 'terminal', 'command line': 'terminal', 'xterm': 'terminal'}
PROGRAM = re.compile(r'[a-z0-9][a-z0-9+._-]{0,60}')  # any other installed app, by its command name
PACKAGE = re.compile(r'[a-z0-9][a-z0-9+.-]{1,62}')


def app_name(text):
    """'Mousepad', 'the text editor', 'LibreOffice Calc' -> the app's key in APPS, or None."""
    t = re.sub(r'^(the|a|an)\s+', '', str(text or '').strip().lower().strip('.\'" '))
    t = re.sub(r'\s+(app|application|program|window|icon)$', '', t)
    return APP_WORDS.get(t)


TASKBAR_WORDS = re.compile(r'\b(task ?bar|panel|launchers?|dock)\b', re.I)


def app_in_text(text):
    """The app a phrase names, anywhere in it: 'Mousepad icon in the taskbar' -> 'notes'."""
    t = ' ' + re.sub(r'[^a-z]+', ' ', str(text or '').lower()) + ' '
    return next((APP_WORDS[w] for w in sorted(APP_WORDS, key=len, reverse=True) if f' {w} ' in t), None)


class ActionFailed(RuntimeError):
    """The desktop could not do this action (an app is not installed, no such window). The agent is told and goes on."""
    expected = True  # the activity log records it as a warning, without a traceback

def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)

def download_name(name):
    """A file still downloading: Chromium calls it 'Unconfirmed 123.crdownload' until it knows the real name."""
    base = re.sub(r'\.(crdownload|part|partial|download)$', '', name)
    return 'a file (its real name appears when it is finished)' if re.fullmatch(r'Unconfirmed \d+', base) else base


# Key names as the desktop (X11) spells them. Models write "enter", "esc", "alt+left", "PageDown"; the desktop
# knows only "Return", "Escape", "alt+Left", "Page_Down" and silently ignores anything else, so a wrong
# spelling used to do nothing at all without any error.
KEY_NAMES = {'enter': 'Return', 'return': 'Return', 'esc': 'Escape', 'escape': 'Escape', 'tab': 'Tab', 'space': 'space',
             'spacebar': 'space', 'backspace': 'BackSpace', 'delete': 'Delete', 'del': 'Delete', 'insert': 'Insert',
             'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right', 'arrowup': 'Up', 'arrowdown': 'Down',
             'arrowleft': 'Left', 'arrowright': 'Right', 'home': 'Home', 'end': 'End',
             'pageup': 'Page_Up', 'pagedown': 'Page_Down', 'page_up': 'Page_Up', 'page_down': 'Page_Down', 'pgup': 'Page_Up', 'pgdn': 'Page_Down',
             'ctrl': 'ctrl', 'control': 'ctrl', 'alt': 'alt', 'shift': 'shift', 'super': 'super', 'meta': 'super', 'cmd': 'ctrl', 'command': 'ctrl',
             'win': 'super', 'windows': 'super', 'option': 'alt', 'plus': 'plus', 'minus': 'minus', 'period': 'period', 'comma': 'comma',
             'slash': 'slash', 'capslock': 'Caps_Lock', 'printscreen': 'Print', 'menu': 'Menu',
             **{f'f{n}': f'F{n}' for n in range(1, 13)}}


def key_name(key):
    """A key or combination as the desktop spells it: "Enter" -> "Return", "Alt+Left" -> "alt+Left", "ctrl l" -> "ctrl+l"."""
    parts = [p for p in re.split(r'[+\s]+', key.strip()) if p]
    out = []
    for p in parts:
        name = KEY_NAMES.get(p.lower().replace('-', '_'))
        out.append(name if name else (p.lower() if len(p) == 1 else p))  # single letters are lower-case keys
    return '+'.join(out)


def describe_action(a):
    """An action for the log, without the text that was typed."""
    k = a['kind']
    if k in ('click', 'double_click'): return f"{k} {a['x']},{a['y']} \"{a['label'][:60]}\""
    if k == 'type': return f"type {len(a['text'])} characters" + (' (saved login)' if a.get('secret') else '')
    if k == 'key': return f"key {a['key']}"
    if k == 'scroll': return f"scroll {a['amount']}"
    if k == 'wait': return f"wait {a['seconds']:g} s"
    if k == 'open_app': return f"open app {a['app']}"
    if k == 'switch_window': return f"switch to window \"{a['window'][:60]}\""
    if k == 'open_file': return f"open file {a['path'][:120]}"
    if k == 'run_command': return f"command: {a['command'][:300]}"
    if k == 'install_app': return f"install {a['package']}"
    return k


def validate_action(raw):
    if not isinstance(raw, dict) or raw.get('kind') not in KINDS:
        raise ValueError('Unknown action type')
    a = {'kind': raw['kind']}
    if a['kind'] in ('click', 'double_click'):
        for key, bound in [('x', WIDTH), ('y', HEIGHT)]:
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < bound:
                raise ValueError(f'{key} must be inside the desktop')
            a[key] = int(value)
    if a['kind'] == 'type':
        a['text'] = str(raw.get('text', ''))
        if not a['text'] or len(a['text']) > 20000:
            raise ValueError('Text must contain 1–20000 characters')
        if raw.get('secret') is True: a['secret'] = True  # a saved password: never left in the clipboard
    if a['kind'] == 'key':
        a['key'] = key_name(str(raw.get('key', '')))
        import re
        if not re.fullmatch(r'[A-Za-z0-9_+]{1,80}', a['key']):
            raise ValueError('Use an X11 key, e.g. Return or ctrl+l')
    if a['kind'] == 'scroll':
        a['amount'] = int(raw.get('amount', 3))
        if not -30 <= a['amount'] <= 30 or not a['amount']:
            raise ValueError('Scroll must be between -30 and 30, excluding zero')
    if a['kind'] == 'wait':
        a['seconds'] = float(raw.get('seconds', 1))
        if not 0.1 <= a['seconds'] <= 600:
            raise ValueError('Wait must be between 0.1 and 600 seconds')
    if a['kind'] == 'open_app':
        name = str(raw.get('app') or '').strip()
        a['app'] = app_name(name) or (name.lower() if PROGRAM.fullmatch(name.lower()) else None)
        if not a['app']: raise ValueError('open_app needs an app: ' + ', '.join(APPS) + ', or the command name of an installed app')
    if a['kind'] == 'open_file':
        a['path'] = str(raw.get('path') or raw.get('file') or '').strip()
        if not a['path'] or len(a['path']) > 500 or '\0' in a['path']: raise ValueError('open_file needs a path, e.g. ~/Downloads/report.pdf')
    if a['kind'] == 'run_command':
        a['command'] = str(raw.get('command') or '').strip()
        if not a['command'] or len(a['command']) > 8000: raise ValueError('run_command needs a command (up to 8000 characters)')
        try: a['seconds'] = min(900.0, max(1.0, float(raw.get('seconds') or 60)))
        except (TypeError, ValueError): raise ValueError('seconds must be a number')
    if a['kind'] == 'install_app':
        a['package'] = str(raw.get('package') or raw.get('app') or '').strip().lower()
        if not PACKAGE.fullmatch(a['package']): raise ValueError('install_app needs a Debian package name, e.g. gimp')
    if a['kind'] == 'switch_window':
        a['window'] = ' '.join(str(raw.get('window') or raw.get('title') or '').split())[:120]
        if not a['window']: raise ValueError('switch_window needs part of a window title')
    a['label'] = str(raw.get('label', a['kind']))[:160]
    return a


# ---------- reading the window manager (xprop output) ----------
WINDOW_PROPS = ('_NET_WM_NAME', 'WM_NAME', 'WM_CLASS', '_NET_WM_STATE', '_NET_WM_WINDOW_TYPE')


def _window_ids(line):
    """'_NET_CLIENT_LIST(WINDOW): window id # 0x400003, 0xa00004' -> [0x400003, 0xa00004]"""
    return [int(x, 16) for x in re.findall(r'0x[0-9a-fA-F]+', line.split('#', 1)[1])] if '#' in line else []


def _xprop_string(value):
    """First quoted string of an xprop value; undoes xprop's escapes (\\\" and octal bytes for non-ASCII)."""
    m = re.match(r'\s*"((?:[^"\\]|\\.)*)"', value or '')
    if not m: return ''
    out, raw, i = bytearray(), m.group(1), 0
    while i < len(raw):
        if raw[i] == '\\' and i + 1 < len(raw):
            octal = re.match(r'[0-7]{3}', raw[i + 1:i + 4])
            if octal: out.append(int(octal.group(), 8)); i += 4; continue
            out += raw[i + 1].encode(); i += 2; continue
        out += raw[i].encode(); i += 1
    return out.decode('utf-8', 'replace')


def parse_windows(root, details):
    """xprop output -> (windows, taskbar_seen). `details` holds one '@@<id>' section of window properties per window."""
    lines = root.splitlines()
    ids = _window_ids(next((l for l in lines if l.startswith('_NET_CLIENT_LIST(')), ''))
    active = _window_ids(next((l for l in lines if l.startswith('_NET_ACTIVE_WINDOW(')), ''))
    active = active[0] if active else None
    props, current = {}, None
    for line in details.splitlines():
        if line.startswith('@@'):
            try: current = int(line[2:].strip(), 16); props[current] = {}
            except ValueError: current = None
            continue
        m = re.match(r'^([A-Z_]+)\([A-Za-z0-9_]+\)\s*=\s*(.*)$', line)
        if m and current is not None: props[current][m.group(1)] = m.group(2)
    windows, taskbar = [], False
    for wid in ids:
        p = props.get(wid, {})
        kinds, state = p.get('_NET_WM_WINDOW_TYPE', ''), p.get('_NET_WM_STATE', '')
        cls = ' '.join(dict.fromkeys(re.findall(r'"([^"]*)"', p.get('WM_CLASS', '').lower())))  # e.g. "libreoffice libreoffice-calc"
        if 'DOCK' in kinds or 'tint2' in cls: taskbar = True; continue
        if 'DESKTOP' in kinds or 'SPLASH' in kinds or 'MENU' in kinds: continue
        windows.append({'id': wid, 'title': (_xprop_string(p.get('_NET_WM_NAME')) or _xprop_string(p.get('WM_NAME')))[:120],
                        'app': cls, 'active': wid == active, 'fullscreen': '_NET_WM_STATE_FULLSCREEN' in state,
                        'minimized': '_NET_WM_STATE_HIDDEN' in state})
    return windows, taskbar


def describe_desktop(windows, taskbar):
    """One line for the model: which window gets the keys, what else is open, and whether the taskbar can be seen."""
    name = lambda w: '"' + (w['title'] or w['app'] or 'untitled').replace('"', "'") + '"' + (' (minimized)' if w['minimized'] else '')
    active = next((w for w in windows if w['active']), None)
    others = [w for w in windows if w is not active]
    parts = []
    if active:
        parts.append('active window (keys and typing go here): ' + name(active))
        if active['fullscreen']:
            parts.append('it is FULL SCREEN: it covers the whole screen and the taskbar is hidden (F11 leaves full screen; '
                         'open_app and switch_window work anyway)')
    else:
        parts.append('no window is active' if windows else 'no windows are open')
    if others: parts.append('other open windows: ' + ', '.join(name(w) for w in others[:8]))
    if not (active and active['fullscreen']):
        parts.append('the taskbar is at the bottom' if taskbar else 'there is no taskbar right now: use open_app and switch_window')
    return 'Desktop now: ' + '; '.join(parts) + '.'

KEYSTROKE_TEXT = re.compile(r'[ -~]{1,300}')  # plain one-line text short enough to type key by key


def glide(start, end):
    """Points from where the pointer is to the target, so hover menus and tooltips on the way react."""
    (x0, y0), (x1, y1) = start, end
    steps = max(3, min(12, int(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / 60)))
    ease = lambda t: 1 - (1 - t) ** 3  # quick start, gentle arrival
    return [(round(x0 + (x1 - x0) * ease(i / steps)), round(y0 + (y1 - y0) * ease(i / steps))) for i in range(1, steps + 1)]


class Desktop:
    """One X display: screen capture and mouse/keyboard input through xdotool. `lab` is a synthetic test screen."""
    def __init__(self, lab=False, display=None):
        self.lab = lab
        self.display = display or os.environ.get('DISPLAY', ':99')
        self.env = {**os.environ, 'DISPLAY': self.display}
        self.lock = threading.RLock()
        self.capture_lock = threading.RLock()
        self.lab_step = 0
        self.lab_text = ''
        self.lab_actions = []  # test screen only: what was executed, for tests to check
        self.lab_windows = None  # test screen only: the window list to pretend (None: one window, the test screen)
        self.lab_taskbar = True
        self.session = None  # the processes behind this display (workers.DesktopSession): opens apps, files, commands
        self.lab_downloads = {}  # test screen only: pretend files in Downloads

    def _run(self, *args, input=None, timeout=12):
        return subprocess.run(list(args), input=input, check=True, capture_output=True, timeout=timeout, env=self.env)

    def active_title(self):
        """Title of the focused window (e.g. the web page's title), read from the window manager."""
        if self.lab: return 'Test screen'
        try: return self._run('xdotool', 'getactivewindow', 'getwindowname', timeout=2).stdout.decode('utf-8', 'replace').strip()[:200]
        except Exception: return ''

    def windows(self):
        """Open windows from the window manager: title, app, active, full screen, minimized. Plus whether a taskbar exists."""
        if self.lab:
            ws = self.lab_windows if self.lab_windows is not None else [
                {'id': 1, 'title': self.active_title(), 'app': 'test', 'active': True, 'fullscreen': False, 'minimized': False}]
            return [dict(w) for w in ws], self.lab_taskbar
        env = {**self.env, 'LC_ALL': 'C.UTF-8'}  # titles in any language come out as UTF-8
        try:
            root = subprocess.run(['xprop', '-root', '_NET_CLIENT_LIST', '_NET_ACTIVE_WINDOW'], capture_output=True, timeout=2, env=env).stdout.decode('utf-8', 'replace')
            ids = _window_ids(next((l for l in root.splitlines() if l.startswith('_NET_CLIENT_LIST(')), ''))[:40]
            script = '; '.join(f'echo @@{i:#x}; xprop -id {i:#x} ' + ' '.join(WINDOW_PROPS) for i in ids)
            details = subprocess.run(['sh', '-c', script], capture_output=True, timeout=3, env=env).stdout.decode('utf-8', 'replace') if ids else ''
        except (OSError, subprocess.SubprocessError):
            return [], None
        windows, taskbar = parse_windows(root, details)
        if not taskbar and shutil.which('xdotool'):
            # Some window managers do not list docks; ask for the taskbar's window directly.
            taskbar = subprocess.run(['xdotool', 'search', '--classname', 'tint2'], capture_output=True, timeout=2, env=self.env).returncode == 0
        return windows, taskbar

    def state_text(self):
        """The desktop in one line, read from the window manager (no screenshot needed), plus downloads in progress."""
        windows, taskbar = self.windows()
        line = ('Active window (keys and typing go here): ' + (self.active_title() or 'unknown')) if taskbar is None else describe_desktop(windows, taskbar)
        _, partial = self.download_files()
        if partial:
            line += (' Downloading now: ' + ', '.join(f'{download_name(n)} ({s / 1048576:.1f} MB so far)' for n, s in partial[:3]) +
                     '. A wait ends as soon as a download finishes, and you are told its file name.')
        return line

    def taskbar_visible(self):
        """True or False from the window manager (a full-screen window hides it); None if that cannot be read."""
        windows, taskbar = self.windows()
        if taskbar is None: return None
        active = next((w for w in windows if w['active']), None)
        return bool(taskbar) and not (active and active['fullscreen'])

    def _find_window(self, target, windows):
        app = app_name(target)
        if app: return next((w for w in windows if APPS[app][1] in w['app']), None)
        t = target.lower()
        hit = next((w for w in windows if t in w['title'].lower()), None)
        if hit: return hit
        words = set(re.findall(r'\w{3,}', t))
        scored = [(len(words & set(re.findall(r'\w{3,}', w['title'].lower()))), w) for w in windows]
        best = max(scored, key=lambda s: s[0], default=(0, None))
        return best[1] if best[0] else None

    def _activate(self, wid):
        if self.lab:
            for w in self.lab_windows or []: w['active'] = w['id'] == wid; w['minimized'] = w['minimized'] and w['id'] != wid
            return
        subprocess.run(['xdotool', 'windowactivate', str(wid)], capture_output=True, timeout=3, env=self.env)

    def _lab_window(self, title, app):
        if self.lab_windows is None: return
        for w in self.lab_windows: w['active'] = False
        self.lab_windows.append({'id': len(self.lab_windows) + 1, 'title': title, 'app': app, 'active': True, 'fullscreen': False, 'minimized': False})

    def _bring_up_new_window(self, before, seconds):
        """Wait for a window that was not there before and make it the active one."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.25)
            new = [w for w in self.windows()[0] if w['id'] not in before]
            if new: return self._activate(new[-1]['id'])

    def _session(self):
        if not self.session: raise ActionFailed('This desktop cannot open apps, files or commands')
        return self.session

    def open_app(self, app):
        """Bring the app to the front, opening it if it is not open yet. Works whatever is on the screen."""
        windows, _ = self.windows()
        cls = APPS[app][1] if app in APPS else app
        open_ = next((w for w in windows if cls in w['app']), None)
        if open_: return self._activate(open_['id'])
        if self.lab and not self.session: return self._lab_window(APPS[app][0] if app in APPS else app, cls)
        self._session().launch(app)
        if not self.lab: self._bring_up_new_window({w['id'] for w in windows}, 10)  # LibreOffice takes a few seconds

    def open_file(self, path):
        """Open a file or folder in the app made for it: PDFs, images, documents, spreadsheets, archives, text."""
        if self.lab and not self.session: return self._lab_window(os.path.basename(path), 'file')
        windows, _ = self.windows()
        self._session().open_file(path)
        if not self.lab: self._bring_up_new_window({w['id'] for w in windows}, 8)

    def run_command(self, command, seconds=60, cancel=None):
        """A shell command on the agent's computer, as the desktop's user. Returns (exit code, output)."""
        return self._session().run(command, seconds, cancel)

    def install(self, package):
        return self._session().install(package)

    def download_files(self):
        """({finished file: size}, [(file, size so far) still downloading]) in ~/Downloads."""
        if self.lab and not self.session:
            return {k: v for k, v in self.lab_downloads.items() if not k.endswith('.crdownload')}, [(k, v) for k, v in self.lab_downloads.items() if k.endswith('.crdownload')]
        return self.session.download_files() if self.session else ({}, [])

    def switch_window(self, target):
        windows, _ = self.windows()
        w = self._find_window(target, windows)
        if not w:
            names = ', '.join('"' + (x['title'] or x['app']) + '"' for x in windows) or 'none'
            raise ActionFailed(f'No open window matches "{target}". Open windows: {names}. Use open_app to open an app.')
        self._activate(w['id'])

    def pointer(self):
        """Where the mouse pointer is now (the screen centre if that cannot be read)."""
        try:
            out = self._run('xdotool', 'getmouselocation', '--shell', timeout=2).stdout.decode()
            return int(re.search(r'X=(\d+)', out).group(1)), int(re.search(r'Y=(\d+)', out).group(1))
        except Exception:
            return WIDTH // 2, HEIGHT // 2

    def _clipboard(self, text):
        subprocess.run(['xclip', '-selection', 'clipboard'], input=text.encode(), env=self.env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=12)

    def available(self):
        return self.lab or bool(self.display and shutil.which('xdotool'))

    def capture(self):
        with self.capture_lock:
            if self.lab:
                # Explicit interactive test fixture, never presented as a live Linux desktop.
                im = Image.new('RGB', (WIDTH, HEIGHT), '#101923')
                d = ImageDraw.Draw(im)
                d.rectangle((0, 0, WIDTH, 48), fill='#202c3c')
                d.text((24, 18), 'SCREENPILOT / INTERACTIVE TEST FIXTURE — NOT A LINUX DESKTOP', fill='#c5d7f0')
                d.text((80, 110), 'Test screen for automated checks.', fill='white')
                d.text((80, 145), f'Executed actions: {self.lab_step}', fill='#9cacbf')
                d.rectangle((80, 220, 440, 300), fill=('#426d53' if self.lab_step % 2 else '#444775'))
                d.text((100, 250), 'Click to change this test state', fill='white')
                d.text((80, 350), self.lab_text[-100:], fill='#d2d8e4')
                x = 80 + (self.lab_step * 71) % 1000
                d.rectangle((x, 450, x + 55, 505), fill='#a3a0ff')
                return im
            if not self.available():
                raise RuntimeError('No X11 desktop. Start with docker compose up --build, or use --lab for the labeled test fixture.')
            image = ImageGrab.grab(xdisplay=self.display).convert('RGB')
            return image if image.size == (WIDTH, HEIGHT) else image.resize((WIDTH, HEIGHT))

    def execute(self, a, cancel=None):
        if a['kind'] == 'wait':
            if cancel:
                if cancel.wait(a['seconds']):
                    raise InterruptedError('Stopped during wait')
            else:
                time.sleep(a['seconds'])
            return
        with self.lock:
            kind = a['kind']
            if kind in ('open_app', 'switch_window', 'open_file', 'run_command', 'install_app'):
                if self.lab: self.lab_step += 1; self.lab_actions.append(dict(a))
                if kind == 'open_app': return self.open_app(a['app'])
                if kind == 'switch_window': return self.switch_window(a['window'])
                if kind == 'open_file': return self.open_file(a['path'])
                if kind == 'run_command': return self.run_command(a['command'], a['seconds'], cancel)
                return self.install(a['package'])
            if self.lab:
                self.lab_step += 1
                self.lab_actions.append(dict(a))
                if a['kind'] == 'type': self.lab_text = a['text']
                return
            if not self.available(): raise RuntimeError('Linux desktop is unavailable')
            if kind in ('click', 'double_click'):
                # The pointer moves to the target in a few steps, then clicks. No --sync: it waits for a
                # pointer-motion event, which never comes when the pointer is already there (a repeated click).
                moves = []
                for x, y in glide(self.pointer(), (a['x'], a['y'])): moves += ['mousemove', str(x), str(y), 'sleep', '0.012']
                self._run('xdotool', *moves, 'sleep', '0.05', 'click', '--repeat', '2' if kind == 'double_click' else '1', '--delay', '100', '1')
            elif kind == 'key':
                done = self._run('xdotool', 'key', '--clearmodifiers', a['key'])
                # xdotool ignores a key it does not know and still reports success: say so, or nothing happens silently.
                if b'No such key name' in (done.stderr or b''):
                    raise ActionFailed(f'There is no key called "{a["key"]}". Use names like Return, Escape, Tab, BackSpace, Page_Down, alt+Left, ctrl+l.')
            elif kind == 'scroll':
                self._run('xdotool', 'click', '--repeat', str(abs(a['amount'])), '--delay', '15', '5' if a['amount'] > 0 else '4')
            elif kind == 'type':
                if not a.get('secret') and KEYSTROKE_TEXT.fullmatch(a['text']):
                    # Short plain text is typed key by key, so fields that react to each key (search
                    # suggestions, validation, editors that block pasting) work as they do for a person.
                    self._run('xdotool', 'type', '--clearmodifiers', '--delay', '25', '--', a['text'], timeout=5 + len(a['text']) * 0.05)
                    return
                # Long text, several lines (Return could submit a form) and other alphabets (Hebrew) are
                # pasted. Each display has its own clipboard, so workers do not paste into each other.
                # xclip stays alive in the background to own the clipboard; capturing its output would block.
                self._clipboard(a['text'])
                self._run('xdotool', 'key', '--clearmodifiers', 'ctrl+v')
                if a.get('secret'):
                    # A saved password must not stay in the clipboard, where a later paste or a command could reveal it.
                    time.sleep(0.3); self._clipboard('')


class Store:
    """The data folder (the Docker volume): settings, chat, memory, vault and browser profile live here."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)


class Engine:
    """Runs one desktop action at a time and keeps a short activity log."""
    def __init__(self, desktop, store):
        self.desktop, self.store = desktop, store
        self.lock = threading.RLock()
        self.events = []

    def event(self, message, level='info'):
        with self.lock:
            self.events.append({'time': time.time(), 'message': message, 'level': level})
            self.events = self.events[-200:]
        ACTIVITY.write('agent', message, 'warning' if level == 'warning' else 'error' if level == 'error' else 'info')

    def act(self, a, source='manual', cancel=None, log=True):
        a = validate_action(a)
        with ACTIVITY.span('action', f"{source} {describe_action(a)}") as info:
            with self.desktop.lock:
                result = self.desktop.execute(a, cancel=cancel)
            if a['kind'] == 'run_command' and isinstance(result, tuple): info['exit'] = result[0]; info['output_chars'] = len(result[1])
        if log:
            with self.lock:
                self.events.append({'time': time.time(), 'message': f"{a['label']} · {source}", 'level': 'info'})
                self.events = self.events[-200:]
        return result  # commands return (exit code, output); installs a message
