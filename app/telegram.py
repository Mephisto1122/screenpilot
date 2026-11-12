"""Telegram bridge: talk to the agent from your phone.

Create a bot with @BotFather, paste its token in Screenpilot, then send the bot the pairing code
shown in the dashboard. Only that one paired chat can give tasks; everyone else is ignored.
"""
import json
import secrets
import threading
import uuid
from urllib.request import Request, urlopen

from app.activity import ACTIVITY
from app.contract import BRIEF_LABELS as BRIEF

API = 'https://api.telegram.org/bot{token}/{method}'
def buttons(kind, worker, ask=''):
    """Inline buttons for a question from a worker. The worker number and the question's id travel in the button
    data, so an old button can never answer a newer question."""
    tag = f'reply:{worker}:{ask}:'
    if kind == 'handoff': return [[{'text': "I'm done · continue", 'callback_data': tag + 'continue'}]]
    if kind == 'code': return [[{'text': "I didn't get a code", 'callback_data': tag + 'deny'}]]
    if kind == 'question': return [[{'text': 'Start anyway', 'callback_data': tag + 'continue'}, {'text': 'Stop the task', 'callback_data': tag + 'deny'}]]
    if kind == 'stuck': return [[{'text': 'Try again', 'callback_data': tag + 'continue'}, {'text': 'Stop the task', 'callback_data': tag + 'deny'}]]
    return [[{'text': 'Approve', 'callback_data': tag + 'approve'}, {'text': 'Deny', 'callback_data': tag + 'deny'}]]
HELP = ('Send me any task and I will do it on the Screenpilot desktop.\n'
        '/screen – current screenshot (/screen 2 for worker 2)\n/status – what every worker is doing\n'
        '/stop – stop all tasks\n/memory – what I remember')


