"""The agent is told what the desktop really looks like (window manager state), and can open apps and switch
windows without the taskbar, which a full-screen window hides."""
import subprocess
import unittest
from unittest.mock import patch

from app import core
from app.core import ActionFailed, Desktop, describe_desktop, parse_windows, validate_action
from app.localexec import parse_step
from test_text_mode import TextModeBase

ROOT = '''_NET_CLIENT_LIST(WINDOW): window id # 0x400003, 0xa00004, 0xc00007
_NET_ACTIVE_WINDOW(WINDOW): window id # 0xa00004'''
DETAILS = '''@@0x400003
_NET_WM_NAME(UTF8_STRING) = "tint2"
WM_CLASS(STRING) = "tint2", "Tint2"
_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DOCK
@@0xa00004
_NET_WM_NAME(UTF8_STRING) = "Reuters | \\"World\\" News - Chromium"
WM_CLASS(STRING) = "chromium", "Chromium"
_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN, _NET_WM_STATE_FOCUSED
_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL
@@0xc00007
_NET_WM_NAME:  not found.
WM_NAME(STRING) = "\\327\\251\\327\\234\\327\\225\\327\\235 - Mousepad"
WM_CLASS(STRING) = "mousepad", "Mousepad"
_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN
'''


def window(wid, title, app, active=False, fullscreen=False):
    return {'id': wid, 'title': title, 'app': app, 'active': active, 'fullscreen': fullscreen, 'minimized': False}


class WindowManagerTests(unittest.TestCase):
    def test_reads_windows_full_screen_and_taskbar(self):
        windows, taskbar = parse_windows(ROOT, DETAILS)
        self.assertTrue(taskbar)
        self.assertEqual([(w['title'], w['active'], w['fullscreen'], w['minimized']) for w in windows],
                         [('Reuters | "World" News - Chromium', True, True, False), ('שלום - Mousepad', False, False, True)])
        line = describe_desktop(windows, taskbar)
        self.assertIn('FULL SCREEN', line); self.assertIn('the taskbar is hidden', line)
        self.assertIn('other open windows: "שלום - Mousepad" (minimized)', line)
        windows[0]['fullscreen'] = False
        self.assertIn('the taskbar is at the bottom', describe_desktop(windows, True))
        self.assertIn('there is no taskbar right now', describe_desktop(windows, False))
        self.assertEqual(describe_desktop([], True), 'Desktop now: no windows are open; the taskbar is at the bottom.')

    def test_real_desktop_asks_the_window_manager(self):
        calls = []
        def run(args, **kw):
            calls.append(args)
            out = ROOT if args[:2] == ['xprop', '-root'] else DETAILS if args[0] == 'sh' else ''
            return subprocess.CompletedProcess(args, 0, out.encode(), b'')
        d = Desktop(display=':120')
        with patch.object(core.subprocess, 'run', run):
            self.assertIn('FULL SCREEN', d.state_text())
            d.switch_window('mousepad')  # by app name
            with self.assertRaisesRegex(ActionFailed, 'No open window matches "Spotify"'): d.switch_window('Spotify')
        self.assertIn('xprop -id 0xa00004', calls[1][2])
        self.assertIn(['xdotool', 'windowactivate', str(0xc00007)], calls)

    def test_actions_by_app_name(self):
        self.assertEqual(validate_action({'kind': 'open_app', 'app': 'Mousepad'})['app'], 'notes')
        self.assertEqual(validate_action({'kind': 'open_app', 'app': 'the web browser'})['app'], 'browser')
        self.assertEqual(validate_action({'kind': 'switch_window', 'title': ' Reuters  News '})['window'], 'Reuters News')
        self.assertEqual(validate_action({'kind': 'open_app', 'app': 'the terminal'})['app'], 'terminal')
        self.assertEqual(validate_action({'kind': 'open_app', 'app': 'GIMP'})['app'], 'gimp')  # any installed app by its command
        with self.assertRaisesRegex(ValueError, 'open_app needs an app'): validate_action({'kind': 'open_app', 'app': 'My App!'})

    def test_local_model_sentences(self):
        self.assertEqual(parse_step('Open Mousepad from the taskbar.'), [('open_app', 'notes')])
        self.assertEqual(parse_step('Click the Chromium icon in the taskbar.'), [('open_app', 'browser')])
        self.assertEqual(parse_step('Switch to the "todo.txt - Mousepad" window.'), [('switch', 'todo.txt - Mousepad')])
        self.assertEqual(parse_step('Open a new tab'), [('key', 'ctrl+t')])
        self.assertEqual(parse_step('Press F11 to leave full screen.'), [('key', 'F11')])


