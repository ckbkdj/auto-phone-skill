"""A deliberately small, closed JSON contract shared by CLI, MCP and planner."""
from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_INPUT = 262144
MAX_RESPONSE = 2097152
ID = r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
PACKAGE = r'^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$'


class Fault(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def obj(properties, required=()):
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties, 'required': list(required)}


def string(maximum=4000, pattern=None, minimum=0):
    result = {'type': 'string', 'minLength': minimum, 'maxLength': maximum}
    if pattern:
        result['pattern'] = pattern
    return result


def integer(low, high):
    return {'type': 'integer', 'minimum': low, 'maximum': high}


def array(items, maximum=16, minimum=0):
    return {'type': 'array', 'items': items, 'maxItems': maximum, 'minItems': minimum}


IDENTIFIER = string(128, ID, 1)
CONDITION = {'oneOf': [
    obj({'kind': {'const': 'text_present'}, 'value': string(512, minimum=1)}, ['kind', 'value']),
    obj({'kind': {'const': 'text_absent'}, 'value': string(512, minimum=1)}, ['kind', 'value']),
    obj({'kind': {'const': 'package_is'}, 'value': string(255, PACKAGE, 3)}, ['kind', 'value']),
    obj({'kind': {'const': 'screen_changed'}}, ['kind']),
]}
ACTION_FIELDS = {
    'launch_app': {'package': string(255, PACKAGE, 3)},
    'tap': {'target': IDENTIFIER},
    'type': {'target': IDENTIFIER, 'text': string(4000, r'^(?![\s\S]*\\n$)[^\r\n\t\uE000-\uF8FF]*$')},
    'clear': {'target': IDENTIFIER},
    'scroll': {'target': IDENTIFIER, 'direction': {'enum': ['up', 'down', 'left', 'right']}},
    'back': {}, 'home': {}, 'wait': {'milliseconds': integer(0, 3000)},
    'finish': {}, 'handoff': {'message': string(200, minimum=1)},
}
# No action list, coordinates, code, URLs, locators, retries, or generated risk levels.
DECISION = {'oneOf': [obj(
    {'action': {'const': action}, **fields,
     'expect': array(CONDITION, 8, 1 if action == 'finish' else 0)},
    ['action', *fields, *(['expect'] if action == 'finish' else [])],
) for action, fields in ACTION_FIELDS.items()]}
COMMANDS = {
    'doctor': obj({}),
    'prepare': obj({'device_id': IDENTIFIER}, ['device_id']),
    'tasks': obj({}),
    'setup_status': obj({}),
    'begin': obj({'goal': string(4000, minimum=1), 'device_id': IDENTIFIER,
                  'idempotency_key': IDENTIFIER, 'success': array(CONDITION, 8)},
                 ['goal', 'device_id', 'idempotency_key']),
    'observe': obj({'task_id': IDENTIFIER}, ['task_id']),
    'step': obj({'task_id': IDENTIFIER, 'observation_id': IDENTIFIER,
                 'operation_id': IDENTIFIER, 'decision': DECISION,
                 'confirmation_token': string(128, minimum=16)},
                ['task_id', 'observation_id', 'operation_id', 'decision']),
    'status': obj({'task_id': IDENTIFIER}, ['task_id']),
    'resume': obj({'task_id': IDENTIFIER, 'token': string(128, minimum=16)}, ['task_id', 'token']),
    'cancel': obj({'task_id': IDENTIFIER}, ['task_id']),
}
NODE = obj({'ref': IDENTIFIER, 'text': string(512), 'description': string(512),
            'resource_id': string(512), 'role': {'enum': ['input', 'scrollable', 'button', 'text']},
            'password': {'type': 'boolean'}, 'clickable': {'type': 'boolean'}},
           ['ref', 'text', 'description', 'resource_id', 'role', 'password', 'clickable'])
OBSERVATION = obj({'id': IDENTIFIER, 'fingerprint': IDENTIFIER, 'package': string(255),
                   'nodes': array(NODE, 160), 'omitted_nodes': integer(0, 5000)},
                  ['id', 'fingerprint', 'package', 'nodes', 'omitted_nodes'])
RECEIPT = obj({'operation_id': IDENTIFIER, 'action': {'enum': list(ACTION_FIELDS)},
               'outcome': {'enum': ['verified', 'observed', 'no_change', 'unknown', 'rejected', 'done']},
               'dispatch_ms': {'type': 'number', 'minimum': 0},
               'observation_ms': {'type': 'number', 'minimum': 0}},
              ['operation_id', 'action', 'outcome', 'dispatch_ms', 'observation_ms'])
GATE = obj({'token': string(128, minimum=16), 'message': string(1024),
            'expires_at': {'type': 'number'}, 'kind': {'enum': ['confirmation', 'handoff', 'unknown']}},
           ['token', 'message', 'expires_at', 'kind'])
STATUS = ['active', 'needs_decision', 'waiting_confirmation', 'waiting_handoff',
          'outcome_unknown', 'succeeded', 'cancelled', 'failed', 'executing', 'initialization_failed']
