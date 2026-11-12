"""Local executor for vision models running in Ollama on this Mac (Qwen3-VL, Qwen2.5-VL).

Measured on a test page, a 3B model is good at two separate things and bad at combining them:
  1. saying the next step in plain words ("Press Ctrl + T to open a new tab."), and
  2. pointing at a named element ("Search routes button" -> 115,565).
Asked to emit a full JSON action in one go, it picks the wrong kind or answers "done".
So the model describes the step, this module turns the sentence into actions, and the model is
only asked *where* each named element is. Browser chrome (address bar, new tab) uses shortcuts.
"""
import re

from app.core import app_name

THINK_PROMPT = """Task: {task}
Steps already done:
{history}

{desktop}
{room}
Text on the screen, top to bottom: {screen}

Desktop: to open an app say "Open the browser" (or Mousepad, Files, Writer, Calc, Impress, the terminal); to change windows say "Switch to the <title> window". Both work even when the taskbar is hidden. To open a file say "Open the file ~/Downloads/name.pdf". To run a command say "Run the command `ls ~/Downloads`" (you get its output). To install an app say "Install the gimp package". If a code was sent to the user's phone, say "Ask the user for the code" (click the code field first). If a QR code must be scanned with a phone, say "Ask the user to scan the QR code". Downloads go to ~/Downloads; save your work in ~/Documents. Keys go to the active window. A full-screen window hides the taskbar; F11 leaves full screen. ctrl+s saves a file.
Browser tips: ctrl+t opens a new tab. ctrl+l selects the address bar; typing there and pressing Return opens a website or searches the web. Only use fields on the page if the task is about that page.

Look at the screen. In one short sentence, what is the single next step? Say exactly what to click, type or which key to press. If the screen already shows the whole task is finished, say "The task is complete." """

LOCATE_PROMPT = 'Locate the {target} in this screenshot and output its point as JSON {{"x":..,"y":..}}.'
LOCATE_PROMPT_RELATIVE = ('Locate the {target} in this screenshot and output its centre point as JSON {{"x":..,"y":..}} '
                          'using coordinates on a 0-1000 scale, where 0,0 is the top-left and 1000,1000 the bottom-right.')


MARK_PROMPT = ('The screenshot has numbered red marks on its text and controls. Which number marks the {target}? '
               'Answer as JSON {{"mark": number}}, or {{"mark": 0}} if no mark fits.')
MARK_SCHEMA = {'type': 'object', 'properties': {'mark': {'type': 'integer'}}, 'required': ['mark']}


def relative_coordinates(model):
    """Qwen3-VL points on a 0-1000 grid (resolution independent); Qwen2.5-VL and others answer in image pixels."""
    return str(model).lower().startswith('qwen3-vl')


def from_relative(x, y, width=1280, height=800):
    return int(round(x / 1000 * width)), int(round(y / 1000 * height))


def strip_thinking(text):
    """Some local models wrap their reasoning in <think> tags before the answer."""
    return re.sub(r'(?s)<think>.*?</think>', '', text or '').strip()
LOCATE_SCHEMA = {'type': 'object', 'properties': {'x': {'type': 'integer'}, 'y': {'type': 'integer'}}, 'required': ['x', 'y']}

KEY_NAMES = {'enter': 'Return', 'return': 'Return', 'tab': 'Tab', 'escape': 'Escape', 'esc': 'Escape',
             'backspace': 'BackSpace', 'delete': 'Delete', 'space': 'space', 'spacebar': 'space',
             'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right', 'home': 'Home', 'end': 'End',
             'page down': 'Page_Down', 'pagedown': 'Page_Down', 'page up': 'Page_Up', 'pageup': 'Page_Up',
             'up arrow': 'Up', 'down arrow': 'Down', 'left arrow': 'Left', 'right arrow': 'Right',
             'arrow up': 'Up', 'arrow down': 'Down', 'arrow left': 'Left', 'arrow right': 'Right'}
MODIFIERS = {'ctrl': 'ctrl', 'control': 'ctrl', 'cmd': 'ctrl', 'command': 'ctrl', 'alt': 'alt',
             'option': 'alt', 'shift': 'shift', 'super': 'super', 'win': 'super'}
