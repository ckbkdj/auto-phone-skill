from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lobster_phone_agent.schemas import DeviceDescriptor, TaskRequest
from lobster_phone_agent.skill.cli import _loopback_only
from lobster_phone_agent.skill.models import SkillConfig


def test_public_device_schema_rejects_private_appium_details() -> None:
    with pytest.raises(ValidationError, match="private to the skill bridge"):
        TaskRequest.model_validate(
            {
                "instruction": "打开美团",
                "device": {
                    "id": "cloud-1",
                    "bridge_id": "lobster-a",
                    "udid": "emulator-5554",
                    "appium_url": "http://127.0.0.1:4723",
                },
            }
        )


def test_public_device_descriptor_contains_only_routing_identity() -> None:
    device = DeviceDescriptor(id="cloud-1", bridge_id="lobster-a", metadata={"region": "cn"})
    assert device.model_dump() == {
        "id": "cloud-1",
        "bridge_id": "lobster-a",
        "metadata": {"region": "cn"},
    }


def test_private_skill_listener_is_loopback_only() -> None:
    assert _loopback_only("127.0.0.1")
    assert _loopback_only("::1")
    assert _loopback_only("localhost")
    assert not _loopback_only("0.0.0.0")
    assert not _loopback_only("192.168.1.10")


def test_skill_websocket_url_uses_outbound_wss() -> None:
    config = SkillConfig.model_validate(
        {
            "server_url": "https://phone.example.com",
            "bridge_id": "lobster-a",
            "bridge_token": "bridge-token-1234567890abcdef",
            "api_token": "api-token-1234567890abcdefghi",
            "local_token": "local-token-1234567890abcdef",
            "devices": [
                {
                    "id": "cloud-1",
                    "appium_url": "http://127.0.0.1:4723",
                    "udid": "emulator-5554",
                    "metadata": {
                        "region": "cn",
                        "provider": "private",
                        "secret": "must-not-leak",
                    },
                }
            ],
        }
    )
    assert config.websocket_url == "wss://phone.example.com/v1/bridge/ws/lobster-a"
    assert config.devices[0].advertised_metadata() == {
        "region": "cn",
        "provider": "private",
    }


def test_control_plane_container_has_no_appium_or_adb_runtime() -> None:
    dockerfile = Path("Dockerfile").read_text("utf-8").lower()
    assert "android-sdk" not in dockerfile
    assert "adb " not in dockerfile
    assert "appium" not in dockerfile


def test_public_compose_exposes_only_caddy_ingress() -> None:
    compose = Path("deploy/docker-compose.yml").read_text("utf-8")
    assert '"80:80"' in compose
    assert '"443:443"' in compose
    assert '"8788:8788"' not in compose
    assert "phone-agent:" in compose
    assert "caddy:" in compose