REPORT = obj({'os': string(64), 'architecture': string(32),
              'node_version': string(64), 'npm_version': string(64),
              'jdk_ready': {'type': 'boolean'}, 'sdk_ready': {'type': 'boolean'},
              'local_prerequisites_ready': {'type': 'boolean'}, 'issues': array(IDENTIFIER, 12),
              'config_source': string(32), 'config_path': string(2048),
              'local_config_ignored': {'type': 'boolean'},
              'python': string(64), 'configured_devices': array(IDENTIFIER, 128),
              'node_available': {'type': 'boolean'}, 'adb_available': {'type': 'boolean'},
              'java_available': {'type': 'boolean'}, 'transport': {'const': 'stdio-or-cli'},
              'runtime_dependencies': {'const': 0}},
             ['python', 'configured_devices', 'node_available', 'adb_available',
              'java_available', 'transport', 'runtime_dependencies'])
OUTPUT = obj({'version': {'const': '1.0'}, 'ok': {'type': 'boolean'},
              'code': IDENTIFIER, 'hint': string(500),
              'setup': obj({'stage': IDENTIFIER, 'code': IDENTIFIER, 'updated_at': {'type': 'number'}}, ['stage','code','updated_at']),
              'tasks': array(obj({'task_id': IDENTIFIER, 'device_id': IDENTIFIER, 'status': {'enum': STATUS}, 'steps': integer(0,10000)}, ['task_id','device_id','status','steps']), 16),
              'task_id': IDENTIFIER, 'status': {'enum': STATUS},
              'goal': string(4000), 'observation': OBSERVATION, 'gate': GATE,
              'receipts': array(RECEIPT, 3), 'report': REPORT}, ['version', 'ok', 'code'])


def validate(schema: dict, value: Any, depth: int = 0) -> None:
    """Validate exactly the JSON Schema subset produced above, without pip dependencies."""
    if depth > 32:
        raise Fault('INVALID_CONTRACT')
    if 'oneOf' in schema:
        matches = 0
        for item in schema['oneOf']:
            try:
                validate(item, value, depth + 1)
                matches += 1
            except Fault:
                pass
        if matches != 1:
            raise Fault('INVALID_CONTRACT')
        return
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        raise Fault('INVALID_CONTRACT')
    if 'enum' in schema and not any(type(value) is type(x) and value == x for x in schema['enum']):
        raise Fault('INVALID_CONTRACT')
    kind = schema.get('type')
    if kind == 'object':
        if type(value) is not dict or set(value) - set(schema['properties']) or not set(schema['required']) <= set(value):
            raise Fault('INVALID_CONTRACT')
        for key, item in value.items():
            validate(schema['properties'][key], item, depth + 1)
    elif kind == 'array':
        if type(value) is not list or not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 5000):
            raise Fault('INVALID_CONTRACT')
        for item in value:
            validate(schema['items'], item, depth + 1)
    elif kind == 'string':
        if type(value) is not str or not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 4000):
            raise Fault('INVALID_CONTRACT')
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            raise Fault('INVALID_CONTRACT')
    elif kind == 'boolean':
        if type(value) is not bool:
            raise Fault('INVALID_CONTRACT')
    elif kind in {'integer', 'number'}:
        types = (int,) if kind == 'integer' else (int, float)
        if type(value) not in types or not math.isfinite(value):
            raise Fault('INVALID_CONTRACT')
        if not schema.get('minimum', -1e100) <= value <= schema.get('maximum', 1e100):
            raise Fault('INVALID_CONTRACT')


def loads(raw: str | bytes, limit: int = MAX_INPUT) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Fault('INVALID_JSON')
            result[key] = value
        return result

    try:
        if isinstance(raw, bytes):
            if len(raw) > limit:
                raise Fault('MESSAGE_TOO_LARGE')
            raw = raw.decode('utf-8', 'strict')
        if len(raw.encode('utf-8')) > limit:
            raise Fault('MESSAGE_TOO_LARGE')
        def reject(_):
            raise Fault('INVALID_JSON')
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
        stack, count = [(value, 0)], 0
        while stack:
            item, depth = stack.pop()
            count += 1
            if depth > 32 or count > 60000:
                raise Fault('INVALID_JSON')
            if isinstance(item, str):
                item.encode('utf-8', 'strict')
                if any(ord(c) < 32 and c not in '\n\r\t' for c in item):
                    raise Fault('INVALID_JSON')
            elif isinstance(item, dict):
                stack.extend((x, depth + 1) for pair in item.items() for x in pair)
            elif isinstance(item, list):
                stack.extend((x, depth + 1) for x in item)
            elif isinstance(item, float) and not math.isfinite(item):
                raise Fault('INVALID_JSON')
        if type(value) is not dict:
            raise Fault('INVALID_JSON')
        return value
    except (UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        raise Fault('INVALID_JSON') from exc


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def result(**fields) -> dict:
    payload = {'version': '1.0', 'ok': True, 'code': 'OK', **fields}
    validate(OUTPUT, payload)
    if len(dumps(payload).encode()) > MAX_RESPONSE:
        raise Fault('MESSAGE_TOO_LARGE')
    return payload
