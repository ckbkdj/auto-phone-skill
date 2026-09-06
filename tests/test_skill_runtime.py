from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from time import monotonic

import pytest

from lobster_phone_agent.errors import ExecutionError
from lobster_phone_agent.skill.client import SkillBridgeClient
from lobster_phone_agent.skill.dispatcher import SkillRpcDispatcher
from lobster_phone_agent.skill.models import (
    LocalDeviceDescriptor,
    SkillConfig,
    SkillRuntimeConfig,
)
from lobster_phone_agent.skill.pool import LocalAppiumPool


class FailingDevice:
    device_id = "cloud-1"

    async def launch_app(self, _package: str, *, operation_id: str | None = None) -> None:
        del operation_id
        raise ExecutionError("activate failed")


class FakePool:
    settings = SkillRuntimeConfig()

    @asynccontextmanager
    async def lease(self, _device_id: str):
        yield FailingDevice()


@pytest.mark.asyncio
async def test_skill_dispatcher_propagates_device_failure() -> None:
    dispatcher = SkillRpcDispatcher(FakePool())  # type: ignore[arg-type]
    with pytest.raises(ExecutionError, match="activate failed"):
        await dispatcher.dispatch(
            "cloud-1",
            "launch_app",
            {"package": "com.example", "operation_id": "launch-op-1"},
        )


class ClosableDevice:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_idle_reaper_closes_the_actual_detached_session() -> None:
    descriptor = LocalDeviceDescriptor(
        id="cloud-1",
        appium_url="http://127.0.0.1:4723",
        udid="emulator-5554",
    )
    pool = LocalAppiumPool(
        [descriptor], SkillRuntimeConfig(appium_session_ttl_seconds=60)
    )
    runtime = pool._runtimes["cloud-1"]
    device = ClosableDevice()
    runtime.device = device  # type: ignore[assignment]
    runtime.last_used = monotonic() - 120
    assert await pool.reap_idle() == 1
    assert device.closed
    assert runtime.device is None


class ConcurrencyDetectingWebSocket:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.005)
        self.sent.append(payload)
        self.active -= 1


@pytest.mark.asyncio
async def test_skill_websocket_sends_are_serialized() -> None:
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
                }
            ],
        }
    )
    client = SkillBridgeClient(config, dispatcher=None)  # type: ignore[arg-type]
    websocket = ConcurrencyDetectingWebSocket()
    await asyncio.gather(*(client._send(websocket, str(index)) for index in range(10)))
    assert websocket.max_active == 1
    assert len(websocket.sent) == 10

class CountingDevice:
    device_id = "cloud-1"

    def __init__(self) -> None:
        self.launches = 0

    async def launch_app(
        self,
        _package: str,
        *,
        operation_id: str | None = None,
    ) -> None:
        assert operation_id
        self.launches += 1
        await asyncio.sleep(0.01)


class CountingPool:
    settings = SkillRuntimeConfig()

    def __init__(self) -> None:
        self.device = CountingDevice()

    @asynccontextmanager
    async def lease(self, _device_id: str):
        yield self.device


@pytest.mark.asyncio
async def test_mutating_rpc_is_exactly_once_for_same_operation_id() -> None:
    pool = CountingPool()
    dispatcher = SkillRpcDispatcher(pool)  # type: ignore[arg-type]
    results = await asyncio.gather(
        *(
            dispatcher.dispatch(
                "cloud-1",
                "launch_app",
                {"package": "com.example", "operation_id": "task-1:launch"},
            )
            for _ in range(8)
        )
    )
    assert pool.device.launches == 1
    assert results == [{"performed": True}] * 8


@pytest.mark.asyncio
async def test_mutating_rpc_without_operation_id_is_rejected() -> None:
    from lobster_phone_agent.errors import BridgeProtocolError

    dispatcher = SkillRpcDispatcher(CountingPool())  # type: ignore[arg-type]
    with pytest.raises(BridgeProtocolError, match="requires operation_id"):
        await dispatcher.dispatch(
            "cloud-1",
            "launch_app",
            {"package": "com.example"},
        )

class AppListingDevice:
    device_id = "cloud-1"

    def __init__(self) -> None:
        self.calls = 0

    async def list_apps(self) -> list[dict[str, object]]:
        self.calls += 1
        return [{"package": "com.example", "label": "Example"}]


class AppListingPool:
    settings = SkillRuntimeConfig(installed_apps_cache_seconds=300)

    def __init__(self) -> None:
        self.device = AppListingDevice()

    @asynccontextmanager
    async def lease(self, _device_id: str):
        yield self.device


@pytest.mark.asyncio
async def test_installed_app_list_is_cached_inside_private_skill() -> None:
    pool = AppListingPool()
    dispatcher = SkillRpcDispatcher(pool)  # type: ignore[arg-type]
    first = await dispatcher.dispatch("cloud-1", "list_apps", {})
    first[0]["package"] = "mutated-by-caller"
    second = await dispatcher.dispatch("cloud-1", "list_apps", {})
    assert pool.device.calls == 1
    assert second == [{"package": "com.example", "label": "Example"}]

@pytest.mark.asyncio
async def test_operation_id_cannot_be_reused_with_different_parameters() -> None:
    from lobster_phone_agent.errors import BridgeProtocolError

    dispatcher = SkillRpcDispatcher(CountingPool())  # type: ignore[arg-type]
    await dispatcher.dispatch(
        "cloud-1",
        "launch_app",
        {"package": "com.example.one", "operation_id": "same-operation"},
    )
    with pytest.raises(BridgeProtocolError, match="different RPC parameters"):
        await dispatcher.dispatch(
            "cloud-1",
            "launch_app",
            {"package": "com.example.two", "operation_id": "same-operation"},
        )
