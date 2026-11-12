"""Supervisor: an independent check that the agent is actually getting closer to the goal.

The acting model judges its own progress and can be wrong about it for many steps (keys going to the
wrong window, a popup in the way, a click loop, the wrong page). The supervisor looks from outside:
every few actions, and at once when a loop signal appears, it compares the goal with what was done
and what the screen shows, and answers one question: is this getting closer?
"""
REVIEW_EVERY = 8  # actions between routine reviews
PROMPT = '''You supervise an agent that operates a Linux desktop for a user. From the evidence below, judge whether it
is getting closer to the goal. Common failures: keys or typing going into the wrong window, a popup or dialog in the
way, clicking the same thing in a loop, being on the wrong page or site, waiting for something that will not happen,
believing in progress the screen does not show, or looking for something the desktop line says is not there (such as
the taskbar behind a full-screen window). The agent can open apps, windows and files by name, run commands and install
apps (open_app, switch_window, open_file, run_command, install_app).
Judge the last actions as a whole, not the last one. Answer "no" only for a real loop or dead end: the same pages or
searches again and again with nothing new, actions that have no effect, the wrong site, or a wall (a login, a block) it
keeps running into. A page that is still loading, a search that was just started, opening and reading results: that is
normal work, so answer "yes" or "unsure". Research takes many steps before its first result. But being on a relevant
page for many actions without writing anything in the notebook is a loop: answer "no" and tell it what to note.
Opening pages that could never qualify, to reach a number the task mentions, is not progress either: answer "no".
Going round the same few searches is a loop even though each one looks relevant: when the actions show search words
that were already used, or the results are only things the notebook already has, answer "no" and name a different KIND
of place to get leads from (pages related to a good find, a tag or list page), not another wording of the search.
In "advice", name a concretely different route (another search phrase, another section, another site the task allows).
Reply with ONE JSON object and nothing else:
{"progress":"yes" or "no" or "unsure","reason":"one sentence","advice":"the concrete next approach",
 "plan":["only if the plan must change: short new steps"],"ask_user":false}
Set ask_user to true only for security: a CAPTCHA, a login or password, a code sent to the user, an identity check.
'''
SCHEMA = {'type': 'object', 'properties': {
    'progress': {'type': 'string', 'enum': ['yes', 'no', 'unsure']}, 'reason': {'type': 'string'},
    'advice': {'type': 'string'}, 'plan': {'type': 'array', 'items': {'type': 'string'}}, 'ask_user': {'type': 'boolean'}},
    'required': ['progress', 'reason', 'advice', 'plan', 'ask_user']}


def _line(a):
    kind = a.get('kind')
    if kind == 'feedback': return 'note: ' + str(a.get('text', ''))[:160]
    if kind == 'email': return 'read email'
    if kind == 'command': return f"ran `{str(a.get('command', ''))[:60]}` (exit {a.get('exit')})"
    detail = a.get('label') or a.get('text') or a.get('key') or ''
    return f"{kind} {str(detail)[:60]}".strip()


class Supervisor:
    def __init__(self, every=REVIEW_EVERY):
        self.every = every
        self.since = 0          # actions since the last review
        self.signals = []       # loop signals seen since the last review
        self.misses = 0         # reviews in a row that saw no progress
        self.screen_before = '' # what the screen said at the last review

    def acted(self, n=1): self.since += n

    def flag(self, why):
        if why not in self.signals: self.signals.append(why)

    def due(self):
        # A warning sign brings the check forward, but never before a few actions: one click is nothing to judge.
        return self.since >= self.every or (bool(self.signals) and self.since >= 3)

    def prompt(self, goal, plan, step, history, notebook, screen_now, window):
        recent = [h for h in history if h.get('kind') not in ('email',)][-14:]
        parts = [PROMPT, 'GOAL: ' + goal]
        if len(plan) > 1:
            parts.append('PLAN: ' + ' | '.join(f"{'->' if i == step else ''}{i + 1}. {g}" for i, g in enumerate(plan)))
        parts.append('LAST ACTIONS (oldest first):\n' + ('\n'.join('- ' + _line(a) for a in recent) or '- none'))
        if notebook: parts.append('AGENT NOTEBOOK:\n' + '\n'.join('- ' + n for n in notebook[-10:]))
        if self.signals: parts.append('WARNING SIGNS: ' + '; '.join(self.signals))
        parts.append('ACTIVE WINDOW: ' + (window or 'unknown'))
        if self.screen_before: parts.append('SCREEN TEXT AT THE LAST CHECK:\n' + self.screen_before[:2500])
        parts.append('SCREEN TEXT NOW:\n' + (screen_now[:3500] or '(not available)'))
        return '\n\n'.join(parts)

    def review(self, client, provider, cfg, run, image, prompt, cancel=None):
        """Ask for a verdict. `image` is None in modes where screenshots must not leave the computer."""
        kwargs = {'schema': SCHEMA} if provider == 'local' else {}
        verdict = client.call(provider, prompt, image, run, 'review', cfg, cancel=cancel, **kwargs)
        progress = verdict.get('progress') if verdict.get('progress') in ('yes', 'no', 'unsure') else 'unsure'
        plan = [str(g).strip()[:300] for g in verdict.get('plan') or [] if str(g).strip()][:30]
        return {'progress': progress, 'reason': str(verdict.get('reason', ''))[:300],
                'advice': str(verdict.get('advice', ''))[:400], 'plan': plan, 'ask_user': bool(verdict.get('ask_user'))}

    def settle(self, verdict, screen_now):
        """Record a verdict; returns True when the user should be asked to look."""
        self.since, self.signals, self.screen_before = 0, [], screen_now
        if verdict['progress'] == 'no':
            self.misses += 1
        else:
            self.misses = 0
        return verdict['progress'] == 'no' and (verdict['ask_user'] or self.misses >= 2)

    @staticmethod
    def feedback(verdict):
        return ('Supervisor: you are NOT getting closer to the goal. ' + verdict['reason'] +
                (' Do this instead: ' + verdict['advice'] if verdict['advice'] else ''))


def screen_text(items, window):
    """Short, comparable text of the screen for reviews (top to bottom)."""
    from app import vision
    return (f'[{window}] ' if window else '') + vision.summary(items, limit=90)

