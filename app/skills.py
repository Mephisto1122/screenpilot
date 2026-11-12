"""Skills: tasks that worked, saved so the same task can be replayed without asking a model.

A skill stores the steps of a finished task. Clicks are stored with the text that was under the
pointer (an "anchor"), so a replay finds the button again even if the page moved. Typed text,
keys and scrolling replay as they were. Saved logins replay by name (the password is not stored).
"""
import json
import re
import threading
import time
import uuid
from pathlib import Path

from app.core import atomic_json

# A skill is a short routine that goes the same way every time (open the app, fill the form, save). Research does
# not: replaying 300 recorded clicks of one day's browsing on another day would be nonsense, and one run did save that.
MAX_STEPS = 40
REPLAYABLE = ('click', 'double_click', 'type', 'key', 'scroll', 'wait', 'fill_login', 'open_app', 'switch_window')


def normalize(task):
    return ' '.join(re.sub(r'[^\w֐-׿ ]', ' ', str(task).lower()).split())


class Skills:
    def __init__(self, root, limit=200):
        self.path = Path(root) / 'skills.json'
        self.lock = threading.RLock()
        self.limit = limit
        self.items = json.loads(self.path.read_text()) if self.path.exists() else []
        kept = [s for s in self.items if len(s.get('steps', [])) <= MAX_STEPS]
        if len(kept) != len(self.items):  # recordings of long tasks saved by an earlier version: never replay them
            self.items = kept; self._save()

    def _save(self):
        atomic_json(self.path, self.items)

    def list(self):
        with self.lock:
            return [{'id': s['id'], 'task': s['task'], 'steps': len(s['steps']), 'uses': s.get('uses', 0),
                     'created': s['created'], 'last_used': s.get('last_used')} for s in reversed(self.items)]

    def match(self, task):
        key = normalize(task)
        with self.lock:
            return next((dict(s) for s in reversed(self.items) if s['key'] == key), None)

    def save(self, task, steps):
        steps = [s for s in steps if s.get('kind') in REPLAYABLE]
        if not steps or len(steps) > MAX_STEPS: return None
        key = normalize(task)
        with self.lock:
            old = next((s for s in self.items if s['key'] == key), None)
            skill = {'id': old['id'] if old else uuid.uuid4().hex[:10], 'task': str(task)[:300], 'key': key,
                     'steps': steps[:300], 'created': time.time(), 'uses': old.get('uses', 0) if old else 0}
            self.items = [s for s in self.items if s['key'] != key] + [skill]
            self.items = self.items[-self.limit:]
            self._save()
        return skill

    def used(self, skill_id):
        with self.lock:
            for s in self.items:
                if s['id'] == skill_id: s['uses'] = s.get('uses', 0) + 1; s['last_used'] = time.time()
            self._save()

    def delete(self, skill_id):
        with self.lock:
            self.items = [s for s in self.items if s['id'] != skill_id]; self._save()