class Telegram:
    def __init__(self, app, opener=None):
        self.app = app
        self.opener = opener or urlopen
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.offset = 0
        self.pair_code = None
        self.failures = 0
        self.error = None
        if self.token(): self.start()

    # ---------- configuration ----------
    def token(self):
        return self.app.room.vault.get('telegram_token', '')

    def chat_id(self):
        return self.app.room.value.get('telegram_chat')

    def status(self):
        return {'connected': bool(self.token()), 'bot': self.app.room.value.get('telegram_bot', ''),
                'paired': bool(self.chat_id()), 'pair_code': None if self.chat_id() else self.pair_code,
                'error': self.error}

    def connect(self, token):
        token = str(token).strip()
        if not token or len(token) > 100 or ':' not in token: raise ValueError('That does not look like a bot token from @BotFather')
        me = self.call('getMe', token=token)
        room = self.app.room
        with room.lock:
            room.vault['telegram_token'] = token; room._save_vault()
            room.value.update(telegram_bot=me.get('username', ''), telegram_chat=None)
            room.update({})
        self.pair_code = f'{secrets.randbelow(900000) + 100000}'
        self.failures = 0; self.error = None
        self.start()
        return self.status()

    def disconnect(self):
        self.stop.set()
        room = self.app.room
        with room.lock:
            room.vault.pop('telegram_token', None); room._save_vault()
            room.value.update(telegram_bot='', telegram_chat=None); room.update({})
        self.pair_code = None
        return self.status()

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop.clear()
        if not self.chat_id() and not self.pair_code: self.pair_code = f'{secrets.randbelow(900000) + 100000}'
        self.thread = threading.Thread(target=self._loop, daemon=True, name='telegram'); self.thread.start()

    def close(self):
        self.stop.set()

    def _fail(self, exc):
        message = str(exc)[:200]
        if message != self.error: ACTIVITY.write('telegram', 'Telegram: ' + message, 'warning', worker=None)  # each new problem once
        self.error = message

    # ---------- Bot API ----------
    def call(self, method, token=None, timeout=35, **params):
        req = Request(API.format(token=token or self.token(), method=method), data=json.dumps(params).encode(),
                      headers={'Content-Type': 'application/json'})
        with self.opener(req, timeout=timeout) as r: data = json.load(r)
        if not data.get('ok'): raise RuntimeError(data.get('description', 'Telegram error'))
        return data['result']

    def send(self, text, buttons=None):
        chat = self.chat_id()
        if not chat or not self.token(): return
        params = {'chat_id': chat, 'text': text[:4000]}
        if buttons: params['reply_markup'] = {'inline_keyboard': buttons}
        try: self.call('sendMessage', timeout=15, **params)
        except Exception as exc: self._fail(exc)

    def send_screen(self, caption='', worker=None):
        chat = self.chat_id()
        stream = (self.app.workers.get(worker) or self.app.main).stream if worker else self.app.main.stream
        _, jpeg = stream.next_frame(timeout=2)
        if not chat or jpeg is None: return self.send('No screen available right now.')
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (('chat_id', str(chat)), ('caption', caption[:1000])):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="screen.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode() + jpeg + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        req = Request(API.format(token=self.token(), method='sendPhoto'), data=b''.join(parts),
                      headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        try:
            with self.opener(req, timeout=30) as r: json.load(r)
        except Exception as exc: self._fail(exc)

    # ---------- incoming ----------
    def _loop(self):
        while not self.stop.is_set() and self.token():
            try:
                updates = self.call('getUpdates', offset=self.offset, timeout=25, allowed_updates=['message', 'callback_query'])
                self.error = None
                for update in updates:
                    self.offset = update['update_id'] + 1
                    try: self.handle(update)
                    except Exception as exc: self._fail(exc)
            except Exception as exc:
                self._fail(exc)
                self.stop.wait(5)

    def handle(self, update):
        if 'callback_query' in update:
            q = update['callback_query']
            if str(q.get('message', {}).get('chat', {}).get('id')) != str(self.chat_id()): return
            parts = str(q.get('data', '')).split(':')  # reply:<worker>:<question>:<answer> (older: reply:<worker>:<answer>, reply:<answer>)
            answer, wid = parts[-1], int(parts[1]) if len(parts) >= 3 and parts[1].isdigit() else 1
            ask = parts[2] if len(parts) == 4 else None
            try:
                worker = self.app.workers.get(wid) or self.app.main
                r = worker.runner.reply(answer, ask)
                note = r.get('message') if r.get('stale') else {'continue': 'Continuing', 'approve': 'Approved', 'deny': 'OK'}.get(answer, 'OK')
            except Exception as exc: note = str(exc)[:150]
            try: self.call('answerCallbackQuery', timeout=10, callback_query_id=q['id'], text=note)
            except Exception: pass
            return
        msg = update.get('message') or {}
        chat = msg.get('chat', {}).get('id'); text = (msg.get('text') or '').strip()
        if chat is None or not text: return
        if not self.chat_id():
            code = text.removeprefix('/start').strip()
            if self.pair_code and code == self.pair_code:
                room = self.app.room
                with room.lock: room.value['telegram_chat'] = chat; room.update({})
                self.pair_code = None
                self.send('Paired with Screenpilot. ' + HELP)
            else:
                self.failures += 1
                if self.failures >= 5: self.pair_code = f'{secrets.randbelow(900000) + 100000}'; self.failures = 0
                try: self.call('sendMessage', timeout=15, chat_id=chat, text='Send the pairing code shown in Screenpilot → Settings → Telegram.')
                except Exception: pass
            return
        if str(chat) != str(self.chat_id()): return  # strangers are ignored
        command = text.split()[0].lower()
        if command in ('/start', '/help'): return self.send(HELP)
        if command == '/stop':
            self.app.stop_all(); return self.send('Stopped.')
        if command == '/screen':
            arg = text.split()[1] if len(text.split()) > 1 else ''
            wid = int(arg) if arg.isdigit() and int(arg) in self.app.workers else 1
            return self.send_screen(f'Worker {wid}', wid)
        if command == '/status':
            lines = []
            for w in self.app.summary()['workers']:
                lines.append(f"Worker {w['id']}: {w['status']}" + (f" · {w['task'][:80]}" if w['task'] and w['status'] != 'idle' else ''))
            return self.send('\n'.join(lines))
        if command == '/memory':
            items = self.app.memory.list()[-20:]
            return self.send('\n'.join('• ' + i['text'] for i in items) or 'Nothing saved yet.')
        # The same rule as the dashboard chat: an answer when the agent waits for one (a code, yes or no, what to
        # do), a correction when it is working, a new task otherwise.
        r = self.app.chat_message(text, source='telegram')
        taken = r.get('taken_as')
        if taken == 'reply': return  # the answer itself is in the chat, and goes out from there
        if taken:
            who = f" (worker {r['worker']})" if len(self.app.workers) > 1 else ''
            self.send({'code': 'Got it. Typing the code', 'approve': 'Approved', 'deny': 'OK, not doing that', 'continue': 'Continuing',
                       'answer': 'Got it. Continuing with that', 'instruction': 'Got it. I will follow that'}[taken] + who + '.')

    # ---------- outgoing (mirrors the dashboard chat) ----------
    def on_message(self, msg):
        if not self.chat_id() or msg['role'] != 'agent': return
        kind, wid = msg['kind'], msg.get('worker', 1)
        prefix = f'Worker {wid}: ' if len(self.app.workers) > 1 else ''
        if kind == 'plan':
            text = (f"You will get: {msg['deliverable']}\n" if msg.get('deliverable') else '') + (f"I decided: {' '.join(msg['assumptions'])}\n" if msg.get('assumptions') else '') + (f"Working by: {', '.join(msg['knowhow'])}\n" if msg.get('knowhow') else '') + \
                   ''.join(f"{label}: {v if isinstance(v, str) else '; '.join(v)}\n" for key, label in BRIEF if (v := (msg.get('brief') or {}).get(key))) + 'Plan:\n' + '\n'.join(f'{i + 1}. {g}' for i, g in enumerate(msg.get('goals', [])))
        elif kind == 'ask':
            text = {'handoff': 'Your turn: ', 'code': 'Code needed: ', 'stuck': 'I need your help: ', 'question': 'Before I start: '}.get(msg.get('ask'), 'Approve? ') + msg['text'] + \
                   ('\nReply here with the code.' if msg.get('ask') == 'code' else
                    '\nReply here to tell me what to do, or use the buttons.' if msg.get('ask') == 'stuck' else
                    '\nReply here with your answer, or use the buttons.' if msg.get('ask') == 'question' else '')
        else:
            text = msg['text']
        keys = buttons(msg.get('ask'), wid, msg.get('ask_id', '')) if kind == 'ask' else None
        threading.Thread(target=self._deliver, args=(prefix + text, keys, kind, wid), daemon=True).start()

    def _deliver(self, text, keys, kind, worker=1):
        if kind in ('ask', 'result'): self.send_screen(text[:1000], worker)
        if kind != 'result' or len(text) > 1000: self.send(text, keys)
        elif keys: self.send('Choose:', keys)
