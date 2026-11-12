"""Conversation with the agent and its long-term memory, both persisted in the data volume."""
import json
import threading
import time
import uuid
from pathlib import Path

from app.core import atomic_json

KINDS = ('text', 'plan', 'ask', 'result', 'status', 'error')


class Conversation:
    """The chat thread shown under the screen (and mirrored to Telegram)."""
    def __init__(self, root, limit=500, name='chat.json'):
        self.path = Path(root) / name
        self.lock = threading.RLock()
        self.limit = limit
        self.listeners = []
        self.messages = json.loads(self.path.read_text()) if self.path.exists() else []
        self.rev = len(self.messages)

    def add(self, role, text, kind='text', **extra):
        if role not in ('user', 'agent'): raise ValueError('role must be user or agent')
        if kind not in KINDS: kind = 'text'
        msg = {'id': uuid.uuid4().hex[:12], 'role': role, 'kind': kind, 'text': str(text)[:6000], 'time': time.time(), **extra}
        with self.lock:
            self.messages.append(msg)
            self.messages = self.messages[-self.limit:]
            self.rev += 1
            atomic_json(self.path, self.messages)
        for listener in list(self.listeners):
            try: listener(msg)
            except Exception: pass
        return msg

    def list(self, limit=200):
        with self.lock: return [dict(m) for m in self.messages[-limit:]]

    def clear(self):
        with self.lock:
            self.messages = []; self.rev += 1; atomic_json(self.path, self.messages)

    def recent_context(self, limit=8):
        """The last few real exchanges, so follow-ups like "now do the same for X" make sense."""
        with self.lock:
            useful = [m for m in self.messages if m['kind'] in ('text', 'result') or m['role'] == 'user']
        lines = [('User' if m['role'] == 'user' else 'You') + ': ' + ' '.join(m['text'].split())[:400] for m in useful[-limit:]]
        return '\n'.join(lines)


class Memory:
    """Facts the agent (or the user) chose to keep across tasks."""
    def __init__(self, root, limit=300):
        self.path = Path(root) / 'memory.json'
        self.lock = threading.RLock()
        self.limit = limit
        self.items = json.loads(self.path.read_text()) if self.path.exists() else []

    def add(self, text, source='agent'):
        text = ' '.join(str(text).split())[:500]
        if not text: raise ValueError('Memory text is empty')
        with self.lock:
            if any(i['text'].lower() == text.lower() for i in self.items): return None
            item = {'id': uuid.uuid4().hex[:10], 'text': text, 'source': source, 'time': time.time()}
            self.items.append(item); self.items = self.items[-self.limit:]
            atomic_json(self.path, self.items)
            return item

    def delete(self, item_id):
        with self.lock:
            self.items = [i for i in self.items if i['id'] != item_id]; atomic_json(self.path, self.items)

    def list(self):
        with self.lock: return [dict(i) for i in self.items]

    def prompt(self, limit=40):
        with self.lock: items = self.items[-limit:]
        return '\n'.join('- ' + i['text'] for i in items)
