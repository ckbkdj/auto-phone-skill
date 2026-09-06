"""Real loopback HTTP/WebSocket stack + private dispatcher; synthetic phone only."""
import asyncio
import socket
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from lobster_phone_agent.app import create_app
from lobster_phone_agent.config import Settings
from lobster_phone_agent.skill.client import SkillBridgeClient
from lobster_phone_agent.skill.dispatcher import SkillRpcDispatcher
from lobster_phone_agent.skill.models import SkillConfig, SkillRuntimeConfig
from .fakes import FakeDevice, screen


@pytest.mark.asyncio
async def test_real_outbound_websocket_atomic_action_roundtrip(tmp_path):
    settings = Settings(api_key="a" * 32, bridge_token="b" * 32, llm_model=None,
                        artifact_dir=tmp_path, action_settle_ms=0, bridge_task_wait_seconds=2)
    phone = FakeDevice(states=[screen("com.android.launcher"), screen("com.android.settings")],
                       installed_apps=[{"package": "com.android.settings", "name": "系统设置"}])
    class Pool:
        device_ids = ["phone"]
        settings = SkillRuntimeConfig(operation_journal_path=tmp_path / "journal.sqlite3")
        @asynccontextmanager
        async def lease(self, device_id):
            assert device_id == "phone"
            yield phone
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="error", lifespan="on"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    config = SkillConfig(server_url=host, allow_insecure_http=True, bridge_id="home",
                         api_token="a" * 32, bridge_token="b" * 32, local_token="c" * 32,
                         devices=[{"id": "phone", "udid": "synthetic", "appium_url": "http://127.0.0.1:4723"}])
    bridge = SkillBridgeClient(config, SkillRpcDispatcher(Pool()))
    job = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        job = asyncio.create_task(bridge.run_forever())
        await asyncio.wait_for(bridge.connected.wait(), timeout=4)
        async with httpx.AsyncClient(base_url=host, trust_env=False, timeout=5) as client:
            response = await client.post("/v1/execute?wait_seconds=3",
                headers={"Authorization": "Bearer " + "a" * 32},
                json={"instruction": "打开系统设置", "app_package": "com.android.settings",
                      "device": {"id": "phone", "bridge_id": "home"}})
        assert response.status_code == 200
        record = response.json()
        assert record["status"] == "succeeded", record
        assert record["result"]["mode"] == "stepwise"
        assert record["result"]["steps_executed"] == 1
        assert phone.actions == [("launch", "com.android.settings")]
        assert (tmp_path / "journal.sqlite3").is_file()
    finally:
        await bridge.stop()
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        server.should_exit = True
        await asyncio.to_thread(thread.join, 3)
        sock.close()
