import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.server import Application, handler_factory
from test_system import await_done


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'anthropic_key': 'fixture'})
        self.calls = []

    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def claude(self, steps_per_task=2, delay=0.0):
        """Each task: click a few times, then report which task it was. Optional delay to overlap tasks."""
        counts = {}
        def transport(url, payload, headers, timeout):
            text = '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text')
            task = text.split('Overall task: ')[1].split('\n')[0]
            counts[task] = counts.get(task, 0) + 1
            self.calls.append((task, time.monotonic()))
            time.sleep(delay)
            body = ({'actions': [{'kind': 'click', 'x': counts[task], 'y': 5, 'label': 'x'}]} if counts[task] <= steps_per_task
                    else {'goal_done': True, 'actions': [], 'note': 'done: ' + task})
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': json.dumps(body)}]}
        self.app.client.transport = transport

    def idle(self):
        await_done(lambda: not self.app.tasks and not any(w.busy() for w in self.app.workers.values()), 15)

    def results(self, w): return [m['text'] for m in w.chat.list() if m['kind'] == 'result']

    def test_two_workers_run_at_the_same_time_on_their_own_desktops(self):
        w2 = self.app.add_worker()
        self.claude(steps_per_task=3, delay=0.15)
        self.app.submit_task('alpha', 'anthropic_direct', worker=1)
        self.app.submit_task('beta', 'anthropic_direct', worker=2)
        self.idle()
        self.assertEqual(self.results(self.app.main), ['done: alpha']); self.assertEqual(self.results(w2), ['done: beta'])
        self.assertEqual(len(self.app.main.desktop.lab_actions), 3); self.assertEqual(len(w2.desktop.lab_actions), 3)
        alpha = [t for k, t in self.calls if k == 'alpha']; beta = [t for k, t in self.calls if k == 'beta']
        self.assertLess(max(alpha[0], beta[0]), min(alpha[-1], beta[-1]))  # the runs overlapped in time

    def test_unpinned_tasks_go_to_free_workers_and_wait_when_all_are_busy(self):
        self.app.add_worker()
        self.claude(steps_per_task=2, delay=0.1)
        first = self.app.submit_task('one', 'anthropic_direct'); second = self.app.submit_task('two', 'anthropic_direct')
        third = self.app.submit_task('three', 'anthropic_direct')
        self.assertEqual((first['worker'], second['worker']), (1, 2))
        self.assertTrue(third['queued']); self.assertIsNone(third['worker'])
        self.assertEqual(self.app.summary()['shared_queue'], 1)
        self.idle()
        everything = self.results(self.app.main) + self.results(self.app.workers[2])
        self.assertEqual(sorted(everything), ['done: one', 'done: three', 'done: two'],
                         [(w.id, [(m['kind'], m['text'][:60]) for m in w.chat.list()]) for w in self.app.workers.values()])
        # the queued task's message appears in the chat of the worker that ran it
        runner = next(w for w in self.app.workers.values() if 'done: three' in self.results(w))
        self.assertIn('three', [m['text'] for m in runner.chat.list() if m['role'] == 'user'])

    def test_pinned_task_waits_for_its_own_worker(self):
        self.app.add_worker()
        self.claude(steps_per_task=3, delay=0.1)
        self.app.submit_task('long', 'anthropic_direct', worker=1)
        pinned = self.app.submit_task('after', 'anthropic_direct', worker=1)
        self.assertTrue(pinned['queued']); self.assertEqual(pinned['worker'], 1)
        self.assertEqual(self.app.workers[2].agent['status'], 'idle')
        self.idle()
        self.assertEqual(self.results(self.app.main), ['done: long', 'done: after'])
        self.assertEqual(self.results(self.app.workers[2]), [])

    def test_workers_persist_and_worker_1_cannot_be_removed(self):
        self.app.add_worker(); self.app.add_worker(); self.app.remove_worker(2)
        self.assertEqual(sorted(self.app.workers), [1, 3])
        with self.assertRaises(ValueError): self.app.remove_worker(1)
        self.app.close()
        again = Application(self.tmp.name, lab=True)
        try:
            self.assertEqual(sorted(again.workers), [1, 3])
            self.assertEqual(again.add_worker().id, 2)  # the free number is reused
        finally:
            again.close()
        self.app = Application(self.tmp.name, lab=True)

    def test_limit_on_workers(self):
        import app.server as server
        for _ in range(server.MAX_WORKERS - 1): self.app.add_worker()
        with self.assertRaisesRegex(ValueError, 'At most'): self.app.add_worker()

    def test_stopping_one_worker_leaves_the_other_running(self):
        w2 = self.app.add_worker()
        self.claude(steps_per_task=50, delay=0.05)
        self.app.submit_task('a', 'anthropic_direct', worker=1); self.app.submit_task('b', 'anthropic_direct', worker=2)
        await_done(lambda: self.app.main.agent['status'] == 'running' and w2.agent['status'] == 'running', 5)
        self.app.stop_worker(self.app.main)
        await_done(lambda: self.app.main.agent['status'] == 'stopped', 5)
        self.assertEqual(w2.agent['status'], 'running')
        self.app.stop_all(); await_done(lambda: w2.agent['status'] == 'stopped', 5)

    def test_telegram_buttons_answer_the_right_worker(self):
        from app.telegram import buttons
        w2 = self.app.add_worker()
        replies = []
        w2.runner.reply = lambda a, ask=None: replies.append(('w2', a)) or {'ok': True}; self.app.main.runner.reply = lambda a, ask=None: replies.append(('w1', a)) or {'ok': True}
        tg = self.app.telegram
        self.app.room.value['telegram_chat'] = 42
        tg.handle({'update_id': 1, 'callback_query': {'id': 'q', 'data': buttons('approval', 2)[0][0]['callback_data'], 'message': {'chat': {'id': 42}}}})
        tg.handle({'update_id': 2, 'callback_query': {'id': 'q', 'data': 'reply:continue', 'message': {'chat': {'id': 42}}}})  # old button
        self.assertEqual(replies, [('w2', 'approve'), ('w1', 'continue')])


class WorkerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(self.app))
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self): self.app.close(); self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()

    def req(self, path, body=None):
        r = urlopen(Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                            headers={'Content-Type': 'application/json', 'X-Screenpilot': '1'}), timeout=5)
        data = r.read(); return json.loads(data) if r.headers['Content-Type'].startswith('application/json') else data

    def test_worker_routes(self):
        added = self.req('/api/workers', {})
        self.assertEqual(added['id'], 2); self.assertEqual([w['id'] for w in added['workers']], [1, 2])
        self.assertEqual(self.req('/api/w/2/state')['worker'], 2)
        self.req('/api/w/2/action', {'kind': 'click', 'x': 10, 'y': 10})
        await_done(lambda: self.app.workers[2].desktop.lab_step == 1)
        self.assertEqual(self.app.main.desktop.lab_step, 0)  # only worker 2's desktop was clicked
        self.assertTrue(self.req('/api/w/2/thumb').startswith(b'\xff\xd8'))  # a JPEG
        self.req('/api/w/2/chat/clear', {})
        with self.assertRaises(HTTPError) as err: self.req('/api/w/9/state')
        self.assertEqual(err.exception.code, 404)
        self.app.workers[2].say('result', 'old work')
        self.assertEqual([w['id'] for w in self.req('/api/workers/remove', {'id': 2})['workers']], [1])
        self.assertEqual(self.req('/api/workers', {})['id'], 2)
        self.assertEqual(self.req('/api/w/2/chat')['messages'], [])  # a new worker 2 starts with an empty conversation

    def test_first_frame_of_the_live_screen_arrives_at_once(self):
        r = urlopen(Request(self.url + '/api/w/1/stream', headers={'X-Screenpilot': '1'}), timeout=5)
        start = time.monotonic(); seen = b''
        while seen.count(b'--screenpilot') < 2: seen += r.read1(65536)
        self.assertLess(time.monotonic() - start, 1.5)  # browsers draw a frame once the next one starts
        r.close()


if __name__ == '__main__': unittest.main()
