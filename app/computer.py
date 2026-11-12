"""The agent's computer: the user its desktop runs as, its home folder, commands, files and app installs.

In Docker, Screenpilot itself runs as root and every worker's desktop (screen, browser, apps, commands)
runs as the user `desk`. That user cannot read Screenpilot's own files (settings, saved passwords, the
mailbox password, the Telegram token), and API keys are not in its environment. So the agent can have a
terminal and install apps without ever reaching them.

Outside Docker (development, tests) everything runs as the current user and its home folder is left alone.
"""
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from app.activity import ACTIVITY
from app.core import ActionFailed, atomic_json

DESK_USER = os.getenv('SCREENPILOT_DESK_USER', 'desk')
CONFIG = Path(__file__).parent / 'desktop'
SHARED_DIRS = ('Documents', 'Downloads')  # kept in the data volume, shared by all workers
PACKAGE = re.compile(r'[a-z0-9][a-z0-9+.-]{1,62}')
PARTIAL = ('.crdownload', '.part', '.partial', '.download', '.tmp')
PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'


def desk_account():
    """The desktop's own user, when Screenpilot runs as root (in Docker). None: run as the current user."""
    if os.geteuid() != 0: return None
    try: return pwd.getpwnam(DESK_USER)
    except KeyError: return None


