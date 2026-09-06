from __future__ import annotations

import copy
import json

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from lobster_phone_agent.agent.store import InMemoryTaskStore
from lobster_phone_agent.api.contracts import ErrorResponse, install_contract_handlers
from lobster_phone_agent.bridge.contracts import validate_rpc_params, validate_rpc_result
from lobster_phone_agent.bridge.protocol import RpcResultMessage
from lobster_phone_agent.config import Settings
from lobster_phone_agent.contracts import MAX_REQUEST_BYTES, strict_json_object
from lobster_phone_agent.errors import BridgeProtocolError, InvalidTaskState, PlanningError
from lobster_phone_agent.llm.client import OpenAICompatibleClient
from lobster_phone_agent.schemas import (
    ActionPlan, ActionStep, LocalTaskRequest, TaskRecord, TaskRequest,
)


def request_body():
    return {"instruction": "打开系统设置", "device_id": "cloud-1"}


@pytest.mark.parametrize("patch", [
    {"unknown": "value"}, {"instruction": 123}, {"instruction": "   "},
    {"instruction": "x" * 4001}, {"device_id": "../other"},
    {"device": {"id": "other", "bridge_id": "other"}},
    {"policy": {"allow_payments": "false"}}, {"policy": {"max_steps": True}},
    {"policy": {"max_steps": 201}}, {"policy": {"shell": "id"}},
    {"locale": "invalid_locale"}, {"timezone": "Not/AZone"},
    {"location": {"latitude": 20.0}},
    {"location": {"latitude": 99.0, "longitude": 10.0}},
    {"location": {"latitude": "20", "longitude": 10.0}},
    {"location": {"latitude": float("nan"), "longitude": 10.0}},
    {"app_package": "com.example; echo secret"},
    {"app_catalog": [{"name": "App", "package": "com.test", "extra": 1}]},
    {"app_catalog": [{"name": "App", "package": "com.test", "aliases": [123]}]},
    {"context": {"nested": {"appium_url": "http://private:4723"}}},
    {"context": {"bridge-token": "secret"}},
    {"context": {"huge": "x" * 17000}},
])
def test_local_request_rejects_invalid_fields(patch):
    with pytest.raises(ValidationError):
        LocalTaskRequest.model_validate({**request_body(), **patch})


@pytest.mark.parametrize("raw", [
    '{"instruction":"a","instruction":"b"}',
    '{"nested":{"x":1,"x":2}}', '{"x":NaN}', '{"x":Infinity}',
    '[]', 'null', '```json\n{}\n```', 'Here is your result: {}',
    '{} {}', '{"bad":"\\ud800"}', '{"x":' + '[' * 30 + '0' + ']' * 30 + '}',
    b'\xff',
])
def test_strict_json_rejects_ambiguous_or_invalid_input(raw):
    with pytest.raises((ValueError, UnicodeError)):
        strict_json_object(raw)


def test_json_limit_and_valid_utf8():
    assert strict_json_object('{"instruction":"打开美团"}') == {"instruction": "打开美团"}
    with pytest.raises(ValueError):
        strict_json_object('{"x":"' + 'x' * MAX_REQUEST_BYTES + '"}', max_bytes=MAX_REQUEST_BYTES)


@pytest.mark.parametrize("payload", [
    {"action": "wait", "value": -1}, {"action": "wait", "value": "100"},
    {"action": "wait", "value": True}, {"action": "wait", "value": 60001},
    {"action": "type", "value": "text"},
    {"action": "tap", "target": {}},
    {"action": "tap", "target": {"text": "OK", "xpath": "//*"}},
    {"action": "tap", "target": {"coordinates": [1, 2]}},
    {"action": "tap", "target": {"coordinates": [True, 2]},
     "metadata": {"screen_fingerprint": "abc"}},
    {"action": "tap", "target": {"text": "OK"}, "metadata": {"retry_safe": True}},
    {"action": "tap", "target": {"text": "OK"}, "metadata": {"clear_first": "true"}},
    {"action": "back", "value": "extra"},
    {"action": "swipe", "direction": "diagonal"},
    {"action": "shell", "value": "id"},
    {"action": "assert"},
    {"action": "assert", "expect": [{"kind": "text_present", "value": ""}]},
    {"action": "assert", "expect": [{"kind": "all", "children": []}]},
    {"action": "home", "retries": "2"},
])
def test_action_contract_rejects_invalid_shapes(payload):
    with pytest.raises(ValidationError):
        ActionStep.model_validate({"id": "step-1", **payload})


def test_plan_ids_must_be_unique():
    step = {"id": "same", "action": "home"}
    with pytest.raises(ValidationError):
        ActionPlan.model_validate({"goal": "home", "steps": [step, step]})


@pytest.mark.parametrize("method,params", [
    ("shell", {}), ("snapshot", {"device_id": "different"}),
    ("tap", {"operation_id": "op-1", "x": "1", "y": 2}),
    ("tap", {"operation_id": "op-1", "x": True, "y": 2}),
    ("tap", {"operation_id": "op-1", "x": -1, "y": 2}),
    ("tap", {"operation_id": "op-1", "x": 1, "y": 2, "shell": "id"}),
    ("tap", {"x": 1, "y": 2}),
    ("type_text", {"operation_id": "op-1", "text": "你好", "clear": "false"}),
    ("type_text", {"operation_id": "op-1", "text": "hi", "element": {"index": 1, "xpath": "//*"}}),
    ("launch_app", {"operation_id": "op-1", "package": "com.test;reboot"}),
    ("swipe", {"operation_id": "op-1", "direction": "up", "percent": float("inf")}),
])
def test_rpc_params_fail_closed(method, params):
    with pytest.raises(BridgeProtocolError):
        validate_rpc_params(method, params)


@pytest.mark.parametrize("method,result", [
    ("tap", {"performed": "false"}), ("tap", {"performed": 1}),
    ("tap", {"performed": True, "secret": "bad"}), ("tap", {}),
    ("is_alive", "true"), ("snapshot", {"nodes": []}),
    ("screenshot_png", "not-base64"), ("screenshot_png", "aGVsbG8="),
    ("list_apps", [{"package": "com.test", "secret": "bad"}]),
])
def test_rpc_results_are_not_trusted(method, result):
    with pytest.raises(BridgeProtocolError):
        validate_rpc_result(method, result)


def test_rpc_false_is_a_valid_negative_not_a_true_result():
    assert validate_rpc_result("tap", {"performed": False}) == {"performed": False}


@pytest.mark.parametrize("payload", [
    {"ok": True, "error": "bad"},
    {"ok": False, "result": {"performed": True}, "error": "bad"},
    {"ok": False}, {"ok": "true", "result": {}},
])
def test_rpc_response_envelope_consistency(payload):
    with pytest.raises(ValidationError):
        RpcResultMessage.model_validate({"type": "rpc_result", "id": "test", "sequence": 1, **payload})


def contract_app():
    app = FastAPI()
    install_contract_handlers(app)

    @app.post("/tasks", response_model=LocalTaskRequest)
    def create(request: LocalTaskRequest):
        return request

    @app.get("/broken", response_model=LocalTaskRequest)
    def broken():
        return {"instruction": "server-secret"}
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("body,headers,status,code", [
    ('{"instruction":"a","instruction":"b"}', {"Content-Type": "application/json"}, 400, "INVALID_JSON"),
    ('[]', {"Content-Type": "application/json"}, 400, "INVALID_JSON"),
    ('{}', {"Content-Type": "text/plain"}, 415, "UNSUPPORTED_MEDIA_TYPE"),
    ('{}', {"Content-Type": "application/json", "Content-Encoding": "gzip"}, 415, "UNSUPPORTED_MEDIA_TYPE"),
    ('{"secret-to-not-echo": "password-to-not-echo"}', {"Content-Type": "application/json"}, 422, "INVALID_REQUEST"),
    ('x' * (MAX_REQUEST_BYTES + 1), {"Content-Type": "application/json"}, 413, "REQUEST_TOO_LARGE"),
])
async def test_http_uniform_error_contract(body, headers, status, code):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=contract_app()), base_url="http://test") as client:
        response = await client.post("/tasks", content=body, headers=headers)
    assert response.status_code == status
    model = ErrorResponse.model_validate(response.json())
    assert model.error.code == code
    assert "secret-to-not-echo" not in response.text
    assert "password-to-not-echo" not in response.text
    assert response.headers["x-request-id"] == model.error.request_id


@pytest.mark.asyncio
async def test_output_validation_error_does_not_echo_output():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=contract_app()), base_url="http://test") as client:
        response = await client.get("/broken")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "INVALID_OUTPUT"
    assert "server-secret" not in response.text


