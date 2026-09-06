#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from lobster_phone_agent.app import create_app
from lobster_phone_agent.llm.next_action import NextAction
from lobster_phone_agent.config import Settings
from lobster_phone_agent.schemas import (
    ActionPlan,
    LocalTaskRequest,
    DeviceDescriptor,
    TaskRecord,
    ConfirmationRequest,
    HandoffResumeRequest,
    TaskRequest,
)
from lobster_phone_agent.bridge.protocol import HelloMessage, RpcRequestMessage, RpcResultMessage
from lobster_phone_agent.bridge.contracts import rpc_contracts
from lobster_phone_agent.api.contracts import ErrorResponse
from lobster_phone_agent.skill.gateway import create_skill_app
from lobster_phone_agent.skill.models import SkillConfig


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    output = Path("contracts")
    output.mkdir(exist_ok=True)
    write_json(output / "next-action.schema.json", NextAction.model_json_schema())
    write_json(output / "action-plan.schema.json", ActionPlan.model_json_schema())
    write_json(output / "task-request.schema.json", TaskRequest.model_json_schema())
    write_json(output / "local-task-request.schema.json", LocalTaskRequest.model_json_schema())
    write_json(output / "task-response.schema.json", TaskRecord.model_json_schema())
    write_json(output / "error-response.schema.json", ErrorResponse.model_json_schema())
    write_json(output / "confirmation-request.schema.json", ConfirmationRequest.model_json_schema())
    write_json(output / "handoff-resume-request.schema.json", HandoffResumeRequest.model_json_schema())
    write_json(output / "bridge-rpc.contract.json", rpc_contracts())
    schema_settings = Settings(enable_a2a=False, llm_model=None, production_mode=False)
    app = create_app(schema_settings)
    write_json(output / "openapi.json", app.openapi())
    skill = create_skill_app(SkillConfig(
        server_url="https://phone.example.com", bridge_id="schema-only",
        bridge_token="schema-bridge-placeholder-32-characters",
        api_token="schema-api-placeholder-32-characters",
        local_token="schema-local-placeholder-32-characters",
        devices=[{"id": "schema-device", "udid": "schema-device", "appium_url": "http://127.0.0.1:4723"}],
    ))
    write_json(output / "skill-openapi.json", skill.openapi())
    print(f"wrote contracts to {output.resolve()}")


if __name__ == "__main__":
    main()