BROWSER_BAR = re.compile(r"\b(address|url|location)\s*bar\b|\bomnibox\b|\bbrowser'?s? (search|address) (bar|box)\b")
NEW_TAB = re.compile(r'\bnew (browser )?tab\b')
DONE = re.compile(r'\b(task|it|everything) (is|has been) (already )?(complete|completed|done|finished)\b|^(the task is )?(complete|done|finished)\.?$|\bno (further|more|other) (steps?|actions?)\b')
TARGET_TAIL = re.compile(r'\s+(to|so that|in order to|which|that will|for)\b.*$')
FILE_TYPES = 'pdf|txt|md|csv|json|xml|docx?|xlsx?|pptx?|odt|ods|odp|rtf|zip|tar|gz|tgz|bz2|xz|7z|rar|png|jpe?g|gif|webp|bmp|svg|mp4|webm|mp3|wav|ogg|py|sh'
OPEN_APP = re.compile(r"\b(?:open|launch|start|run|bring up)\s+(?:up\s+)?(?:the\s+|a\s+(?:new\s+)?)?([a-z][a-z ]{1,30}?)(?:\s+(?:app|application|program))?(?:\s+(?:from|in|on|to|and|so|by)\b.*)?$", re.I)
SWITCH = re.compile(r"\b(?:switch|go back|go|change)\s+(?:back\s+)?to\s+(?:the\s+)?(.+?)(?:\s+window)?$", re.I)


def key_name(phrase):
    p = phrase.strip().lower().strip('.\'" ')
    p = re.sub(r'\s+key$', '', p)
    p = re.sub(r'^the\s+', '', p)
    if p in KEY_NAMES: return KEY_NAMES[p]
    parts = [x.strip() for x in re.split(r'\s*\+\s*|\s*-\s*(?=\w)', p) if x.strip()]
    if len(parts) >= 2 and all(x in MODIFIERS for x in parts[:-1]):
        last = parts[-1]
        last = KEY_NAMES.get(last, last)
        if re.fullmatch(r'[a-z0-9]|F\d{1,2}|f\d{1,2}|Return|Tab|Escape|BackSpace|Delete|space|Up|Down|Left|Right|Home|End|Page_Up|Page_Down', last):
            return '+'.join(MODIFIERS[x] for x in parts[:-1]) + '+' + last
    if re.fullmatch(r'f\d{1,2}', p): return p.upper()
    return None


def clean_target(t):
    t = TARGET_TAIL.sub('', t.strip())
    t = re.sub(r'^(on|at)\s+', '', t)
    t = re.sub(r'^(the|a|an)\s+', '', t)
    t = re.sub(r'\s+(on|in) (the )?(screen|page|window)$', '', t)
    return t.strip(' .,:;\'"')


