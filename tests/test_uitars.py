"""UI-TARS as a local executor: it looks at the screenshot and writes one action itself, on the Mac."""
import json
import tempfile
import unittest

from app import uitars, vision
from app.server import Application
from test_system import await_done


class ParseTests(unittest.TestCase):
    def test_actions(self):
        self.assertEqual(uitars.parse("Thought: open it\nAction: click(start_box='(500,500)')"),
                         {'actions': [{'kind': 'click', 'x': 640, 'y': 400, 'label': 'open it'}], 'note': 'open it'})
        self.assertEqual(uitars.parse("Action: type(content='hello')")['actions'][0], {'kind': 'type', 'text': 'hello', 'label': 'type'})
        self.assertEqual(uitars.parse("Action: hotkey(key='ctrl l')")['actions'][0]['key'], 'ctrl+l')
        self.assertEqual(uitars.parse("Action: scroll(start_box='(1,1)', direction='up')")['actions'][0]['amount'], -3)
        self.assertEqual(uitars.parse("Action: finished(content='all done')"), {'goal_done': True, 'actions': [], 'note': 'all done'})
        self.assertEqual(uitars.parse("Action: call_user(content='scan the QR')")['actions'][0], {'kind': 'ask_user', 'reason': 'scan the QR'})
        self.assertTrue(uitars.parse("Action: right_single(start_box='(1,1)')")['stuck'])
        self.assertTrue(uitars.parse("I am not sure what to do")['stuck'])

    def test_pixels_mode_keeps_coordinates(self):
        self.assertEqual(uitars.parse("Action: click(start_box='(640,400)')", coords='pixels')['actions'][0]['x'], 640)


class UitarsAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'uitars_model': 'ui-tars'})
        import unittest.mock as mock
        for p in (mock.patch.object(vision, 'available', return_value=True), mock.patch.object(vision, 'elements', return_value=[])):
            p.start(); self.addCleanup(p.stop)
        self.sent = []

    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def script(self, replies):
        n = {'i': 0}
        def transport(url, payload, headers, timeout):
            if not url.endswith('/api/chat'):  # a cloud call (should not happen in pure uitars mode)
                return {'usage': {'input_tokens': 1, 'output_tokens': 1}, 'content': [{'type': 'text', 'text': '{"progress":"yes","reason":"ok","advice":"","plan":[],"ask_user":false}'}]}
            if 'format' in payload:  # the progress check runs on the local vision model
                return {'message': {'content': '{"progress":"yes","reason":"ok","advice":"","plan":[],"ask_user":false}'}, 'prompt_eval_count': 10, 'eval_count': 5}
            self.sent.append(payload['model'])
            text = replies[min(n['i'], len(replies) - 1)]; n['i'] += 1
            return {'message': {'content': text}, 'prompt_eval_count': 50, 'eval_count': 20}
        self.app.client.transport = transport

    def run_task(self, task, planner='uitars'):
        self.app.start_agent(task, planner)
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        return self.app.agent

    def test_uitars_runs_a_task_locally_with_its_own_grounding(self):
        self.script(["Thought: Focus the address bar.\nAction: hotkey(key='ctrl l')",
                     "Thought: Type the site.\nAction: type(content='duckduckgo.com')",
                     "Thought: Go.\nAction: hotkey(key='enter')",
                     "Thought: The page is open, task complete.\nAction: finished(content='Opened DuckDuckGo.')"])
        agent = self.run_task('Open DuckDuckGo')
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertEqual(self.app.agent['final'], 'Opened DuckDuckGo.')
        kinds = [a['kind'] for a in self.app.desktop.lab_actions]
        self.assertEqual(kinds, ['key', 'type', 'key'])  # it acted; no cloud call
        self.assertTrue(all(m == 'ui-tars' for m in self.sent))  # every action came from the UI-TARS model

    def test_progress_check_in_uitars_mode_does_not_crash(self):
        # The supervisor reviews on the local vision model (needs a screenshot); it must not raise 'needs a screenshot'.
        reviews = []
        self.script(["Thought: click.\nAction: click(start_box='(%d,500)')" % (i * 70) for i in range(1, 11)] +
                    ["Thought: done.\nAction: finished(content='ok')"])
        base = self.app.client.transport
        def transport(url, payload, headers, timeout):
            if url.endswith('/api/chat') and 'format' in payload:
                reviews.append('images' in payload['messages'][0])
            return base(url, payload, headers, timeout)
        self.app.client.transport = transport
        agent = self.run_task('Do many steps')
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertTrue(reviews and all(reviews))  # the check ran, on a screenshot, without crashing

    def test_claude_plans_and_uitars_executes(self):
        # The plan call is a cloud (anthropic) call; the steps are UI-TARS (local) calls.
        plan = {'goals': ['Open the browser', 'Search']}
        self.app.settings.update({'anthropic_key': 'fixture'})
        n = {'i': 0}
        steps = ["Thought: click.\nAction: click(start_box='(500,500)')", "Thought: done.\nAction: finished(content='ok')"]
        def transport(url, payload, headers, timeout):
            if url.endswith('/api/chat'):
                if 'format' in payload:
                    return {'message': {'content': '{"progress":"yes","reason":"ok","advice":"","plan":[],"ask_user":false}'}, 'prompt_eval_count': 1, 'eval_count': 1}
                t = steps[min(n['i'], len(steps) - 1)]; n['i'] += 1
                return {'message': {'content': t}, 'prompt_eval_count': 10, 'eval_count': 5}
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(plan)}]}
        self.app.client.transport = transport
        agent = self.run_task('Search for cats', 'anthropic_uitars')
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertEqual(agent['plan'], ['Open the browser', 'Search'])  # Claude wrote the plan


if __name__ == '__main__': unittest.main()
