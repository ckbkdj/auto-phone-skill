from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lobster_phone_agent.agent.store import InMemoryTaskStore
from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.apps.registry import AppRegistry
from lobster_phone_agent.config import Settings
from lobster_phone_agent.errors import PlanningError
from lobster_phone_agent.llm.client import OpenAICompatibleClient
from lobster_phone_agent.llm.planner import HybridPlanner
from lobster_phone_agent.schemas import DeviceDescriptor, TaskRecord, TaskRequest

from .fakes import screen


@pytest.mark.asyncio
async def test_unknown_app_is_rejected_before_appium_activation() -> None:
    settings = Settings(config_dir=Path("config"), llm_model=None)
    llm = OpenAICompatibleClient(settings)
    planner = HybridPlanner(
        settings=settings,
        recipes=RecipePlanner.from_directory(Path("config/recipes")),
        registry=AppRegistry.from_yaml(Path("config/apps.yaml")),
        llm=llm,
    )
    request = TaskRequest(
        instruction="打开绝不存在的测试应用",
        device=DeviceDescriptor(id="cloud-1", bridge_id="lobster-a"),
    )
    with pytest.raises(PlanningError, match="cannot resolve app"):
        await planner.plan(request, snapshot=screen("com.android.launcher"), installed_apps=[])
    await llm.close()


@pytest.mark.asyncio
async def test_plan_cache_is_partitioned_by_device_catalog() -> None:
    snapshot = screen("com.android.launcher")
    first = TaskRequest(
        instruction="打开测试应用",
        device=DeviceDescriptor(id="cloud-1", bridge_id="lobster-a"),
    )
    second = TaskRequest(
        instruction="打开测试应用",
        device=DeviceDescriptor(id="cloud-2", bridge_id="lobster-a"),
    )
    key_one = HybridPlanner._cache_key(first, snapshot, [{"package": "com.one"}])
    key_two = HybridPlanner._cache_key(second, snapshot, [{"package": "com.two"}])
    assert key_one != key_two


@pytest.mark.asyncio
async def test_concurrent_idempotent_creates_return_one_record() -> None:
    store = InMemoryTaskStore()
    request = TaskRequest(
        instruction="打开美团",
        device=DeviceDescriptor(id="cloud-1", bridge_id="lobster-a"),
        idempotency_key="same-task",
    )
    results = await asyncio.gather(*(store.create(TaskRecord(request=request)) for _ in range(20)))
    assert len({record.id for record, _created in results}) == 1
    assert sum(created for _record, created in results) == 1
