from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lobster_phone_agent.agent.executor import ExecutionHooks
from lobster_phone_agent.apps.registry import AppRecord, AppRegistry
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.appium_device import AppiumDevice
from lobster_phone_agent.errors import BridgeProtocolError, ExecutionError, PlanningError
from lobster_phone_agent.schemas import TaskRequest
from lobster_phone_agent.skill.atomic import AtomicDispatcher, OperationJournal
from lobster_phone_agent.skill.models import SkillRuntimeConfig
from lobster_phone_agent.stepwise.models import AtomicAction, AtomicRequest, AtomicResult, Decision
from lobster_phone_agent.stepwise.runtime import StepwiseController, observed_nodes
from .fakes import FakeDevice, screen
from .test_appium_adapter import FakeDriver, descriptor


def decision(observation, kind="tap", **values):
    return {"version": "2.0", "observation_id": observation, "status": "act",
            "action": AtomicAction.make(kind, **values).model_dump(),
            "checks": [{"kind": "screen_changed", "value": ""}], "summary": "执行本步"}


@pytest.mark.parametrize("bad", [
    {"steps": []}, {"thoughts": "long private reasoning"},
    {"action": []}, {"observation_id": "old"},
    {"status": "done"}, {"status": "handoff"}, {"checks": []},
])
def test_single_step_contract_forbids_future_plans_and_ambiguous_status(bad):
    with pytest.raises(ValidationError):
        Decision.model_validate({**decision("a" * 32, node_id=0), **bad})


@pytest.mark.parametrize("kind,values", [
    ("tap", {"node_id": True}), ("tap", {"node_id": "1"}),
    ("tap", {"node_id": -1}), ("tap", {"node_id": 0, "text": "extra"}),
    ("type", {"node_id": 0}), ("swipe", {"direction": "diagonal"}),
    ("launch_app", {"package": "com.test;reboot"}), ("wait", {"wait_ms": 2000}),
    ("shell", {"text": "id"}), ("home", {"package": "com.test"}),
])
def test_atomic_action_payload_is_closed(kind, values):
    with pytest.raises(ValidationError):
        AtomicAction.make(kind, **values)


async def noop(*args):
    pass


def hooks(confirm=noop, handoff=noop, trace=noop):
    return ExecutionHooks(emit=noop, confirm=confirm, handoff=handoff, trace=trace,
                          set_step_index=noop, is_cancelled=lambda: False)


def request(**kw):
    return TaskRequest(instruction="完成两个页面步骤", device={"id": "cloud", "bridge_id": "home"}, **kw)


class Model:
    def __init__(self, fn):
        self.fn, self.inputs = fn, []

    async def complete_json(self, **kwargs):
        payload = json.loads(kwargs["user"])
        self.inputs.append(payload)
        assert kwargs["schema_name"] == "phone_next_step"
        assert kwargs["max_output_tokens"] <= 800
        return self.fn(payload)


class Phone(FakeDevice):
    async def atomic_action(self, payload):
        req = AtomicRequest.model_validate(payload)
        assert req.fingerprint == (await self.snapshot()).fingerprint
        self.actions.append((req.action.kind, req.operation_id))
        self.advance()
        return {"state": "executed", "code": "ok"}


def controller(model, **options):
    return StepwiseController(Settings(llm_model="test", post_action_timeout_ms=20,
                                       condition_poll_ms=20, **options), AppRegistry(), model)


@pytest.mark.asyncio
async def test_model_sees_fresh_page_each_round_and_exactly_one_action():
    states = [screen("com.test", {"text": text, "class_name": "android.widget.Button"})
              for text in ["第一步", "第二步", "完成"]]
    phone = Phone(states=states)

    def choose(p):
        if p["screen"]["nodes"][0]["text"] == "完成":
            return {"version": "2.0", "observation_id": p["observation_id"], "status": "done",
                    "action": None, "checks": [{"kind": "text_present", "value": "完成"}], "summary": "已完成"}
        return decision(p["observation_id"], node_id=0)
    model = Model(choose)
    result = await controller(model).run(request(), phone, hooks(), task_id="a" * 32)
    assert [p["screen"]["nodes"][0]["text"] for p in model.inputs] == ["第一步", "第二步", "完成"]
    assert len({p["observation_id"] for p in model.inputs}) == 3
    assert [len(p["history"]) for p in model.inputs] == [0, 1, 2]
    assert len(phone.actions) == 2
    assert result["steps_executed"] == 2 and result["llm_calls"] == 3


@pytest.mark.asyncio
async def test_expired_observation_and_nonexistent_node_are_rejected():
    for fn in [lambda p: decision("b" * 32, node_id=0),
               lambda p: decision(p["observation_id"], node_id=2999)]:
        phone = Phone(states=[screen("com.test", {"text": "继续"})])
        with pytest.raises(PlanningError):
            await controller(Model(fn)).run(request(), phone, hooks(), task_id="a" * 32)
        assert not phone.actions


@pytest.mark.asyncio
async def test_done_is_not_accepted_without_current_evidence():
    model = Model(lambda p: {"version": "2.0", "observation_id": p["observation_id"],
        "status": "done", "action": None, "checks": [{"kind": "text_present", "value": "不存在的结果"}],
        "summary": "不能相信这个结论"})
    with pytest.raises(ExecutionError, match="completion evidence"):
        await controller(model).run(request(), Phone(states=[screen("com.test")]), hooks(), task_id="a" * 32)


@pytest.mark.asyncio
async def test_unknown_result_is_handed_off_and_never_automatically_retried():
    class UncertainPhone(Phone):
        async def atomic_action(self, payload):
            self.actions.append(("tap", None))
            raise TimeoutError("reply was lost after effect")
    handoffs = []
    async def handoff(code, reason):
        handoffs.append(code)
    phone = UncertainPhone(states=[screen("com.test", {"text": "继续"})])
    model = Model(lambda p: decision(p["observation_id"], node_id=0))
    with pytest.raises(ExecutionError, match="outcome_unknown"):
        await controller(model).run(request(), phone, hooks(handoff=handoff), task_id="a" * 32)
    assert len(phone.actions) == 1 and handoffs == ["outcome_unknown"]


@pytest.mark.asyncio
async def test_high_risk_still_requires_confirmation_when_caller_sets_none():
    phone = Phone(states=[screen("com.test", {"text": "立即叫车"}), screen("com.test", {"text": "正在呼叫"})])
    model = Model(lambda p: decision(p["observation_id"], node_id=0) if p["screen"]["nodes"][0]["text"] == "立即叫车" else
                  {"version": "2.0", "observation_id": p["observation_id"], "status": "done", "action": None,
                   "checks": [{"kind": "text_present", "value": "正在呼叫"}], "summary": "收到呼叫回执"})
    confirmations = []
    async def confirm(step, risk):
        confirmations.append(step)
    await controller(model).run(request(policy={"allow_irreversible": True, "confirmation_mode": "none"}),
                                phone, hooks(confirm=confirm), task_id="a" * 32)
    assert len(confirmations) == 1 and len(phone.actions) == 1


@pytest.mark.asyncio
async def test_confirmation_page_change_discards_old_action():
    phone = Phone(states=[screen("com.test", {"text": "立即叫车"}), screen("com.test", {"text": "用户已手工完成"})])
    model = Model(lambda p: decision(p["observation_id"], node_id=0) if not p["history"] else
                  {"version": "2.0", "observation_id": p["observation_id"], "status": "done", "action": None,
                   "checks": [{"kind": "text_present", "value": "用户已手工完成"}], "summary": "观察到手工完成"})
    async def confirm(step, risk):
        phone.advance()
    result = await controller(model).run(request(policy={"allow_irreversible": True}), phone,
                                        hooks(confirm=confirm), task_id="a" * 32)
    assert result["steps_executed"] == 0 and not phone.actions
    assert model.inputs[-1]["history"][0]["outcome"] == "approval_expired_page_changed"


@pytest.mark.asyncio
async def test_repeat_on_same_screen_stops_instead_of_clicking_twice():
    phone = Phone(states=[screen("com.test", {"text": "继续"})])
    model = Model(lambda p: decision(p["observation_id"], node_id=0))
    with pytest.raises(ExecutionError, match="repeated action"):
        await controller(model).run(request(), phone, hooks(), task_id="a" * 32)
    assert len(phone.actions) == 1


@pytest.mark.asyncio
async def test_simple_open_uses_local_decision_but_observes_completion():
    registry = AppRegistry([AppRecord(name="设置", package="com.android.settings")])
    c = StepwiseController(Settings(llm_model=None), registry, None)
    phone = Phone(states=[screen("com.launcher"), screen("com.android.settings")])
    r = TaskRequest(instruction="打开设置", device={"id": "cloud", "bridge_id": "home"})
    result = await c.run(r, phone, hooks(), task_id="a" * 32)
    assert result["llm_calls"] == 0 and result["final_package"] == "com.android.settings"


