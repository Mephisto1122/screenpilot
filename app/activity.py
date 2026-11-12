"""Activity log: everything Screenpilot does, how long each part took, and what is still in progress.

A hang shows up twice: as an entry in "now" that keeps counting, and as "still waiting after 60 s" lines.
Written to <data>/logs/screenpilot.log (one JSON object per line, rotated at 5 MB, 3 old files kept) and
to the container's output (docker compose logs). The Log tab next to the chat shows it and downloads it.

It never contains passwords, API keys, typed text, model prompts or command output.
"""
import collections
import contextlib
import itertools
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

WARN_AFTER = (20, 60, 120, 300, 600, 1800)  # seconds: "still waiting" lines for something in progress
ROTATE_BYTES = 5 * 1024 * 1024
KEEP = 3


class Activity:
    def __init__(self):
        self.lock = threading.RLock()
        self.recent = collections.deque(maxlen=4000)
        self.ids = itertools.count(1)
        self.inflight = {}
        self.local = threading.local()
        self.path = None
        self.echo = False
        self.watcher = None

    def configure(self, root, echo=True):
        """Start writing to <root>/logs/screenpilot.log (and to the container's output)."""
        folder = Path(root) / 'logs'
        folder.mkdir(parents=True, exist_ok=True)
        with self.lock: self.path, self.echo = folder / 'screenpilot.log', echo
        if self.watcher is None:
            self.watcher = threading.Thread(target=self._watch, daemon=True, name='activity-watch'); self.watcher.start()

    # ---------- which worker is this thread working for ----------
    def bind(self, worker): self.local.worker = worker

    @property
    def worker(self): return getattr(self.local, 'worker', None)

    # ---------- writing ----------
    def write(self, kind, msg, level='info', worker=..., **fields):
        entry = {'id': next(self.ids), 't': round(time.time(), 3), 'w': self.worker if worker is ... else worker,
                 'level': level, 'kind': kind, 'msg': ' '.join(str(msg).split())[:1500]}
        entry.update({k: v for k, v in fields.items() if v is not None})
        with self.lock:
            self.recent.append(entry)
            if self.path:
                try: self._append(json.dumps(entry, ensure_ascii=False))
                except OSError: pass
        if self.echo:
            try: print(self.line(entry), file=sys.stdout, flush=True)
            except Exception: pass
        return entry

    def _append(self, line):
        if self.path.exists() and self.path.stat().st_size > ROTATE_BYTES:
            for n in range(KEEP, 0, -1):
                older = self.path.with_name(f'{self.path.name}.{n}')
                newer = self.path if n == 1 else self.path.with_name(f'{self.path.name}.{n - 1}')
                if newer.exists(): os.replace(newer, older)
        with open(self.path, 'a', encoding='utf-8') as f: f.write(line + '\n')

    def error(self, kind, msg, **fields):
        """An error with the code location it came from (call inside an except block)."""
        return self.write(kind, msg, 'error', trace=traceback.format_exc()[-4000:], **fields)

    @contextlib.contextmanager
    def span(self, kind, msg, quiet=False, **fields):
        """Something that takes time. Logged when it ends, with its duration; listed in now() while it runs.
        Add results to the yielded dict. quiet: waiting is normal (for the user), so no "still waiting" lines."""
        sid, start = next(self.ids), time.monotonic()
        with self.lock:
            self.inflight[sid] = {'kind': kind, 'msg': msg, 'w': self.worker, 'start': start, 'since': time.time(), 'warned': 0, 'quiet': quiet}
        info = dict(fields)
        try:
            yield info
        except InterruptedError:
            self.write(kind, msg + ' · stopped', ms=self._ms(start), **info); raise
        except Exception as exc:
            if getattr(exc, 'expected', False):  # an outcome the agent handles (no such window): no traceback
                self.write(kind, f'{msg} · {exc}', 'warning', ms=self._ms(start), **info)
            else:
                self.write(kind, f'{msg} · failed: {type(exc).__name__}: {exc}', 'error', ms=self._ms(start),
                           trace=traceback.format_exc()[-3000:], **info)
            raise
        else:
            slow = 'warning' if self._ms(start) > 60000 and not quiet else 'info'
            self.write(kind, msg, slow, ms=self._ms(start), **info)
        finally:
            with self.lock: self.inflight.pop(sid, None)

    @staticmethod
    def _ms(start): return round((time.monotonic() - start) * 1000)

    def _watch(self):
        while True:
            time.sleep(5)
            self.check()

    def check(self):
        """Write a "still waiting" line for anything in progress longer than the next step of WARN_AFTER."""
        if True:
            now = time.monotonic()
            with self.lock: items = [(sid, dict(v)) for sid, v in self.inflight.items()]
            for sid, v in items:
                if v['quiet']: continue
                waited = now - v['start']
                due = [s for s in WARN_AFTER if s <= waited and s > v['warned']]
                if due:
                    with self.lock:
                        if sid in self.inflight: self.inflight[sid]['warned'] = due[-1]
                    self.write('hang', f'Still waiting after {waited:.0f} s: {v["msg"]}', 'warning', worker=v['w'], of=v['kind'])

    # ---------- reading ----------
    def now(self, worker=None):
        t = time.monotonic()
        with self.lock:
            return [{'w': v['w'], 'kind': v['kind'], 'msg': v['msg'], 'seconds': round(t - v['start'], 1), 'since': v['since']}
                    for v in self.inflight.values() if worker is None or v['w'] in (worker, None)]

    def entries(self, after=0, worker=None, limit=400):
        with self.lock:
            out = [e for e in self.recent if e['id'] > after and (worker is None or e['w'] in (worker, None))]
        return out[-limit:]

    @staticmethod
    def line(e):
        stamp = time.strftime('%H:%M:%S', time.localtime(e['t']))
        who = f"W{e['w']}" if e.get('w') else '  '
        took = f" ({e['ms'] / 1000:.1f} s)" if 'ms' in e else ''
        extra = ' '.join(f'{k}={v}' for k, v in e.items() if k not in ('id', 't', 'w', 'level', 'kind', 'msg', 'ms', 'trace'))
        text = f"{stamp} {who} {e['level'].upper():7} {e['kind']}: {e['msg']}{took}" + (f'  [{extra}]' if extra else '')
        return text + ('\n' + e['trace'].rstrip() if e.get('trace') else '')

    def download(self):
        """The whole log as readable text, oldest first (the rotated files too)."""
        if not self.path: return '\n'.join(self.line(e) for e in self.recent) + '\n'
        lines = []
        for n in range(KEEP, -1, -1):
            f = self.path if n == 0 else self.path.with_name(f'{self.path.name}.{n}')
            if not f.exists(): continue
            for raw in f.read_text(encoding='utf-8', errors='replace').splitlines():
                try: lines.append(self.line(json.loads(raw)))
                except ValueError: lines.append(raw)
        return '\n'.join(lines) + '\n'


ACTIVITY = Activity()
