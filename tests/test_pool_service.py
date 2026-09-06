from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from lobster_phone_agent.agent.service import TaskService
from lobster_phone_agent.agent.store import InMemoryTaskStore
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.pool import DeviceSessionPool
from lobster_phone_agent.errors import ExecutionError, InvalidTaskState
from lobster_phone_agent.schemas import (
    ActionPlan,
    ActionStep,
    ActionType,
    ConfirmationRequest,
    DeviceDescriptor,
    HandoffResumeRequest,
    TaskRequest,
    TaskStatus,
)

from .fakes import FakeDevice, screen


class FakeHub:
    device_count = 1

    def is_connected(self, bridge_id: str, device_id: str | None = None) -> bool:
        return bridge_id == "bridge-test" and device_id in {None, "dev"}

    async def wait_connected(self, *_args, **_kwargs) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_remote_pool_serializes_leases_for_one_device() -> None:
    pool = DeviceSessionPool(FakeHub())  # type: ignore[arg-type]
    descriptor = DeviceDescriptor(id="dev", bridge_id="bridge-test")
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with pool.lease(descriptor):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(5)))
    assert peak == 1
    assert pool.active_count == 1


class FakePool:
    hub = FakeHub()
    active_count = 1

    def __init__(self) -> None:
        self.device = FakeDevice(states=[screen("com.example", {"text": "ready"})])

    @asynccontextmanager
    async def lease(self, _descriptor):
        yield self.device

    async def close_all(self) -> None:
        return None


class StaticPlanner:
    async def plan(self, *_args, **_kwargs) -> ActionPlan:
        return ActionPlan(
            goal="test",
            steps=[ActionStep(action=ActionType.FINISH, value={"ok": True})],
        )


class FailingExecutor:
    async def execute(self, **_kwargs):
        raise ExecutionError("real action failure")


@pytest.mark.asyncio
async def test_service_records_original_execution_error(tmp_path: Path) -> None:
    service = TaskService(
        settings=Settings(artifact_dir=tmp_path),
        store=InMemoryTaskStore(),
        pool=FakePool(),  # type: ignore[arg-type]
        planner=StaticPlanner(),  # type: ignore[arg-type]
        executor=FailingExecutor(),  # type: ignore[arg-type]
    )
    record = await service.submit(
        TaskRequest(
            instruction="test",
            device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
        )
    )
    record = await service.wait(record.id, timeout=2)
    assert record.status is TaskStatus.FAILED
    assert record.error == "real action failure"
    assert (tmp_path / f"{record.id}.json").exists()
    await service.shutdown()


class HandoffExecutor:
    async def execute(self, *, hooks, **_kwargs):
        await hooks.handoff("login_required", "please login")
        return {"done": True}


@pytest.mark.asyncio
async def test_handoff_resume_requires_current_token(tmp_path: Path) -> None:
    service = TaskService(
        settings=Settings(artifact_dir=tmp_path, confirmation_timeout_seconds=5),
        store=InMemoryTaskStore(),
        pool=FakePool(),  # type: ignore[arg-type]
        planner=StaticPlanner(),  # type: ignore[arg-type]
        executor=HandoffExecutor(),  # type: ignore[arg-type]
    )
    record = await service.submit(
        TaskRequest(
            instruction="login",
            device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
        )
    )
    for _ in range(100):
        record = await service.get(record.id)
        if record.status is TaskStatus.WAITING_HANDOFF:
            break
        await asyncio.sleep(0.005)
    assert record.status is TaskStatus.WAITING_HANDOFF
    assert record.confirmation
    with pytest.raises(InvalidTaskState, match="invalid handoff token"):
        await service.resume_handoff(
            record.id,
            HandoffResumeRequest(token="incorrect-token"),
        )
    await service.resume_handoff(
        record.id,
        HandoffResumeRequest(token=str(record.confirmation["token"])),
    )
    record = await service.wait(record.id, timeout=2)
    assert record.status is TaskStatus.SUCCEEDED
    with pytest.raises(InvalidTaskState):
        await service.resume_handoff(
            record.id,
            HandoffResumeRequest(token="incorrect-token"),
        )
    await service.shutdown()

class ConfirmationExecutor:
    async def execute(self, *, hooks, **_kwargs):
        from lobster_phone_agent.agent.risk import RiskDecision
        from lobster_phone_agent.schemas import RiskLevel, SemanticTarget

        step = ActionStep(
            id="submit",
            action=ActionType.TAP,
            description="确认呼叫车辆",
            target=SemanticTarget(text="确认呼叫", role="button"),
            risk=RiskLevel.HIGH,
        )
        await hooks.confirm(
            step,
            RiskDecision(
                risk=RiskLevel.HIGH,
                confirmation_required=True,
                blocked=False,
                reason="may create cost",
            ),
        )
        return {"submitted": True}


@pytest.mark.asyncio
async def test_confirmation_token_is_single_use_and_approval_resumes(tmp_path: Path) -> None:
    service = TaskService(
        settings=Settings(artifact_dir=tmp_path, confirmation_timeout_seconds=5),
        store=InMemoryTaskStore(),
        pool=FakePool(),  # type: ignore[arg-type]
        planner=StaticPlanner(),  # type: ignore[arg-type]
        executor=ConfirmationExecutor(),  # type: ignore[arg-type]
    )
    record = await service.submit(
        TaskRequest(
            instruction="ride",
            device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
        )
    )
    for _ in range(100):
        record = await service.get(record.id)
        if record.status is TaskStatus.WAITING_CONFIRMATION:
            break
        await asyncio.sleep(0.005)
    assert record.status is TaskStatus.WAITING_CONFIRMATION
    assert record.confirmation
    with pytest.raises(InvalidTaskState, match="invalid confirmation token"):
        await service.confirm(
            record.id,
            ConfirmationRequest(approved=True, token="incorrect-token"),
        )
    token = str(record.confirmation["token"])
    await service.confirm(record.id, ConfirmationRequest(approved=True, token=token))
    record = await service.wait(record.id, timeout=2)
    assert record.status is TaskStatus.SUCCEEDED
    assert record.result == {"submitted": True}
    with pytest.raises(InvalidTaskState):
        await service.confirm(record.id, ConfirmationRequest(approved=True, token=token))
    await service.shutdown()
