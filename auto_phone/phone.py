"""Live Android semantic elements over the Appium/W3C wire protocol; no coordinates."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote

from .contracts import Fault, dumps
from .platform import Http, ensure_appium

ELEMENT = 'element-6066-11e4-a52e-4f735466cecf'
SENSITIVE = re.compile(r'密码|验证码|动态口令|人脸|指纹|刷脸|滑动验证|图形验证|captcha|password|\botp\b|verification.code', re.I)
COSTLY = re.compile(r'支付|付款|购买|下单|提交订单|预订|订购|叫车|呼叫|用车|删除|注销|发送|发布|评论|拨打|转账|充值|同意|授权|允许|确认|提交|pay|purchase|buy|order|book|send|post|publish|delete|remove|confirm|allow|authorize|agree', re.I)


def parse_screen(source: str, package: str) -> dict:
    if not isinstance(source, str) or len(source.encode()) > 1500000 or '<!DOCTYPE' in source.upper() or '<!ENTITY' in source.upper():
        raise Fault('INVALID_UI')
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise Fault('INVALID_UI') from exc
    nodes, fingerprint = [], []
    stack = [(root, 0)]
    while stack:
        element, depth = stack.pop()
        if depth > 64 or len(fingerprint) >= 5000:
            raise Fault('UI_LIMIT_EXCEEDED')
        a = element.attrib
        text, desc = a.get('text', ''), a.get('content-desc', '')
        password = a.get('password') == 'true' or bool(SENSITIVE.search(' '.join([text, desc, a.get('resource-id', '')])))
        role = ('input' if 'EditText' in a.get('class', '') else
                'scrollable' if a.get('scrollable') == 'true' else
                'button' if a.get('clickable') == 'true' else 'text')
        enabled = a.get('enabled', 'true') == 'true'
        displayed = a.get('displayed', a.get('visible', 'true')) != 'false'
        # Include complete field hashes and bounds to detect movement. Never expose coordinates.
        fingerprint.append([a.get('class', element.tag), text, desc, a.get('resource-id', ''),
                            a.get('bounds', ''), enabled, displayed, a.get('focused', ''),
                            a.get('checked', ''), a.get('selected', ''), password])
        if displayed and enabled and (text or desc or a.get('resource-id') or role != 'text'):
            record = {'ref': 'n' + str(len(nodes)), 'text': '' if password else text[:512],
                      'description': '' if password else desc[:512],
                      'resource_id': a.get('resource-id', '')[:512], 'role': role,
                      'password': password, 'clickable': a.get('clickable') == 'true'}
            # Button containers often put their visible label on a child TextView.
            # Expose that live label for risk assessment, but keep the actual parent locator.
            if not password and record['clickable'] and not text.strip() and not desc.strip():
                child_labels = [child.attrib.get('text', '') or child.attrib.get('content-desc', '')
                                for child in element.iter() if child is not element and child.attrib.get('password') != 'true']
                record['description'] = ' '.join(x for x in child_labels if x)[:512]
            record['_locator'] = {'text': text, 'description': desc,
                                  'resource_id': a.get('resource-id', ''), 'class': a.get('class', ''),
                                  'package': a.get('package', package), 'role': role}
            if password:
                record['_locator']['text'] = ''
                record['_locator']['description'] = ''
            nodes.append(record)
        stack.extend((child, depth + 1) for child in reversed(list(element)))
    # Put actionable controls first but keep stable refs from the full tree.
    nodes.sort(key=lambda n: (n['role'] == 'text', not n['clickable']))
    if not package:
        package = next((x['_locator']['package'] for x in nodes if x['_locator']['package']), '')
    return {'id': uuid.uuid4().hex, 'fingerprint': hashlib.sha256(dumps([package, fingerprint]).encode()).hexdigest(),
            'package': package[:255], 'nodes': nodes[:160], 'omitted_nodes': max(0, len(nodes) - 160),
            '_captured': time.time()}


def public_screen(screen: dict) -> dict:
    return {k: [{x: y for x, y in n.items() if not x.startswith('_')} for n in v] if k == 'nodes' else v
            for k, v in screen.items() if not k.startswith('_')}


def find_node(screen: dict, ref: str) -> dict:
    for node in screen['nodes']:
        if node['ref'] == ref:
            return node
    raise Fault('UNKNOWN_TARGET')


def conditions_met(conditions: list, screen: dict, previous: dict | None = None) -> bool:
    labels = [n['text'] for n in screen['nodes'] if not n['password']]
    labels += [n['description'] for n in screen['nodes'] if not n['password']]
    for item in conditions:
        kind, value = item['kind'], item.get('value', '')
        present = any(value.casefold() in s.casefold() for s in labels) if value else False
        if kind == 'text_present' and not present:
            return False
        if kind == 'text_absent' and (present or screen['omitted_nodes']):
            return False
        if kind == 'package_is' and screen['package'] != value:
            return False
        if kind == 'screen_changed' and (not previous or previous['fingerprint'] == screen['fingerprint']):
            return False
    return True


def node_risk(decision: dict, screen: dict, confirm_all: bool = False) -> tuple[str, str]:
    action = decision['action']
    if action in {'finish', 'wait', 'handoff'}:
        return 'normal', action
    target = find_node(screen, decision['target']) if 'target' in decision else None
    if target and (target['password'] or SENSITIVE.search(target['text'] + ' ' + target['description'])):
        return 'handoff', '请用户在手机上完成登录、密码、验证码或安全验证，然后重新观察。'
    label = (target['text'] + ' ' + target['description']).strip() if target else decision.get('package', action)
    # Launching a ride app is not the same as submitting a ride.
    if confirm_all or (target and COSTLY.search(label)):
        return 'confirmation', f"即将在 {screen['package']} 执行 {action}：{label[:512]}"
    return 'normal', label


class Phone:
    def __init__(self, config, device_id, store):
        self.config, self.device_id, self.store = config, device_id, store
        self.descriptor = config.device(device_id)
        ensure_appium(config, self.descriptor)
        self.http = Http(self.descriptor['appium_url'])
        key = config.identity(device_id)
        self.session = store.session(key)
        if self.session:
            try:
                self.command('GET', '/window/rect')
                return
            except Fault:
                self.session = None
        caps = {'platformName': 'Android', 'appium:automationName': 'UiAutomator2',
                'appium:udid': self.descriptor['udid'], 'appium:deviceName': self.descriptor['udid'],
                'appium:systemPort': self.descriptor['system_port'], 'appium:noReset': True,
                'appium:newCommandTimeout': 1800}
        response = self.http.call('POST', '/session', {'capabilities': {'alwaysMatch': caps, 'firstMatch': [{}]}}, timeout=90)
        value = response.get('value')
        if not isinstance(value, dict) or not isinstance(value.get('sessionId'), str):
            raise Fault('INVALID_SESSION_RESPONSE')
        self.session = value['sessionId']
        store.save_session(key, self.session)
        # These are Appium settings, not nested capabilities. Failure is explicit.
        self.command('POST', '/appium/settings', {'settings': {'waitForIdleTimeout': 300, 'waitForSelectorTimeout': 500}})

    def command(self, method, path, body=None, mutation=False, timeout=20):
        response = self.http.call(method, '/session/' + quote(self.session, safe='') + path,
                                  body, mutation=mutation, timeout=timeout)
        if 'value' not in response:
            raise Fault('INVALID_APPIUM_RESPONSE', uncertain=mutation)
        value = response['value']
        if isinstance(value, dict) and 'error' in value:
            raise Fault('APPIUM_COMMAND_FAILED', uncertain=mutation)
        return value

    def void(self, path, body):
        value = self.command('POST', path, body, True)
        if value is not None:
            raise Fault('INVALID_MUTATION_RECEIPT', uncertain=True)

    def observe(self):
        package = self.command('GET', '/appium/device/current_package')
        xml = self.command('GET', '/source')
        if not isinstance(package, str):
            raise Fault('INVALID_APPIUM_RESPONSE')
        return parse_screen(xml, package)

    def element(self, node):
        loc = node['_locator']
        selector = 'new UiSelector().enabled(true)'
        for key, method in [('resource_id', 'resourceId'), ('text', 'text'),
                            ('description', 'description'), ('class', 'className'), ('package', 'packageName')]:
            if loc.get(key):
                selector += '.' + method + '(' + json.dumps(loc[key], ensure_ascii=True) + ')'
        if loc['role'] == 'scrollable':
            selector += '.scrollable(true)'
        matches = self.command('POST', '/elements', {'using': '-android uiautomator', 'value': selector})
        if not isinstance(matches, list) or len(matches) != 1:
            raise Fault('AMBIGUOUS_OR_MISSING_TARGET')
        element_id = matches[0].get(ELEMENT)
        if not isinstance(element_id, str):
            raise Fault('INVALID_APPIUM_RESPONSE')
        return element_id

    def prepare(self, decision, screen):
        if 'target' not in decision:
            return None
        node = find_node(screen, decision['target'])
        if node['password']:
            raise Fault('SENSITIVE_TARGET')
        action = decision['action']
        if action in {'type', 'clear'} and node['role'] != 'input':
            raise Fault('TARGET_NOT_EDITABLE')
        if action == 'tap' and not node['clickable']:
            raise Fault('TARGET_NOT_CLICKABLE')
        if action == 'scroll' and node['role'] != 'scrollable':
            raise Fault('TARGET_NOT_SCROLLABLE')
        return self.element(node)

    def act(self, decision, element_id=None):
        action = decision['action']
        base = '/element/' + quote(element_id or '', safe='')
        if action == 'launch_app':
            self.void('/appium/device/activate_app', {'appId': decision['package']})
        elif action == 'tap':
            self.void(base + '/click', {})
        elif action == 'type':
            # Replacement is one native element command; no extra focus tap or fallback replay.
            self.void('/execute/sync', {'script': 'mobile: replaceElementValue',
                         'args': [{'elementId': element_id, 'text': decision['text']}]})
            actual = self.command('GET', base + '/text')
            if actual != decision['text']:
                raise Fault('INPUT_NOT_VERIFIED', uncertain=True)
        elif action == 'clear':
            self.void(base + '/clear', {})
            if self.command('GET', base + '/text') != '':
                raise Fault('INPUT_NOT_VERIFIED', uncertain=True)
        elif action == 'scroll':
            result = self.command('POST', '/execute/sync', {'script': 'mobile: scrollGesture',
                'args': [{'elementId': element_id, 'direction': decision['direction'], 'percent': 0.65}]}, True)
            # false means no more scrolling, NOT that the gesture failed. Never retry it blindly.
            if type(result) is not bool:
                raise Fault('INVALID_APPIUM_RESPONSE', uncertain=True)
        elif action == 'back':
            self.void('/back', {})
        elif action == 'home':
            self.void('/appium/device/press_keycode', {'keycode': 3})
        elif action == 'wait':
            time.sleep(decision['milliseconds'] / 1000)
        else:
            raise Fault('UNSUPPORTED_ACTION')
