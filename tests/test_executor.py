from pathlib import Path

import pytest

from lobster_phone_agent.agent.conditions import ConditionEvaluator
from lobster_phone_agent.agent.dialogs import CommonDialogHandler
from lobster_phone_agent.agent.executor import ActionExecutor, ExecutionHooks
from lobster_phone_agent.agent.risk import RiskEngine
from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.apps.registry import AppRegistry
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.llm.client import OpenAICompatibleClient
from lobster_phone_agent.llm.planner import HybridPlanner
from lobster_phone_agent.schemas import DeviceDescriptor, EventType, TaskRequest

from .fakes import FakeDevice, screen


@pytest.mark.asyncio
async def test_meituan_flow_executes_locally_and_confirms_final_call(tmp_path: Path) -> None:
    settings = Settings(
        config_dir=Path("config"),
        artifact_dir=tmp_path,
        llm_model=None,
        action_settle_ms=0,
        post_action_timeout_ms=100,
        enable_a2a=False,
    )
    matcher = SemanticMatcher()
    planner = HybridPlanner(
        settings=settings,
        recipes=RecipePlanner.from_directory(Path("config/recipes")),
        registry=AppRegistry.from_yaml(Path("config/apps.yaml")),
        llm=OpenAICompatibleClient(settings),
    )
    executor = ActionExecutor(
        settings=settings,
        matcher=matcher,
        conditions=ConditionEvaluator(matcher),
        dialogs=CommonDialogHandler(matcher),
        risk=RiskEngine(),
        planner=planner,
    )
    states = [
        screen("com.android.launcher", {"text": "Home"}),
        screen(
            "com.sankuai.meituan",
            {"text": "打车", "class_name": "android.widget.Button"},
        ),
        screen(
            "com.sankuai.meituan",
            {
                "text": "你要去哪儿",
                "class_name": "android.widget.EditText",
                "focusable": True,
            },
        ),
        screen(
            "com.sankuai.meituan",
            {
                "text": "你要去哪儿",
                "class_name": "android.widget.EditText",
                "focusable": True,
            },
        ),
        screen(
            "com.sankuai.meituan",
            {"text": "北京南站", "class_name": "android.widget.TextView"},
        ),
        screen(
            "com.sankuai.meituan",
            {"text": "北京南站", "class_name": "android.widget.TextView"},
        ),
        screen(
            "com.sankuai.meituan",
            {"text": "立即叫车", "class_name": "android.widget.Button"},
        ),
        screen(
            "com.sankuai.meituan",
            {"text": "正在呼叫", "class_name": "android.widget.TextView"},
        ),
    ]
    device = FakeDevice(
        states=states,
        installed_apps=[{"package": "com.sankuai.meituan", "name": "美团"}],
    )
    request = TaskRequest(
        instruction="用美团打车去北京南站",
        device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
        policy={"allow_irreversible": True},
    )
    plan = await planner.plan(
        request,
        snapshot=states[0],
        installed_apps=device.installed_apps,
    )
    confirmations: list[str] = []
    events: list[EventType] = []
    traces = []
    hooks = ExecutionHooks(
        emit=lambda event_type, _message, _data: _append(events, event_type),
        confirm=lambda step, _decision: _append(confirmations, step.id),
        handoff=lambda _code, reason: pytest.fail(f"unexpected handoff: {reason}"),
        trace=lambda trace: _append(traces, trace),
        set_step_index=lambda _index: _noop(),
        is_cancelled=lambda: False,
    )
    result = await executor.execute(
        request=request,
        plan=plan,
        device=device,
        hooks=hooks,
    )
    await planner.llm.close()

    assert confirmations == ["submit_ride"]
    assert result["destination"] == "北京南站"
    assert ("launch", "com.sankuai.meituan") in device.actions
    assert any(action == "type" for action, _payload in device.actions)
    assert all(trace.dispatch_ms is not None for trace in traces)


async def _append(items, value):
    items.append(value)


async def _noop():
    return None

