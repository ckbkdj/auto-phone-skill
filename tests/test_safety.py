import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from auto_phone.contracts import DECISION, Fault, dumps, loads, validate
from auto_phone.entry import install, mcp_server
from auto_phone.phone import Phone, node_risk, parse_screen
from auto_phone.platform import Config
from auto_phone.runtime import Runtime


class ExtraSafety(unittest.TestCase):
    def test_input_cannot_hide_enter_or_webdriver_key(self):
        for text in ['hello\\n', 'hello\n', 'hello\r', 'hello\t', 'hello\ue007']:
            with self.subTest(text=repr(text)), self.assertRaises(Fault):
                validate(DECISION, {'action': 'type', 'target': 'n0', 'text': text})

    def test_visible_child_label_contributes_to_parent_risk(self):
        snapshot = parse_screen('<hierarchy><node class="android.view.ViewGroup" clickable="true" resource-id="parent">'
                                '<node class="android.widget.TextView" text="立即支付"/></node></hierarchy>', 'com.example')
        ref = next(n['ref'] for n in snapshot['nodes'] if n['clickable'])
        self.assertEqual(node_risk({'action': 'tap', 'target': ref}, snapshot)[0], 'confirmation')

    def test_void_false_is_not_valid_success(self):
        phone = object.__new__(Phone)
        phone.command = lambda *_: False
        with self.assertRaises(Fault) as cm:
            phone.void('/click', {})
        self.assertTrue(cm.exception.uncertain)

    def test_readiness_failure_returns_recoverable_task_id(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_PHONE_HOME': tmp}):
            Path(tmp, 'config.json').write_text(dumps({'devices': {'phone': {'udid': 'test'}}}))
            def fail(*_):
                raise Fault('APPIUM_NOT_READY')
            runtime = Runtime(Config(), fail)
            try:
                reply = runtime.call('begin', {'goal': 'test', 'device_id': 'phone', 'idempotency_key': 'a'})
                self.assertEqual(reply['code'], 'APPIUM_NOT_READY')
                self.assertTrue(reply['task_id'])
                self.assertEqual(runtime.call('cancel', {'task_id': reply['task_id']})['status'], 'cancelled')
            finally:
                runtime.close()

    def test_cancel_does_not_change_completed_status(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_PHONE_HOME': tmp}):
            Path(tmp, 'config.json').write_text(dumps({'devices': {'phone': {'udid': 'test'}}}))
            runtime = Runtime(Config())
            task, _ = runtime.store.new({'goal': 'test', 'device_id': 'phone', 'idempotency_key': 'a'}, runtime.config.identity('phone'))
            task['status'] = 'succeeded'
            with runtime.store.db:
                runtime.store.save(task)
            try:
                self.assertEqual(runtime.call('cancel', {'task_id': task['id']})['status'], 'succeeded')
            finally:
                runtime.close()

    def test_mcp_notification_does_not_replace_initialize(self):
        output = io.StringIO()
        wire = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        mcp_server(None, io.BytesIO(wire), output)
        self.assertIn('error', loads(output.getvalue()))

    def test_mcp_startup_error_does_not_pollute_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'config.json').write_text('{"invalid":true}')
            launcher = Path(__file__).resolve().parents[1] / 'scripts/phone_agent.py'
            result = subprocess.run([sys.executable, '-I', '-S', str(launcher), 'mcp'],
                                    env=dict(os.environ, AUTO_PHONE_HOME=tmp), capture_output=True, timeout=5)
            self.assertEqual(result.stdout, b'')
            self.assertEqual(result.returncode, 1)
            self.assertIn(b'INVALID_CONFIG', result.stderr)

    def test_installer_rejects_recursive_self_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(Fault) as cm:
                install(root, root / 'inside')
            self.assertEqual(cm.exception.code, 'INSTALL_TARGET_INSIDE_SOURCE')

    def test_catalog_numeric_alias_is_string(self):
        path = Path(__file__).resolve().parents[1] / 'auto_phone/apps.json'
        apps = loads(path.read_bytes())['apps']
        self.assertTrue(any('12306' in x.get('aliases', []) for x in apps))
        self.assertTrue(all(isinstance(v, str) for x in apps for v in x.get('aliases', [])))

class BootstrapSafety(unittest.TestCase):
    def test_local_appium_is_started_automatically_on_loopback(self):
        from types import SimpleNamespace
        from auto_phone.platform import ensure_appium
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_PHONE_HOME': tmp}):
            Path(tmp, 'config.json').write_text(dumps({'devices': {'phone': {'udid': 'test'}}}))
            config = Config()
            ready_calls = 0
            def request(*_args, **_kwargs):
                nonlocal ready_calls
                ready_calls += 1
                return {'value': {'ready': ready_calls >= 3}}
            with patch('auto_phone.environment.require_local', return_value=dict(os.environ)), \
                 patch('auto_phone.platform.Http.call', request), \
                 patch('auto_phone.platform.shutil.which', return_value='/usr/local/bin/appium'), \
                 patch('auto_phone.platform.subprocess.run', return_value=SimpleNamespace(stdout=b'{"uiautomator2":{}}')) as run, \
                 patch('auto_phone.platform.subprocess.Popen') as popen:
                popen.return_value.poll.return_value = None
                ensure_appium(config, config.device('phone'))
            args = popen.call_args.args[0]
            self.assertIn('127.0.0.1', args)
            self.assertNotIn('0.0.0.0', args)
            self.assertNotIn('--relaxed-security', args)
            self.assertEqual(run.call_count, 1)

    def test_missing_appium_can_fail_without_installing_anything(self):
        from auto_phone.platform import ensure_appium
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AUTO_PHONE_HOME': tmp}):
            Path(tmp, 'config.json').write_text(dumps({'devices': {'phone': {'udid': 'test'}}, 'auto_install_appium': False}))
            config = Config()
            with patch('auto_phone.platform.Http.call', return_value={'value': {'ready': False}}), \
                 patch('auto_phone.platform.shutil.which', return_value=None), \
                 self.assertRaises(Fault) as cm:
                ensure_appium(config, config.device('phone'))
            self.assertEqual(cm.exception.code, 'APPIUM_INSTALL_REQUIRED')


if __name__ == '__main__':
    unittest.main()
