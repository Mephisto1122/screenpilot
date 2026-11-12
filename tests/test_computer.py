"""The agent's own computer: commands, files, downloads, app installs, and a desktop user without secrets."""
import io
import json
import os
import pwd
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from app import computer as computer_mod
from app import models
from app.computer import Computer
from app.core import ActionFailed, validate_action
from app.localexec import parse_step
from app.models import ModelClient, Unreachable
from test_text_mode import TextModeBase
from test_system import await_done


class ComputerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.c = Computer(self.tmp.name)
        self.c.user = None; self.c.home = Path(self.tmp.name)  # development mode: runs as the current user

    def tearDown(self): self.tmp.cleanup()

    def test_commands_return_output_and_stop_on_time(self):
        code, out = self.c.run('echo hello; echo oops >&2; exit 3', ':99')
        self.assertEqual(code, 3); self.assertIn('hello', out); self.assertIn('oops', out)
        start = time.monotonic(); code, out = self.c.run('sleep 30', ':99', seconds=1)
        self.assertIsNone(code); self.assertIn('still running after 1 seconds', out); self.assertLess(time.monotonic() - start, 5)
        stop = threading.Event(); threading.Timer(0.3, stop.set).start()
        self.assertIn('[stopped]', self.c.run('sleep 30', ':99', cancel=stop)[1])
        code, out = self.c.run('seq 1 200000', ':99')
        self.assertIn('left out', out); self.assertTrue(out.rstrip().endswith('200000'))  # the end is kept
        self.assertLess(len(out), 4500)

    def test_the_desktop_gets_no_secrets(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-secret', 'OPENAI_API_KEY': 'sk-other'}):
            self.assertNotIn('sk-secret', self.c.run('env', ':99')[1])
            env = self.c.env(':101')
        self.assertEqual(env['DISPLAY'], ':101'); self.assertNotIn('ANTHROPIC_API_KEY', env)

    def test_downloads_finished_and_in_progress(self):
        d = self.c.downloads; d.mkdir()
        (d / 'report.pdf').write_bytes(b'x' * 2048); (d / 'big.zip.crdownload').write_bytes(b'x' * 10); (d / '.hidden').write_text('x')
        done, partial = self.c.download_files()
        self.assertEqual(done, {'report.pdf': 2048}); self.assertEqual(partial, [('big.zip.crdownload', 10)])

    def test_paths_and_opening(self):
        self.assertEqual(self.c.resolve('~/Downloads/a.pdf'), Path(self.tmp.name) / 'Downloads/a.pdf')
        self.assertEqual(self.c.resolve('Documents/x'), Path(self.tmp.name) / 'Documents/x')
        self.assertEqual(self.c.resolve('/etc/hosts'), Path('/etc/hosts'))
        with self.assertRaisesRegex(ActionFailed, 'There is no file "~/nope.pdf"'): self.c.open('~/nope.pdf', ':99')

    def test_installs_only_in_docker(self):
        with patch.object(computer_mod.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(ActionFailed, 'only be installed when Screenpilot runs in Docker'): self.c.install('gimp')
        with self.assertRaisesRegex(ActionFailed, 'not a package name'): self.c.install('rm -rf /')

    def test_prepare_shares_documents_and_downloads_and_hides_the_rest(self):
        data = Path(self.tmp.name) / 'data'; home = Path(self.tmp.name) / 'home'; data.mkdir(); home.mkdir()
        (data / 'vault.json').write_text('{}'); os.chmod(data / 'vault.json', 0o644)
        c = Computer(data); c.user = pwd.getpwuid(os.getuid()); c.home = home
        c.prepare()
        self.assertEqual((home / 'Downloads').resolve(), (data / 'Downloads').resolve())
        self.assertEqual((home / 'Documents').resolve(), (data / 'Documents').resolve())
        self.assertIn('application/pdf=chromium.desktop', (home / '.config/mimeapps.list').read_text())
        self.assertEqual(os.stat(data / 'vault.json').st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(data).st_mode & 0o777, 0o711)
        c.prepare()  # running again keeps the links

    def test_the_whole_home_folder_is_kept_in_the_data_volume(self):
        data = Path(self.tmp.name) / 'data'; image_home = Path(self.tmp.name) / 'home-desk'; data.mkdir(); image_home.mkdir()
        (image_home / '.bashrc').write_text('# from the image')
        c = Computer(data); c.user = pwd.getpwuid(os.getuid()); c.home = image_home
        c.prepare()
        self.assertTrue(image_home.is_symlink()); self.assertEqual(image_home.resolve(), (data / 'home').resolve())
        (image_home / '.local').mkdir(); (image_home / '.local' / 'tool').write_text('installed with pip')
        # An update: a new container starts with a fresh home from the image.
        image_home.unlink(); image_home.mkdir(); (image_home / '.bashrc').write_text('# from the new image')
        c.prepare()
        self.assertEqual((image_home / '.local' / 'tool').read_text(), 'installed with pip')  # still there
        self.assertEqual((image_home / '.bashrc').read_text(), '# from the image')  # the kept one wins
        self.assertTrue((image_home / 'Downloads').resolve() == (data / 'Downloads').resolve())
        mime = image_home / '.config' / 'mimeapps.list'
        mime.write_text('[Default Applications]\napplication/pdf=org.gnome.Evince.desktop\n')  # the agent changed it
        c.prepare()
        self.assertIn('Evince', mime.read_text())  # its choice is kept


class ActionTests(unittest.TestCase):
    def test_validation(self):
        a = validate_action({'kind': 'run_command', 'command': 'ls ~/Downloads', 'seconds': 5000})
        self.assertEqual(a['seconds'], 900.0)
        self.assertEqual(validate_action({'kind': 'install_app', 'package': 'GIMP'})['package'], 'gimp')
        self.assertEqual(validate_action({'kind': 'open_file', 'path': '~/Downloads/a.pdf'})['path'], '~/Downloads/a.pdf')
        for bad in ({'kind': 'run_command', 'command': ''}, {'kind': 'install_app', 'package': 'a b'}, {'kind': 'open_file'}):
            with self.assertRaises(ValueError): validate_action(bad)

    def test_local_model_sentences(self):
        self.assertEqual(parse_step('Run the command `unzip ~/Downloads/a.zip -d ~/Documents, and ls`.'),
                         [('run', 'unzip ~/Downloads/a.zip -d ~/Documents, and ls')])
        self.assertEqual(parse_step('Install the gimp package.'), [('install', 'gimp')])
        self.assertEqual(parse_step('Click the Install button.'), [('click', 'Install button')])
        self.assertEqual(parse_step('Open the file ~/Downloads/report.pdf'), [('open_file', '~/Downloads/report.pdf')])
        self.assertEqual(parse_step('Open the Downloads folder.'), [('open_file', '~/Downloads')])
        self.assertEqual(parse_step('Open www.sport5.co.il'), [('key', 'ctrl+l'), ('type', 'www.sport5.co.il', None), ('key', 'Return')])
        self.assertEqual(parse_step('Open the terminal.'), [('open_app', 'terminal')])


class FakeSession:
    def __init__(self): self.commands, self.installed, self.opened = [], [], []
    def run(self, command, seconds=60, cancel=None):
        self.commands.append(command); return 0, 'a.zip\nreport.pdf'
    def install(self, package): self.installed.append(package); return f'Installed {package}'
    def open_file(self, path): self.opened.append(path)
    def launch(self, app): pass
    def download_files(self): return {}, []


class AgentComputerTests(TextModeBase):
    def test_commands_installs_files_and_downloads_reach_the_model(self):
        d = self.app.desktop; d.session = session = FakeSession()
        self.claude([{'actions': [{'kind': 'run_command', 'command': 'ls ~/Downloads'}]},
                     {'actions': [{'kind': 'install_app', 'package': 'gimp'}]},
                     {'actions': [{'kind': 'open_file', 'path': '~/Downloads/report.pdf'}]},
                     {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.assertEqual(self.run_task('Open the report I downloaded')['status'], 'completed')
        self.assertEqual(session.commands, ['ls ~/Downloads']); self.assertEqual(session.installed, ['gimp'])
        self.assertEqual(session.opened, ['~/Downloads/report.pdf'])
        self.assertIn('report.pdf', self.prompt(1)); self.assertIn('"exit": 0', self.prompt(1))
        self.assertIn('Installed gimp', self.prompt(2))

    def test_finished_download_is_announced(self):
        d = self.app.desktop
        def reply(n):
            if n == 1: d.lab_downloads['big.zip.crdownload'] = 5 << 20
            if n == 2: d.lab_downloads.pop('big.zip.crdownload', None); d.lab_downloads['big.zip'] = 9 << 20
            return {'actions': [{'kind': 'wait', 'seconds': 0.1 * n}]} if n < 3 else {'goal_done': True, 'actions': [], 'note': 'ok'}
        self.claude([None])
        transport = self.app.client.transport
        def wrapped(url, payload, headers, timeout):
            r = transport(url, payload, headers, timeout)
            if '\n'.join(c['text'] for c in payload['messages'][0]['content'] if c['type']=='text').startswith('You supervise'): return r
            r['content'][0]['text'] = json.dumps(reply(len(self.payloads))); return r
        self.app.client.transport = wrapped
        self.run_task('Download the archive')
        self.assertIn('Downloading now: big.zip (5.0 MB so far)', self.prompt(1))
        self.assertIn('Download finished: ~/Downloads/big.zip (9.0 MB)', self.prompt(2))

    def test_destructive_command_is_asked_first_when_approvals_are_on(self):
        self.app.desktop.session = session = FakeSession()
        self.app.room.update({'require_approval': True})
        self.claude([{'actions': [{'kind': 'run_command', 'command': 'rm -rf ~/Documents/old'}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        self.app.start_agent('Clean up', 'anthropic_text')
        await_done(lambda: self.app.agent['status'] == 'waiting', 10)
        self.assertIn('rm -rf ~/Documents/old', self.app.agent['waiting']['reason'])
        self.app.runner.reply('deny')
        await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(session.commands, [])


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = models.Settings(Path(self.tmp.name)); self.ledger = models.Ledger(Path(self.tmp.name))
        self.settings.update({'anthropic_key': 'fixture'})
        p = patch.object(models, 'RETRY_WAITS', (0, 0, 0, 0)); p.start(); self.addCleanup(p.stop)

    def tearDown(self): self.tmp.cleanup()

    def test_a_busy_api_is_tried_again(self):
        tries, notes = [], []
        def transport(url, payload, headers, timeout):
            tries.append(timeout)
            if len(tries) < 3: raise Unreachable('busy or temporarily unavailable (HTTP 529)')
            return {'usage': {'input_tokens': 1, 'output_tokens': 1}, 'content': [{'type': 'text', 'text': '{"actions":[]}'}]}
        client = ModelClient(self.settings, self.ledger, transport)
        self.assertEqual(client.call('anthropic', 'x', None, 'r', 'execute', on_retry=notes.append), {'actions': []})
        self.assertEqual(len(tries), 3); self.assertEqual(tries[0], 60); self.assertIn('Trying again', notes[0])

    def test_gives_up_with_a_clear_message(self):
        def transport(url, payload, headers, timeout): raise Unreachable('the connection failed')
        with self.assertRaisesRegex(Unreachable, r'Could not reach Claude \(Anthropic\): the connection failed, also after 4 more tries'):
            ModelClient(self.settings, self.ledger, transport).call('anthropic', 'x', None, 'r', 'execute')

    def test_which_http_errors_are_worth_retrying(self):
        client = ModelClient(self.settings, self.ledger)
        for code, kind in ((529, Unreachable), (503, Unreachable), (401, RuntimeError)):
            with patch('app.models.urlopen', side_effect=HTTPError('u', code, 'x', {}, io.BytesIO(b''))):
                with self.assertRaises(kind) as err: client._http('https://api.anthropic.com/v1/messages', {}, {}, 5)
                self.assertEqual(type(err.exception) is Unreachable, kind is Unreachable)


class StreamTests(unittest.TestCase):
    def test_claude_answers_are_read_as_they_stream(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        client = ModelClient(models.Settings(Path(tmp.name)), models.Ledger(Path(tmp.name)))
        sse = b'''event: message_start
data: {"type":"message_start","message":{"usage":{"input_tokens":1200,"output_tokens":1}}}

event: ping
data: {"type":"ping"}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"{\\"actions\\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"[]}"}}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}

event: message_stop
data: {"type":"message_stop"}
'''
        seen = []
        client.context.progress = lambda chars, t: seen.append(chars)
        with patch('app.models.urlopen', return_value=io.BytesIO(sse)):
            r = client._http('https://api.anthropic.com/v1/messages', {'stream': True}, {}, 60)
        self.assertEqual(r['content'][0]['text'], '{"actions":[]}'); self.assertEqual(r['usage']['output_tokens'], 7)
        self.assertEqual(r['usage']['input_tokens'], 1200); self.assertEqual(seen[-1], 14)
        overloaded = b'data: {"type":"error","error":{"type":"overloaded_error"}}\n'
        with patch('app.models.urlopen', return_value=io.BytesIO(overloaded)):
            with self.assertRaises(Unreachable): client._http('https://api.anthropic.com/v1/messages', {'stream': True}, {}, 60)
        with patch('app.models.urlopen', return_value=io.BytesIO(sse.split(b'event: message_delta')[0])):
            with self.assertRaisesRegex(Unreachable, 'stopped half way'): client._http('https://api.anthropic.com/v1/messages', {'stream': True}, {}, 60)


class AgentWaitsForTheNetwork(TextModeBase):
    def test_unreachable_model_pauses_instead_of_failing(self):
        down = {'on': True}
        self.claude([{'goal_done': True, 'actions': [], 'note': 'ok'}])
        transport = self.app.client.transport
        def flaky(url, payload, headers, timeout):
            if down['on']: raise Unreachable('the connection failed')
            return transport(url, payload, headers, timeout)
        self.app.client.transport = flaky
        self.app.settings.update({'ask_when_stuck': True})
        with patch.object(models, 'RETRY_WAITS', (0,)):
            self.app.start_agent('Read the news', 'anthropic_text')
            await_done(lambda: self.app.agent['status'] == 'waiting', 10)
            self.assertIn('Could not reach Claude', self.app.agent['waiting']['reason'])
            down['on'] = False; self.app.runner.reply('continue')
            await_done(lambda: self.app.agent['status'] not in ('running', 'waiting') and not self.app.runner.thread.is_alive(), 10)
        self.assertEqual(self.app.agent['status'], 'completed', self.app.agent)


if __name__ == '__main__': unittest.main()


class InputTests(unittest.TestCase):
    """Typing and clicking on a real display, with the X tools replaced by a recorder."""
    def setUp(self):
        from app.core import Desktop
        self.d = Desktop(display=':150'); self.runs, self.clip = [], []
        def run(*args, input=None, timeout=12):
            self.runs.append(list(args))
            import subprocess
            out = b'X=100\nY=100\nSCREEN=0\nWINDOW=1\n' if 'getmouselocation' in args else b''
            return subprocess.CompletedProcess(args, 0, out, b'')
        self.d._run = run; self.d._clipboard = self.clip.append
        p = patch('app.core.shutil.which', return_value='/usr/bin/xdotool'); p.start(); self.addCleanup(p.stop)

    def test_short_plain_text_is_typed_key_by_key(self):
        self.d.execute(validate_action({'kind': 'type', 'text': 'cheap flights to Rome'}))
        self.assertEqual(self.runs[-1][:2], ['xdotool', 'type']); self.assertEqual(self.runs[-1][-1], 'cheap flights to Rome')
        self.assertEqual(self.clip, [])

    def test_other_alphabets_and_several_lines_are_pasted(self):
        for text in ('שלום עולם', 'line one\nline two', 'x' * 400):
            self.clip.clear(); self.d.execute(validate_action({'kind': 'type', 'text': text}))
            self.assertEqual(self.clip, [text]); self.assertEqual(self.runs[-1][-1], 'ctrl+v')

    def test_saved_passwords_do_not_stay_in_the_clipboard(self):
        self.d.execute(validate_action({'kind': 'type', 'text': 'hunter2', 'secret': True}))
        self.assertEqual(self.clip, ['hunter2', ''])  # pasted, then cleared
        self.assertFalse(any('hunter2' in ' '.join(r) for r in self.runs))  # never on a command line

    def test_the_pointer_moves_to_the_target_before_clicking(self):
        from app.core import glide
        path = glide((100, 100), (700, 500))
        self.assertEqual(path[-1], (700, 500)); self.assertGreaterEqual(len(path), 3)
        self.assertTrue(all(100 <= x <= 700 and 100 <= y <= 500 for x, y in path))
        self.d.execute(validate_action({'kind': 'click', 'x': 700, 'y': 500}))
        cmd = self.runs[-1]
        self.assertGreater(cmd.count('mousemove'), 2); self.assertEqual(cmd[-1], '1'); self.assertIn('click', cmd)


class LongWaitTests(TextModeBase):
    def test_a_long_wait_ends_when_the_download_finishes(self):
        d = self.app.desktop
        d.lab_downloads['Unconfirmed 48213.crdownload'] = 13 << 20
        self.claude([{'actions': [{'kind': 'wait', 'seconds': 300}]}, {'goal_done': True, 'actions': [], 'note': 'ok'}])
        def finish():
            time.sleep(1.2); d.lab_downloads.pop('Unconfirmed 48213.crdownload'); d.lab_downloads['tor-browser.tar.xz'] = 110 << 20
        threading.Thread(target=finish, daemon=True).start()
        start = time.monotonic()
        self.assertEqual(self.run_task('Download Tor Browser')['status'], 'completed')
        self.assertLess(time.monotonic() - start, 8)  # not 300 s
        self.assertIn('Downloading now: a file (its real name appears when it is finished) (13.0 MB so far)', self.prompt(0))
        self.assertIn('Download finished: ~/Downloads/tor-browser.tar.xz (110.0 MB)', self.prompt(1))


class ReplyToolTests(unittest.TestCase):
    """Claude answers through a forced tool, so its reply is always one JSON object."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings = models.Settings(Path(self.tmp.name)); self.settings.update({'anthropic_key': 'fixture'})
        self.ledger = models.Ledger(Path(self.tmp.name))

    def test_the_request_forces_the_reply_tool_and_its_input_is_the_reply(self):
        sent = []
        def transport(url, payload, headers, timeout):
            sent.append(payload)
            return {'usage': {'input_tokens': 10, 'output_tokens': 5},
                    'content': [{'type': 'tool_use', 'name': 'reply', 'input': {'actions': [{'kind': 'key', 'key': 'Return'}]}}]}
        r = ModelClient(self.settings, self.ledger, transport).call('anthropic', 'x', None, 'r', 'execute')
        self.assertEqual(r, {'actions': [{'kind': 'key', 'key': 'Return'}]})
        self.assertEqual(sent[0]['tool_choice'], {'type': 'tool', 'name': 'reply'}); self.assertEqual(sent[0]['tools'][0]['name'], 'reply')

    def test_streamed_tool_input(self):
        client = ModelClient(self.settings, self.ledger)
        sse = b'''data: {"type":"message_start","message":{"usage":{"input_tokens":900,"output_tokens":1}}}
data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"t1","name":"reply","input":{}}}
data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"actions\\": [{\\"kind\\""}}
data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":": \\"scroll\\", \\"amount\\": 3}]}"}}
data: {"type":"content_block_stop","index":0}
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":20}}
data: {"type":"message_stop"}
'''
        with patch('app.models.urlopen', return_value=io.BytesIO(sse)):
            r = client._http('https://api.anthropic.com/v1/messages', {'stream': True}, {}, 60)
        self.assertEqual(r['content'][-1], {'type': 'tool_use', 'input': {'actions': [{'kind': 'scroll', 'amount': 3}]}})

    def test_its_own_tool_markup_is_read_as_actions(self):
        from app.models import parse_object
        said = ('<invoke name="run_command"> <parameter name="command">ls ~/Downloads; ps aux | grep -i tor | head -5</parameter> '
                '<parameter name="seconds">20</parameter> </invoke> ``` I\'ll wait for the output.')
        self.assertEqual(parse_object(said), {'actions': [{'kind': 'run_command', 'command': 'ls ~/Downloads; ps aux | grep -i tor | head -5', 'seconds': 20}]})

    def test_a_refused_reply_tool_falls_back_to_plain_json(self):
        from app.models import ReplyToolRejected
        sent = []
        def transport(url, payload, headers, timeout):
            sent.append('tools' in payload)
            if 'tools' in payload: raise ReplyToolRejected('invalid_request_error: tool_choice not supported')
            return {'usage': {'input_tokens': 10, 'output_tokens': 5}, 'content': [{'type': 'text', 'text': '{"actions":[]}'}]}
        client = ModelClient(self.settings, self.ledger, transport)
        self.assertEqual(client.call('anthropic', 'x', None, 'r', 'execute'), {'actions': []})
        client.call('anthropic', 'x', None, 'r', 'execute')
        self.assertEqual(sent, [True, False, False])  # remembered for this model
        body = io.BytesIO(b'{"type":"error","error":{"type":"invalid_request_error","message":"max_tokens too large"}}')
        with patch('app.models.urlopen', side_effect=HTTPError('u', 400, 'x', {}, body)):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 400 \\(invalid_request_error: max_tokens too large\\)'):
                client._http('https://api.anthropic.com/v1/messages', {}, {}, 5)
