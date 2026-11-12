"""Measure Screenpilot: the same small tasks in each mode, with success, time, tokens and cost.

    python3 bench.py                    all three modes (private, smart, fast)
    python3 bench.py private smart      only these modes
    python3 bench.py --tasks 1,3        only these tasks
    python3 bench.py --list             show the tasks

Screenpilot must be running (./run.sh). Each task has an answer that can be checked, so "done" is not taken
on the model's word. Approvals are answered yes; a task that gets stuck or needs you counts as failed and is
stopped. Skills and memories a run creates are removed again, so one mode cannot help the next.
"""
import argparse
import json
import re
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MODES = ('private', 'smart', 'fast')
# (name, what to do, a pattern the final report must contain). The answers are not in the instructions.
TASKS = (
    ('Read a page', 'Open https://example.com in the browser and tell me the main heading of the page.', r'example\s+domain'),
    ('Use a command', 'Use a terminal command to multiply 4721 by 23 and tell me the result.', r'108[,.\s]?583'),
    ('Write and read a file', 'Create the file ~/Documents/bench-note.txt containing exactly the output of the command '
                              '"echo 12 | sha1sum", then read the file and tell me its first 8 characters.', r'\bad552e6d\b'),
    ('Find a fact', 'Go to en.wikipedia.org, open the article about the Eiffel Tower and tell me the year its construction was completed.', r'\b1889\b'),
    ('Follow links', 'Open https://www.iana.org/help/example-domains and tell me the number of the RFC it refers to for example domains.', r'\b(2606|6761)\b'),
)


class Api:
    def __init__(self, url): self.url = url.rstrip('/') + '/api/'

    def call(self, path, body=None, timeout=20):
        req = Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                      headers={'Content-Type': 'application/json', 'X-Screenpilot': '1'})
        try:
            with urlopen(req, timeout=timeout) as r: return json.load(r)
        except HTTPError as exc:
            try: reason = json.load(exc).get('error', '')
            except Exception: reason = ''
            raise RuntimeError(reason or f'HTTP {exc.code}') from None


def passed(pattern, report):
    return bool(re.search(pattern, report or '', re.I))


def last_error(api):
    errors = [m['text'] for m in api.call('chat').get('messages', []) if m.get('kind') == 'error']
    return errors[-1][:160] if errors else 'no reason given'


def run_task(api, mode, instruction, pattern, limit, poll=1.0):
    """One task in one mode -> {'ok', 'status', 'seconds', 'steps', 'tokens', 'cost', 'why'}."""
    before = api.call('chat')
    old_skills, old_memory = {s['id'] for s in before.get('skills', [])}, {m['id'] for m in before.get('memory', [])}
    api.call('reset', {})
    # The tag makes every run a new task, so a skill saved by an earlier run is not replayed.
    text = f'{instruction} (benchmark run {uuid.uuid4().hex[:6]})'
    started = time.time(); why = ''
    try:
        api.call('chat', {'text': text, 'planner': mode, 'queue': True})  # always a task, never a chat reply
        agent = {}
        while True:
            time.sleep(poll)
            agent = api.call('state')['agent']
            if agent.get('task') != text[:300]:  # not started yet (the previous task is still shown)
                if time.time() - started > 30: why = 'did not start: ' + last_error(api); break
                continue
            status = agent.get('status')
            if status == 'waiting':
                w = agent.get('waiting') or {}
                if w.get('type') == 'approval': api.call('agent/reply', {'answer': 'approve', 'ask': w.get('id')}); continue
                why = {'stuck': 'got stuck: ', 'code': 'needed a code: '}.get(w.get('type'), 'needed the user: ') + str(w.get('reason', ''))[:120]
                api.call('stop', {}); break
            if status not in ('running', 'waiting'): break
            if time.time() - started > limit:
                why = f'took longer than {limit:.0f} s'; api.call('stop', {}); break
        for _ in range(20):  # let a stopped task settle
            agent = api.call('state')['agent']
            if agent.get('status') not in ('running', 'waiting'): break
            time.sleep(0.5)
    except RuntimeError as exc:
        return {'ok': False, 'status': 'not started', 'seconds': time.time() - started, 'steps': 0, 'tokens': 0, 'cost': 0.0, 'why': str(exc)[:160]}
    finally:
        try:
            after = api.call('chat')
            for s in after.get('skills', []):
                if s['id'] not in old_skills: api.call('skills/delete', {'id': s['id']})
            for m in after.get('memory', []):
                if m['id'] not in old_memory: api.call('memory/delete', {'id': m['id']})
        except Exception: pass
    mine = agent.get('task') == text[:300]
    report = (agent.get('final') or '') if mine else ''
    done = mine and agent.get('status') == 'completed'
    ok = done and passed(pattern, report)
    if done and not ok: why = 'wrong answer: ' + ' '.join(report.split())[:120]
    elif not done and not why: why = str(agent.get('message', ''))[:160]
    usage = (api.call('usage').get('run') or {})
    if not mine: usage, agent = {}, {'status': 'not started'}
    return {'ok': ok, 'status': agent.get('status'), 'seconds': time.time() - started, 'steps': agent.get('steps', 0),
            'tokens': usage.get('total_tokens', 0), 'cost': usage.get('cost_usd', 0.0), 'why': '' if ok else why}


