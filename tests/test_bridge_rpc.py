from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lobster_phone_agent.app import create_app
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.remote_device import RemoteDevice
from lobster_phone_agent.schemas import DeviceDescriptor

from .fakes import screen


AUTH = {"Authorization": "Bearer api-key-123456"}
BRIDGE_AUTH = {"Authorization": "Bearer bridge-key-123456"}


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        config_dir=Path("config"),
        artifact_dir=tmp_path,
        enable_a2a=False,
        api_key="api-key-123456",
        bridge_token="bridge-key-123456",
        llm_model=None,
        action_settle_ms=0,
        snapshot_stable_interval_ms=1,
        snapshot_stable_timeout_ms=5,
        bridge_rpc_timeout_seconds=2,
        bridge_connect_grace_seconds=1,
        bridge_task_wait_seconds=2,
    )


def hello() -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol": "1.0",
        "bridge_id": "lobster-a",
        "instance_id": "test-instance",
        "devices": [{"id": "cloud-1", "platform": "android", "metadata": {"region": "cn"}}],
    }


def rpc_result(request: dict[str, Any], result: Any, *, ok: bool = True, error: str | None = None):
    return {
        "type": "rpc_result",
        "id": request["id"],
        "sequence": request["sequence"],
        "ok": ok,
        "result": result,
        "error": error,
        "duration_ms": 1.0,
    }


def test_bridge_requires_authentication(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/bridge/ws/lobster-a"):
                pass


def test_bridge_registration_is_visible_without_private_details(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        with client.websocket_connect(
            "/v1/bridge/ws/lobster-a", headers=BRIDGE_AUTH
        ) as websocket:
            websocket.send_json(hello())
            assert websocket.receive_json()["type"] == "hello_ack"
            response = client.get("/v1/bridges", headers=AUTH)
            assert response.status_code == 200
            payload = response.json()
            assert payload["connected_bridges"] == 1
            rendered = str(payload)
            assert "appium" not in rendered.lower()
            assert "emulator-5554" not in rendered


def _execute_open_app(client: TestClient, websocket, *, performed: bool) -> dict[str, Any]:
    output: dict[str, Any] = {}

    def request_task() -> None:
        response = client.post(
            "/v1/execute?wait_seconds=0.2",
            headers=AUTH,
            json={
                "instruction": "打开美团",
                "device": {"id": "cloud-1", "bridge_id": "lobster-a"},
            },
        )
        output["status_code"] = response.status_code
        output["json"] = response.json()

    thread = threading.Thread(target=request_task, daemon=True)
    thread.start()
    foreground = "com.android.launcher"
    # Success: initial snapshot, list apps, pre-action snapshot, launch, two stable snapshots.
    # Failure: initial snapshot, list apps, then two pre-action snapshot/launch attempts.
    for _ in range(5 if performed else 3):
        request = websocket.receive_json()
        assert request["type"] == "rpc_request"
        method = request["method"]
        if method == "snapshot":
            payload = screen(foreground, {"text": "Home" if "launcher" in foreground else "美团"})
            websocket.send_json(rpc_result(request, payload.to_payload()))
        elif method == "list_apps":
            websocket.send_json(
                rpc_result(request, [{"package": "com.sankuai.meituan", "name": "美团"}])
            )
        elif method == "atomic_action":
            assert request["params"]["action"]["kind"] == "launch_app"
            if performed:
                foreground = "com.sankuai.meituan"
            websocket.send_json(rpc_result(request, {"state": "executed" if performed else "unknown", "code": "ok" if performed else "execution_uncertain"}))
        else:
            raise AssertionError(method)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert output["status_code"] == 200
    return output["json"]


def test_full_public_control_plane_to_private_skill_rpc_roundtrip(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        with client.websocket_connect(
            "/v1/bridge/ws/lobster-a", headers=BRIDGE_AUTH
        ) as websocket:
            websocket.send_json(hello())
            websocket.receive_json()
            result = _execute_open_app(client, websocket, performed=True)
            assert result["status"] == "succeeded"
            assert result["result"]["app"] == "美团"


def test_performed_false_never_becomes_false_success(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        with client.websocket_connect(
            "/v1/bridge/ws/lobster-a", headers=BRIDGE_AUTH
        ) as websocket:
            websocket.send_json(hello())
            websocket.receive_json()
            result = _execute_open_app(client, websocket, performed=False)
            assert result["status"] == "waiting_handoff"
            assert result["confirmation"]["code"] == "outcome_unknown"


class FalseHub:
    async def call(self, **_kwargs):
        return {"performed": False}


@pytest.mark.asyncio
async def test_remote_device_rejects_false_mutation_ack() -> None:
    device = RemoteDevice(
        DeviceDescriptor(id="cloud-1", bridge_id="lobster-a"),
        FalseHub(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="did not perform tap"):
        await device.tap(1, 2)
