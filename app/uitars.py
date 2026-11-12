"""UI-TARS: an open-source model trained end to end for computer control (ByteDance), run locally in Ollama.

Unlike the general vision model (which we steer with our own prompt and then locate elements for), UI-TARS
looks at the screenshot and writes one action itself, with the coordinates — it does its own grounding and
its own per-step reasoning. So the screenshot never leaves the Mac, and there is no describe-then-point dance.

It writes:
    Thought: <why>
    Action: click(start_box='(638,420)')

This module turns that into Screenpilot's own action dicts. Coordinates are read on a 0-1000 grid by default
(UI-TARS-1.5), or as pixels of the screenshot we sent; set uitars_coords to switch.
"""
import re

WIDTH, HEIGHT = 1280, 800

PROMPT = '''You are a GUI agent operating a {w}x{h} Linux desktop (Debian). You get the task and a screenshot,
and you output your reasoning and ONE action.

## Output format (exactly two lines)
Thought: <ONE short sentence: what you will do>
Action: <one action from the space below>

You MUST end every reply with an Action line. Keep the Thought to one sentence so you reach the Action.

## Action space
click(start_box='(x,y)')
left_double(start_box='(x,y)')
right_single(start_box='(x,y)')
type(content='text to type into the focused field')
hotkey(key='ctrl l')
scroll(start_box='(x,y)', direction='down')   # or up, left, right
wait()
finished(content='the answer or a short summary for the user')
call_user(content='what only the user can do, e.g. a CAPTCHA or a code')

## Rules
- Coordinates are on a 0-1000 grid: (0,0) is the top-left, (1000,1000) the bottom-right.
- ONE action per step. After it you get a fresh screenshot; do not plan several ahead.
- Keys and typing go to the ACTIVE window. To open a URL: click the address bar (or hotkey ctrl l), type it, hotkey Return.
- Use finished(...) only when the whole task is complete.
{desktop}'''


def prompt(task, history, desktop=''):
    body = PROMPT.format(w=WIDTH, h=HEIGHT, desktop=('\nDesktop now: ' + desktop if desktop else ''))
    parts = [body, '\n## Task\n' + task]
    if history: parts.append('\n## What you did already\n' + history)
    return ''.join(parts)


def history_text(steps, limit=6):
    """A compact 'Thought … Action …' trail, which is how UI-TARS expects its own history."""
    lines = []
    for s in steps[-limit:]:
        note = str(s.get('note') or s.get('label') or '').strip()
        lines.append(f'Thought: {note[:160]}' if note else 'Action: ' + _describe(s))
        if note: lines.append('Action: ' + _describe(s))
    return '\n'.join(lines)


def _describe(a):
    k = a.get('kind')
    if k in ('click', 'double_click') and a.get('x') is not None: return f"click({a['x']},{a['y']})"
    if k == 'type': return f"type({str(a.get('text',''))[:40]!r})"
    if k == 'key': return f"hotkey({a.get('key')})"
    if k == 'scroll': return f"scroll({'up' if a.get('amount',1) < 0 else 'down'})"
    return str(k or '')


BOX = re.compile(r"\(?\s*(\d+)\s*,\s*(\d+)\s*\)?")
CALL = re.compile(r"(\w+)\s*\((.*)\)\s*$", re.S)
ARG = re.compile(r"(\w+)\s*=\s*'([^']*)'|(\w+)\s*=\s*\"([^\"]*)\"")
HOTKEY_PART = re.compile(r"[\s+]+")


def _point(box, coords, width=WIDTH, height=HEIGHT):
    m = BOX.search(box or '')
    if not m: return None
    x, y = int(m.group(1)), int(m.group(2))
    if coords == 'pixels':
        return (x, y) if 0 <= x < width and 0 <= y < height else None
    if not (0 <= x <= 1000 and 0 <= y <= 1000): return None
    return int(round(x / 1000 * width)), int(round(y / 1000 * height))


