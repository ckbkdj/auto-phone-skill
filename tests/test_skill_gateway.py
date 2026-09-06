from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from lobster_phone_agent.skill.gateway import create_skill_app
from lobster_phone_agent.skill.models import SkillConfig
from lobster_phone_agent.schemas import TaskRecord, TaskRequest


BRIDGE_TOKEN = "bridge-token-1234567890-abcdef"
API_TOKEN = "public-api-token-1234567890-abcd"
LOCAL_TOKEN = "local-skill-token-1234567890-abc"


def config_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "server_url": "https://phone.example.com",
        "bridge_id": "lobster-a",
        "bridge_token": BRIDGE_TOKEN,
        "api_token": API_TOKEN,
        "local_token": LOCAL_TOKEN,
        "devices": [
            {
                "id": "cloud-1",
                "appium_url": "http://127.0.0.1:4723",
                "udid": "emulator-5554",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_skill_requires_tls_and_three_distinct_secrets() -> None:
    with pytest.raises(ValidationError, match="must use https"):
        SkillConfig.model_validate(
            config_payload(server_url="http://phone.example.com")
        )
    with pytest.raises(ValidationError, match="must be different"):
        SkillConfig.model_validate(
            config_payload(api_token=BRIDGE_TOKEN)
        )


def test_skill_rejects_unspecified_appium_address() -> None:
    payload = config_payload()
    payload["devices"][0]["appium_url"] = "http://0.0.0.0:4723"
    with pytest.raises(ValidationError, match="unspecified"):
        SkillConfig.model_validate(payload)


def test_multiple_devices_on_one_appium_server_require_unique_system_ports() -> None:
    devices = [
        {
            "id": "cloud-1",
            "appium_url": "http://127.0.0.1:4723",
            "udid": "emulator-5554",
        },
        {
            "id": "cloud-2",
            "appium_url": "http://127.0.0.1:4723",
            "udid": "emulator-5556",
        },
    ]
    with pytest.raises(ValidationError, match="unique explicit system_port"):
        SkillConfig.model_validate(config_payload(devices=devices))

    devices[0]["system_port"] = 8200
    devices[1]["system_port"] = 8201
    config = SkillConfig.model_validate(config_payload(devices=devices))
    assert [item.system_port for item in config.devices] == [8200, 8201]


def test_duplicate_private_device_target_is_rejected() -> None:
    devices = [
        {
            "id": "cloud-1",
            "appium_url": "http://127.0.0.1:4723",
            "udid": "emulator-5554",
            "system_port": 8200,
        },
        {
            "id": "cloud-alias",
            "appium_url": "http://127.0.0.1:4723",
            "udid": "emulator-5554",
            "system_port": 8201,
        },
    ]
    with pytest.raises(ValidationError, match="unique Appium target"):
        SkillConfig.model_validate(config_payload(devices=devices))


@pytest.mark.asyncio
async def test_loopback_gateway_auth_and_public_request_sanitization() -> None:
    config = SkillConfig.model_validate(config_payload())
    app = create_skill_app(config)
    captured: list[tuple[str, str, Any]] = []

    async def fake_request(method: str, path: str, *, json: Any = None) -> dict[str, Any]:
        captured.append((method, path, json))
        return TaskRecord(request=TaskRequest.model_validate(json)).model_dump(mode="json")

    app.state.public_client.request = fake_request
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://skill.local") as client:
        unauthorized = await client.post(
            "/v1/tasks",
            json={"instruction": "打开美团", "device_id": "cloud-1"},
        )
        assert unauthorized.status_code == 401

        headers = {"Authorization": f"Bearer {LOCAL_TOKEN}"}
        forbidden = await client.post(
            "/v1/tasks",
            headers=headers,
            json={
                "instruction": "打开美团",
                "device_id": "cloud-1",
                "appium_url": "http://127.0.0.1:4723",
            },
        )
        assert forbidden.status_code == 422

        accepted = await client.post(
            "/v1/tasks",
            headers=headers,
            json={"instruction": "打开美团", "device_id": "cloud-1"},
        )
        assert accepted.status_code == 200
        assert captured
        forwarded = captured[-1][2]
        assert forwarded["device"] == {
            "id": "cloud-1",
            "bridge_id": "lobster-a",
            "metadata": {},
        }
        rendered = str(forwarded).lower()
        assert "appium_url" not in rendered
        assert "emulator-5554" not in rendered
        assert BRIDGE_TOKEN.lower() not in rendered

@pytest.mark.asyncio
async def test_execute_gateway_timeout_exceeds_requested_wait() -> None:
    config = SkillConfig.model_validate(config_payload())
    app = create_skill_app(config)
    captured: dict[str, Any] = {}

    async def fake_request(
        method: str,
        path: str,
        *,
        json: Any = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict[str, Any]:
        captured.update(method=method, path=path, json=json, timeout=timeout)
        return TaskRecord(request=TaskRequest.model_validate(json)).model_dump(mode="json")

    app.state.public_client.request = fake_request
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://skill.local") as client:
        response = await client.post(
            "/v1/execute?wait_seconds=120",
            headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
            json={"instruction": "打开美团", "device_id": "cloud-1"},
        )
    assert response.status_code == 200
    assert captured["path"] == "/v1/execute?wait_seconds=120.0"
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 135.0


@pytest.mark.asyncio
async def test_skill_health_matches_closed_response_contract() -> None:
    config = SkillConfig.model_validate(config_payload())
    app = create_skill_app(config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://skill.local") as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "bridge_connected": False, "bridge_id": "lobster-a", "devices": ["cloud-1"], "scope": "loopback-only"}
    await app.state.public_client.close()


def test_skill_handshake_rejects_duplicate_json_keys() -> None:
    from lobster_phone_agent.skill.client import SkillBridgeClient
    with pytest.raises(ValueError, match="duplicate"):
        SkillBridgeClient._parse_payload('{"type":"hello_ack","type":"rpc_request"}')