@pytest.mark.asyncio
async def test_idempotency_conflict_and_exact_replay():
    store = InMemoryTaskStore()
    request = TaskRequest(instruction="打开设置", device={"id": "cloud-1", "bridge_id": "home"}, idempotency_key="k1")
    one, created = await store.create(TaskRecord(request=request))
    two, replay_created = await store.create(TaskRecord(request=request))
    assert created and not replay_created and one.id == two.id
    for changed in ({"instruction": "打开美团"}, {"device": {"id": "cloud-2", "bridge_id": "home"}}):
        body = {**request.model_dump(mode="json"), **changed}
        with pytest.raises(InvalidTaskState):
            await store.create(TaskRecord(request=TaskRequest.model_validate(body)))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["extra", "nested_extra", "prose", "truncated", "refusal", "no_finish", "duplicates", "many_choices"])
async def test_llm_output_local_validation_is_mandatory(change):
    content = {"count": 1, "nested": {"ok": True}}
    choice = {"finish_reason": "stop", "message": {"content": json.dumps(content)}}
    if change == "extra":
        content["extra"] = "no"
    elif change == "nested_extra":
        content["nested"]["extra"] = "no"
    choice["message"]["content"] = json.dumps(content)
    if change == "prose":
        choice["message"]["content"] = 'Here is your JSON: ' + choice["message"]["content"]
    elif change == "truncated":
        choice["finish_reason"] = "length"
    elif change == "refusal":
        choice["message"]["refusal"] = "refused"
    elif change == "no_finish":
        del choice["finish_reason"]
    elif change == "duplicates":
        choice["message"]["content"] = '{"count":1,"count":2,"nested":{"ok":true}}'
    data = {"choices": [choice, copy.deepcopy(choice)] if change == "many_choices" else [choice]}
    schema = {"type": "object", "additionalProperties": False, "required": ["count", "nested"],
              "properties": {"count": {"type": "integer"}, "nested": {"type": "object", "additionalProperties": False,
                            "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}}}
    llm = OpenAICompatibleClient(Settings(llm_model="test"))
    await llm._client.aclose()
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)), base_url="http://llm.local/")
    try:
        with pytest.raises(PlanningError):
            await llm.complete_json(system="s", user="u", schema_name="test", schema=schema)
    finally:
        await llm.close()


def test_action_json_schema_checks_payload_rules():
    validator = Draft202012Validator(ActionPlan.model_json_schema())
    assert list(validator.iter_errors({"goal": "wait", "steps": [{"id": "a", "action": "wait", "value": "bad"}]}))
    assert not list(validator.iter_errors({"goal": "home", "steps": [{"id": "a", "action": "home"}]}))

@pytest.mark.parametrize("url", [
    "http://public.example.com:8790", "https://127.0.0.1:8790", "http://127.0.0.1:8790/?key=secret",
    "http://127.0.0.1:8790/#fragment", "http://user:pass@127.0.0.1:8790", "http://localhost:8790",
    "http://127.0.0.1:8790/public", "http://0.0.0.0:8790",
])
def test_skill_caller_cannot_redirect_token_to_public_origin(url):
    from lobster_phone_agent.skill.caller import loopback_base_url
    with pytest.raises(ValueError):
        loopback_base_url(url)


def test_skill_caller_loopback_and_task_paths():
    from lobster_phone_agent.skill.caller import loopback_base_url, task_path
    assert loopback_base_url("http://127.0.0.1:8790/") == "http://127.0.0.1:8790"
    assert task_path("a" * 32) == "/v1/tasks/" + "a" * 32
    with pytest.raises(ValueError):
        task_path("../../secrets")


def test_installable_skill_name_matches_folder():
    from pathlib import Path
    import re
    import yaml
    path = Path("skills/auto-phone-skill/SKILL.md")
    metadata = yaml.safe_load(path.read_text().split("---", 2)[1])
    assert metadata["name"] == path.parent.name
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", metadata["name"])
    assert len(metadata["name"]) <= 64


def test_valid_rpc_envelope_and_password_snapshot_redaction():
    from dataclasses import asdict
    from datetime import UTC, datetime
    from lobster_phone_agent.device.ui import UiNode
    assert RpcResultMessage(id="ok", sequence=1, ok=True, result={"performed": False}).ok is True
    node = UiNode(index=0, class_name="EditText", package="com.test", text="do-not-send",
                  content_desc="do-not-send", resource_id="input", bounds=None,
                  clickable=True, enabled=True, focusable=True, focused=True, scrollable=False,
                  selected=False, checked=False, password=True, displayed=True, depth=0, path="0")
    result = validate_rpc_result("snapshot", {
        "package": "com.test", "activity": "Main", "xml_hash": "abc", "fingerprint": "def",
        "width": 1080, "height": 1920, "captured_at": datetime.now(UTC).isoformat(), "nodes": [asdict(node)],
    })
    assert "do-not-send" not in str(result)
    assert result["nodes"][0]["password"] is True
