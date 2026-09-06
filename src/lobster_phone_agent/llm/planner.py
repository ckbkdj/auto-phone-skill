from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from lobster_phone_agent.agent.cache import PlanCache
from lobster_phone_agent.apps.recipes import RecipePlanner
from lobster_phone_agent.apps.registry import AppRecord, AppRegistry
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.ui import ScreenSnapshot
from lobster_phone_agent.edge.router import LocalIntentRouter
from lobster_phone_agent.errors import PlanningError
from lobster_phone_agent.llm.client import OpenAICompatibleClient
from lobster_phone_agent.llm.prompts import PLANNER_SYSTEM_PROMPT, REPAIR_SYSTEM_PROMPT
from lobster_phone_agent.schemas import ActionPlan, ActionStep, TaskRequest
from lobster_phone_agent.util.text import compact_text, redact_text


class HybridPlanner:
    def __init__(
        self,
        *,
        settings: Settings,
        recipes: RecipePlanner,
        registry: AppRegistry,
        llm: OpenAICompatibleClient,
        cache: PlanCache | None = None,
        router: LocalIntentRouter | None = None,
    ) -> None:
        self.settings = settings
        self.recipes = recipes
        self.registry = registry
        self.llm = llm
        self.cache = cache or PlanCache()
        self.router = router or LocalIntentRouter(settings.edge_model_dir)

    async def plan(
        self,
        request: TaskRequest,
        *,
        snapshot: ScreenSnapshot | None,
        installed_apps: list[dict[str, object]],
    ) -> ActionPlan:
        scoped_registry = self.registry.with_task_catalog(request.app_catalog)
        route = self.router.route(request.instruction)
        cache_key = self._cache_key(request, snapshot, installed_apps)
        cached = await self.cache.get(cache_key)
        if cached is not None:
            cached.metadata["cache_hit"] = True
            cached.metadata["local_route"] = {
                "intent": route.intent,
                "confidence": route.confidence,
                "source": route.source,
            }
            return self._resolve_launches(cached, scoped_registry, installed_apps, request)

        recipe = self.recipes.plan(request)
        if recipe is not None:
            recipe.metadata["local_route"] = {
                "intent": route.intent,
                "confidence": route.confidence,
                "source": route.source,
            }
            recipe = self._resolve_launches(recipe, scoped_registry, installed_apps, request)
            await self.cache.put(cache_key, recipe)
            return recipe

        if not self.settings.llm_enabled:
            raise PlanningError(
                "No deterministic recipe matched and PHONE_AGENT_LLM_MODEL is not configured"
            )

        prompt = self._planner_input(
            request, snapshot, scoped_registry, installed_apps, route_intent=route.intent
        )
        raw = await self.llm.complete_json(
            system=PLANNER_SYSTEM_PROMPT,
            user=prompt,
            schema_name="android_action_plan",
            schema=self._plan_schema(),
        )
        try:
            plan = ActionPlan.model_validate(raw)
        except Exception as exc:
            raise PlanningError("LLM plan failed validation") from exc
        plan.planner = "llm"
        plan.metadata["local_route"] = {
            "intent": route.intent,
            "confidence": route.confidence,
            "source": route.source,
        }
        plan = self._resolve_launches(plan, scoped_registry, installed_apps, request)
        await self.cache.put(cache_key, plan)
        return plan

    async def repair(
        self,
        *,
        request: TaskRequest,
        original_plan: ActionPlan,
        failed_step: ActionStep,
        error: str,
        snapshot: ScreenSnapshot,
        previous: ScreenSnapshot | None,
    ) -> ActionPlan:
        if not self.settings.llm_enabled:
            raise PlanningError("LLM repair is disabled")
        payload = {
            "goal": request.instruction,
            "target_app": original_plan.target_app,
            "target_package": original_plan.target_package,
            "failed_step": failed_step.model_dump(mode="json"),
            "error": redact_text(error, 500),
            "current_screen": snapshot.compact(max_nodes=72),
            "screen_diff": snapshot.diff(previous, max_items=32),
        }
        raw = await self.llm.complete_json(
            system=REPAIR_SYSTEM_PROMPT,
            user=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            schema_name="android_repair_plan",
            schema=self._plan_schema(),
            max_output_tokens=min(1000, self.settings.llm_max_output_tokens),
        )
        try:
            plan = ActionPlan.model_validate(raw)
        except Exception as exc:
            raise PlanningError("LLM repair failed validation") from exc
        plan.planner = "repair"
        plan.goal = original_plan.goal
        if len(plan.steps) > 4:
            raise PlanningError("LLM repair exceeds the four-step repair budget")
        return plan

    def _resolve_launches(
        self,
        plan: ActionPlan,
        registry: AppRegistry,
        installed_apps: list[dict[str, object]],
        request: TaskRequest,
    ) -> ActionPlan:
        explicit = request.app_package or plan.target_package
        query = plan.target_app or ""
        record = registry.resolve(
            query,
            installed_apps=installed_apps,
            explicit_package=explicit,
        )
        if record and record.package:
            plan.target_package = record.package
            plan.target_app = plan.target_app or record.name
        for step in plan.steps:
            if step.action.value != "launch_app" or not step.value:
                continue
            value = str(step.value)
            if plan.target_package and value == plan.target_app:
                step.value = plan.target_package
                continue
            resolved = registry.resolve(
                value,
                installed_apps=installed_apps,
                explicit_package=request.app_package if value == query else None,
            )
            if resolved and resolved.package:
                step.value = resolved.package
                continue
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", value):
                raise PlanningError(
                    f"cannot resolve app {value!r} to an installed Android package; "
                    "pass app_package or add it to app_catalog"
                )
        return plan

    @staticmethod
    def _cache_key(
        request: TaskRequest,
        snapshot: ScreenSnapshot | None,
        installed_apps: list[dict[str, object]],
    ) -> str:
        installed_packages = sorted(
            str(item.get("package") or item.get("packageName") or item.get("id") or "")
            for item in installed_apps
            if item.get("package") or item.get("packageName") or item.get("id")
        )
        normalized = {
            "instruction": compact_text(request.instruction),
            "device_id": request.device.id,
            "package": request.app_package,
            "locale": request.locale,
            "location": request.location.model_dump(mode="json") if request.location else None,
            "app_catalog": request.app_catalog,
            "installed_packages": installed_packages,
            "screen": snapshot.fingerprint if snapshot else "none",
            "policy": request.policy.model_dump(mode="json"),
        }
        raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _planner_input(
        request: TaskRequest,
        snapshot: ScreenSnapshot | None,
        registry: AppRegistry,
        installed_apps: list[dict[str, object]],
        *,
        route_intent: str,
    ) -> str:
        likely_apps: list[AppRecord] = []
        for token in request.instruction.replace("，", " ").split():
            record = registry.resolve(token, installed_apps=installed_apps)
            if record and all(existing.package != record.package for existing in likely_apps):
                likely_apps.append(record)
        if request.app_package:
            record = registry.resolve(
                request.app_package,
                installed_apps=installed_apps,
                explicit_package=request.app_package,
            )
            if record:
                likely_apps.insert(0, record)
        compact_apps = [
            {
                "name": item.name,
                "package": item.package,
                "aliases": item.aliases[:6],
                "category": item.category,
            }
            for item in likely_apps[:12]
        ]
        payload: dict[str, Any] = {
            "instruction": request.instruction,
            "local_route": route_intent,
            "locale": request.locale,
            "location": request.location.model_dump(mode="json") if request.location else None,
            "explicit_package": request.app_package,
            "likely_apps": compact_apps,
            "current_screen": snapshot.compact(max_nodes=80) if snapshot else None,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _plan_schema() -> dict[str, Any]:
        schema = ActionPlan.model_json_schema()
        # Structured-output APIs reject recursive `$defs` in some implementations only when
        # additionalProperties is omitted. Pydantic's schema is otherwise the source of truth.
        return schema
