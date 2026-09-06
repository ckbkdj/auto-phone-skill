from time import monotonic

import pytest

from lobster_phone_agent.errors import ExecutionError
from lobster_phone_agent.skill.models import LocalDeviceDescriptor, SkillRuntimeConfig
from lobster_phone_agent.skill.pool import LocalAppiumPool
from .test_skill_runtime import ClosableDevice


@pytest.mark.asyncio
async def test_quarantined_device_cannot_be_reconnected_or_reaped():
    descriptor = LocalDeviceDescriptor(id="cloud-1", appium_url="http://127.0.0.1:4723", udid="test")
    pool = LocalAppiumPool([descriptor], SkillRuntimeConfig(appium_session_ttl_seconds=60))
    device = ClosableDevice()
    device.outcome_uncertain = True
    runtime = pool._runtimes["cloud-1"]
    runtime.device = device
    runtime.last_used = monotonic() - 120
    with pytest.raises(ExecutionError, match="quarantined"):
        async with pool.lease("cloud-1"):
            pytest.fail("must not issue more commands")
    assert await pool.reap_idle() == 0
    assert not device.closed
