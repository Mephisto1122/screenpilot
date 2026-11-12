"""Know-how: how an expert goes about a kind of work, given to the agent when the task calls for it.

These are not the replayable skills of skills.py (recorded clicks of a task done once). They are short written
playbooks: how to search the web, how to find people and accounts, how to dig for what is hard to find, how to
draw conclusions and find insights, how to carry out actions on a site, how to work the way a careful person does.

The built-in ones are the files in app/playbooks. The user can switch each one off and add their own (kept in
knowhow.json in the data folder). When a task is planned, the planner picks the ones the task needs (at most three);
their text then goes into every step's prompt, in the part that stays the same and is charged at the cached rate.
One is always given to the planner (use: planning) and one to every task (use: always).
"""
import json
import re
import threading
import uuid
from pathlib import Path

from app.core import atomic_json

BUILT_IN = Path(__file__).parent / 'playbooks'
MAX_PICKED = 3
MAX_TEXT = 4000   # characters of one playbook


def parse(text, pid, builtin=True):
    head, _, body = text.partition('\n---\n')
    fields = dict((k.strip(), v.strip()) for k, v in (line.split(':', 1) for line in head.splitlines() if ':' in line))
    return {'id': pid, 'name': fields.get('name', pid), 'when': fields.get('when', ''), 'use': fields.get('use', 'chosen'),
            'words': fields.get('words', ''), 'text': body.strip()[:MAX_TEXT], 'builtin': builtin,
            'order': int(fields['order']) if fields.get('order', '').isdigit() else 99}


class Knowhow:
    def __init__(self, root):
        self.path = Path(root) / 'knowhow.json'
        self.lock = threading.RLock()
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.own = [p for p in saved.get('own', []) if isinstance(p, dict) and p.get('id') and p.get('text')]
        self.off = set(saved.get('off', []))
        self.built_in = sorted((parse(f.read_text(encoding='utf-8'), f.stem) for f in sorted(BUILT_IN.glob('*.md'))), key=lambda p: p['order'])

    def _save(self): atomic_json(self.path, {'own': self.own, 'off': sorted(self.off)})

    def list(self):
        with self.lock:
            return [{**p, 'on': p['id'] not in self.off} for p in self.built_in + [{'use': 'chosen', 'words': '', 'builtin': False, **p} for p in self.own]]

    def _on(self, use=None):
        return [p for p in self.list() if p['on'] and (use is None or p['use'] == use)]

    def add(self, name, when, text):
        name, when, text = ' '.join(str(name).split())[:80], ' '.join(str(when).split())[:300], str(text).strip()[:MAX_TEXT]
        if not name or not when or len(text) < 20: raise ValueError('Give it a name, say when to use it, and write the know-how itself')
        with self.lock:
            if len(self.own) >= 40: raise ValueError('That is a lot of know-how. Remove one first.')
            item = {'id': 'own-' + uuid.uuid4().hex[:8], 'name': name, 'when': when, 'text': text}
            self.own.append(item); self._save()
        return item

    def delete(self, pid):
        with self.lock:
            self.own = [p for p in self.own if p['id'] != pid]; self.off.discard(pid); self._save()

    def switch(self, pid, on):
        with self.lock:
            if not any(p['id'] == pid for p in self.list()): raise ValueError('Unknown know-how')
            (self.off.discard if on else self.off.add)(pid); self._save()

    # ---------- for a task ----------
    def menu(self):
        """For the planner: what it can give the agent, one line each."""
        return '\n'.join(f"- {p['id']}: {p['name']}. {p['when']}" for p in self._on('chosen'))

    def for_planning(self):
        return '\n\n'.join(p['text'] for p in self._on('planning'))

    def guess(self, task):
        """Without a planner to choose: the ones whose trigger words are in the task."""
        hits = [(len(re.findall(p['words'], task, re.I)), p['id']) for p in self._on('chosen') if p['words']]
        return [pid for n, pid in sorted(hits, reverse=True) if n][:MAX_PICKED]

    def pick(self, ids):
        """The playbooks for a task: those always given, then the chosen ones (unknown or switched-off ids are dropped)."""
        chosen = {p['id']: p for p in self._on('chosen')}
        wanted = [chosen[i] for i in dict.fromkeys(str(i) for i in ids or []) if i in chosen][:MAX_PICKED]
        return self._on('always') + wanted

    @staticmethod
    def text(picked):
        return '\n\n'.join(f"{p['name'].upper()}\n{p['text']}" for p in picked)