def table(results, modes):
    """results: {mode: [result per task]} -> the report as text."""
    lines = []
    for mode in modes:
        lines.append(f'\n{mode.upper()}')
        for (name, _, _), r in zip(results['tasks'], results[mode]):
            lines.append(f"  {'PASS' if r['ok'] else 'FAIL'}  {name:<22} {r['seconds']:6.0f} s  {r['steps']:3} actions  {r['tokens']:>8,} tokens  ${r['cost']:.3f}"
                         + (f"   {r['why']}" if r['why'] else ''))
    lines.append('\nSUMMARY')
    lines.append(f"  {'mode':<10} {'passed':>8} {'time':>9} {'tokens':>11} {'cost':>9}")
    for mode in modes:
        rs = results[mode]
        lines.append(f"  {mode:<10} {sum(r['ok'] for r in rs):>4} of {len(rs)} {sum(r['seconds'] for r in rs):>7.0f} s {sum(r['tokens'] for r in rs):>11,} {'$%.3f' % sum(r['cost'] for r in rs):>9}")
    return '\n'.join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description='Measure Screenpilot: success, time, tokens and cost per mode.')
    p.add_argument('modes', nargs='*', help='private, smart, fast (default: all three)')
    p.add_argument('--tasks', help='task numbers, e.g. 1,3 (default: all)')
    p.add_argument('--url', default='http://localhost:8080')
    p.add_argument('--timeout', type=float, default=600, help='seconds per task (default 600)')
    p.add_argument('--list', action='store_true', help='show the tasks and exit')
    args = p.parse_args(argv)
    if args.list:
        for i, (name, text, _) in enumerate(TASKS, 1): print(f'{i}. {name}: {text}')
        return 0
    modes = args.modes or list(MODES)
    bad = [m for m in modes if m not in MODES]
    if bad: p.error(f"unknown mode: {', '.join(bad)} (choose from {', '.join(MODES)})")
    try: picked = [TASKS[int(n) - 1] for n in args.tasks.split(',')] if args.tasks else list(TASKS)
    except (ValueError, IndexError): p.error(f'--tasks takes numbers from 1 to {len(TASKS)}, e.g. 1,3')
    api = Api(args.url)
    try: state = api.call('state')
    except (URLError, OSError, RuntimeError): sys.exit(f'Screenpilot is not answering at {args.url}. Start it with ./run.sh')
    info = state.get('modes', {})
    for mode in list(modes):
        m = info.get(mode, {})
        if m and not m.get('ready', True): print(f'Skipping {mode}: {m.get("hint", "not ready")}'); modes.remove(mode)
    if not modes: sys.exit('No mode is ready to run.')
    if state['agent'].get('status') in ('running', 'waiting'): sys.exit('A task is running. Wait for it or stop it first.')
    results = {'tasks': picked}
    for mode in modes:
        exact = info.get(mode, {}).get('planner', '')
        print(f'\n{mode}' + (f' ({exact})' if exact else ''))
        results[mode] = []
        for name, text, pattern in picked:
            print(f'  {name} ... ', end='', flush=True)
            r = run_task(api, mode, text, pattern, args.timeout)
            results[mode].append(r)
            print(('pass' if r['ok'] else 'FAIL') + f" in {r['seconds']:.0f} s" + (f" ({r['why']})" if r['why'] else ''))
    print(table(results, modes))
    return 0 if all(r['ok'] for m in modes for r in results[m]) else 1


if __name__ == '__main__':
    sys.exit(main())
