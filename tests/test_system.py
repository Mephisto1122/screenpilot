import io
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from app.core import validate_action
from app.server import Application, handler_factory


def await_done(predicate, timeout=30):
    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout: raise AssertionError('Timed out')
        time.sleep(0.02)


class ValidationTests(unittest.TestCase):
    def test_bad_coordinates_and_keys_rejected(self):
        for bad in ({'kind': 'click', 'x': 1280, 'y': 1}, {'kind': 'click', 'x': -1, 'y': 1}, {'kind': 'key', 'key': 'ctrl+l; rm'},
                    {'kind': 'type', 'text': ''}, {'kind': 'teleport'}):
            with self.assertRaises(ValueError): validate_action(bad)
        self.assertEqual(validate_action({'kind': 'key', 'key': 'ctrl+l'})['key'], 'ctrl+l')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(self.tmp.name, lab=True)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(self.app))
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    def tearDown(self):
        self.app.close(); self.server.shutdown(); self.server.server_close(); self.tmp.cleanup()
    def request(self, path, body=None, headers=None):
        h = {'Content-Type': 'application/json', 'X-Screenpilot': '1'}
        h.update(headers or {})
        return urlopen(Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=h), timeout=5)

    def test_page_screen_manual_input_chat_and_memory(self):
        with self.request('/') as r: self.assertIn(b'screenpilot', r.read())
        with self.request('/api/screen') as r: self.assertEqual(Image.open(io.BytesIO(r.read())).size, (1280, 800))
        self.request('/api/action', {'kind': 'click', 'x': 100, 'y': 100}).close()
        await_done(lambda: self.app.desktop.lab_step == 1)
        with self.request('/api/memory', {'text': 'Prefers short answers'}) as r: self.assertEqual(len(json.load(r)['memory']), 1)
        with self.request('/api/chat') as r: data = json.load(r)
        self.assertEqual(data['memory'][0]['text'], 'Prefers short answers')
        self.assertFalse(data['telegram']['connected'])
        for gone in ('/classic', '/api/export', '/practice'):
            with self.assertRaises(HTTPError) as err: self.request(gone)
            self.assertEqual(err.exception.code, 404)

    def test_control_rejects_cross_origin_and_missing_header(self):
        with self.assertRaises(HTTPError) as err: self.request('/api/stop', {}, headers={'Origin': 'https://evil.example'})
        self.assertEqual(err.exception.code, 403)
        with self.assertRaises(HTTPError) as err: self.request('/api/stop', {}, headers={'X-Screenpilot': ''})
        self.assertEqual(err.exception.code, 403)
        with self.assertRaises(HTTPError) as err: self.request('/api/state', headers={'Host': 'evil.example'})
        self.assertEqual(err.exception.code, 403)


if __name__ == '__main__': unittest.main()