def parse_step(sentence):
    """Turn one plain-language step into ops: ('key', k) ('type', text, target|None) ('click', target) ('scroll', n) ('done',)."""
    s = (sentence or '').strip().splitlines()[0] if (sentence or '').strip() else ''
    s = s.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
    # A command in backticks is run as one piece (it may contain "and", commas or quotes).
    m = re.search(r'\b(?:run|execute|enter|type)\b[^`]*`([^`]+)`', s, re.I)
    if m: return [('run', m.group(1).strip())]
    lead = r'^(?:(?:now|next|then|first|finally),?\s+)?'
    m = re.search(lead + r'ask the user (?:for|to (?:give|send|enter|type)(?: me)?)\s+(?:the\s+|a\s+)?(.*\bcode\b.*)$', s.strip().rstrip('.'), re.I)
    if m and not re.search(r'\bqr\b', m.group(1), re.I): return [('ask_code', s.strip()[:300])]
    m = re.search(lead + r'ask the user to (.+)$', s.strip().rstrip('.'), re.I)
    if m: return [('ask_user', m.group(1)[:300])]
    m = re.search(lead + r'install\s+(?:the\s+)?["\']?([A-Za-z0-9][A-Za-z0-9+.-]{1,62})["\']?(?:\s+(?:package|app|application|program))?\b', s.strip(), re.I)
    if m and m.group(1).lower() not in ('it', 'the', 'an', 'a', 'app', 'package'): return [('install', m.group(1).lower())]
    m = re.search(lead + r'open\s+(?:the\s+|my\s+)?(downloads|documents)\s+folder\b', s.strip(), re.I)
    if m: return [('open_file', '~/' + m.group(1).capitalize())]
    m = re.search(lead + r'open\s+(?:the\s+)?(?:file|folder|document|pdf|image|archive|picture)?\s*["\']?((?:~|/)\S*|\S+\.(?:' + FILE_TYPES + r'))["\']?(?:\s+(?:in|with)\b.*)?\.?$', s.strip(), re.I)
    if m: return [('open_file', m.group(1).rstrip('.,'))]
    if DONE.search(s.lower()): return [('done',)]
    quoted = []
    def keep(m):
        quoted.append(m.group(2)); return f'\x00{len(quoted)-1}\x00'
    # Quoted text (typed text or element names); apostrophes inside words are left alone.
    s2 = re.sub(r"(?<![\w])(['\"])(.+?)\1(?![\w])", keep, s)
    def unq(t): return re.sub(r'\x00(\d+)\x00', lambda m: quoted[int(m.group(1))], t)
    clauses = [c for c in re.split(r'\s*(?:,\s*(?:and\s+)?then\b|,\s*and\b|\bthen\b|\band\b|;|,)\s*', s2) if c.strip()]
    ops = []
    for c in clauses:
        low = c.lower().strip().rstrip('.')
        m = re.match(r"^(?:you should |we need to |next,? |now,? |first,? |finally,? )*(press(?:ing)?|hit|use)\s+(?:the\s+)?(.+?)(?:\s+(?:key|keys|shortcut))?(?:\s+(?:to|so|on|in)\b.*)?$", low)
        if m:
            k = key_name(unq(m.group(2)))
            if k: ops.append(('key', k)); continue
        m = re.search(r"\b(?:go to|navigate to|visit|open the website|open website|open (?:the )?(?:page |site )?(?=https?://|www\.))\s*(\S+\.\w{2,}\S*)", unq(c), re.I)
        if m:
            ops += [('key', 'ctrl+l'), ('type', m.group(1).rstrip('.,'), None), ('key', 'Return')]; continue
        m = re.search(r"\b(?:type|enter|write|input|search for|fill in)\s+(.+)$", c, re.I)
        if m and '\x00' in m.group(1):
            rest = m.group(1)
            qm = re.search(r'\x00(\d+)\x00', rest)
            text = quoted[int(qm.group(1))]
            tm = re.search(r"\b(?:in|into|inside|on)\s+(.+)$", rest[qm.end():], re.I)
            ops.append(('type', text, clean_target(unq(tm.group(1))) if tm else None)); continue
        m = re.search(r"\b(?:type|write|input|search for)\s+(\w[\w .'-]{0,60}?)\s+(?:in|into)\s+(.+)$", unq(c), re.I)
        if m and re.fullmatch(r"(?:the |a )?(?:verification |confirmation |sms |login )?(?:code|otp|pin)", m.group(1).strip(), re.I):
            ops.append(('ask_code', 'Send me the code you received'))  # the model does not know the code: the user does
            continue
        if m:
            ops.append(('type', m.group(1).strip(), clean_target(m.group(2)))); continue
        m = re.search(r"\bscroll\s+(down|up)\b", low)
        if m: ops.append(('scroll', 3 if m.group(1) == 'down' else -3)); continue
        # Apps and windows by name: works without the taskbar (it may be hidden by a full-screen window).
        m = OPEN_APP.search(unq(c).rstrip('.'))
        if m and app_name(m.group(1)): ops.append(('open_app', app_name(m.group(1)))); continue
        m = SWITCH.search(unq(c).rstrip('.'))
        if m and 'window' in low and not re.search(r'\btab\b', m.group(1).lower()):
            ops.append(('switch', clean_target(m.group(1)))); continue
        m = re.search(r"\b(?:double[- ]click|click|tap|select|choose|check|open|press)(?:\s+on)?\s+(.+)$", unq(c), re.I)
        if m:
            target = clean_target(m.group(1))
            if target and NEW_TAB.search(target.lower()) and 'button' not in target.lower(): ops.append(('key', 'ctrl+t')); continue
            launcher = re.match(r'(.+?)(?:\s+(?:icon|launcher|button))?\s+(?:in|on|from)\s+(?:the\s+)?(?:taskbar|panel|dock)$', target or '', re.I)
            if launcher and app_name(launcher.group(1)):
                ops.append(('open_app', app_name(launcher.group(1)))); continue  # same result, and no taskbar needed
            if target: ops.append(('click', target)); continue
    return ops[:4]


def is_browser_bar(target):
    return bool(target and BROWSER_BAR.search(target.lower()))
