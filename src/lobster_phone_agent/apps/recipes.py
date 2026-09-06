from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from lobster_phone_agent.schemas import ActionPlan, ActionStep, ScreenCondition, TaskRequest


class RecipeSpec(BaseModel):
    id: str
    description: str = ""
    priority: int = 0
    patterns: list[str]
    target_app: str | None = None
    target_package: str | None = None
    steps: list[dict[str, Any]]
    success: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format_value(value: Any, slots: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(SafeFormatDict(slots))
    if isinstance(value, list):
        return [_format_value(item, slots) for item in value]
    if isinstance(value, dict):
        return {key: _format_value(item, slots) for key, item in value.items()}
    return value


class RecipePlanner:
    def __init__(self, recipes: list[RecipeSpec]) -> None:
        self.recipes = sorted(recipes, key=lambda recipe: recipe.priority, reverse=True)
        self._compiled: dict[str, list[re.Pattern[str]]] = {
            recipe.id: [re.compile(pattern, re.IGNORECASE) for pattern in recipe.patterns]
            for recipe in self.recipes
        }

    @classmethod
    def from_directory(cls, path: Path) -> RecipePlanner:
        recipes: list[RecipeSpec] = []
        if path.exists():
            for file_path in sorted(path.glob("*.yaml")):
                raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
                if not raw:
                    continue
                recipes.append(RecipeSpec.model_validate(raw))
        return cls(recipes)

    def plan(self, request: TaskRequest) -> ActionPlan | None:
        instruction = request.instruction.strip()
        for recipe in self.recipes:
            for pattern in self._compiled[recipe.id]:
                match = pattern.fullmatch(instruction) or pattern.search(instruction)
                if not match:
                    continue
                slots: dict[str, Any] = {
                    key: self._clean_slot(key, value)
                    for key, value in match.groupdict().items()
                    if value is not None
                }
                slots.update(
                    {
                        "instruction": instruction,
                        "app_package": request.app_package or "",
                        "current_address": request.location.address
                        if request.location and request.location.address
                        else "",
                    }
                )
                target_app = (
                    _format_value(recipe.target_app, slots) if recipe.target_app else None
                )
                target_package = request.app_package or (
                    _format_value(recipe.target_package, slots)
                    if recipe.target_package
                    else None
                )
                slots.setdefault("app", target_app or "")
                steps = [
                    ActionStep.model_validate(_format_value(step, slots))
                    for step in recipe.steps
                ]
                success = [
                    ScreenCondition.model_validate(_format_value(condition, slots))
                    for condition in recipe.success
                ]
                return ActionPlan(
                    goal=instruction,
                    target_app=target_app,
                    target_package=target_package,
                    planner="recipe",
                    steps=steps,
                    success=success,
                    metadata={
                        **recipe.metadata,
                        "recipe_id": recipe.id,
                        "slots": slots,
                    },
                )
        return None

    @staticmethod
    def _clean_slot(key: str, value: str) -> str:
        cleaned = value.strip(" ，,。.!！?？")
        if key in {"destination", "query", "contact", "message"}:
            cleaned = re.sub(r"^(去|到|搜索|查找|找一下)", "", cleaned).strip()
        if key == "app":
            cleaned = re.sub(r"(app|应用)$", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned
