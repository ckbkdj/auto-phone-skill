from pathlib import Path

from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.schemas import DeviceDescriptor, TaskRequest


def test_meituan_ride_recipe_extracts_destination() -> None:
    planner = RecipePlanner.from_directory(Path("config/recipes"))
    request = TaskRequest(
        instruction="帮我用美团打车去北京南站",
        device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
    )
    plan = planner.plan(request)
    assert plan is not None
    assert plan.planner == "recipe"
    assert plan.metadata["recipe_id"] == "meituan_ride"
    assert plan.metadata["slots"]["destination"] == "北京南站"
    assert plan.steps[-2].risk.value == "high"
    assert "北京南站" in (plan.steps[-2].confirmation_text or "")


def test_open_app_recipe() -> None:
    planner = RecipePlanner.from_directory(Path("config/recipes"))
    request = TaskRequest(
        instruction="打开微信",
        device=DeviceDescriptor(id="dev", bridge_id="bridge-test"),
    )
    plan = planner.plan(request)
    assert plan is not None
    assert plan.target_app == "微信"
    assert plan.steps[0].value == "微信"
