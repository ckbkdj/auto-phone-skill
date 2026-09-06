"""Actual Appium Python SDK against a W3C HTTP fixture, not an Android certification."""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from lobster_phone_agent.device.appium_device import AppiumDevice
from lobster_phone_agent.skill.models import LocalDeviceDescriptor, SkillRuntimeConfig


@pytest.mark.skipif(importlib.util.find_spec('appium') is None, reason='SDK installed in CI with [skill]')
@pytest.mark.asyncio
async def test_real_appium_sdk_session_settings_and_gesture_http():
    calls, state = [], {'package': 'com.example'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def reply(self, value):
            raw = json.dumps({'value': value}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            calls.append(('GET',self.path,None))
            if self.path.endswith('/source'):
                value = '<hierarchy><node text="Continue" class="android.widget.Button" clickable="true" bounds="[0,0][100,100]"/></hierarchy>'
            elif self.path.endswith('/current_package'): value = state['package']
            elif self.path.endswith('/current_activity'): value = '.MainActivity'
            else: value = {'x':0,'y':0,'width':1080,'height':2400}
            self.reply(value)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
            calls.append(('POST',self.path,body)); value = None
            if self.path == '/session': value = {'sessionId':'fixture-session','capabilities':{'platformName':'Android','appium:automationName':'UiAutomator2'}}
            elif self.path.endswith('/activate_app'): state['package'] = body['appId']
            elif body.get('script') == 'mobile: activateApp': state['package'] = body['args'][0]['appId']
            elif body.get('script') == 'mobile: listApps': value = {'com.example':{'versionName':'1.0'}}
            self.reply(value)
        def do_DELETE(self):
            calls.append(('DELETE',self.path,None)); self.reply(None)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        d = LocalDeviceDescriptor(id='fixture',udid='fixture-device',appium_url=f'http://127.0.0.1:{server.server_port}')
        device = await AppiumDevice.connect(d,SkillRuntimeConfig())
        assert (await device.snapshot()).package == 'com.example'
        await device.tap(50,50); await device.home(); await device.swipe('up')
        await device.launch_app('com.test.target')
        assert await device.is_alive()
        assert (await device.list_apps())[0]['package'] == 'com.example'
        await device.close()
        caps = next(body for method,path,body in calls if path == '/session')['capabilities']['alwaysMatch']
        assert caps['appium:udid'] == 'fixture-device'
        assert any(path.endswith('/appium/settings') for _,path,_ in calls)
        assert any(body and body.get('script') == 'mobile: clickGesture' for _,_,body in calls)
        assert any(method == 'DELETE' for method,_,_ in calls)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
