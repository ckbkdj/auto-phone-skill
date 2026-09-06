from __future__ import annotations

from pathlib import Path

import pytest

from lobster_phone_agent.agent.conditions import ConditionEvaluator
from lobster_phone_agent.agent.dialogs import CommonDialogHandler
from lobster_phone_agent.agent.executor import ActionExecutor, ExecutionHooks
from lobster_phone_agent.agent.risk import RiskEngine
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.schemas import (
    ActionPlan,
    ActionStep,
    ActionType,
    DeviceDescriptor,
    ScreenCondition,
    SemanticTarget,
    TaskRequest,
)

from .fakes import FakeDevice, screen


async def noop(*_args, **_kwargs) -> None:
    return None


def hooks() -> ExecutionHooks:
    return ExecutionHooks(
        emit=noop,
        confirm=noop,
        handoff=noop,
        trace=noop,
        set_step_index=noop,
        is_cancelled=lambda: False,
    )


def settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "config_dir": Path("config"),
        "artifact_dir": tmp_path,
        "action_settle_ms": 0,
        "snapshot_stable_interval_ms": 1,
        "snapshot_stable_timeout_ms": 3,
        "max_scroll_searches": 0,
        "max_safe_dialog_dismissals": 0,
    }
    values.update(overrides)
    return Settings(**values)


def executor(config: Settings, planner=None) -> ActionExecutor:
    matcher = SemanticMatcher()
    return ActionExecutor(
        settings=config,
        matcher=matcher,
        conditions=ConditionEvaluator(matcher, poll_ms=1),
        dialogs=CommonDialogHandler(matcher),
        risk=RiskEngine(),
        planner=planner,
    )


@pytest.mark.asyncio
async def test_mutation_without_explicit_expect_returns_fresh_snapshot(tmp_path: Path) -> None:
    before = screen("com.example", {"text": "打开", "class_name": "android.widget.Button"})
    after = screen("com.example", {"text": "已打开", "class_name": "android.widget.TextView"})
    device = FakeDevice(states=[before, after])
    step = ActionStep(
        id="tap-open",
        action=ActionType.TAP,
        target=SemanticTarget(text="打开", role="button"),
        retry={"max_attempts": 1, "allow_repair": False},
    )
    observed, _match, _dispatch_ms, _observation_ms = await executor(settings(tmp_path))._execute_once(
        step=step,
        device=device,
        previous_snapshot=None,
        hooks=hooks(),
        operation_id="task:tap-open",
        attempt=1,
    )
    assert observed is not None
    assert observed.fingerprint == after.fingerprint
    assert observed.fingerprint != before.fingerprint


class RepairPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def repair(self, **_kwargs) -> ActionPlan:
        self.calls += 1
        return ActionPlan(
            goal="恢复搜索入口",
            planner="repair",
            steps=[ActionStep(id="repair-back", action=ActionType.BACK)],
        )


@pytest.mark.asyncio
async def test_repair_restores_context_then_retries_original_failed_action(tmp_path: Path) -> None:
    blocked = screen("com.example", {"text": "错误页面"})
    ready = screen("com.example", {"text": "继续", "class_name": "android.widget.Button"})
    done = screen("com.example", {"text": "完成"})
    device = FakeDevice(states=[blocked, ready, done])
    planner = RepairPlanner()
    config = settings(tmp_path, llm_model="test-model", max_repairs=1)
    plan = ActionPlan(
        goal="继续",
        steps=[
            ActionStep(
                id="continue",
                action=ActionType.TAP,
                target=SemanticTarget(text="继续", role="button"),
                expect=[ScreenCondition(kind="text_present", value="完成")],
                retry={"max_attempts": 1, "allow_repair": True},
            )
        ],
    )
    request = TaskRequest(
        instruction="继续",
        device=DeviceDescriptor(id="cloud-1", bridge_id="lobster-a"),
    )
    await executor(config, planner).execute(
        request=request,
        plan=plan,
        device=device,
        hooks=hooks(),
    )
    assert planner.calls == 1
    assert [name for name, _ in device.actions] == ["back", "tap"]
    assert device.state_index == 2


@pytest.mark.asyncio
async def test_offscreen_semantic_target_is_found_by_bounded_scroll_search(tmp_path: Path) -> None:
    before = screen("com.example", {"text": "列表顶部"})
    target = screen("com.example", {"text": "继续", "class_name": "android.widget.Button"})
    done = screen("com.example", {"text": "完成"})
    device = FakeDevice(states=[before, target, done])
    config = settings(tmp_path, max_scroll_searches=2)
    step = ActionStep(
        id="continue",
        action=ActionType.TAP,
        target=SemanticTarget(text="继续", role="button"),
        retry={"max_attempts": 1, "allow_repair": False},
    )
    observed = await executor(config)._run_step_with_retries(
        step=step,
        device=device,
        previous_snapshot=None,
        hooks=hooks(),
        operation_id="task:continue",
    )
    assert [name for name, _ in device.actions] == ["swipe", "tap"]
    assert observed is not None
    assert observed.contains_text("完成")


class IdRecordingDevice(FakeDevice):
    operation_ids: list[str]

    def __post_init__(self) -> None:
        self.operation_ids = []

    async def tap(self, x: int, y: int, *, operation_id: str | None = None) -> None:
        if not hasattr(self, "operation_ids"):
            self.operation_ids = []
        self.operation_ids.append(str(operation_id))
        self.actions.append(("tap", (x, y)))
        if len(self.operation_ids) == 1:
            raise RuntimeError("temporary transport failure")
        self.advance()


@pytest.mark.asyncio
async def test_mutating_step_retries_reuse_stable_operation_id(tmp_path: Path) -> None:
    before = screen("com.example", {"text": "继续", "class_name": "android.widget.Button"})
    done = screen("com.example", {"text": "完成"})
    device = IdRecordingDevice(states=[before, done])
    step = ActionStep(
        id="continue",
        action=ActionType.TAP,
        target=SemanticTarget(text="继续", role="button"),
        retry={"max_attempts": 2, "delay_ms": 0, "allow_repair": False},
    )
    await executor(settings(tmp_path))._run_step_with_retries(
        step=step,
        device=device,
        previous_snapshot=None,
        hooks=hooks(),
        operation_id="task:continue",
    )
    assert device.operation_ids == ["task:continue:tap", "task:continue:tap"]
