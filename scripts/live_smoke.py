#!/usr/bin/env python3
"""Explicit, non-destructive REAL Appium smoke test; never used by unit-test fixtures."""
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auto_phone.platform import Config
from auto_phone.runtime import Runtime
from auto_phone.contracts import dumps


def main():
    runtime = Runtime(Config())
    try:
        device_id = os.environ.get('AUTO_PHONE_DEVICE_ID', 'cloud-1')
        answer = runtime.call('begin', {'goal': '打开系统设置以检查真实设备接入', 'device_id': device_id,
            'idempotency_key': 'live-' + uuid.uuid4().hex,
            'success': [{'kind': 'package_is', 'value': 'com.android.settings'}]})
        if answer['ok']:
            answer = runtime.call('step', {'task_id': answer['task_id'],
                'observation_id': answer['observation']['id'], 'operation_id': 'launch-settings',
                'decision': {'action': 'launch_app', 'package': 'com.android.settings',
                    'expect': [{'kind': 'package_is', 'value': 'com.android.settings'}]}})
        if answer['ok'] and answer.get('status') == 'needs_decision':
            answer = runtime.call('step', {'task_id': answer['task_id'],
                'observation_id': answer['observation']['id'], 'operation_id': 'finish',
                'decision': {'action': 'finish', 'expect': [{'kind': 'package_is', 'value': 'com.android.settings'}]}})
        print(dumps(answer))
        return 0 if answer.get('status') == 'succeeded' else 1
    finally:
        runtime.close()

if __name__ == '__main__':
    raise SystemExit(main())
