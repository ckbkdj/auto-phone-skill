#!/usr/bin/env python3
"""Validate the agent without opening an Appium session or contacting an LLM."""
from __future__ import annotations

import compileall
import json
from pathlib import Path

from fastapi.testclient import TestClient

from lobster_phone_agent.app import create_app
from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.apps.registry import AppRegistry
from lobster_phone_agent.config import Settings
from lobster_phone_agent.edge.router import LocalIntentRouter
from lobster_phone_agent.schemas import DeviceDescriptor, TaskRequest


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    assert compileall.compile_dir(root / "src", quiet=1)
    settings = Settings(
        config_dir=root / "config",
        artifact_dir=root / "artifacts" / "validation",
        enable_a2a=False,
        api_key="validation-key",
    )
    registry = AppRegistry.from_yaml(settings.config_dir / "apps.yaml")
    recipes = RecipePlanner.from_directory(settings.config_dir / "recipes")
    router = LocalIntentRouter()
    examples = [
        "打开微信",
        "在淘宝搜索蓝牙耳机",
        "用美团打车去北京南站",
        "滴滴去首都机场",
        "导航到天安门",
    ]
    results = []
    for text in examples:
        request = TaskRequest(
            instruction=text,
            device=DeviceDescriptor(id="validation", bridge_id="validation-bridge"),
        )
        plan = recipes.plan(request)
        assert plan is not None
        route = router.route(text)
        results.append({"instruction": text, "recipe": plan.metadata["recipe_id"], "route": route.intent})
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/apps").status_code == 401
        assert client.get("/v1/apps", headers={"X-API-Key": "validation-key"}).status_code == 200
        schema = client.get("/v1/contracts/action-plan", headers={"X-API-Key": "validation-key"}).json()
        assert schema["title"] == "ActionPlan"
    report = {
        "source_compile": "passed",
        "apps": len(registry.records),
        "recipes": len(recipes.recipes),
        "api_auth": "passed",
        "examples": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