def test_observation_is_bounded_and_never_contains_password_text():
    s = screen("com.test", *[{"text": "Button" * 100} for _ in range(200)],
               {"text": "never-send-this", "password": True})
    nodes = observed_nodes(s)
    assert len(nodes) <= 80 and len(json.dumps(nodes, ensure_ascii=False).encode()) <= 15000
    assert "never-send-this" not in str(nodes)


class Pool:
    device_ids = ["cloud"]
    def __init__(self, device):
        self.device = device
    @asynccontextmanager
    async def lease(self, device_id):
        yield self.device


def atomic_request(s, operation="one", **kw):
    return AtomicRequest(operation_id=operation, fingerprint=s.fingerprint,
                         action=AtomicAction.make("tap", node_id=0, **kw))


@pytest.mark.asyncio
async def test_journal_replay_survives_dispatcher_restart_and_does_not_click_twice(tmp_path):
    s = screen("com.test", {"text": "继续"})
    phone = FakeDevice(states=[s])
    journal = OperationJournal(tmp_path / "ops.sqlite3")
    req = atomic_request(s)
    d = AtomicDispatcher(Pool(phone), journal)
    results = await asyncio.gather(*(d.dispatch("cloud", req.model_dump()) for _ in range(8)))
    assert len(phone.actions) == 1 and all(r["state"] == "executed" for r in results)
    d2 = AtomicDispatcher(Pool(phone), OperationJournal(journal.path))
    await d2.dispatch("cloud", req.model_dump())
    assert len(phone.actions) == 1
    changed = req.model_copy(update={"fingerprint": "changed"})
    with pytest.raises(BridgeProtocolError):
        await d2.dispatch("cloud", changed.model_dump())


@pytest.mark.asyncio
async def test_unresolved_operation_blocks_new_ids_until_local_operator_reconciles(tmp_path):
    s = screen("com.test", {"text": "继续"})
    phone = FakeDevice(states=[s])
    journal = OperationJournal(tmp_path / "ops.sqlite3")
    req = atomic_request(s)
    assert journal.reserve("cloud", req)
    d = AtomicDispatcher(Pool(phone), OperationJournal(journal.path))
    assert (await d.dispatch("cloud", req.model_dump()))["state"] == "unknown"
    assert (await d.dispatch("cloud", atomic_request(s, "two").model_dump()))["state"] == "unknown"
    assert not phone.actions
    journal.reconcile("cloud", "one", executed=False)
    assert (await d.dispatch("cloud", req.model_dump()))["state"] == "not_executed"
    assert (await d.dispatch("cloud", atomic_request(s, "two").model_dump()))["state"] == "executed"
    assert len(phone.actions) == 1


@pytest.mark.asyncio
async def test_private_guard_rechecks_fingerprint_and_protected_targets(tmp_path):
    s = screen("com.test", {"text": "继续"})
    phone = FakeDevice(states=[s])
    d = AtomicDispatcher(Pool(phone), OperationJournal(tmp_path / "ops.sqlite3"))
    req = atomic_request(s).model_copy(update={"fingerprint": "old"})
    assert (await d.dispatch("cloud", req.model_dump()))["code"] == "stale_screen"
    req = atomic_request(s).model_copy(update={"action": AtomicAction.make("tap", node_id=99)})
    assert (await d.dispatch("cloud", req.model_dump()))["code"] == "invalid_target"
    phone.states = [screen("com.test", {"password": True})]
    req = atomic_request(phone.states[0])
    assert (await d.dispatch("cloud", req.model_dump()))["code"] == "blocked_surface"
    assert not phone.actions


@pytest.mark.asyncio
async def test_command_timeout_quarantines_session_until_manual_recovery():
    driver = FakeDriver()
    device = AppiumDevice(descriptor(), driver, SkillRuntimeConfig())
    ended = threading.Event()
    def slow():
        time.sleep(0.06)
        ended.set()
    with pytest.raises(ExecutionError, match="quarantined"):
        await device._run(slow, timeout=0.005)
    with pytest.raises(ExecutionError, match="auto-recreated"):
        await device.is_alive()
    with pytest.raises(ExecutionError, match="quarantined"):
        await device.tap(1, 2)
    await asyncio.sleep(0.08)
    assert ended.is_set() and not driver.scripts


