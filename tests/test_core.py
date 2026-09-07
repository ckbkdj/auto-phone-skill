from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from auto_phone.contracts import COMMANDS, DECISION, OUTPUT, Fault, dumps, loads, validate
from auto_phone.entry import autonomous, mcp_server
from auto_phone.phone import conditions_met, node_risk, parse_screen
from auto_phone.platform import Config, Lock, private_url
from auto_phone.runtime import Runtime, gate


def screen(text='Search', package='com.example', password=False, clickable=True):
    import xml.sax.saxutils as esc
    text = esc.escape(text, {'"': '&quot;'})
    return parse_screen(f'<hierarchy><node class="android.widget.Button" text="{text}" '
                        f'clickable="{str(clickable).lower()}" password="{str(password).lower()}" '
                        f'resource-id="com.example:id/button" bounds="[0,0][100,100]" /></hierarchy>', package)


class FakePhone:
    def __init__(self):
        self.current = screen()
        self.calls = []
        self.fail = False
        self.after = None

    def observe(self):
        out = copy.deepcopy(self.current)
        out['id'] = __import__('uuid').uuid4().hex
        out['_captured'] = time.time()
        return out

    def prepare(self, decision, live):
        if 'target' in decision and not any(n['ref'] == decision['target'] for n in live['nodes']):
            raise Fault('UNKNOWN_TARGET')
        return 'element-live'

    def act(self, decision, _element=None):
        self.calls.append(copy.deepcopy(decision))
        if self.fail:
            raise Fault('TRANSPORT_UNAVAILABLE', uncertain=True)
        if self.after:
            self.current = self.after
            self.after = None


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'AUTO_PHONE_HOME': self.tmp.name}, clear=False)
        self.env.start()
        Path(self.tmp.name, 'config.json').write_text(json.dumps({'devices': {'cloud-1': {'udid': 'emulator-test'}}}))
        self.config = Config()
        self.phone = FakePhone()
        self.runtime = Runtime(self.config, lambda *_: self.phone)
        self.started = self.runtime.call('begin', {'goal': 'Find the result', 'device_id': 'cloud-1', 'idempotency_key': 'user-turn-1'})
        self.assertTrue(self.started['ok'])

    def tearDown(self):
        self.runtime.close()
        self.env.stop()
        self.tmp.cleanup()

    def step(self, decision=None, operation='op-1', base=None, **kwargs):
        base = base or self.started
        return {'task_id': base['task_id'], 'observation_id': base['observation']['id'],
                'operation_id': operation, 'decision': decision or {'action': 'tap', 'target': 'n0'}, **kwargs}

    def test_one_action_then_new_observation(self):
        self.phone.after = screen('Results')
        out = self.runtime.call('step', self.step())
        self.assertTrue(out['ok'])
        self.assertEqual(len(self.phone.calls), 1)
        self.assertEqual(out['status'], 'needs_decision')
        self.assertEqual(out['receipts'][0]['outcome'], 'observed')
        self.assertNotEqual(out['observation']['id'], self.started['observation']['id'])
        validate(OUTPUT, out)

    def test_page_change_rejects_old_action(self):
        self.phone.current = screen('New page')
        out = self.runtime.call('step', self.step())
        self.assertEqual(out['code'], 'SCREEN_CHANGED_REPLAN')
        self.assertEqual(self.phone.calls, [])

    def test_duplicate_receipt_survives_process_recreation(self):
        request = self.step()
        out = self.runtime.call('step', request)
        self.runtime.close()
        self.runtime = Runtime(self.config, lambda *_: self.phone)
        replay = self.runtime.call('step', request)
        self.assertEqual(out, replay)
        self.assertEqual(len(self.phone.calls), 1)

    def test_conflicting_operation_body_rejected(self):
        request = self.step()
        self.runtime.call('step', request)
        request['decision'] = {'action': 'home'}
        self.assertEqual(self.runtime.call('step', request)['code'], 'OPERATION_ID_CONFLICT')
        self.assertEqual(len(self.phone.calls), 1)

    def test_unknown_outcome_blocks_new_actions_and_cancel(self):
        self.phone.fail = True
        out = self.runtime.call('step', self.step())
        self.assertEqual(out['status'], 'outcome_unknown')
        self.assertEqual(self.runtime.call('step', self.step(operation='op-2'))['code'], 'TASK_NOT_ACTIONABLE')
        cancelled = self.runtime.call('cancel', {'task_id': out['task_id']})
        self.assertEqual(cancelled['code'], 'RECONCILE_REQUIRED')
        self.assertEqual(len(self.phone.calls), 1)

    def test_crashed_started_record_is_not_replayed(self):
        task = self.runtime.store.get(self.started['task_id'])
        self.runtime.store.start(task, 'lost', 'digest')
        self.runtime.close()
        self.runtime = Runtime(self.config, lambda *_: self.phone)
        status = self.runtime.call('status', {'task_id': task['id']})
        self.assertEqual(status['status'], 'outcome_unknown')
        self.assertEqual(self.phone.calls, [])

    def test_explicit_reconcile_observes_again(self):
        self.phone.fail = True
        out = self.runtime.call('step', self.step())
        token = out['gate']['token']
        self.assertEqual(self.runtime.call('resume', {'task_id': out['task_id'], 'token': 'x' * 24})['code'], 'INVALID_RESUME_TOKEN')
        resumed = self.runtime.call('resume', {'task_id': out['task_id'], 'token': token})
        self.assertEqual(resumed['status'], 'needs_decision')
        self.assertEqual(len(self.phone.calls), 1)
        self.assertEqual(self.runtime.call('resume', {'task_id': out['task_id'], 'token': token})['code'], 'INVALID_RESUME_TOKEN')

    def test_real_label_triggers_confirmation(self):
        self.phone.current = screen('立即支付')
        observed = self.runtime.call('observe', {'task_id': self.started['task_id']})
        request = self.step(base=observed)
        out = self.runtime.call('step', request)
        self.assertEqual(out['status'], 'waiting_confirmation')
        self.assertEqual(self.phone.calls, [])
        request['confirmation_token'] = out['gate']['token']
        executed = self.runtime.call('step', request)
        self.assertTrue(executed['ok'])
        self.assertEqual(len(self.phone.calls), 1)

    def test_confirmation_never_carries_over_changed_screen(self):
        self.phone.current = screen('立即支付')
        observed = self.runtime.call('observe', {'task_id': self.started['task_id']})
        request = self.step(base=observed)
        out = self.runtime.call('step', request)
        request['confirmation_token'] = out['gate']['token']
        self.phone.current = screen('支付另一笔')
        self.assertEqual(self.runtime.call('step', request)['code'], 'SCREEN_CHANGED_REPLAN')
        self.assertEqual(self.phone.calls, [])

    def test_sensitive_target_only_handoff(self):
        self.phone.current = screen('private', password=True)
        observed = self.runtime.call('observe', {'task_id': self.started['task_id']})
        self.assertNotIn('private', dumps(observed))
        out = self.runtime.call('step', self.step(base=observed))
        self.assertEqual(out['status'], 'waiting_handoff')
        self.assertEqual(self.phone.calls, [])

    def test_same_screen_repeated_action_is_blocked(self):
        first = self.runtime.call('step', self.step())
        second = self.runtime.call('step', self.step(operation='op-2', base=first))
        self.assertEqual(second['code'], 'REPEATED_ACTION_BLOCKED')
        self.assertEqual(len(self.phone.calls), 1)

    def test_finish_requires_visible_evidence(self):
        out = self.runtime.call('step', self.step({'action': 'finish', 'expect': [{'kind': 'text_present', 'value': 'Not here'}]}))
        self.assertEqual(out['code'], 'FINISH_NOT_VERIFIED')
        good = self.runtime.call('step', self.step({'action': 'finish', 'expect': [{'kind': 'text_present', 'value': 'Search'}]}))
        self.assertEqual(good['status'], 'succeeded')
        self.assertEqual(self.phone.calls, [])

    def test_negative_only_finish_cannot_succeed(self):
        out = self.runtime.call('step', self.step({'action': 'finish', 'expect': [{'kind': 'text_absent', 'value': 'missing'}]}))
        self.assertEqual(out['code'], 'FINISH_NOT_VERIFIED')

    def test_task_idempotency_conflict(self):
        out = self.runtime.call('begin', {'goal': 'Different goal', 'device_id': 'cloud-1', 'idempotency_key': 'user-turn-1'})
        self.assertEqual(out['code'], 'IDEMPOTENCY_CONFLICT')

    def test_other_task_cannot_share_active_device(self):
        out = self.runtime.call('begin', {'goal': 'Other goal', 'device_id': 'cloud-1', 'idempotency_key': 'user-turn-2'})
        self.assertEqual(out['code'], 'DEVICE_HAS_ACTIVE_TASK')

    def test_new_observe_invalidates_old_observation(self):
        self.runtime.call('observe', {'task_id': self.started['task_id']})
        self.assertEqual(self.runtime.call('step', self.step())['code'], 'STALE_OBSERVATION')
        self.assertEqual(self.phone.calls, [])

    def test_foreign_target_rejected(self):
        out = self.runtime.call('step', self.step({'action': 'tap', 'target': 'n999'}))
        self.assertEqual(out['code'], 'UNKNOWN_TARGET')
        self.assertEqual(self.phone.calls, [])

    def test_confirmation_all_mode(self):
        self.config.confirm_all = True
        self.assertEqual(self.runtime.call('step', self.step())['status'], 'waiting_confirmation')
        self.assertEqual(self.phone.calls, [])

    def test_max_action_budget(self):
        self.config.max_steps = 1
        first = self.runtime.call('step', self.step())
        out = self.runtime.call('step', self.step({'action': 'home'}, operation='op-2', base=first))
        self.assertEqual(out['code'], 'STEP_LIMIT')
        self.assertEqual(len(self.phone.calls), 1)

    def test_cancel_releases_phone(self):
        self.runtime.call('cancel', {'task_id': self.started['task_id']})
        out = self.runtime.call('begin', {'goal': 'next', 'device_id': 'cloud-1', 'idempotency_key': 'next'})
        self.assertTrue(out['ok'])

    def test_config_mapping_cannot_change_existing_task(self):
        self.config.devices['cloud-1']['udid'] = 'other-phone'
        self.assertEqual(self.runtime.call('step', self.step())['code'], 'DEVICE_CONFIGURATION_CHANGED')

    def test_autonomous_uses_isolated_one_step_messages(self):
        self.runtime.call('cancel', {'task_id': self.started['task_id']})
        self.config.llm.update(base_url='https://model.example', model='example')
        self.phone.after = screen('Results')
        requests = []
        def complete(_client, method, path, body, **kwargs):
            requests.append(body)
            decision = {'action': 'tap', 'target': 'n0'} if len(requests) == 1 else {
                'action': 'finish', 'expect': [{'kind': 'text_present', 'value': 'Results'}]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': dumps(decision)}}]}
        with patch('auto_phone.entry.Http.call', complete):
            out = autonomous(self.runtime, {'goal': 'Show results', 'device_id': 'cloud-1', 'idempotency_key': 'auto'})
        self.assertEqual(out['status'], 'succeeded')
        self.assertEqual(len(self.phone.calls), 1)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all([m['role'] for m in r['messages']] == ['system', 'user'] for r in requests))
        self.assertIn('Results', requests[1]['messages'][1]['content'])


