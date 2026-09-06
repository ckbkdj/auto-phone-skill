from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic

from lobster_phone_agent.device.appium_device import AppiumDevice
from lobster_phone_agent.device.base import DeviceAdapter
from lobster_phone_agent.errors import ExecutionError
from lobster_phone_agent.skill.models import LocalDeviceDescriptor, SkillRuntimeConfig


@dataclass(slots=True)
class LocalRuntime:
    descriptor: LocalDeviceDescriptor
    device: DeviceAdapter | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=monotonic)


class LocalAppiumPool:
    def __init__(
        self, devices: list[LocalDeviceDescriptor], settings: SkillRuntimeConfig
    ) -> None:
        self.settings = settings
        self._runtimes = {item.id: LocalRuntime(descriptor=item) for item in devices}
        self._guard = asyncio.Lock()

    @property
    def device_ids(self) -> list[str]:
        return list(self._runtimes)

    @asynccontextmanager
    async def lease(self, device_id: str) -> AsyncIterator[DeviceAdapter]:
        runtime = self._runtimes.get(device_id)
        if runtime is None:
            raise ExecutionError(f"private skill has no device {device_id!r}")
        async with runtime.lock:
            if runtime.device is not None and getattr(runtime.device, "outcome_uncertain", False):
                raise ExecutionError("device quarantined after an uncertain command; manual reconciliation required")
            if runtime.device is None or not await runtime.device.is_alive():
                if runtime.device is not None:
                    await runtime.device.close()
                runtime.device = await AppiumDevice.connect(runtime.descriptor, self.settings)
            runtime.last_used = monotonic()
            yield runtime.device
            runtime.last_used = monotonic()

    async def reap_idle(self) -> int:
        expired: list[DeviceAdapter] = []
        async with self._guard:
            now = monotonic()
            for runtime in self._runtimes.values():
                if runtime.lock.locked() or runtime.device is None or getattr(runtime.device, "outcome_uncertain", False):
                    continue
                if now - runtime.last_used >= self.settings.appium_session_ttl_seconds:
                    expired.append(runtime.device)
                    runtime.device = None
        await asyncio.gather(*(device.close() for device in expired), return_exceptions=True)
        return len(expired)

    async def close_all(self) -> None:
        devices = [runtime.device for runtime in self._runtimes.values() if runtime.device]
        for runtime in self._runtimes.values():
            runtime.device = None
        await asyncio.gather(*(device.close() for device in devices), return_exceptions=True)