@pytest.mark.asyncio
async def test_private_network_client_rejects_legacy_mutation_rpc():
    from lobster_phone_agent.bridge.protocol import RpcRequestMessage
    from lobster_phone_agent.skill.client import SkillBridgeClient
    from .test_skill_gateway import config_payload
    from lobster_phone_agent.skill.models import SkillConfig
    class DenyDispatcher:
        async def dispatch(self, *args):
            raise AssertionError("legacy dispatch must not run")
    sent = []
    class Socket:
        async def send(self, data):
            sent.append(json.loads(data))
    client = SkillBridgeClient(SkillConfig.model_validate(config_payload()), DenyDispatcher())
    req = RpcRequestMessage(sequence=1, device_id="cloud", method="tap", params={"x": 1, "y": 2, "operation_id": "old"})
    await client._handle_rpc(Socket(), req)
    assert sent[0]["ok"] is False

@pytest.mark.asyncio
async def test_control_plane_client_cannot_mutate_another_bridge_task():
    import httpx
    from fastapi import HTTPException
    from lobster_phone_agent.schemas import TaskRecord
    from lobster_phone_agent.skill.gateway import ControlPlaneClient
    from lobster_phone_agent.skill.models import SkillConfig
    from .test_skill_gateway import config_payload
    public = ControlPlaneClient(SkillConfig.model_validate(config_payload()))
    await public._client.aclose()
    calls = []
    record = TaskRecord(request=TaskRequest(instruction="open", device={"id": "cloud-1", "bridge_id": "OTHER"}))
    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json=record.model_dump(mode="json"))
    public._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fixture/")
    with pytest.raises(HTTPException) as caught:
        await public.request("POST", "/v1/tasks/" + "a" * 32 + "/confirm", json={"approved": True})
    assert caught.value.status_code == 403 and calls == ["GET"]
    await public.close()


def test_ui_parser_preserves_case_and_rejects_entity_documents():
    from lobster_phone_agent.device.ui import parse_uiautomator_xml
    s = screen("com.test", {"text": "UserID AbC  123"})
    assert s.nodes[0].text == "UserID AbC  123"
    with pytest.raises(ValueError):
        parse_uiautomator_xml('<!DOCTYPE root><hierarchy/>')


def test_fingerprint_binds_node_order_and_all_nodes_not_only_first_300():
    a = {"text": "继续", "bounds": "[0,0][100,100]"}
    b = {"text": "删除", "bounds": "[200,0][300,100]"}
    assert screen("com.test", a, b).fingerprint != screen("com.test", b, a).fingerprint
    many = [{"text": f"item-{i}"} for i in range(500)]
    before = screen("com.test", *many)
    many[-1] = {"text": "different-sensitive-action"}
    assert before.fingerprint != screen("com.test", *many).fingerprint


def test_appium_input_with_duplicate_resource_ids_requires_exact_bounds():
    class Element:
        def __init__(self, x):
            self.rect = {"x": x, "y": 0, "width": 100, "height": 100}
    one, two = Element(0), Element(200)
    class Driver:
        def find_elements(self, by, value):
            return [one, two]
    phone = AppiumDevice(descriptor(), Driver(), SkillRuntimeConfig())
    assert phone._find_element_sync({"resource_id": "com.test:id/input"}) is None
    assert phone._find_element_sync({"resource_id": "com.test:id/input", "bounds": [200,0,300,100]}) is two


@pytest.mark.asyncio
async def test_reordered_nodes_are_rejected_by_private_guard(tmp_path):
    a, b = {"text": "继续", "bounds": "[0,0][100,100]"}, {"text": "删除", "bounds": "[200,0][300,100]"}
    original, reordered = screen("com.test", a, b), screen("com.test", b, a)
    phone = FakeDevice(states=[reordered])
    dispatcher = AtomicDispatcher(Pool(phone), OperationJournal(tmp_path / "ops.sqlite3"))
    result = await dispatcher.dispatch("cloud", atomic_request(original).model_dump())
    assert result["state"] == "not_executed" and not phone.actions


def test_atomic_nullable_fields_survive_all_rpc_boundaries():
    from lobster_phone_agent.bridge.protocol import RpcRequestMessage
    from lobster_phone_agent.bridge.contracts import validate_rpc_params
    action = AtomicAction.make("tap", node_id=0)
    payload = AtomicRequest(operation_id="task:0", fingerprint="abc", action=action).model_dump(mode="json")
    first = RpcRequestMessage(sequence=1, device_id="phone", method="atomic_action", params=payload)
    second = RpcRequestMessage.model_validate_json(first.model_dump_json())
    third = validate_rpc_params("atomic_action", second.params)
    assert AtomicRequest.model_validate(third).action == action
    assert "text" in third["action"] and third["action"]["text"] is None
