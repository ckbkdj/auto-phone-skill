from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from lobster_phone_agent.bridge.hub import BridgeHub
from lobster_phone_agent.device.base import DeviceAdapter
from lobster_phone_agent.device.remote_device import RemoteDevice
from lobster_phone_agent.schemas import DeviceDescriptor


@dataclass(slots=True)
class RemoteDeviceRuntime:
    descriptor: DeviceDescriptor
    device: DeviceAdapter
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    leases: int = 0


class DeviceSessionPool:
    """Serializes tasks per private bridge/device without owning an Appium session."""

    def __init__(self, hub: BridgeHub) -> None:
        self.hub = hub
        self._runtimes: dict[tuple[str, str], RemoteDeviceRuntime] = {}
        self._guard = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return self.hub.device_count

    @asynccontextmanager
    async def lease(self, descriptor: DeviceDescriptor) -> AsyncIterator[DeviceAdapter]:
        runtime = await self._reserve(descriptor)
        try:
            async with runtime.lock:
                runtime.leases += 1
                try:
                    await self.hub.wait_connected(descriptor.bridge_id, descriptor.id)
                    yield runtime.device
                finally:
                    runtime.leases = max(0, runtime.leases - 1)
        finally:
            pass

    async def _reserve(self, descriptor: DeviceDescriptor) -> RemoteDeviceRuntime:
        key = (descriptor.bridge_id, descriptor.id)
        async with self._guard:
            runtime = self._runtimes.get(key)
            if runtime is None:
                runtime = RemoteDeviceRuntime(
                    descriptor=descriptor,
                    device=RemoteDevice(descriptor, self.hub),
                )
                self._runtimes[key] = runtime
            return runtime

    async def reap_idle(self) -> int:
        # Real Appium sessions are reaped by the private skill, never by the public server.
        return 0

    async def close_all(self) -> None:
        async with self._guard:
            self._runtimes.clear()
        await self.hub.close()
