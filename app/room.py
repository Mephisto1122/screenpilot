"""The agent's room: its profile, saved logins, its own mailbox and phone notifications.

Secrets (login passwords, the mailbox app password) live in /data/vault.json with owner-only
permissions. They are never returned by the API, never put in a model prompt, and never written
to logs: the agent asks for a login by name and Screenpilot types it.
"""
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import threading
import time
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

from app.core import atomic_json

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,59}$')
DEFAULT_ROOM = {'profile': '', 'phone': '', 'mail_address': '', 'imap_host': 'imap.gmail.com', 'ntfy_topic': '',
                'require_approval': True}
PHONE = re.compile(r'\+?[0-9][0-9 ().-]{5,24}')


class Room:
    def __init__(self, root, opener=None):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.path = self.root / 'room.json'
        self.vault_path = self.root / 'vault.json'
        self.value = dict(DEFAULT_ROOM)
        if self.path.exists(): self.value.update(json.loads(self.path.read_text()))
        self.vault = json.loads(self.vault_path.read_text()) if self.vault_path.exists() else {'logins': {}, 'mail_password': ''}
        self.opener = opener or urlopen
        self.imap = imaplib.IMAP4_SSL

    # ---------- persistence ----------
    def _save_vault(self):
        tmp = self.vault_path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(self.vault, f)
        os.chmod(tmp, 0o600); tmp.replace(self.vault_path)

    def public(self):
        """What the dashboard may see: no passwords."""
        with self.lock:
            return {**self.value, 'mail_configured': bool(self.value['mail_address'] and self.vault.get('mail_password')),
                    'logins': [{'name': n, 'site': l.get('site', ''), 'username': l.get('username', ''),
                                'has_password': bool(l.get('password'))} for n, l in sorted(self.vault['logins'].items())]}

    def update(self, data):
        with self.lock:
            v = dict(self.value)
            if 'profile' in data:
                v['profile'] = str(data['profile'])[:4000]
            for key, limit in (('mail_address', 200), ('imap_host', 200), ('ntfy_topic', 120)):
                if key in data: v[key] = str(data[key]).strip()[:limit]
            if v['ntfy_topic'] and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', v['ntfy_topic']):
                raise ValueError('Notification topic: letters, digits, - and _ only')
            if v['imap_host'] and not re.fullmatch(r'[A-Za-z0-9.-]{1,200}', v['imap_host']):
                raise ValueError('Invalid IMAP host')
            if 'phone' in data:
                v['phone'] = ' '.join(str(data['phone']).split())[:30]
                if v['phone'] and not PHONE.fullmatch(v['phone']): raise ValueError('Phone number: digits with the country code, e.g. +972 50 123 4567')
            if 'require_approval' in data: v['require_approval'] = bool(data['require_approval'])
            if data.get('mail_password'): self.vault['mail_password'] = str(data['mail_password'])[:500]; self._save_vault()
            if data.get('clear_mail_password'): self.vault['mail_password'] = ''; self._save_vault()
            atomic_json(self.path, v); self.value = v
        return self.public()

    def save_login(self, data):
        name = str(data.get('name', '')).strip()
        if not NAME.match(name): raise ValueError('Login name: letters, digits, space . _ @ - (max 60)')
        with self.lock:
            entry = dict(self.vault['logins'].get(name, {}))
            entry['site'] = str(data.get('site', entry.get('site', ''))).strip()[:200]
            if 'username' in data: entry['username'] = str(data['username'])[:300]
            if data.get('password'): entry['password'] = str(data['password'])[:500]
            self.vault['logins'][name] = entry; self._save_vault()
        return self.public()

    def delete_login(self, name):
        with self.lock:
            self.vault['logins'].pop(str(name), None); self._save_vault()
        return self.public()

    def secret(self, name, field):
        with self.lock:
            entry = self.vault['logins'].get(name)
            if entry is None: raise ValueError(f'No saved login named "{name}"')
            if field not in ('username', 'password'): raise ValueError('Login field must be username or password')
            value = entry.get(field, '')
            if not value: raise ValueError(f'Saved login "{name}" has no {field}')
            return value

    # ---------- what the model is told ----------
    def prompt_context(self):
        with self.lock:
            v = self.value; logins = self.vault['logins']
            lines = []
            if v['profile'].strip(): lines.append('Profile to use when a form asks for details (provided by the user):\n' + v['profile'].strip())
            if v.get('phone'):
                lines.append(f"The user's phone number, for sign-ups and verification (give it when a site asks for a phone number): {v['phone']}. "
                             'A code sent to it (text message, call, WhatsApp, Telegram) reaches the user: get it with ask_code. '
                             'A QR code to scan with the phone (WhatsApp Web, Telegram Desktop): ask_user, the user sees the screen.')
            if v['mail_address']: lines.append('Your email address (the agent mailbox): ' + v['mail_address'])
            if logins: lines.append('Saved logins (fill them by name; you never see the passwords): ' +
                                    ', '.join(f"{n} ({l.get('site') or 'any site'})" for n, l in sorted(logins.items())))
            return '\n'.join(lines)

    # ---------- phone notification ----------
    def notify(self, title, message):
        topic = self.value.get('ntfy_topic')
        if not topic: return False
        try:
            req = Request('https://ntfy.sh/' + topic, data=message[:400].encode(), method='POST',
                          headers={'Title': title[:80].encode('ascii', 'ignore').decode() or 'Screenpilot', 'Tags': 'robot'})
            with self.opener(req, timeout=8): pass
            return True
        except Exception:
            return False

    # ---------- mailbox ----------
    def check_email(self, query='', limit=3):
        """Newest messages from the last 2 days matching `query` (sender or subject). Read-only."""
        with self.lock:
            address, host, password = self.value['mail_address'], self.value['imap_host'], self.vault.get('mail_password', '')
        if not (address and host and password): raise ValueError('Set up the agent mailbox in Agent room first')
        query = str(query or '').strip().lower()[:100]
        since = time.strftime('%d-%b-%Y', time.gmtime(time.time() - 2 * 86400))
        box = self.imap(host, 993, timeout=20)
        try:
            box.login(address, password)
            box.select('INBOX', readonly=True)
            status, data = box.search(None, 'SINCE', since)
            ids = (data[0] or b'').split()[-40:] if status == 'OK' else []
            found = []
            for mid in reversed(ids):
                status, parts = box.fetch(mid, '(RFC822)')
                if status != 'OK' or not parts or not isinstance(parts[0], tuple): continue
                msg = email.message_from_bytes(parts[0][1])
                sender = decode(msg.get('From', '')); subject = decode(msg.get('Subject', ''))
                if query and query not in (sender + ' ' + subject).lower(): continue
                text = body_text(msg)
                found.append({'from': sender[:120], 'subject': subject[:200], 'date': msg.get('Date', '')[:40],
                              'text': ' '.join(text.split())[:1200], 'links': links(text)[:8]})
                if len(found) >= limit: break
            return found
        finally:
            try: box.logout()
            except Exception: pass


def decode(value):
    try: return str(email.header.make_header(email.header.decode_header(value)))
    except Exception: return str(value)


def body_text(msg):
    plain, html = '', ''
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition', '').startswith('attachment'): continue
        try: payload = part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', 'replace')
        except Exception: continue
        if ctype == 'text/plain' and not plain: plain = payload
        elif ctype == 'text/html' and not html: html = payload
    if plain: return plain
    html = re.sub(r'(?is)<(script|style).*?</\1>', ' ', html)
    html = re.sub(r'(?i)<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>', r' \1 ', html)
    return unescape(re.sub(r'<[^>]+>', ' ', html))


def links(text):
    seen = []
    for url in re.findall(r'https?://[^\s<>"\')\]]+', text):
        url = url.rstrip('.,;')
        if url not in seen: seen.append(url)
    return seen
