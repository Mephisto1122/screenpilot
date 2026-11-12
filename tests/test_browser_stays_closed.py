"""A browser closed by hand stays closed (the desktop is a computer, not only a browser). It comes back when it is
asked for, after a crash, and after a reset."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.workers import DesktopSession


class FakeProc:
    def __init__(self): self.done = threading.Event(); self.code = None; self.pid = 0
    def wait(self, timeout=None): self.done.wait(timeout); return self.code
    def poll(self): return self.code if self.done.is_set() else None
    def exit(self, code): self.code = code; self.done.set()


class FakeComputer:
    user = None
    def __init__(self, root): self.data = Path(root); self.home = root
    def env(self, display): return {'DISPLAY': display, 'PATH': os.environ.get('PATH', '')}
    def give(self, path): pass


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.started = []
        self.s = DesktopSession(1, FakeComputer(self.tmp.name))
        def spawn(args, **kw): p = FakeProc(); self.started.append(p); return p
        self.s._spawn = spawn
        self.env = patch.dict(os.environ, {'SCREENPILOT_BROWSER': 'python3'}); self.env.start()
        self.thread = threading.Thread(target=self.s._browser_loop, daemon=True); self.thread.start()
        self.until(1)
    def tearDown(self):
        self.s.stopping.set(); self.s.wake.set()
        for p in self.started: p.exit(0)
        self.thread.join(timeout=3); self.env.stop(); self.tmp.cleanup()
    def until(self, n, seconds=4):
        end = time.monotonic() + seconds
        while len(self.started) < n and time.monotonic() < end: time.sleep(0.02)
        self.assertEqual(len(self.started), n)

    def test_closed_by_hand_it_stays_closed_until_it_is_asked_for(self):
        self.started[0].exit(0)                      # the user closed the window
        time.sleep(2.6); self.assertEqual(len(self.started), 1)   # it used to be back after two seconds
        self.s.launch('browser')                     # the agent's open_app, or "open the browser"
        self.until(2)

    def test_after_a_crash_it_comes_back(self):
        self.started[0].exit(-11); self.s.wake.set()  # (the wake only skips the two-second pause)
        self.until(2)

    def test_after_a_reset_it_comes_back(self):
        self.s.fresh = True; self.started[0].exit(0); self.s.wake.set()
        self.until(2)

    def test_a_reset_brings_back_a_browser_that_was_closed_by_hand(self):
        self.started[0].exit(0); time.sleep(0.3)
        with patch.object(self.s, '_kill_apps'): self.s.reset()
        self.until(2)

    def test_every_browser_on_this_desktop_gets_its_profile(self):
        self.assertEqual(self.s.env['SCREENPILOT_PROFILE'], str(Path(self.tmp.name) / 'chromium-profile'))
        flags = (Path(__file__).parent.parent / 'app' / 'desktop' / 'chromium-flags').read_text()
        self.assertIn('--user-data-dir=$SCREENPILOT_PROFILE', flags)


if __name__ == '__main__': unittest.main()
