"""Local "eyes": find text and clickable-looking boxes on the screen without any AI model.

Text comes from Tesseract OCR, boxes (fields, buttons, checkboxes) from OpenCV edge contours.
Each element gets a number that can be drawn on the screenshot ("set-of-mark"), so a model can
answer "click 14" instead of guessing pixel positions. Everything runs on this computer.
"""
import difflib
import hashlib
import time
import re
import shutil
import subprocess
import threading
from collections import OrderedDict

from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    import cv2
    import numpy as np
except Exception:  # OpenCV is optional; text-only marks still work
    cv2 = None

FIELD_WORDS = re.compile(r'\b(field|box|input|textbox|text box|search bar|bar|area)\b', re.I)
NOISE = re.compile(r'\b(button|link|field|box|input|textbox|checkbox|icon|tab|dropdown|menu|option|the)\b', re.I)
_cache = OrderedDict()  # recent screens -> elements; several workers read screens at the same time
_word_cache = OrderedDict()  # the same screens -> every single word with its box
CACHE_SIZE = 12
_lock = threading.Lock()


def available():
    return shutil.which('tesseract') is not None


_langs = None
def langs():
    """English, plus Hebrew when its data is installed (it is in the Docker image)."""
    global _langs
    if _langs is None:
        try: have = subprocess.run(['tesseract', '--list-langs'], capture_output=True, timeout=10).stdout.decode()
        except Exception: have = ''
        _langs = 'eng+heb' if '\nheb' in have else 'eng'
    return _langs


def _tesseract(png, psm):
    """One single-threaded Tesseract run (several run side by side)."""
    import os
    env = {**os.environ, 'OMP_THREAD_LIMIT': '1'}
    return subprocess.run(['tesseract', 'stdin', 'stdout', '--psm', str(psm), '-l', langs(), 'tsv'], input=png,
                          capture_output=True, timeout=30, env=env).stdout.decode('utf-8', 'replace')


def _words(tsv, scale, dx=0, dy=0, min_conf=40):
    for row in tsv.splitlines()[1:]:
        parts = row.split('\t')
        if len(parts) < 12: continue
        text = parts[11].strip()
        try: conf = float(parts[10])
        except ValueError: continue
        if not text or conf < min_conf or not re.search(r'[A-Za-z0-9\u0590-\u05FF]', text): continue
        x, y, w, h = (int(v) / scale for v in parts[6:10])
        yield {'text': text, 'conf': conf, 'line': tuple(parts[2:5]),
               'box': (int(x + dx), int(y + dy), int(x + w + dx), int(y + h + dy))}