def _key(value):
    # UI-TARS writes keys space-separated ("ctrl l", "ctrl shift t"); we join with +.
    parts = [p for p in HOTKEY_PART.split(value.strip().lower()) if p]
    names = {'enter': 'Return', 'return': 'Return', 'esc': 'Escape', 'escape': 'Escape', 'tab': 'Tab',
             'space': 'space', 'backspace': 'BackSpace', 'delete': 'Delete', 'up': 'Up', 'down': 'Down',
             'left': 'Left', 'right': 'Right', 'pageup': 'Page_Up', 'pagedown': 'Page_Down', 'home': 'Home', 'end': 'End'}
    parts = [names.get(p, p) for p in parts]
    return '+'.join(parts) if parts else ''


def parse(text, coords='normalized'):
    """UI-TARS output -> {'actions':[...]} or {'goal_done':True,...} or {'stuck':True,...}, in Screenpilot's shape."""
    text = (text or '').strip()
    thought = ''
    m = re.search(r'Thought:\s*(.*?)(?:\n\s*Action:|$)', text, re.S | re.I)
    if m: thought = ' '.join(m.group(1).split())[:300]
    m = re.search(r'Action:\s*(.*)$', text, re.S | re.I)
    body = (m.group(1) if m else text).strip()
    # Sometimes several actions are separated by ')\n\n' or ';'; take the first real one.
    body = body.split('\n\n')[0].strip().rstrip(';').strip()
    call = CALL.search(body)
    if not call:
        return {'stuck': True, 'actions': [], 'note': ('UI-TARS gave no action · ' + (thought or body))[:300]}
    name = call.group(1).lower()
    args = {}
    for a, b, c, d in ARG.findall(call.group(2)):
        args[a or c] = b if a else d
    label = thought[:60] or name

    def click(kind):
        p = _point(args.get('start_box', ''), coords)
        if not p: return {'stuck': True, 'actions': [], 'note': ('UI-TARS gave no coordinates · ' + thought)[:300]}
        return {'actions': [{'kind': kind, 'x': p[0], 'y': p[1], 'label': label}], 'note': thought}

    if name in ('click', 'left_single', 'left_click'): return click('click')
    if name in ('left_double', 'double_click'): return click('double_click')
    if name in ('right_single', 'right_click'):
        # No right-click action (no context menus needed so far): look again instead of a wrong click.
        return {'stuck': True, 'actions': [], 'note': ('UI-TARS asked for a right-click, which is not supported · ' + thought)[:300]}
    if name == 'type':
        content = args.get('content', '')
        if not content: return {'stuck': True, 'actions': [], 'note': 'UI-TARS type had no content'}
        return {'actions': [{'kind': 'type', 'text': content, 'label': label}], 'note': thought}
    if name == 'hotkey':
        key = _key(args.get('key', ''))
        if not key: return {'stuck': True, 'actions': [], 'note': 'UI-TARS hotkey had no key'}
        return {'actions': [{'kind': 'key', 'key': key, 'label': 'press ' + key}], 'note': thought}
    if name == 'scroll':
        direction = args.get('direction', 'down').lower()
        amount = {'down': 3, 'up': -3, 'left': -3, 'right': 3}.get(direction, 3)
        return {'actions': [{'kind': 'scroll', 'amount': amount, 'label': 'scroll ' + direction}], 'note': thought}
    if name == 'wait':
        return {'actions': [{'kind': 'wait', 'seconds': 3, 'label': 'wait'}], 'note': thought}
    if name in ('finished', 'done', 'finish'):
        return {'goal_done': True, 'actions': [], 'note': (args.get('content') or thought or 'Done.')[:2000]}
    if name in ('call_user', 'ask_user'):
        return {'actions': [{'kind': 'ask_user', 'reason': (args.get('content') or thought or 'Please take over')[:300]}], 'note': thought}
    return {'stuck': True, 'actions': [], 'note': ('UI-TARS used an unknown action: ' + name)[:300]}