class FullScreenTests(TextModeBase):
    def test_agent_sees_full_screen_and_opens_apps_without_the_taskbar(self):
        d = self.app.desktop
        d.lab_windows = [window(1, 'Reuters - Chromium', 'chromium', active=True, fullscreen=True)]
        self.claude([{'actions': [{'kind': 'open_app', 'app': 'notes'}]},
                     {'actions': [{'kind': 'switch_window', 'window': 'Spotify'}]},
                     {'actions': [{'kind': 'switch_window', 'window': 'Reuters'}]},
                     {'actions': [{'kind': 'open_app', 'app': 'My App!'}]},
                     {'goal_done': True, 'actions': [], 'note': 'ok'}])
        agent = self.run_task('Write the headline in a note')
        self.assertEqual(agent['status'], 'completed', agent)
        self.assertIn('FULL SCREEN', self.prompt(0)); self.assertIn('taskbar is hidden', self.prompt(0))
        self.assertIn('active window (keys and typing go here): "Mousepad text editor"', self.prompt(1))
        self.assertIn('No open window matches \\"Spotify\\"', self.prompt(2))  # told, not stopped
        self.assertIn('active window (keys and typing go here): "Reuters - Chromium"', self.prompt(3))
        self.assertIn('That action was not valid: open_app needs an app', self.prompt(4))
        self.assertEqual([a['kind'] for a in d.lab_actions], ['open_app', 'switch_window', 'switch_window'])

    def test_open_app_brings_an_open_app_to_the_front(self):
        d = self.app.desktop
        d.lab_windows = [window(1, 'todo.txt - Mousepad', 'mousepad'), window(2, 'Reuters - Chromium', 'chromium', active=True, fullscreen=True)]
        d.execute(validate_action({'kind': 'open_app', 'app': 'notes'}))
        self.assertEqual(len(d.lab_windows), 2)  # no second Mousepad
        self.assertEqual([w['active'] for w in d.lab_windows], [True, False])

    def test_clicks_on_a_missing_taskbar_open_the_app_instead(self):
        d = self.app.desktop
        d.lab_windows = [window(1, 'Reuters - Chromium', 'chromium', active=True)]; d.lab_taskbar = False
        self.claude([{'actions': [{'kind': 'click', 'x': 330, 'y': 785, 'label': 'Mousepad icon in the taskbar'}]},
                     {'actions': [{'kind': 'click', 'x': 640, 'y': 790, 'label': 'the taskbar'}]},
                     {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(self.run_task('Write a note')['status'], 'completed')
        self.assertEqual([a['kind'] for a in d.lab_actions], ['open_app'])  # the bare taskbar click was not made
        self.assertEqual(d.lab_windows[-1]['app'], 'mousepad')
        self.assertIn('The taskbar is not visible right now', self.prompt(2))


class RepoDesktopFiles(unittest.TestCase):
    """The window manager and taskbar configs ship with the app (a merge once dropped the taskbar's)."""
    def test_configs_exist(self):
        from pathlib import Path
        from app.workers import DesktopSession
        for args in DesktopSession.SHELL: self.assertTrue(Path(args[-1]).is_file(), args)
        rc = (Path(DesktopSession.SHELL[1][-1])).read_text()
        self.assertIn('chromium.desktop', rc); self.assertIn('mousepad.desktop', rc)
        self.assertNotIn('xterm', rc)  # no terminal in the taskbar (open_app and run_command reach it)
        self.assertNotIn('toggle_iconify', rc)  # clicking a window in the taskbar must not minimize it


class LocalMissTests(unittest.TestCase):
    """This Mac: a thing that is not on the screen is a retry with a hint, not the end of the task."""
    def setUp(self):
        import json, tempfile
        from app import vision
        from app.server import Application
        self.tmp = tempfile.TemporaryDirectory(); self.app = Application(self.tmp.name, lab=True)
        self.app.settings.update({'local_model': 'qwen3-vl:8b'})
        for p in (patch.object(vision, 'available', return_value=True), patch.object(vision, 'elements', return_value=[])):
            p.start(); self.addCleanup(p.stop)
        thoughts = ['Click the taskbar at the bottom of the screen.', 'Open Mousepad.', 'The task is complete.']
        self.thinks = []
        def transport(url, payload, headers, timeout):
            content = payload['messages'][0]['content']
            if 'format' not in payload:
                self.thinks.append(content); text = thoughts[min(len(self.thinks), len(thoughts)) - 1]
            else:
                text = json.dumps({'x': -5, 'y': -5})  # the model cannot point at it either
            return {'message': {'content': text}, 'prompt_eval_count': 10, 'eval_count': 5}
        self.app.client.transport = transport
    def tearDown(self): self.app.close(); self.tmp.cleanup()

    def test_missing_taskbar_is_a_hint_not_a_stop(self):
        from test_system import await_done
        d = self.app.desktop
        d.lab_windows = [window(1, 'Reuters - Chromium', 'chromium', active=True, fullscreen=True)]
        self.app.start_agent('Write a note', 'local')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)
        self.assertIn('FULL SCREEN', self.thinks[0])
        self.assertIn('Could not find "taskbar at the bottom of the screen" on the screen. There is no taskbar to click', self.thinks[1])
        self.assertIn('Say "Open Mousepad"', self.thinks[1])  # the whole hint reaches the model
        self.assertEqual([a['kind'] for a in d.lab_actions], ['open_app'])



if __name__ == '__main__': unittest.main()
