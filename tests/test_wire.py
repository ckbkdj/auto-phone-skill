from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from auto_phone.contracts import OUTPUT, dumps, loads, validate
from auto_phone.platform import Config, Http, Lock, ensure_appium
from auto_phone.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scripts/phone_agent.py'


class WireHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, value, code=200):
        data = json.dumps({'value': value}).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        state = self.server.state
        state['requests'].append(('GET', self.path, None))
        if self.path == '/status':
            return self.reply({'ready': True})
        if self.path.endswith('/window/rect'):
            return self.reply({'x': 0, 'y': 0, 'width': 1080, 'height': 2400})
        if self.path.endswith('/current_package'):
            return self.reply(state['package'])
        if self.path.endswith('/source'):
            import xml.sax.saxutils as esc
            text = esc.escape(state['text'], {'"': '&quot;'})
            return self.reply(f'<hierarchy><node class="{state["class"]}" text="{text}" clickable="true" '
                              f'scrollable="{str(state["scrollable"]).lower()}" resource-id="com.example:id/current" bounds="[1,1][100,100]" /></hierarchy>')
        if self.path.endswith('/text'):
            return self.reply(state['text'])
        return self.reply({'error': 'unknown command'}, 404)

    def do_POST(self):
        state = self.server.state
        data = loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
        state['requests'].append(('POST', self.path, data))
        if self.path == '/session':
            state['sessions'] += 1
            return self.reply({'sessionId': 'wire-session', 'capabilities': {}})
        if self.path.endswith('/appium/settings'):
            return self.reply(None)
        if self.path.endswith('/elements'):
            if state.get('duplicates'):
                return self.reply([{'element-6066-11e4-a52e-4f735466cecf': 'e1'},
                                   {'element-6066-11e4-a52e-4f735466cecf': 'e2'}])
            return self.reply([{'element-6066-11e4-a52e-4f735466cecf': 'e1'}])
        if self.path.endswith('/click'):
            state['clicks'] += 1
            state['text'] = 'Next screen'
            return self.reply(None)
        if self.path.endswith('/activate_app'):
            state['package'] = data['appId']
            state['text'] = 'Settings'
            return self.reply(None)
        if self.path.endswith('/execute/sync'):
            if data['script'] == 'mobile: replaceElementValue':
                state['text'] = data['args'][0]['text']
                state['types'] += 1
                return self.reply(None)
            if data['script'] == 'mobile: scrollGesture':
                state['scrolls'] += 1
                state['text'] = 'End of list'
                return self.reply(False)
        if self.path.endswith('/clear'):
            state['text'] = ''
            return self.reply(None)
        return self.reply({'error': 'unknown command'}, 404)


