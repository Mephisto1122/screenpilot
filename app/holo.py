"""Holo (H Company): open models that excel at GUI grounding — given a described target and a screenshot, they
point at it precisely. Apache-2.0, run in Ollama on the Mac with vision bundled (ahmadwaqar/holo-3.1:4b).

Holo is used as Screenpilot's precise "finder": a reasoner says what to click (in words), Holo says exactly
where. It is a grounding model, not a full agent, so it only returns a point. Coordinates are 0-1000.
"""
import json
import re

DEFAULT_MODEL = 'ahmadwaqar/holo-3.1:4b'
SYSTEM = ('You locate a single element on a screenshot. Given an instruction, output ONLY a JSON object '
          '{"x": N, "y": N} where x and y are integers from 0 to 1000 (0,0 is the top-left, 1000,1000 the '
          'bottom-right): the point to click to carry out the instruction. No other text.')
POINT = re.compile(r'["\']?x["\']?\s*:\s*(\d+)\s*,\s*["\']?y["\']?\s*:\s*(\d+)', re.I)


def prompt(target):
    return SYSTEM + '\nInstruction: click ' + str(target).strip()


def parse_point(text, width=1280, height=800):
    """Holo's reply -> (x, y) in screen pixels, or None. Accepts JSON, a loose {x:..,y:..}, and a <think> prefix."""
    text = re.sub(r'(?s)<think>.*?</think>', '', text or '').strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and 'x' in obj and 'y' in obj:
            return _scale(obj['x'], obj['y'], width, height)
    except (ValueError, TypeError):
        pass
    m = POINT.search(text)
    return _scale(m.group(1), m.group(2), width, height) if m else None


def _scale(x, y, width, height):
    try:
        x, y = int(x), int(y)
    except (ValueError, TypeError):
        return None
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        return None
    return int(round(x / 1000 * width)), int(round(y / 1000 * height))
