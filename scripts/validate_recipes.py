#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.apps.registry import AppRegistry
from lobster_phone_agent.schemas import DeviceDescriptor, TaskRequest


EXAMPLES = (
    "打开微信",
    "在淘宝搜索蓝牙耳机",
    "用美团打车去北京南站",
    "滴滴去首都机场",
    "导航到天安门",
)


def main() -> None:
    registry = AppRegistry.from_yaml(Path("config/apps.yaml"))
    planner = RecipePlanner.from_directory(Path("config/recipes"))
    if len(registry.records) < 60:
        raise SystemExit("app seed unexpectedly small")
    for example in EXAMPLES:
        plan = planner.plan(
            TaskRequest(
                instruction=example,
                device=DeviceDescriptor(id="validate", bridge_id="validation-bridge"),
            )
        )
        if plan is None:
            raise SystemExit(f"no recipe for {example}")
        print(example, "->", plan.metadata.get("recipe_id"), len(plan.steps))
    print(f"validated {len(planner.recipes)} recipes and {len(registry.records)} app records")


if __name__ == "__main__":
    main()