def clip(text, limit=4000):
    """Long command output keeps its start and its end (where errors and results usually are)."""
    text = text.strip()
    if len(text) <= limit: return text
    return text[:limit // 3] + f'\n… [{len(text) - limit} characters left out] …\n' + text[-(limit - limit // 3):]


def human_size(n):
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB': return f'{n:.0f} {unit}' if unit == 'bytes' else f'{n:.1f} {unit}'
        n /= 1024


class Computer:
    """What all workers share: one home folder (with Documents and Downloads) and one set of installed apps."""
    def __init__(self, data_root):
        self.data = Path(data_root)
        self.user = desk_account()
        self.home = Path(self.user.pw_dir) if self.user else Path.home()
        self.apt_lock = threading.Lock()
        self.apt_ready = False
        self.apps_file = self.data / 'apps.json'

    @property
    def downloads(self): return self.home / 'Downloads'

    # ---------- the desktop user ----------
    def as_user(self):
        """Popen arguments for a process that belongs to the desktop (its user, normal file permissions)."""
        if not self.user: return {}
        return {'user': self.user.pw_uid, 'group': self.user.pw_gid, 'extra_groups': [], 'umask': 0o022}

    def env(self, display):
        """The desktop's environment. Built from scratch, so API keys and server settings never reach it."""
        name = self.user.pw_name if self.user else os.getenv('USER', 'user')
        env = {'DISPLAY': display, 'HOME': str(self.home), 'USER': name, 'LOGNAME': name, 'PATH': PATH,
               'SHELL': '/bin/bash', 'LANG': 'C.UTF-8', 'TERM': 'xterm-256color'}
        if os.getenv('TZ'): env['TZ'] = os.environ['TZ']
        return env

    def give(self, path):
        """Hand a folder to the desktop user (once: skipped when it already owns it)."""
        if not self.user: return
        path = Path(path)
        try:
            if path.stat().st_uid == self.user.pw_uid: return
        except OSError:
            return
        for root, dirs, files in os.walk(path):
            for name in [root, *(os.path.join(root, n) for n in dirs + files)]:
                try: os.lchown(name, self.user.pw_uid, self.user.pw_gid)
                except OSError: pass

    def prepare(self):
        """The desktop's whole home folder lives in the data volume (so everything the agent sets up there
        survives restarts and updates), with Documents and Downloads, default apps for files, and Screenpilot's
        own files made private."""
        if not self.user: return  # development: do not touch the developer's home folder
        self._persistent_home()
        for name in SHARED_DIRS:
            target = self.data / name
            target.mkdir(exist_ok=True)
            self.give(target)
            link = self.home / name
            if link.is_symlink() or not link.exists() or (link.is_dir() and not any(link.iterdir())):
                if link.is_symlink() or link.is_file(): link.unlink()
                elif link.is_dir(): link.rmdir()
                link.symlink_to(target)
        config = self.home / '.config'
        config.mkdir(exist_ok=True)
        mime = config / 'mimeapps.list'
        # Our defaults for opening files; kept if the agent changed them (then our first line is gone).
        if not mime.exists() or mime.read_text(errors='replace').startswith('# Which app opens which kind of file'):
            shutil.copyfile(CONFIG / 'mimeapps.list', mime)
            os.lchown(mime, self.user.pw_uid, self.user.pw_gid)
        self.give(config)
        # The data folder can be passed through (to Documents and Downloads) but not listed or read.
        os.chmod(self.data, 0o711)
        for entry in self.data.iterdir():
            if entry.name in SHARED_DIRS or entry.name == 'home' or entry.name.startswith('chromium-profile'): continue
            try: os.chmod(entry, 0o700 if entry.is_dir() else 0o600)
            except OSError: pass

    def _persistent_home(self):
        """/home/desk points to <data>/home. A new container (after an update) starts with an empty
        /home/desk from the image; the folder in the data volume keeps everything from before."""
        kept = self.data / 'home'
        if self.home.is_symlink():
            if self.home.resolve() == kept.resolve(): return
            self.home.unlink()
        if not kept.exists():
            # First start with a kept home: take what the image put there (.bashrc, .profile).
            if self.home.is_dir(): shutil.copytree(self.home, kept, symlinks=True)
            else: kept.mkdir()
            self.give(kept)  # everything in it, not only the folder
        if self.home.is_dir(): shutil.rmtree(self.home)
        self.home.symlink_to(kept)
        os.chmod(kept, 0o750)

    # ---------- commands ----------
    def run(self, command, display, seconds=60, cancel=None):
        """Run a shell command as the desktop user. Returns (exit code or None, output)."""
        with tempfile.TemporaryFile() as out:
            try:
                proc = subprocess.Popen(['bash', '-lc', command], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        env=self.env(display), cwd=self.home, start_new_session=True, **self.as_user())
            except OSError as exc:
                raise ActionFailed(f'The command could not start: {exc}')
            deadline, ended = time.monotonic() + seconds, None
            while True:
                try:
                    code = proc.wait(timeout=0.2); break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set(): ended = 'stopped'
                    elif time.monotonic() > deadline: ended = f'still running after {seconds:g} seconds, so it was stopped'
                    else: continue
                    try: os.killpg(proc.pid, signal.SIGKILL)
                    except OSError: pass
                    proc.wait(); code = None; break
            size = out.seek(0, 2); out.seek(0)
            if size <= 200_000: text = out.read().decode('utf-8', 'replace')
            else:  # huge output: its start and its end are what matter
                head = out.read(20_000); out.seek(size - 40_000)
                text = (head + f'\n… [{size - 60_000} bytes left out] …\n'.encode() + out.read()).decode('utf-8', 'replace')
        return code, clip(text) + (f'\n[{ended}]' if ended else '')

    # ---------- files ----------
    def resolve(self, path):
        p = str(path).strip()
        if p == '~' or p.startswith('~/'): p = str(self.home) + p[1:]
        return Path(p) if p.startswith('/') else self.home / p

    def open(self, path, display):
        """Open a file or folder in the app made for it (xdg-open), as the desktop user."""
        full = self.resolve(path)
        # Checked as the desktop user: it may only open what it is allowed to read.
        if subprocess.run(['test', '-r', str(full)], env=self.env(display), **self.as_user()).returncode != 0:
            raise ActionFailed(f'There is no file "{path}" (or it cannot be read). Downloads are in ~/Downloads, '
                               'your files in ~/Documents; run_command "ls ~/Downloads" lists them.')
        if not shutil.which('xdg-open'): raise ActionFailed('Opening files needs xdg-open, which is not installed')
        subprocess.Popen(['xdg-open', str(full)], env=self.env(display), cwd=self.home, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, **self.as_user())

    def download_files(self):
        """Files in Downloads: ({finished name: size}, [(name, size so far) still downloading])."""
        done, partial = {}, []
        try:
            for e in os.scandir(self.downloads):
                if e.name.startswith('.') or not e.is_file(): continue
                size = e.stat().st_size
                if e.name.endswith(PARTIAL): partial.append((e.name, size))
                else: done[e.name] = size
        except OSError:
            pass
        return done, partial

    # ---------- apps ----------
    def installed(self, package):
        r = subprocess.run(['dpkg-query', '-W', '-f=${Status}', package], capture_output=True, text=True)
        return r.stdout.endswith('install ok installed')

    def _apt(self, args, timeout):
        env = {'PATH': PATH, 'DEBIAN_FRONTEND': 'noninteractive', 'LANG': 'C.UTF-8'}
        try: return subprocess.run(['apt-get', *args], capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired: raise ActionFailed(f'apt-get {args[0]} took too long and was stopped')

    def install(self, package):
        """Install a Debian package (an app or a command-line tool). It is installed again after an update."""
        package = str(package).strip().lower()
        if not PACKAGE.fullmatch(package): raise ActionFailed(f'"{package}" is not a package name')
        if os.geteuid() != 0 or not shutil.which('apt-get'):
            raise ActionFailed('Apps can only be installed when Screenpilot runs in Docker')
        if self.installed(package):
            self._remember(package); return f'{package} is already installed'
        with self.apt_lock:
            if not self.apt_ready:
                r = self._apt(['update'], 600)
                if r.returncode: raise ActionFailed('Could not reach the package servers: ' + clip(r.stderr or r.stdout, 400))
                self.apt_ready = True
            r = self._apt(['install', '-y', '--no-install-recommends', package], 1800)
        if r.returncode:
            raise ActionFailed(f'Could not install {package}: ' + clip(r.stderr or r.stdout, 600))
        self._remember(package)
        return f'Installed {package}'

    def _remember(self, package):
        with self.apt_lock:
            apps = self.remembered()
            if package not in apps: atomic_json(self.apps_file, {'packages': sorted(apps + [package])})

    def remembered(self):
        try: return list(json.loads(self.apps_file.read_text()).get('packages', []))
        except (OSError, ValueError): return []

    def restore(self):
        """After an update the container is new: install the apps the agent had installed before."""
        for package in self.remembered():
            if not PACKAGE.fullmatch(package) or self.installed(package): continue
            try:
                with ACTIVITY.span('computer', f'Installing {package} again (it was installed before the update)'): self.install(package)
            except ActionFailed as exc:
                ACTIVITY.write('computer', f'Could not install {package} again: {exc}', 'warning', worker=None)
