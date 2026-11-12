"""This Mac's eyes for a cloud model that never gets the screenshot ("Claude, text only").

Tesseract and OpenCV give the cloud model the screen's text and boxes. The local vision model (Qwen3-VL in
Ollama, on this Mac) adds what text alone cannot say: what the screen is, what is in the way (a popup, a
cookie banner, a CAPTCHA), and what the icons without text are. On request it also answers questions about
the screen ("look") and finds things by description ("click the cart icon"). Screenshots stay on this Mac.
"""
import threading
import time

from app import localexec, vision
from app.activity import ACTIVITY
from app.models import BadReply

SEE_PROMPT = '''This is a computer screen.
1. In one or two short sentences: which app or website is this, what kind of page or window, and is anything in
the way (a popup, a dialog, a cookie banner, a login wall, a CAPTCHA, a loading spinner, an error)?
2. Icons without text have red numbers. For each number, say in one to four words what the icon shows or does
(for example "search", "shopping cart", "close", "menu", "user profile", "play video", "settings").
Answer JSON: {"screen": "...", "icons": [{"mark": 12, "name": "search"}]}'''
SEE_SCHEMA = {'type': 'object', 'properties': {
    'screen': {'type': 'string'},
    'icons': {'type': 'array', 'items': {'type': 'object', 'properties': {'mark': {'type': 'integer'}, 'name': {'type': 'string'}},
                                         'required': ['mark', 'name']}}},
    'required': ['screen', 'icons']}
LOOK_PROMPT = 'Look at this computer screen and answer in one or two short sentences: {question}'
ICONS = 24  # at most this many unnamed icons are asked about per screen


def unnamed(items):
    """Boxes without any text that are about the size of an icon or a small button."""
    out = []
    for i in items:
        x0, y0, x1, y1 = i['box']
        if not i['text'] and i['kind'] in ('box', 'button') and 10 <= x1 - x0 <= 160 and 10 <= y1 - y0 <= 120:
            out.append(i)
    return out[:ICONS]


def _iou(a, b):
    ix, iy = max(0, min(a[2], b[2]) - max(a[0], b[0])), max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0


