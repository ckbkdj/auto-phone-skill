from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from time import perf_counter
from typing import Awaitable, Callable
from uuid import uuid4

from lobster_phone_agent.agent.conditions import ConditionEvaluator
from lobster_phone_agent.agent.dialogs import CommonDialogHandler
from lobster_phone_agent.agent.risk import RiskDecision, RiskEngine
from lobster_phone_agent.agent.selector_memory import SelectorMemory
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.base import DeviceAdapter
from lobster_phone_agent.device.matcher import MatchResult, SemanticMatcher
from lobster_phone_agent.device.ui import ScreenSnapshot
from lobster_phone_agent.errors import (
    ConditionTimeout,
    ElementNotFound,
    ExecutionError,
    HandoffRequired,
    TaskCancelled,
)
from lobster_phone_agent.llm.planner import HybridPlanner
from lobster_phone_agent.llm.vision import VisionGrounder
from lobster_phone_agent.schemas import (
    ActionPlan,
    ActionStep,
    ActionType,
    EventType,
    StepTrace,
    TaskRequest,
    utc_now,
)

EmitCallback = Callable[[EventType, str, dict[str, object]], Awaitable[None]]
ConfirmationCallback = Callable[[ActionStep, RiskDecision], Awaitable[None]]
HandoffCallback = Callable[[str, str], Awaitable[None]]
TraceCallback = Callable[[StepTrace], Awaitable[None]]
StepIndexCallback = Callable[[int], Awaitable[None]]
CancelCallback = Callable[[], bool]


@dataclass(slots=True)
class ExecutionHooks:
    emit: EmitCallback
    confirm: ConfirmationCallback
    handoff: HandoffCallback
    trace: TraceCallback
    set_step_index: StepIndexCallback
    is_cancelled: CancelCallback


