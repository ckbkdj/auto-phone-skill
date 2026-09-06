"""Real Python Appium SDK against an in-process W3C HTTP fixture, NOT a real phone."""
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lobster_phone_agent.device.appium_device import AppiumDevice
from lobster_phone_agent.skill.models import LocalDeviceDescriptor, SkillRuntimeConfig


@pytest.mark.skipif(importlib.util.find_spec("appium") is None, reason="real Appium SDK is installed in CI [skill]")
@pytest.mark.asyncio
async def test_real_appium_sdk_http_session_settings_and_gestures():
    calls = []
    state = {"package": "com.example"}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def respond(self, value):
            body = json.dumps({"value": value}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            calls.append(("GET", self.path, None))
            if self.path.endswith("/source"):
                value = '<hierarchy><node class="android.widget.Button" text="Continue" clickable="true" enabled="true" bounds="[0,0][100,100]" /></hierarchy>'
            elif self.path.endswith("/current_package"):
                value = state["package"]
            elif self.path.endswith("/current_activity"):
                value = ".MainActivity"
            else:
                value = {"x": 0, "y": 0, "width": 1080, "height": 2400}
            self.respond(value)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            calls.append(("POST", self.path, body))
            if self.path == "/session":
                value = {"sessionId": "fixture-session", "capabilities": {"platformName": "Android", "appium:automationName": "UiAutomator2"}}
            elif self.path.endswith("/activate_app"):
                state["package"] = body["appId"]
                value = None
            elif body.get("script") == "mobile: listApps":
                value = {"com.example": {"versionName": "1.0"}}
            else:
                value = None
            self.respond(value)
        def do_DELETE(self):
            calls.append(("DELETE", self.path, None))
            self.respond(None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        d = LocalDeviceDescriptor(id="fixture", appium_url=f"http://127.0.0.1:{server.server_port}", udid="fixture-device")
        phone = await AppiumDevice.connect(d, SkillRuntimeConfig())
        assert (await phone.snapshot()).package == "com.example"
        await phone.tap(50, 50)
        await phone.home()
        await phone.swipe("up")
        await phone.launch_app("com.test.target")
        assert await phone.is_alive()
        assert (await phone.list_apps())[0]["package"] == "com.example"
        await phone.close()
        created = next(body for method, path, body in calls if path == "/session")
        assert created["capabilities"]["alwaysMatch"]["appium:udid"] == "fixture-device"
        assert any(path.endswith("/appium/settings") for _, path, _ in calls)
        assert any(body and body.get("script") == "mobile: clickGesture" for _, _, body in calls)
        assert any(method == "DELETE" for method, _, _ in calls)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