class Contracts(unittest.TestCase):
    def test_reject_invalid_json(self):
        bad = ['[]', 'null', '{} {}', 'prefix {}', '```json\n{}\n```',
               '{"x":1,"x":2}', '{"x":{"a":1,"a":2}}', '{"n":NaN}',
               '{"n":Infinity}', '{"s":"\\ud800"}', b'\xff',
               '{"s":' + '['*34 + '0' + ']'*34 + '}']
        for item in bad:
            with self.subTest(item=str(item)[:40]), self.assertRaises(Fault):
                loads(item)

    def test_reject_invalid_actions(self):
        bad = [{'action': 'shell', 'text': 'ls'}, {'action': 'tap', 'x': 10, 'y': 20},
               {'action': 'tap', 'target': 'n0', 'allow_payments': True},
               {'action': 'wait', 'milliseconds': True}, {'action': 'wait', 'milliseconds': '100'},
               {'action': 'wait', 'milliseconds': 4000}, {'action': 'tap'},
               {'action': 'type', 'target': 'n0', 'text': 123}, {'action': 'finish', 'expect': []},
               {'action': 'scroll', 'target': 'n0', 'direction': 'diagonal'},
               {'action': 'launch_app', 'package': 'bad; shell'},
               {'steps': [{'action': 'home'}, {'action': 'back'}]},
               {'action': 'home', 'reasoning': 'long thought'},
               {'action': 'tap', 'target': 'n0', 'risk': 'low'},
               {'action': 'tap', 'target': 'n0', 'timeout': 9999}]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(Fault):
                validate(DECISION, item)

    def test_valid_decisions(self):
        for item in [{'action': 'tap', 'target': 'n0'}, {'action': 'type', 'target': 'n1', 'text': '北京南站'},
                     {'action': 'wait', 'milliseconds': 100}, {'action': 'home'},
                     {'action': 'scroll', 'target': 'n0', 'direction': 'up'},
                     {'action': 'finish', 'expect': [{'kind': 'package_is', 'value': 'com.android.settings'}]}]:
            validate(DECISION, item)

    def test_private_device_connection_never_in_command(self):
        for key in ['appium_url', 'udid', 'bridge_id', 'system_port', 'capabilities', 'api_key']:
            with self.subTest(key=key), self.assertRaises(Fault):
                validate(COMMANDS['begin'], {'goal': 'open', 'device_id': 'd', 'idempotency_key': 'k', key: 'x'})

    def test_xml_entities_and_limits_rejected(self):
        for xml in ['<!DOCTYPE x><hierarchy/>', '<!ENTITY xx "secret"><hierarchy/>', '<bad']:
            with self.assertRaises(Fault):
                parse_screen(xml, '')

    def test_unicode_case_and_spaces_preserved(self):
        observed = screen('Order ABC 北京')
        self.assertEqual(observed['nodes'][0]['text'], 'Order ABC 北京')

    def test_password_redacted(self):
        observed = screen('do-not-store-me', password=True)
        self.assertNotIn('do-not-store-me', dumps(observed))
        self.assertEqual(node_risk({'action': 'tap', 'target': 'n0'}, observed)[0], 'handoff')

    def test_plaintext_public_endpoint_rejected(self):
        for value in ['http://public.example.com', 'https://u:p@example.com', 'http://0.0.0.0:4723',
                      'https://example.com/?token=x', 'file:///tmp/test']:
            with self.subTest(value=value), self.assertRaises(Fault):
                private_url(value)
        self.assertEqual(private_url('http://127.0.0.1:4723/'), 'http://127.0.0.1:4723')

    def test_no_text_absence_proof_on_truncated_tree(self):
        observed = screen('known')
        observed['omitted_nodes'] = 100
        self.assertFalse(conditions_met([{'kind': 'text_absent', 'value': 'missing'}], observed))

    def test_schema_matches_reference_validator_when_installed(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('Optional independent JSON Schema implementation is not installed')
        for schema in [*COMMANDS.values(), DECISION, OUTPUT]:
            jsonschema.Draft202012Validator.check_schema(schema)
        for value in [{'action': 'home'}, {'action': 'wait', 'milliseconds': True},
                      {'action': 'tap', 'target': 'n0'}, {'steps': []}, {'action': 'wait', 'milliseconds': '2'}]:
            expected = jsonschema.Draft202012Validator(DECISION).is_valid(value)
            try:
                validate(DECISION, value)
                actual = True
            except Fault:
                actual = False
            self.assertEqual(actual, expected)


class Protocol(unittest.TestCase):
    def test_mcp_handshake_and_doctor(self):
        class RuntimeStub:
            def call(self, command, args):
                return {'version': '1.0', 'ok': True, 'code': 'OK'}
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'phone_doctor', 'arguments': {}}}]
        output = io.StringIO()
        mcp_server(RuntimeStub(), io.BytesIO(('\n'.join(dumps(x) for x in messages)+'\n').encode()), output)
        lines = [loads(x) for x in output.getvalue().splitlines()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(len(lines[1]['result']['tools']), len(COMMANDS))
        self.assertEqual(lines[2]['result']['structuredContent']['code'], 'OK')

    def test_mcp_parse_error_and_oversize(self):
        for wire in [b'{bad}\n', b'x'*262145 + b'\n']:
            output = io.StringIO()
            mcp_server(None, io.BytesIO(wire), output)
            self.assertIn('error', loads(output.getvalue()))

    def test_tools_require_handshake(self):
        output = io.StringIO()
        mcp_server(None, io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'), output)
        self.assertIn('error', loads(output.getvalue()))


if __name__ == '__main__':
    unittest.main()