class ActionExecutor:
    def __init__(
        self,
        *,
        settings: Settings,
        matcher: SemanticMatcher,
        conditions: ConditionEvaluator,
        dialogs: CommonDialogHandler,
        risk: RiskEngine,
        planner: HybridPlanner,
        selector_memory: SelectorMemory | None = None,
        vision_grounder: VisionGrounder | None = None,
    ) -> None:
        self.settings = settings
        self.matcher = matcher
        self.conditions = conditions
        self.dialogs = dialogs
        self.risk = risk
        self.planner = planner
        self.selector_memory = selector_memory or SelectorMemory()
        self.vision_grounder = vision_grounder

    async def execute(
        self,
        *,
        request: TaskRequest,
        plan: ActionPlan,
        device: DeviceAdapter,
        hooks: ExecutionHooks,
    ) -> dict[str, object]:
        max_steps = request.policy.max_steps or self.settings.max_steps
        if len(plan.steps) > max_steps:
            raise ExecutionError(f"plan contains {len(plan.steps)} steps; limit is {max_steps}")

        repairs_left = request.policy.max_repairs
        if repairs_left is None:
            repairs_left = self.settings.max_repairs
        result: dict[str, object] = {"goal": plan.goal}
        previous_snapshot: ScreenSnapshot | None = None
        execution_seed = request.idempotency_key or uuid4().hex
        execution_id = hashlib.sha256(execution_seed.encode("utf-8")).hexdigest()[:24]

        for index, step in enumerate(plan.steps):
            operation_id = f"{execution_id}:main:{index}"
            if hooks.is_cancelled():
                raise TaskCancelled("task cancelled")
            await hooks.set_step_index(index)
            await hooks.emit(
                EventType.STEP_STARTED,
                step.description or step.action.value,
                {"index": index, "step": step.model_dump(mode="json")},
            )

            decision = self.risk.assess(step, request.policy)
            if decision.blocked:
                if "用户" in decision.reason or step.action is ActionType.HANDOFF:
                    await hooks.handoff("sensitive_input_required", decision.reason)
                    await hooks.emit(
                        EventType.STEP_SUCCEEDED,
                        "sensitive step completed by human handoff",
                        {"index": index, "step_id": step.id, "handoff": True},
                    )
                    previous_snapshot = await device.snapshot()
                    continue
                raise ExecutionError(decision.reason)
            if decision.confirmation_required:
                await hooks.confirm(step, decision)

            try:
                previous_snapshot = await self._run_step_with_retries(
                    step=step,
                    device=device,
                    previous_snapshot=previous_snapshot,
                    hooks=hooks,
                    operation_id=operation_id,
                )
            except (ElementNotFound, ConditionTimeout, ExecutionError) as exc:
                if step.optional:
                    await hooks.emit(
                        EventType.STEP_SUCCEEDED,
                        f"optional step skipped: {exc}",
                        {"index": index, "step_id": step.id, "skipped": True},
                    )
                    previous_snapshot = await device.snapshot()
                    continue

                if (
                    repairs_left <= 0
                    or not step.retry.allow_repair
                    or not self.settings.llm_enabled
                ):
                    await hooks.emit(
                        EventType.STEP_FAILED,
                        str(exc),
                        {"index": index, "step_id": step.id},
                    )
                    raise

                snapshot = await device.snapshot()
                blocking = self.dialogs.detect_blocking(snapshot)
                if blocking:
                    await hooks.handoff(blocking.code, blocking.reason)
                    previous_snapshot = await device.snapshot()
                    # Human handoff only removes the blocker; it does not complete the original
                    # action. Retry the original action and verify it before advancing.
                    previous_snapshot = await self._run_step_with_retries(
                        step=step,
                        device=device,
                        previous_snapshot=previous_snapshot,
                        hooks=hooks,
                        operation_id=operation_id,
                    )
                else:
                    repairs_left -= 1
                    repair_plan = await self.planner.repair(
                        request=request,
                        original_plan=plan,
                        failed_step=step,
                        error=str(exc),
                        snapshot=snapshot,
                        previous=previous_snapshot,
                    )
                    await hooks.emit(
                        EventType.STEP_RETRYING,
                        "executing local repair plan",
                        {
                            "index": index,
                            "step_id": step.id,
                            "repairs_left": repairs_left,
                            "repair_plan": repair_plan.model_dump(mode="json"),
                        },
                    )
                    previous_snapshot = await self._run_repair_plan(
                        repair_plan=repair_plan,
                        request=request,
                        device=device,
                        previous_snapshot=snapshot,
                        hooks=hooks,
                        operation_namespace=f"{execution_id}:repair:{index}:{repairs_left}",
                    )
                    # A repair plan restores the expected UI context. The failed original action
                    # still has to run and pass its postcondition; skipping it was a major source
                    # of false task success in the first release.
                    previous_snapshot = await self._run_step_with_retries(
                        step=step,
                        device=device,
                        previous_snapshot=previous_snapshot,
                        hooks=hooks,
                        allow_repair=False,
                        operation_id=operation_id,
                    )

            if step.action is ActionType.FINISH and isinstance(step.value, dict):
                result.update(step.value)
            await hooks.emit(
                EventType.STEP_SUCCEEDED,
                step.description or step.action.value,
                {"index": index, "step_id": step.id},
            )

        if plan.success:
            final_snapshot = await device.snapshot()
            if not self.conditions.all_met(
                plan.success, final_snapshot, previous=previous_snapshot
            ):
                raise ConditionTimeout("final success conditions were not observed")
            result["final_package"] = final_snapshot.package
            result["final_activity"] = final_snapshot.activity
        return result

    async def _run_repair_plan(
        self,
        *,
        repair_plan: ActionPlan,
        request: TaskRequest,
        device: DeviceAdapter,
        previous_snapshot: ScreenSnapshot | None,
        hooks: ExecutionHooks,
        operation_namespace: str,
    ) -> ScreenSnapshot | None:
        current = previous_snapshot
        for repair_index, repair_step in enumerate(repair_plan.steps):
            decision = self.risk.assess(repair_step, request.policy)
            if decision.blocked:
                if "用户" not in decision.reason and repair_step.action is not ActionType.HANDOFF:
                    raise ExecutionError(decision.reason)
                await hooks.handoff("repair_handoff", decision.reason)
                current = await device.snapshot()
                continue
            if repair_step.action is ActionType.HANDOFF:
                await hooks.handoff(
                    "repair_handoff",
                    repair_step.description or str(repair_step.value or "需要用户接管手机"),
                )
                current = await device.snapshot()
                continue
            if decision.confirmation_required:
                await hooks.confirm(repair_step, decision)
            current = await self._run_step_with_retries(
                step=repair_step,
                device=device,
                previous_snapshot=current,
                hooks=hooks,
                allow_repair=False,
                operation_id=f"{operation_namespace}:{repair_index}",
            )
        return current

    async def _run_step_with_retries(
        self,
        *,
        step: ActionStep,
        device: DeviceAdapter,
        previous_snapshot: ScreenSnapshot | None,
        hooks: ExecutionHooks,
        allow_repair: bool = True,
        operation_id: str,
    ) -> ScreenSnapshot | None:
        last_error: Exception | None = None
        for attempt in range(1, step.retry.max_attempts + 1):
            if hooks.is_cancelled():
                raise TaskCancelled("task cancelled")
            trace = StepTrace(
                step_id=step.id,
                action=step.action,
                started_at=utc_now(),
                attempts=attempt,
            )
            try:
                snapshot, match, dispatch_ms, observation_ms = await self._execute_once(
                    step=step,
                    device=device,
                    previous_snapshot=previous_snapshot,
                    hooks=hooks,
                    operation_id=operation_id,
                    attempt=attempt,
                )
                trace.ended_at = utc_now()
                trace.dispatch_ms = dispatch_ms
                trace.observation_ms = observation_ms
                trace.selected_node = self._node_payload(match) if match else None
                trace.success = True
                await hooks.trace(trace)
                return snapshot
            except HandoffRequired as exc:
                await hooks.handoff(exc.code, exc.reason)
                trace.ended_at = utc_now()
                trace.error = f"handoff: {exc.reason}"
                sensitive_type_completed = (
                    step.action is ActionType.TYPE
                    and exc.code
                    in {
                        "password_required",
                        "payment_password_required",
                        "otp_required",
                        "captcha_required",
                        "biometric_required",
                    }
                )
                if step.action is ActionType.HANDOFF or sensitive_type_completed:
                    trace.success = True
                    await hooks.trace(trace)
                    return await device.snapshot()
                await hooks.trace(trace)
                if attempt >= step.retry.max_attempts:
                    raise ExecutionError(
                        f"blocking surface remained after human handoff: {exc.reason}"
                    ) from exc
                continue
            except Exception as exc:
                last_error = exc
                trace.ended_at = utc_now()
                trace.error = str(exc)
                await hooks.trace(trace)
                if attempt >= step.retry.max_attempts:
                    break
                await hooks.emit(
                    EventType.STEP_RETRYING,
                    str(exc),
                    {
                        "step_id": step.id,
                        "attempt": attempt,
                        "max_attempts": step.retry.max_attempts,
                        "allow_repair": allow_repair,
                    },
                )
                await asyncio.sleep(step.retry.delay_ms / 1000.0)
        if isinstance(last_error, (ElementNotFound, ConditionTimeout, ExecutionError)):
            raise last_error
        raise ExecutionError(str(last_error or "step failed"))

    async def _execute_once(
        self,
        *,
        step: ActionStep,
        device: DeviceAdapter,
        previous_snapshot: ScreenSnapshot | None,
        hooks: ExecutionHooks,
        operation_id: str,
        attempt: int,
    ) -> tuple[ScreenSnapshot | None, MatchResult | None, float, float]:
        observation_started = perf_counter()
        snapshot: ScreenSnapshot | None = None
        match: MatchResult | None = None

        mutating = step.action in {
            ActionType.LAUNCH_APP,
            ActionType.TAP,
            ActionType.TYPE,
            ActionType.CLEAR,
            ActionType.SWIPE,
            ActionType.BACK,
            ActionType.HOME,
        }
        needs_snapshot = mutating or bool(step.preconditions or step.expect or step.target) or (
            step.action in {ActionType.ASSERT, ActionType.HANDOFF}
        )
        if needs_snapshot:
            snapshot = await device.snapshot()
            blocking = self.dialogs.detect_blocking(snapshot)
            if blocking and step.action is not ActionType.HANDOFF:
                raise HandoffRequired(blocking.reason, code=blocking.code)
            if step.preconditions and not self.conditions.all_met(
                step.preconditions,
                snapshot,
                previous=previous_snapshot,
            ):
                raise ConditionTimeout("step preconditions are not met")

        dispatch_started = perf_counter()
        if step.action is ActionType.LAUNCH_APP:
            await device.launch_app(
                str(step.value), operation_id=f"{operation_id}:launch"
            )
        elif step.action in {ActionType.TAP, ActionType.TYPE, ActionType.CLEAR}:
            if snapshot is None or step.target is None:
                raise ExecutionError("semantic action requires a screen snapshot and target")
            if step.action is ActionType.TYPE and self._password_target_present(
                snapshot, step.target
            ):
                raise HandoffRequired(
                    "密码输入框必须由用户接管完成",
                    code="password_required",
                )
            snapshot, match = await self._find_target(
                step=step,
                device=device,
                snapshot=snapshot,
                operation_namespace=f"{operation_id}:attempt:{attempt}",
            )
            point: tuple[int, int] | None = None
            if match is not None and match.node.bounds is not None:
                interaction = self.matcher.interaction_node(snapshot, match, step.action)
                match = interaction
                if match is not None and step.action is ActionType.TYPE and match.node.password:
                    raise HandoffRequired(
                        "密码输入框必须由用户接管完成",
                        code="password_required",
                    )
                if match is not None and match.node.bounds is not None:
                    self.selector_memory.remember(snapshot, step.target, match)
                    point = match.node.bounds.clamped_center(snapshot.width, snapshot.height)
            if point is None and self.vision_grounder is not None and self.vision_grounder.enabled:
                png = await device.screenshot_png()
                grounded = await self.vision_grounder.ground(
                    png=png,
                    snapshot=snapshot,
                    target=step.target,
                )
                if grounded is not None:
                    verification = await device.snapshot()
                    if verification.fingerprint == snapshot.fingerprint:
                        point = (grounded.x, grounded.y)
            if point is None:
                coordinates = step.target.coordinates
                expected_fingerprint = step.metadata.get("screen_fingerprint")
                if coordinates and expected_fingerprint == snapshot.fingerprint:
                    point = coordinates
                else:
                    raise ElementNotFound(
                        "no reliable element matched target "
                        f"{step.target.model_dump(exclude_none=True)}"
                    )
            x, y = point
            element_payload = match.node.interaction_payload() if match is not None else None
            if step.action is ActionType.TAP:
                await device.tap(x, y, operation_id=f"{operation_id}:tap")
            elif step.action is ActionType.CLEAR:
                await device.tap(x, y, operation_id=f"{operation_id}:focus")
                await device.clear_active(
                    element=element_payload, operation_id=f"{operation_id}:clear"
                )
            else:
                await device.tap(x, y, operation_id=f"{operation_id}:focus")
                await device.type_text(
                    str(step.value),
                    clear=bool(step.metadata.get("clear_first")),
                    element=element_payload,
                    operation_id=f"{operation_id}:type",
                )
        elif step.action is ActionType.SWIPE:
            await device.swipe(
                step.direction or "up", operation_id=f"{operation_id}:swipe"
            )
        elif step.action is ActionType.BACK:
            await device.back(operation_id=f"{operation_id}:back")
        elif step.action is ActionType.HOME:
            await device.home(operation_id=f"{operation_id}:home")
        elif step.action is ActionType.WAIT:
            wait_ms = int(step.value or self.settings.action_settle_ms)
            await asyncio.sleep(max(0, wait_ms) / 1000.0)
        elif step.action is ActionType.ASSERT:
            if not step.expect:
                raise ExecutionError("assert step requires expect conditions")
        elif step.action is ActionType.HANDOFF:
            reason = step.description or str(step.value or "需要用户接管手机")
            raise HandoffRequired(reason)
        elif step.action is ActionType.FINISH:
            pass
        else:
            raise ExecutionError(f"unsupported action: {step.action}")
        dispatch_ms = (perf_counter() - dispatch_started) * 1000.0

        after: ScreenSnapshot | None = snapshot
        if step.expect:
            after = await self.conditions.wait(
                device,
                step.expect,
                timeout_ms=step.timeout_ms,
                previous=snapshot or previous_snapshot,
            )
            if not self.conditions.all_met(
                step.expect,
                after,
                previous=snapshot or previous_snapshot,
            ):
                raise ConditionTimeout(f"postconditions not met for step {step.id}")
        elif mutating:
            after = await self._observe_after(device)
            if (
                snapshot is not None
                and after.fingerprint == snapshot.fingerprint
                and bool(step.metadata.get("require_screen_change"))
                and not bool(step.metadata.get("allow_noop"))
            ):
                raise ExecutionError(
                    f"{step.action.value} was dispatched but the observed UI did not change"
                )
        elif step.action is ActionType.ASSERT:
            after = snapshot
            if after is None or not self.conditions.all_met(
                step.expect,
                after,
                previous=previous_snapshot,
            ):
                raise ConditionTimeout(f"assertion failed for step {step.id}")

        observation_ms = (perf_counter() - observation_started) * 1000.0 - dispatch_ms
        return after, match, dispatch_ms, max(0.0, observation_ms)

    @staticmethod
    def _password_target_present(snapshot: ScreenSnapshot, target) -> bool:
        labels = {label.casefold() for label in target.labels() if label}
        wanted_id = (target.resource_id or "").casefold()
        wanted_accessibility = (target.accessibility_id or "").casefold()
        for node in snapshot.nodes:
            if not node.password or not node.displayed or not node.enabled:
                continue
            if wanted_id:
                resource_id = node.resource_id.casefold()
                if resource_id == wanted_id or resource_id.endswith(wanted_id):
                    return True
            if wanted_accessibility and wanted_accessibility in node.content_desc.casefold():
                return True
            if labels and any(
                label in candidate.casefold()
                for label in labels
                for candidate in (node.text, node.content_desc)
                if candidate
            ):
                return True
        return False

    async def _find_target(
        self,
        *,
        step: ActionStep,
        device: DeviceAdapter,
        snapshot: ScreenSnapshot,
        operation_namespace: str,
    ) -> tuple[ScreenSnapshot, MatchResult | None]:
        assert step.target is not None
        current = snapshot
        match = self.selector_memory.recall(current, step.target) or self.matcher.best(
            current, step.target
        )
        if match is not None:
            return current, match

        for dismiss_index in range(self.settings.max_safe_dialog_dismissals):
            dismissed = await self.dialogs.dismiss_safe(
                device,
                current,
                operation_id=f"{operation_namespace}:dismiss:{dismiss_index}",
            )
            if not dismissed:
                break
            await asyncio.sleep(self.settings.action_settle_ms / 1000.0)
            next_snapshot = await device.snapshot()
            if next_snapshot.fingerprint == current.fingerprint:
                break
            current = next_snapshot
            blocking = self.dialogs.detect_blocking(current)
            if blocking:
                raise HandoffRequired(blocking.reason, code=blocking.code)
            match = self.selector_memory.recall(current, step.target) or self.matcher.best(
                current, step.target
            )
            if match is not None:
                return current, match

        if not bool(step.metadata.get("allow_scroll_search", True)):
            return current, None
        direction = str(step.metadata.get("scroll_direction", "up"))
        if direction not in {"up", "down", "left", "right"}:
            direction = "up"
        for scroll_index in range(self.settings.max_scroll_searches):
            await device.swipe(
                direction,
                percent=0.62,
                operation_id=f"{operation_namespace}:search:{scroll_index}",
            )
            await asyncio.sleep(self.settings.action_settle_ms / 1000.0)
            next_snapshot = await device.snapshot()
            if next_snapshot.fingerprint == current.fingerprint:
                break
            current = next_snapshot
            match = self.selector_memory.recall(current, step.target) or self.matcher.best(
                current, step.target
            )
            if match is not None:
                return current, match
        return current, None

    async def _observe_after(self, device: DeviceAdapter) -> ScreenSnapshot:
        if self.settings.action_settle_ms:
            await asyncio.sleep(self.settings.action_settle_ms / 1000.0)
        deadline = asyncio.get_running_loop().time() + (
            self.settings.snapshot_stable_timeout_ms / 1000.0
        )
        previous = await device.snapshot()
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self.settings.snapshot_stable_interval_ms / 1000.0)
            current = await device.snapshot()
            if current.fingerprint == previous.fingerprint:
                return current
            previous = current
        return previous

    @staticmethod
    def _node_payload(match: MatchResult) -> dict[str, object]:
        node = match.node
        return {
            "index": node.index,
            "text": node.text,
            "content_desc": node.content_desc,
            "resource_id": node.resource_id,
            "role": node.role,
            "bounds": node.bounds.as_tuple() if node.bounds else None,
            "score": round(match.score, 3),
            "reason": match.reason,
        }