@pytest.mark.asyncio
async def test_login_surface_handoff_then_retries_original_action(tmp_path: Path) -> None:
    settings = Settings(
        config_dir=Path("config"),
        artifact_dir=tmp_path,
        llm_model=None,
        action_settle_ms=0,
        enable_a2a=False,
    )
    matcher = SemanticMatcher()
    planner = HybridPlanner(
        settings=settings,
        recipes=RecipePlanner.from_directory(Path("config/recipes")),
        registry=AppRegistry.from_yaml(Path("config/apps.yaml")),
        llm=OpenAICompatibleClient(settings),
    )
    executor = ActionExecutor(
        settings=settings,
        matcher=matcher,
        conditions=ConditionEvaluator(matcher),
        dialogs=CommonDialogHandler(matcher),
        risk=RiskEngine(),
        planner=planner,
    )
    from lobster_phone_agent.schemas import ActionPlan, ActionStep, ActionType, SemanticTarget

    login = screen(
        "com.example",
        {"text": "手机号", "class_name": "android.widget.EditText"},
        {"text": "请输入密码", "class_name": "android.widget.EditText", "password": True},
        {"text": "登录", "class_name": "android.widget.Button"},
    )
    ready = screen(
        "com.example",
        {"text": "搜索", "class_name": "android.widget.Button"},
    )
    done = screen("com.example", {"text": "搜索结果"})
    device = FakeDevice(states=[login, ready, done])
    plan = ActionPlan(
        goal="打开搜索",
        steps=[
            ActionStep(
                id="tap_search",
                action=ActionType.TAP,
                target=SemanticTarget(text="搜索", role="button"),
                retry={"max_attempts": 2, "allow_repair": False},
            )
        ],
    )
    handoffs: list[str] = []

    async def handoff(_code: str, reason: str) -> None:
        handoffs.append(reason)
        device.advance()  # Simulate the user completing login in the cloud-phone UI.

    hooks = ExecutionHooks(
        emit=lambda _type, _message, _data: _noop(),
        confirm=lambda _step, _decision: _noop(),
        handoff=handoff,
        trace=lambda _trace: _noop(),
        set_step_index=lambda _index: _noop(),
        is_cancelled=lambda: False,
    )
    request = TaskRequest(
        instruction="打开搜索",
        device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
    )
    await executor.execute(request=request, plan=plan, device=device, hooks=hooks)
    await planner.llm.close()
    assert len(handoffs) == 1
    assert any(action == "tap" for action, _ in device.actions)


@pytest.mark.asyncio
async def test_sensitive_type_is_never_automated_after_handoff(tmp_path: Path) -> None:
    settings = Settings(
        config_dir=Path("config"),
        artifact_dir=tmp_path,
        llm_model=None,
        action_settle_ms=0,
        enable_a2a=False,
    )
    matcher = SemanticMatcher()
    planner = HybridPlanner(
        settings=settings,
        recipes=RecipePlanner.from_directory(Path("config/recipes")),
        registry=AppRegistry.from_yaml(Path("config/apps.yaml")),
        llm=OpenAICompatibleClient(settings),
    )
    executor = ActionExecutor(
        settings=settings,
        matcher=matcher,
        conditions=ConditionEvaluator(matcher),
        dialogs=CommonDialogHandler(matcher),
        risk=RiskEngine(),
        planner=planner,
    )
    from lobster_phone_agent.schemas import ActionPlan, ActionStep, ActionType, SemanticTarget

    device = FakeDevice(
        states=[
            screen(
                "com.example",
                {
                    "text": "支付密码",
                    "class_name": "android.widget.EditText",
                    "password": True,
                },
            )
        ]
    )
    plan = ActionPlan(
        goal="付款",
        steps=[
            ActionStep(
                id="password",
                action=ActionType.TYPE,
                description="输入支付密码",
                target=SemanticTarget(text="支付密码", role="input"),
                value="must-not-be-used",
            )
        ],
    )
    handoffs: list[str] = []
    hooks = ExecutionHooks(
        emit=lambda _type, _message, _data: _noop(),
        confirm=lambda _step, _decision: _noop(),
        handoff=lambda _code, reason: _append(handoffs, reason),
        trace=lambda _trace: _noop(),
        set_step_index=lambda _index: _noop(),
        is_cancelled=lambda: False,
    )
    request = TaskRequest(
        instruction="付款",
        device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
    )
    await executor.execute(request=request, plan=plan, device=device, hooks=hooks)
    await planner.llm.close()
    assert handoffs
    assert not any(action == "type" for action, _ in device.actions)