def _ocr(image, scale=2, bands=3, words_out=None):
    """Words with boxes in screen pixels, joined into phrases along Tesseract's own text lines.
    The screen is read in overlapping horizontal bands at the same time, which is several times faster."""
    import io
    from concurrent.futures import ThreadPoolExecutor
    from PIL import ImageOps
    gray = ImageOps.autocontrast(image.convert('L'), cutoff=1)
    bands = max(1, min(bands, image.height // 200))  # a small piece of the screen is read in one go
    overlap, step = 48, -(-image.height // bands)
    jobs = []
    for i in range(bands):
        top, bottom = max(0, i * step - overlap), min(image.height, (i + 1) * step + overlap)
        band = gray.crop((0, top, image.width, bottom)).resize((image.width * scale, (bottom - top) * scale), Image.LANCZOS)
        buf = io.BytesIO(); band.save(buf, 'PNG'); jobs.append((buf.getvalue(), top))
    with ThreadPoolExecutor(bands) as pool:
        results = list(pool.map(lambda j: (_tesseract(j[0], 11), j[1]), jobs))
    phrases = []
    for n, (tsv, top) in enumerate(results):
        lines = {}
        for w in _words(tsv, scale, dy=top):
            lines.setdefault(w['line'], []).append(w)
            if words_out is not None: words_out.append({'text': w['text'], 'box': w['box']})
        for words in lines.values():
            words.sort(key=lambda w: w['box'][0])
            current = None
            for w in words:
                x0, y0, x1, y1 = w['box']
                if current and x0 - current['box'][2] < max(12, 1.1 * (y1 - y0)):
                    cx0, cy0, cx1, cy1 = current['box']
                    current['text'] += ' ' + w['text']; current['box'] = (cx0, min(cy0, y0), x1, max(cy1, y1))
                else:
                    current = {'text': w['text'], 'box': w['box']}; phrases.append(current)
    # Lines in an overlap were read twice (or cut in half in one band): keep the longer reading.
    phrases.sort(key=lambda p: -len(p['text']))
    kept = []
    for p in phrases:
        if any(_iou(p['box'], k['box']) > 0.3 for k in kept): continue
        kept.append(p)
    return kept


def _read_boxes(image, boxes):
    """Read many small labels at once: each crop on its own (stacking them confuses Tesseract), in parallel."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    if not boxes: return {}
    with ThreadPoolExecutor(max(2, min(8, os.cpu_count() or 2))) as pool:
        texts = list(pool.map(lambda b: _read_box(image, b), boxes))
    return {b: t for b, t in zip(boxes, texts) if t}


def _boxes(image):
    """Rectangles that look like fields, buttons or checkboxes."""
    if cv2 is None: return []
    gray = np.asarray(image.convert('L'))
    edges = cv2.Canny(gray, 30, 90)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (10 <= w <= 700 and 10 <= h <= 90) or w * h < 150: continue
        if cv2.contourArea(c) < 0.6 * w * h and not (w < 30 and h < 30): continue  # mostly-filled outline only
        box = (x, y, x + w, y + h)
        if any(_iou(box, b) > 0.6 for b in found): continue
        found.append(box)
    return found


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / float((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter or 1)


def _inside(inner, outer, pad=3):
    return inner[0] >= outer[0] - pad and inner[1] >= outer[1] - pad and inner[2] <= outer[2] + pad and inner[3] <= outer[3] + pad


def _read_box(image, box):
    """Read a single button label: an enlarged crop, read as one line."""
    import io
    x0, y0, x1, y1 = box
    from PIL import ImageOps
    # Buttons: trim the border. Tight word boxes: add room around the letters; OCR needs a margin.
    m = -2 if y1 - y0 >= 18 else 4
    crop = image.crop((max(0, x0 - m), max(0, y0 - m), min(image.width, x1 + m), min(image.height, y1 + m))).convert('L')
    crop = ImageOps.autocontrast(crop, cutoff=1)
    factor = 4 if crop.height < 24 else 3
    crop = crop.resize((crop.width * factor, crop.height * factor), Image.LANCZOS)
    crop = ImageOps.expand(crop, border=12, fill=crop.getpixel((0, 0)))
    buf = io.BytesIO(); crop.save(buf, 'PNG')
    try:
        out = _tesseract(buf.getvalue(), 7)
    except Exception:
        return ''
    words, confs = [], []
    for row in out.splitlines()[1:]:
        parts = row.split('\t')
        if len(parts) >= 12 and parts[11].strip():
            try: confs.append(float(parts[10]))
            except ValueError: continue
            words.append(parts[11].strip())
    text = ' '.join(words)
    if not confs or sum(confs) / len(confs) < 60: return ''  # unsure readings would mislead the model
    return text if re.search(r'[A-Za-z0-9\u0590-\u05FF]{2,}', text) else ''


_timing = threading.local()


def last_timing():
    """How the last elements() call in this thread spent its time (for the activity log)."""
    return dict(getattr(_timing, 'value', {}))


def elements(image):
    """Numbered screen elements: text phrases and boxes (a box holding text takes that text)."""
    key = hashlib.blake2s(image.tobytes(), digest_size=12).digest()
    with _lock:
        if key in _cache: _cache.move_to_end(key); _timing.value = {'cached': True}; return _cache[key]
    started = time.monotonic()
    single = []
    phrases = _ocr(image, words_out=single) if available() else []
    with _lock:
        _word_cache[key] = single
        while len(_word_cache) > CACHE_SIZE: _word_cache.popitem(last=False)
    text_ms = round((time.monotonic() - started) * 1000)
    boxes = []
    for b in _boxes(image):
        # Outlines of letters and words are not controls: drop boxes that sit on a line of text.
        if any(_iou(b, p['box']) > 0 and (b[3] - b[1]) <= (p['box'][3] - p['box'][1]) + 8 and
               b[0] >= p['box'][0] - 4 and b[2] <= p['box'][2] + 4 for p in phrases):
            continue
        boxes.append(b)
    items, used = [], set()
    def label_inside(b):
        inside = [i for i, p in enumerate(phrases) if _inside(p['box'], b)]
        if inside and (b[2] - b[0]) > 1.6 * max(phrases[i]['box'][2] - phrases[i]['box'][0] for i in inside) + 40: inside = []
        return inside
    unread = [b for b in boxes if not label_inside(b) and 12 <= b[2] - b[0] <= 400 and 9 <= b[3] - b[1] <= 60][:60]
    boxes_done = time.monotonic()
    read = _read_boxes(image, unread) if available() else {}
    _timing.value = {'text_ms': text_ms, 'boxes_ms': round((boxes_done - started) * 1000) - text_ms,
                     'labels_read': len(unread), 'labels_ms': round((time.monotonic() - boxes_done) * 1000)}
    for b in sorted(boxes, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
        inside = [i for i, p in enumerate(phrases) if _inside(p['box'], b)]
        if inside and (b[2] - b[0]) > 1.6 * max(phrases[i]['box'][2] - phrases[i]['box'][0] for i in inside) + 40:
            inside = []  # a large panel or field around text is not a button with that label
        text = ' '.join(phrases[i]['text'] for i in inside)
        word = False
        if not text and b in read:
            text = read[b]; word = b[3] - b[1] < 18
        if any(_inside(b, o['box'], pad=1) and o['text'] for o in items):
            continue  # word shapes inside a labelled button
        used.update(inside)
        items.append({'text': text, 'box': b, 'kind': 'text' if word else 'button' if text else 'box'})
    items += [{'text': p['text'], 'box': p['box'], 'kind': 'text'} for i, p in enumerate(phrases) if i not in used]
    # Words read one by one ("This", "Mac") become one phrase again when they sit side by side.
    items.sort(key=lambda i: (i['box'][1], i['box'][0]))
    merged = []
    for item in sorted(items, key=lambda i: (round((i['box'][1] + i['box'][3]) / 2 / 6), i['box'][0])):
        last = merged[-1] if merged else None
        if (last and last['kind'] == 'text' and item['kind'] == 'text' and last['text'] and item['text']
                and abs((last['box'][1] + last['box'][3]) - (item['box'][1] + item['box'][3])) < 8
                and 0 <= item['box'][0] - last['box'][2] < 10):
            last['text'] += ' ' + item['text']
            last['box'] = (last['box'][0], min(last['box'][1], item['box'][1]), item['box'][2], max(last['box'][3], item['box'][3]))
        else:
            merged.append(item)
    items = merged
    items = [i for i in items if i['text'] or (i['box'][2] - i['box'][0]) >= 12]
    items.sort(key=lambda i: (round(i['box'][1] / 12), i['box'][0]))
    items = items[:120]
    for n, item in enumerate(items, 1):
        item['id'] = n
        item['center'] = ((item['box'][0] + item['box'][2]) // 2, (item['box'][1] + item['box'][3]) // 2)
    with _lock:
        _cache[key] = items
        while len(_cache) > CACHE_SIZE: _cache.popitem(last=False)
    return items


def _norm(text):
    return ' '.join(re.sub(r'[^\w֐-׿ ]', ' ', NOISE.sub(' ', text.lower())).split())


def find(items, target):
    """Best element for a description like "Search routes button" or "destination field"."""
    want = _norm(target)
    if not want: return None
    best, best_score = None, 0.0
    for item in items:
        have = _norm(item['text'])
        if not have: continue
        if have == want: score = 1.0
        elif set(want.split()) <= set(have.split()):
            score = 0.8 + 0.15 * len(want) / len(have)  # every word of the description is in this element
        elif want in have or have in want: score = 0.85 * min(len(have), len(want)) / max(len(have), len(want)) + 0.15
        else: score = difflib.SequenceMatcher(None, have, want).ratio()
        if score > best_score: best, best_score = item, score
    if best is None or best_score < 0.72: return None
    if FIELD_WORDS.search(target) and best['kind'] == 'text':
        # "the destination field": the words are usually a label next to an empty box. Use the box.
        lx0, ly0, lx1, ly1 = best['box']
        near = [i for i in items if i['kind'] in ('box', 'button') and (i['box'][2] - i['box'][0]) >= 120 and
                ((0 <= i['box'][1] - ly1 <= 60 and i['box'][0] - 40 <= lx0 <= i['box'][2]) or       # box below the label
                 (0 <= i['box'][0] - lx1 <= 80 and abs(i['center'][1] - (ly0 + ly1) / 2) < 20))]    # box to the right
        if near: return min(near, key=lambda i: abs(i['box'][1] - ly1) + abs(i['box'][0] - lx0))
    return best


def words(image):
    """Every word read on the screen with its own box. elements() joins words into phrases, and the middle of a
    phrase is not where one name inside it is."""
    key = hashlib.blake2s(image.tobytes(), digest_size=12).digest()
    with _lock: got = _word_cache.get(key)
    if got is None:
        elements(image)
        with _lock: got = _word_cache.get(key) or []
    return list(got)


def _plain(word):
    return re.sub(r'[^\w֐-׿]', '', word.lower())


def locate_text(image, text, near=None):
    """Exactly where these words are on the screen: the centre of the matching word or words, not of the line they
    stand in. With several matches, the one nearest to `near` (x, y). None when they are not there."""
    want = [w for w in (_plain(p) for p in str(text).split()) if w]
    if not want or not available(): return None
    read = sorted(words(image), key=lambda w: (round((w['box'][1] + w['box'][3]) / 2 / 8), w['box'][0]))
    plain = [_plain(w['text']) for w in read]
    def same(have, need):
        if have == need: return True
        # one misread letter in a long word still counts; a short word must be exact
        return len(need) >= 6 and difflib.SequenceMatcher(None, have, need).ratio() >= 0.88
    hits = []
    for i in range(len(read) - len(want) + 1):
        if not all(same(plain[i + k], want[k]) for k in range(len(want))): continue
        span = read[i:i + len(want)]
        if any(abs((a['box'][1] + a['box'][3]) - (b['box'][1] + b['box'][3])) > 16 for a, b in zip(span, span[1:])): continue  # one line
        x0, y0 = min(w['box'][0] for w in span), min(w['box'][1] for w in span)
        x1, y1 = max(w['box'][2] for w in span), max(w['box'][3] for w in span)
        hits.append(((x0 + x1) // 2, (y0 + y1) // 2))
    if not hits: return None
    if near is None: return hits[0]
    return min(hits, key=lambda p: (p[0] - near[0]) ** 2 + (p[1] - near[1]) ** 2)


def anchor_at(items, x, y):
    """Text of the element under a click, so a saved skill can find it again on a changed screen."""
    hits = [i for i in items if i['box'][0] - 4 <= x <= i['box'][2] + 4 and i['box'][1] - 4 <= y <= i['box'][3] + 4 and i['text']]
    return min(hits, key=lambda i: (i['box'][2] - i['box'][0]) * (i['box'][3] - i['box'][1]))['text'] if hits else None


def click_crop(image, x, y, w=240, h=50):
    """A small piece of the screen around a click, kept so its label can be read later without a full-screen OCR."""
    x0, y0 = max(0, int(x) - w), max(0, int(y) - h)
    return image.crop((x0, y0, min(image.width, int(x) + w), min(image.height, int(y) + h))), x0, y0


def anchor_near(crop, x0, y0, x, y):
    """anchor_at for a click_crop: the text of the element under (x, y)."""
    return anchor_at(elements(crop), x - x0, y - y0)


def summary(items, limit=70):
    """Short text list for prompts: what the screen says, top to bottom."""
    texts = [i['text'] for i in items if i['text']][:limit]
    return ' | '.join(texts)


def draw_marks(image, items):
    marked = image.convert('RGB').copy()
    d = ImageDraw.Draw(marked)
    try: font = ImageFont.load_default(size=13)
    except TypeError: font = ImageFont.load_default()
    for item in items:
        x0, y0, x1, y1 = item['box']
        d.rectangle(item['box'], outline=(200, 30, 30), width=1)
        label = str(item['id'])
        tw = d.textlength(label, font=font)
        d.rectangle((x0, max(0, y0 - 15), x0 + tw + 6, max(15, y0)), fill=(200, 30, 30))
        d.text((x0 + 3, max(0, y0 - 15) + 1), label, fill='white', font=font)
    return marked


def describe(items, desktop='', seen=None):
    """The screen in words, for a model that does not get the screenshot (text-only mode). `desktop` is the
    window manager's line (active window, other windows, full screen, taskbar); `seen` is what this computer's
    vision model saw (a summary and names for icons without text, see eyes.py)."""
    lines = [desktop] if desktop else []
    icons = (seen or {}).get('icons', {})
    if seen and seen.get('screen'): lines.append('Seen on this computer: ' + seen['screen'])
    lines.append('Elements, top to bottom: [number] type "text" (centre x,y) size')
    for i in items:
        x0, y0, x1, y1 = i['box']
        text = (i['text'] or '').replace('"', "'")[:90]
        kind = {'button': 'button', 'box': 'box', 'text': 'text'}[i['kind']]
        size = '' if kind == 'text' else f' {x1 - x0}x{y1 - y0}'
        icon = f' icon: {icons[i["id"]]}' if i['id'] in icons else ''
        lines.append(f'[{i["id"]}] {kind} "{text}" ({i["center"][0]},{i["center"][1]}){size}{icon}')
    if not items: lines.append('(nothing readable on the screen)')
    return '\n'.join(lines)


def _small(image): return image.convert('L').resize((320, 200))


def moved(a, b, share=0.004):
    """True when two looks at the screen (as made by _small) really differ: more than a blinking cursor or a clock."""
    return sum(ImageChops.difference(a, b).histogram()[31:]) > share * 320 * 200


class Arrival:
    """Tells when something that is being waited for has arrived on the screen: the screen changed, and then stood
    still. So a wait for a page or for search results ends when they are there, not when the seconds are used up."""
    def __init__(self, image):
        self.first = self.last = _small(image); self.changed = False

    def arrived(self, image):
        now = _small(image)
        if not self.changed: self.changed = moved(self.first, now)
        elif not moved(self.last, now): return True
        self.last = now
        return False
