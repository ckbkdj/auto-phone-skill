"""Observe -> decide ONE action -> guard -> execute once -> verify -> observe again."""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter, deque
from time import perf_counter
from uuid import uuid4

from lobster_phone_agent.agent.executor import ExecutionHooks
from lobster_phone_agent.agent.risk import RiskDecision
from lobster_phone_agent.errors import ConditionTimeout, ExecutionError, TaskCancelled
from lobster_phone_agent.llm.next_action import NextAction, NextActionPlanner
from lobster_phone_agent.schemas import (
    ActionPlan, ActionStep, ActionType, EventType, RiskLevel, ScreenCondition,
    SemanticTarget, StepTrace, utc_now,
)


class StepwiseExecutor:
    def __init__(self, *, settings, planner: NextActionPlanner, matcher, conditions, dialogs, risk):
        self.settings, self.planner, self.matcher = settings, planner, matcher
        self.conditions, self.dialogs, self.risk = conditions, dialogs, risk

    async def execute_live(self, *, request, device, hooks: ExecutionHooks,
                           snapshot, installed_apps, on_decision):
        limit = min(request.policy.max_steps or self.settings.max_steps, self.settings.max_steps)
        receipts = deque(maxlen=3)
        seen = Counter()
        nonce = uuid4().hex  # Task execution identity; never a guessed LLM identifier.
        completed = 0
        for turn in range(limit):
            if hooks.is_cancelled():
                raise TaskCancelled("task cancelled")
            observation_id = uuid4().hex
            blocking = self.dialogs.detect_blocking(snapshot)
            if blocking:
                await hooks.handoff(blocking.code, blocking.reason)
                snapshot = await self._observe(device)
                receipts.append({"action": "handoff", "outcome": "returned_from_user"})
                continue
            decision_started = perf_counter()
            decision = await self.planner.choose(
                request=request, snapshot=snapshot, observation_id=observation_id,
                installed_apps=installed_apps, recent_receipts=list(receipts),
            )
            decision_ms = (perf_counter() - decision_started) * 1000
            # Revalidate even local/custom planner instances: the executor is a trust boundary.
            decision = NextAction.model_validate(decision.model_dump(mode="json"))
            if decision.observation_id != observation_id:
                raise ExecutionError("decision belongs to a different observation")
            step, selected = self._compile(decision, snapshot, turn)
            if selected is not None and any(x in snapshot.package.lower() for x in ("permissioncontroller", "packageinstaller")):
                await hooks.handoff("system_permission", "请在手机上确认系统权限请求")
                snapshot = await self._observe(device)
                continue
            risk = self.risk.assess(step, request.policy)
            step.risk = risk.risk
            await on_decision(ActionPlan(
                goal=request.instruction, planner="llm" if self.settings.llm_enabled else "recipe",
                steps=[step], metadata={"mode": "single_step", "round": turn,
                                       "observation_id": observation_id},
            ))
            await hooks.set_step_index(turn)
            if decision.action == "handoff":
                await hooks.handoff("planner_handoff", decision.text or "请人工确认当前页面")
                snapshot = await self._observe(device)
                receipts.append({"action": "handoff", "outcome": "returned_from_user"})
                continue
            if risk.blocked:
                raise ExecutionError(risk.reason)
            # Model labels, caller preauthorization and confirmation_mode=none cannot remove
            # the final UI transaction gate in live single-step mode.
            if risk.risk == RiskLevel.HIGH or risk.confirmation_required:
                await hooks.confirm(step, RiskDecision(risk.risk, True, False, risk.reason))

            # Always re-read after inference / human confirmation. Never use old coordinates.
            current = await device.snapshot()
            if current.fingerprint != snapshot.fingerprint or not self._same_target(selected, current):
                receipts.append({"action": decision.action, "outcome": "stale_not_executed"})
                snapshot = current
                continue
            snapshot = current
            if decision.action == "finish":
                if not self.conditions.all_met(decision.expect, current):
                    raise ConditionTimeout("completion evidence is not visible")
                simple = self.planner.launch_goal(request, installed_apps)
                if simple and current.package != simple.package:
                    raise ConditionTimeout("requested application is not in the foreground")
                return {"goal": request.instruction, "app": simple.name if simple else current.package,
                        "final_package": current.package, "final_activity": current.activity,
                        "verified_actions": completed, "mode": "single_step"}

            identity = json.dumps({"screen": snapshot.fingerprint,
                                   "action": decision.model_dump(mode="json", exclude={"observation_id", "expect"})},
                                  sort_keys=True, ensure_ascii=False)
            seen[identity] += 1
            if seen[identity] > 1 and decision.action != "wait":
                raise ExecutionError("repeated action on the same unchanged screen was blocked")
            if seen[identity] > 3:
                raise ExecutionError("no-progress wait loop was blocked")
            operation_id = hashlib.sha256(f"{nonce}:{turn}".encode()).hexdigest()
            trace = StepTrace(step_id=step.id, action=step.action, started_at=utc_now(), attempts=1)
            await hooks.emit(EventType.STEP_STARTED, step.description, {"round": turn, "step_id": step.id, "operation_id": operation_id, "decision_ms": decision_ms})
            started = perf_counter()
            try:
                await self._dispatch(decision, selected, snapshot, device, operation_id)
                trace.dispatch_ms = (perf_counter() - started) * 1000
                observed = perf_counter()
                after = await self._observe(device)
                if decision.action == "type":
                    self._verify_input(after, selected, decision.text or "")
                elif decision.action == "clear":
                    self._verify_input(after, selected, "")
                checks = decision.expect
                if decision.action == "launch_app":
                    checks = [ScreenCondition(kind="package_is", value=decision.app_package)]
                if checks and not self.conditions.all_met(checks, after, previous=snapshot):
                    after = await self.conditions.wait(
                        device, checks, timeout_ms=self.settings.post_action_timeout_ms, previous=snapshot
                    )
                    if not self.conditions.all_met(checks, after, previous=snapshot):
                        raise ConditionTimeout("action was sent but its postcondition was not verified")
                trace.observation_ms = (perf_counter() - observed) * 1000
                trace.success = True
                completed += 1
                receipts.append({"action": decision.action, "outcome": "verified",
                                 "before": snapshot.fingerprint, "after": after.fingerprint})
                snapshot = after
            except Exception as exc:
                # An uncertain or failed mutation is never retried, repaired, or changed into
                # success by another LLM completion. Operator must reconcile external effects.
                trace.error = f"{type(exc).__name__}: action failed or outcome unverified"
                await hooks.emit(EventType.STEP_FAILED, trace.error, {"round": turn, "step_id": step.id})
                raise ExecutionError(trace.error) from exc
            finally:
                trace.ended_at = utc_now()
                await hooks.trace(trace)
            await hooks.emit(EventType.STEP_SUCCEEDED, "single action verified", {"round": turn, "step_id": step.id})
        raise ExecutionError("single-step observation/action budget exhausted; goal not certified")

    def _compile(self, decision, snapshot, turn):
        node = None
        target = None
        description = decision.action
        if decision.target_node is not None:
            raw = next((n for n in snapshot.nodes if n.index == decision.target_node), None)
            if raw is None or raw.password or not raw.enabled or not raw.displayed or raw.bounds is None:
                raise ExecutionError("target is absent, hidden, disabled or sensitive")
            if not raw.bounds.intersects(snapshot.width, snapshot.height):
                raise ExecutionError("target is outside the visible display")
            from lobster_phone_agent.device.matcher import MatchResult
            match = self.matcher.interaction_node(snapshot, MatchResult(raw, 10.0, "observed-node"), ActionType(decision.action))
            if match is None or match.node.password:
                raise ExecutionError("target is not an actionable control")
            node = match.node
            if node.bounds.area > max(1, snapshot.width * snapshot.height) * .8:
                raise ExecutionError("refusing an oversized container target")
            target = SemanticTarget(text=raw.label or None, resource_id=raw.resource_id or None,
                                    role=node.role, allow_partial=False)
            # Risk is derived from the live UI, not from model-supplied risk/description.
            description = f"{decision.action} {raw.text} {raw.content_desc} {raw.resource_id} {node.label}"
        value = None
        if decision.action == "launch_app":
            value = decision.app_package
        elif decision.action in {"type", "handoff"}:
            value = decision.text
        elif decision.action == "wait":
            value = decision.wait_ms
        step = ActionStep(id=f"round_{turn}", action=ActionType(decision.action),
                          description=description[:2000], target=target, value=value,
                          direction=decision.direction, expect=decision.expect,
                          retry={"max_attempts": 1, "allow_repair": False},
                          metadata={"allow_scroll_search": False})
        return step, node

    @staticmethod
    def _same_target(selected, current):
        if selected is None:
            return True
        return any(n.stable_key == selected.stable_key and n.enabled and n.displayed and not n.password
                   for n in current.nodes)

    @staticmethod
    def _verify_input(snapshot, selected, expected):
        candidates = [n for n in snapshot.nodes if n.editable and not n.password and n.displayed
                      and ((selected.resource_id and n.resource_id == selected.resource_id)
                           or (not selected.resource_id and n.path == selected.path))]
        from lobster_phone_agent.util.text import normalize_text
        if len(candidates) != 1 or candidates[0].text != expected:
            raise ConditionTimeout("input value was not verified on the selected field")

    @staticmethod
    async def _dispatch(d, node, snapshot, device, operation_id):
        if hasattr(device, "perform_observed") and d.action != "wait":
            params = {"operation_id": operation_id}
            method = d.action
            if d.action == "launch_app":
                params["package"] = d.app_package
            elif d.action == "tap":
                params["x"], params["y"] = node.bounds.clamped_center(snapshot.width, snapshot.height)
            elif d.action == "type":
                method = "type_text"
                params.update(text=d.text, clear=True, element=node.interaction_payload())
            elif d.action == "clear":
                method = "clear_active"
                params["element"] = node.interaction_payload()
            elif d.action == "swipe":
                params["direction"] = d.direction
            await device.perform_observed(method, params, snapshot.fingerprint)
            return
        if d.action == "launch_app":
            await device.launch_app(d.app_package, operation_id=operation_id)
        elif d.action == "tap":
            await device.tap(*node.bounds.clamped_center(snapshot.width, snapshot.height), operation_id=operation_id)
        elif d.action == "type":
            await device.type_text(d.text, clear=True, element=node.interaction_payload(), operation_id=operation_id)
        elif d.action == "clear":
            await device.clear_active(element=node.interaction_payload(), operation_id=operation_id)
        elif d.action == "swipe":
            await device.swipe(d.direction, operation_id=operation_id)
        elif d.action == "back":
            await device.back(operation_id=operation_id)
        elif d.action == "home":
            await device.home(operation_id=operation_id)
        elif d.action == "wait":
            await asyncio.sleep(d.wait_ms / 1000)
        else:
            raise ExecutionError("unsupported single action")

    async def _observe(self, device):
        if self.settings.action_settle_ms:
            await asyncio.sleep(self.settings.action_settle_ms / 1000)
        deadline = asyncio.get_running_loop().time() + self.settings.snapshot_stable_timeout_ms / 1000
        previous = await device.snapshot()
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(max(1, self.settings.snapshot_stable_interval_ms) / 1000)
            current = await device.snapshot()
            if current.fingerprint == previous.fingerprint:
                return current
            previous = current
        raise ConditionTimeout("screen did not stabilize; no next action allowed")