class Eyes:
    """One per agent run. The Mac looks in the background and never holds up the cloud model: a step waits at
    most BUDGET seconds for the look at its screen; if it is not ready, the step goes on with the text and a later
    step on the same page gets the look. A screen that barely changed reuses the description."""
    BUDGET = 4.0   # seconds a step waits for the Mac's look at its screen
    GIVE_UP = 25   # a look still running after this means the Mac is struggling: stop looking for this task
    SLOW_HINT = ('This Mac took too long to look at the screen, so Claude goes on with the screen text alone. '
                 'One look should take a few seconds: Ollama may be short of memory or not using the GPU. '
                 'Try qwen3-vl:4b, close other apps, or turn the look off in Settings → Models.')

    def __init__(self, client, run, cfg, cancel=None):
        self.client, self.run = client, run
        self.cancel = threading.Event()  # set when the task ends, so a look still running is dropped
        # Qwen3-VL points on a 0-1000 grid, other models in pixels of this image.
        self.cfg = {**cfg, 'local_image_width': 1280, 'local_timeout_seconds': min(120, cfg.get('local_timeout_seconds', 120)),
                    'local_output_tokens': max(700, cfg.get('local_output_tokens', 256))}  # room for two dozen icon names
        self.relative = localexec.relative_coordinates(cfg['local_model'])
        self.off = None   # why the local model is not used (Ollama not running, too slow); the agent goes on without it
        self.last = None  # (texts on the screen, window title, what was seen)
        self.lock = threading.Lock()
        self.job = None   # the look in progress: {'texts', 'title', 'done'}
        self.took = None  # seconds the last look took

    def close(self): self.cancel.set()

    def _call(self, prompt, image, purpose, **kw):
        return self.client.call('local', prompt, image, self.run, purpose, self.cfg, cancel=self.cancel, **kw)

    def _known(self, texts, title):
        last = self.last
        if last and last[1] == title:
            before = last[0]
            if texts == before or (texts and before and len(texts & before) / len(texts | before) >= 0.75):
                return last[2]  # the same page, scrolled a little or slightly changed
        return None

    def see(self, im, items, title='', budget=None):
        """{'screen': summary, 'icons': {element number: name}} for this screen, or None (not ready, or no local model)."""
        if self.off: return None
        texts = {i['text'] for i in items if i['text']}
        known = self._known(texts, title)
        if known: return self._renumber(known, items)
        with self.lock:
            job = self.job
            if job is not None and not job['done'].is_set() and time.monotonic() - job['started'] > self.GIVE_UP:
                # Waiting longer only takes the Mac's memory and CPU away from everything else.
                self.off = self.SLOW_HINT; self.cancel.set()
                ACTIVITY.write('eyes', self.SLOW_HINT, 'warning', ms=round((time.monotonic() - job['started']) * 1000))
                return None
            if job is None or job['done'].is_set():
                # The local model does one thing at a time: start a look only when none is running.
                job = self.job = {'texts': texts, 'title': title, 'done': threading.Event(), 'started': time.monotonic()}
                threading.Thread(target=self._look, args=(job, im, unnamed(items), ACTIVITY.worker), daemon=True, name='mac-eyes').start()
        if job['texts'] == texts and job['title'] == title:
            job['done'].wait(self.BUDGET if budget is None else budget)
        known = self._known(texts, title)
        return self._renumber(known, items) if known else None

    def _look(self, job, im, icons, worker=None):
        ACTIVITY.bind(worker)
        start = time.monotonic()
        prompt = SEE_PROMPT + ('\nNumbered icons: ' + ', '.join(str(i['id']) for i in icons) if icons else '\nThere are no numbered icons: answer "icons": [].')
        try:
            r = self._call(prompt, vision.draw_marks(im, icons) if icons else im, 'see', schema=SEE_SCHEMA)
            by_id = {i['id']: i for i in icons}
            named = [(by_id[x['mark']]['box'], ' '.join(str(x['name']).split())[:40]) for x in r.get('icons') or []
                     if isinstance(x, dict) and x.get('mark') in by_id and str(x.get('name', '')).strip()]
            self.last = (job['texts'], job['title'], {'screen': ' '.join(str(r.get('screen', '')).split())[:400], 'icons': named})
        except BadReply:
            pass
        except InterruptedError:
            pass
        except (RuntimeError, ValueError) as exc:
            self.off = str(exc)[:200]
        except Exception as exc:  # a look must never take the task down
            if not self.cancel.is_set(): ACTIVITY.error('eyes', f'The look failed: {type(exc).__name__}: {exc}')
        finally:
            self.took = time.monotonic() - start
            if self.took > self.GIVE_UP and not self.off: self.off = self.SLOW_HINT
            if not self.cancel.is_set():  # after the task, or once it gave up, nobody is waiting for this look
                ACTIVITY.write('eyes', f'Looked at the screen ({len(icons)} icons to name)' if not self.off else 'Stopped looking: ' + self.off,
                               'info' if not self.off else 'warning', ms=round(self.took * 1000))
            job['done'].set()

    @staticmethod
    def _renumber(seen, items):
        """Icon names belong to places on the screen; element numbers change from one screen to the next."""
        icons = {}
        for box, name in seen['icons']:
            best = max(unnamed(items), key=lambda i: _iou(i['box'], box), default=None)
            if best is not None and _iou(best['box'], box) > 0.5: icons[best['id']] = name
        return {'screen': seen['screen'], 'icons': icons}

    def look(self, im, question):
        """Answer a question about the screen."""
        if self.off: return 'This computer cannot look right now: ' + self.off
        try:
            return ' '.join(self._call(LOOK_PROMPT.format(question=question), im, 'look', raw=True)['text'].split())[:600] or 'No answer.'
        except (RuntimeError, ValueError) as exc:
            return 'This computer could not look: ' + str(exc)[:200]

    def find(self, target, im, items):
        """Where something described in words is: its text on the screen, else the local model points at it."""
        el = vision.find(items, target) if items else None
        if el: return el['center']
        if self.off: return None
        prompt = (localexec.LOCATE_PROMPT_RELATIVE if self.relative else localexec.LOCATE_PROMPT).format(target=target)
        try: p = self._call(prompt, im, 'locate', schema=localexec.LOCATE_SCHEMA)
        except (RuntimeError, ValueError): return None
        x, y = p.get('x'), p.get('y')
        if isinstance(x, bool) or not isinstance(x, int) or not isinstance(y, int): return None
        if self.relative:
            if not (0 <= x <= 1000 and 0 <= y <= 1000) or (x, y) == (0, 0): return None
            x, y = localexec.from_relative(min(x, 999), min(y, 999))
        return (x, y) if 0 <= x < 1280 and 0 <= y < 800 else None