@pytest.mark.asyncio
async def test_password_node_forces_handoff_even_without_sensitive_step_text(tmp_path: Path) -> None:
    """A password flag in the live hierarchy is a runtime security boundary."""
    from lobster_phone_agent.schemas import ActionPlan, ActionStep, ActionType, SemanticTarget

    settings = Settings(
        config_dir=Path("config"),
        artifact_dir=tmp_path,
        llm_model=None,
        action_settle_ms=0,
        enable_a2a=False,
    )
    matcher = SemanticMatcher()
    planner = HybridPlanner(
        settings=settings,
        recipes=RecipePlanner.from_directory(Path("config/recipes")),
        registry=AppRegistry.from_yaml(Path("config/apps.yaml")),
        llm=OpenAICompatibleClient(settings),
    )
    executor = ActionExecutor(
        settings=settings,
        matcher=matcher,
        conditions=ConditionEvaluator(matcher),
        dialogs=CommonDialogHandler(matcher),
        risk=RiskEngine(),
        planner=planner,
    )
    device = FakeDevice(
        states=[
            screen(
                "com.example",
                {
                    "resource_id": "com.example:id/secret",
                    "class_name": "android.widget.EditText",
                    "focusable": True,
                    "password": True,
                },
            ),
            screen("com.example", {"text": "已完成"}),
        ]
    )
    plan = ActionPlan(
        goal="填写字段",
        steps=[
            ActionStep(
                id="neutral_type",
                action=ActionType.TYPE,
                description="填写内容",
                target=SemanticTarget(
                    resource_id="com.example:id/secret",
                    role="input",
                ),
                value="must-not-be-used",
            )
        ],
    )
    handoffs: list[str] = []

    async def handoff(_code: str, reason: str) -> None:
        handoffs.append(reason)
        device.advance()

    hooks = ExecutionHooks(
        emit=lambda _type, _message, _data: _noop(),
        confirm=lambda _step, _decision: _noop(),
        handoff=handoff,
        trace=lambda _trace: _noop(),
        set_step_index=lambda _index: _noop(),
        is_cancelled=lambda: False,
    )
    request = TaskRequest(
        instruction="填写字段",
        device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
    )
    await executor.execute(request=request, plan=plan, device=device, hooks=hooks)
    await planner.llm.close()

    assert handoffs == ["密码输入框必须由用户接管完成"]
    assert not any(action == "type" for action, _ in device.actions)


@pytest.mark.asyncio
async def test_repair_plan_cannot_turn_policy_block_into_human_bypass(tmp_path: Path) -> None:
    from lobster_phone_agent.errors import ExecutionError
    from lobster_phone_agent.schemas import ActionPlan, ActionStep, ActionType, SemanticTarget

    settings = Settings(
        config_dir=Path("config"),
        artifact_dir=tmp_path,
        llm_model=None,
        action_settle_ms=0,
        enable_a2a=False,
    )
    matcher = SemanticMatcher()
    planner = HybridPlanner(
        settings=settings,
        recipes=RecipePlanner.from_directory(Path("config/recipes")),
        registry=AppRegistry.from_yaml(Path("config/apps.yaml")),
        llm=OpenAICompatibleClient(settings),
    )
    executor = ActionExecutor(
        settings=settings,
        matcher=matcher,
        conditions=ConditionEvaluator(matcher),
        dialogs=CommonDialogHandler(matcher),
        risk=RiskEngine(),
        planner=planner,
    )
    repair = ActionPlan(
        goal="叫车",
        planner="repair",
        steps=[
            ActionStep(
                action=ActionType.TAP,
                description="确认呼叫车辆",
                target=SemanticTarget(text="确认呼叫", role="button"),
                risk="high",
            )
        ],
    )
    device = FakeDevice(
        states=[
            screen(
                "com.example",
                {"text": "确认呼叫", "class_name": "android.widget.Button"},
            )
        ]
    )
    handoffs: list[str] = []
    hooks = ExecutionHooks(
        emit=lambda _type, _message, _data: _noop(),
        confirm=lambda _step, _decision: _noop(),
        handoff=lambda _code, reason: _append(handoffs, reason),
        trace=lambda _trace: _noop(),
        set_step_index=lambda _index: _noop(),
        is_cancelled=lambda: False,
    )
    request = TaskRequest(
        instruction="叫车",
        device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
    )

    with pytest.raises(ExecutionError, match="策略未授权"):
        await executor._run_repair_plan(
            repair_plan=repair,
            request=request,
            device=device,
            previous_snapshot=None,
            hooks=hooks,
            operation_namespace="repair-test",
        )
    await planner.llm.close()

    assert handoffs == []
    assert device.actions == []