class Wire(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), WireHandler)
        self.server.state = {'requests': [], 'package': 'com.example', 'text': 'Search',
                             'class': 'android.widget.Button', 'scrollable': False,
                             'clicks': 0, 'sessions': 0, 'types': 0, 'scrolls': 0}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.env = dict(os.environ, AUTO_PHONE_HOME=self.tmp.name)
        self.patcher = patch.dict(os.environ, self.env)
        self.patcher.start()
        Path(self.tmp.name, 'config.json').write_text(dumps({'devices': {'cloud-1': {
            'udid': 'wire-test-only', 'appium_url': self.url, 'auto_start': False}}}))
        self.config = Config()
        self.runtime = Runtime(self.config)

    def tearDown(self):
        self.runtime.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.patcher.stop()
        self.tmp.cleanup()

    def begin(self):
        return self.runtime.call('begin', {'goal': 'test', 'device_id': 'cloud-1', 'idempotency_key': 'test'})

    def test_http_real_roundtrip_and_element_click_no_coordinates(self):
        started = self.begin()
        self.assertTrue(started['ok'], started)
        out = self.runtime.call('step', {'task_id': started['task_id'],
            'observation_id': started['observation']['id'], 'operation_id': 'one',
            'decision': {'action': 'tap', 'target': 'n0'}})
        self.assertTrue(out['ok'], out)
        self.assertEqual(self.server.state['clicks'], 1)
        self.assertEqual(out['observation']['nodes'][0]['text'], 'Next screen')
        self.assertTrue(any(p.endswith('/element/e1/click') for _,p,_ in self.server.state['requests']))
        self.assertFalse(any('clickGesture' in str(b) or '"x"' in str(b) for _,_,b in self.server.state['requests']))

    def test_native_chinese_replace_one_command(self):
        self.server.state.update(text='', **{'class': 'android.widget.EditText'})
        started = self.begin()
        out = self.runtime.call('step', {'task_id': started['task_id'],
            'observation_id': started['observation']['id'], 'operation_id': 'type-one',
            'decision': {'action': 'type', 'target': 'n0', 'text': '北京南站'}})
        self.assertTrue(out['ok'], out)
        self.assertEqual(self.server.state['types'], 1)
        self.assertEqual(self.server.state['clicks'], 0)
        self.assertEqual(self.server.state['text'], '北京南站')

    def test_scroll_false_is_not_replayed(self):
        self.server.state['scrollable'] = True
        started = self.begin()
        out = self.runtime.call('step', {'task_id': started['task_id'],
            'observation_id': started['observation']['id'], 'operation_id': 'scroll-one',
            'decision': {'action': 'scroll', 'target': 'n0', 'direction': 'down'}})
        self.assertTrue(out['ok'], out)
        self.assertEqual(self.server.state['scrolls'], 1)

    def test_ambiguous_native_element_never_clicked(self):
        self.server.state['duplicates'] = True
        started = self.begin()
        out = self.runtime.call('step', {'task_id': started['task_id'],
            'observation_id': started['observation']['id'], 'operation_id': 'one',
            'decision': {'action': 'tap', 'target': 'n0'}})
        self.assertEqual(out['code'], 'AMBIGUOUS_OR_MISSING_TARGET')
        self.assertEqual(self.server.state['clicks'], 0)

    def test_session_reused_across_cli_processes_no_manual_service(self):
        def cli(command, args):
            completed = subprocess.run([sys.executable, '-I', '-S', str(LAUNCHER), command, '--json', dumps(args)],
                                       env=self.env, cwd=tempfile.gettempdir(), capture_output=True, timeout=10)
            self.assertEqual(completed.stderr, b'')
            out = loads(completed.stdout)
            validate(OUTPUT, out)
            return out
        started = cli('begin', {'goal': 'test', 'device_id': 'cloud-1', 'idempotency_key': 'separate-process'})
        self.assertTrue(started['ok'], started)
        observed = cli('observe', {'task_id': started['task_id']})
        self.assertTrue(observed['ok'])
        self.assertEqual(self.server.state['sessions'], 1)

    def test_mcp_real_subprocess_session(self):
        messages = [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'phone_begin','arguments':{'goal':'test','device_id':'cloud-1','idempotency_key':'mcp-subprocess'}}},
        ]
        process = subprocess.run([sys.executable, '-I', '-S', str(LAUNCHER), 'mcp'],
                                 input=('\n'.join(dumps(x) for x in messages)+'\n').encode(),
                                 capture_output=True, env=self.env, cwd=tempfile.gettempdir(), timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, b'')
        replies = [loads(x) for x in process.stdout.splitlines()]
        self.assertEqual(len(replies), 3)
        task = replies[-1]['result']['structuredContent']
        self.assertTrue(task['ok'], task)
        self.assertTrue(task['observation']['nodes'])

    def test_existing_appium_not_reinstalled(self):
        with patch('auto_phone.platform.subprocess.Popen') as popen, patch('auto_phone.platform.subprocess.run') as run:
            ensure_appium(self.config, self.config.device('cloud-1'))
        popen.assert_not_called()
        run.assert_not_called()


class Installation(unittest.TestCase):
    def test_install_self_contained_from_any_cwd_without_site_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            installed = subprocess.run([sys.executable, '-I', '-S', str(LAUNCHER), 'install', '--skills-dir', str(path / 'skills')],
                                       cwd=tmp, capture_output=True, timeout=10)
            self.assertEqual(installed.returncode, 0, installed.stdout)
            launcher = path / 'skills/auto-phone-skill/scripts/phone_agent.py'
            self.assertTrue(launcher.exists())
            env = dict(os.environ, AUTO_PHONE_HOME=str(path / 'state'))
            run = subprocess.run([sys.executable, '-I', '-S', str(launcher), 'doctor', '--json', '{}'],
                                 cwd=tmp, env=env, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertEqual(loads(run.stdout)['report']['runtime_dependencies'], 0)
            duplicate = subprocess.run([sys.executable, str(LAUNCHER), 'install', '--skills-dir', str(path / 'skills')],
                                       cwd=tmp, capture_output=True, timeout=10)
            self.assertEqual(duplicate.returncode, 1)
            self.assertEqual(loads(duplicate.stdout)['code'], 'INSTALL_TARGET_EXISTS')

    def test_process_lock_cannot_be_bypassed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Lock(Path(tmp) / 'test.lock'):
                with self.assertRaises(Exception):
                    with Lock(Path(tmp) / 'test.lock'):
                        self.fail('must not acquire')


if __name__ == '__main__':
    unittest.main()
