from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lobster_phone_agent.agent.executor import ActionExecutor, ExecutionHooks
from lobster_phone_agent.agent.risk import RiskDecision
from lobster_phone_agent.agent.store import InMemoryTaskStore
from lobster_phone_agent.config import Settings
from lobster_phone_agent.device.pool import DeviceSessionPool
from lobster_phone_agent.errors import (
    ConfirmationRejected,
    InvalidTaskState,
    TaskCancelled,
    TaskNotFound,
)
from lobster_phone_agent.llm.planner import HybridPlanner
from lobster_phone_agent.schemas import (
    ActionStep,
    ConfirmationRequest,
    EventType,
    HandoffResumeRequest,
    StepTrace,
    TaskRecord,
    TaskRequest,
    TaskStatus,
)
from lobster_phone_agent.util.text import redact_text


@dataclass(slots=True)
class PendingGate:
    token: str
    future: asyncio.Future[bool]


class TaskService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: InMemoryTaskStore,
        pool: DeviceSessionPool,
        planner: HybridPlanner,
        executor: ActionExecutor,
    ) -> None:
        self.settings = settings
        self.store = store
        self.pool = pool
        self.planner = planner
        self.executor = executor
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._confirmations: dict[str, PendingGate] = {}
        self._handoffs: dict[str, PendingGate] = {}
        self._cancelled: set[str] = set()
        self._gate_lock = asyncio.Lock()

    async def submit(self, request: TaskRequest) -> TaskRecord:
        record, created = await self.store.create(TaskRecord(request=request))
        if not created:
            return record
        await self.store.emit(
            record.id,
            EventType.TASK_CREATED,
            message="task accepted",
            data={"device_id": request.device.id},
        )
        worker = asyncio.create_task(self._run(record.id), name=f"phone-agent:{record.id}")
        self._workers[record.id] = worker
        worker.add_done_callback(lambda _task, task_id=record.id: self._workers.pop(task_id, None))
        return await self.store.get(record.id)

    async def get(self, task_id: str) -> TaskRecord:
        return await self.store.get(task_id)

    async def wait(self, task_id: str, *, timeout: float | None = None) -> TaskRecord:
        worker = self._workers.get(task_id)
        if worker is not None:
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)
            except TimeoutError:
                return await self.store.get(task_id)
        return await self.store.get(task_id)

    async def confirm(self, task_id: str, request: ConfirmationRequest) -> TaskRecord:
        gate = await self._get_gate(self._confirmations, task_id, TaskStatus.WAITING_CONFIRMATION)
        if not secrets.compare_digest(request.token, gate.token):
            raise InvalidTaskState("invalid confirmation token")
        if not gate.future.done():
            gate.future.set_result(request.approved)
        return await self.store.get(task_id)

    async def resume_handoff(
        self,
        task_id: str,
        request: HandoffResumeRequest,
    ) -> TaskRecord:
        gate = await self._get_gate(self._handoffs, task_id, TaskStatus.WAITING_HANDOFF)
        if not secrets.compare_digest(request.token, gate.token):
            raise InvalidTaskState("invalid handoff token")
        if not gate.future.done():
            gate.future.set_result(request.resume)
        return await self.store.get(task_id)

    async def cancel(self, task_id: str) -> TaskRecord:
        record = await self.store.get(task_id)
        if record.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            return record
        self._cancelled.add(task_id)
        async with self._gate_lock:
            for gates in (self._confirmations, self._handoffs):
                gate = gates.get(task_id)
                if gate and not gate.future.done():
                    gate.future.set_result(False)
        worker = self._workers.get(task_id)
        if worker and not worker.done():
            worker.cancel()
        await self.store.mutate(
            task_id,
            lambda record: self._set_terminal(record, TaskStatus.CANCELLED, error="cancelled"),
        )
        await self.store.emit(task_id, EventType.TASK_CANCELLED, message="task cancelled")
        return await self.store.get(task_id)

    async def shutdown(self) -> None:
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        await self.pool.close_all()

    async def _run(self, task_id: str) -> None:
        try:
            record = await self.store.get(task_id)
            descriptor = record.request.device
            if not self.pool.hub.is_connected(descriptor.bridge_id, descriptor.id):
                await self.store.mutate(
                    task_id,
                    lambda item: setattr(item, "status", TaskStatus.WAITING_BRIDGE),
                )
                await self.store.emit(
                    task_id,
                    EventType.BRIDGE_WAITING,
                    message="waiting for the private Lobster skill bridge",
                    data={"bridge_id": descriptor.bridge_id, "device_id": descriptor.id},
                )
            await self.pool.hub.wait_connected(
                descriptor.bridge_id,
                descriptor.id,
                timeout=self.settings.bridge_task_wait_seconds,
            )
            await self.store.emit(
                task_id,
                EventType.BRIDGE_CONNECTED,
                message="private skill bridge is connected",
                data={"bridge_id": descriptor.bridge_id, "device_id": descriptor.id},
            )
            await self.store.mutate(
                task_id,
                lambda item: setattr(item, "status", TaskStatus.PLANNING),
            )
            async with self.pool.lease(descriptor) as device:
                snapshot = await device.snapshot()
                installed_apps = await device.list_apps()

                hooks = ExecutionHooks(
                    emit=lambda event_type, message, data: self.store.emit(
                        task_id, event_type, message=message, data=data
                    ),
                    confirm=lambda step, decision: self._await_confirmation(
                        task_id, step, decision
                    ),
                    handoff=lambda code, reason: self._await_handoff(task_id, code, reason),
                    trace=lambda trace: self._append_trace(task_id, trace),
                    set_step_index=lambda index: self._set_step_index(task_id, index),
                    is_cancelled=lambda: task_id in self._cancelled,
                )
                if hasattr(self.executor, "execute_live"):
                    async def on_decision(plan):
                        await self.store.mutate(task_id, lambda item: self._set_plan(item, plan))
                        await self.store.emit(
                            task_id, EventType.PLAN_READY, message="single action ready",
                            data={"mode": "single_step", "steps": 1, "round": plan.metadata["round"]},
                        )
                    result = await self.executor.execute_live(
                        request=record.request, device=device, hooks=hooks,
                        snapshot=snapshot, installed_apps=installed_apps, on_decision=on_decision,
                    )
                else:
                    plan = await self.planner.plan(
                        record.request,
                        snapshot=snapshot,
                        installed_apps=installed_apps,
                    )
                    await self.store.mutate(task_id, lambda item: self._set_plan(item, plan))
                    await self.store.emit(
                        task_id,
                        EventType.PLAN_READY,
                        message=f"{plan.planner} plan ready",
                        data={
                            "planner": plan.planner,
                            "steps": len(plan.steps),
                            "target_app": plan.target_app,
                            "target_package": plan.target_package,
                            "cache_hit": bool(plan.metadata.get("cache_hit")),
                        },
                    )
    
                    result = await self.executor.execute(
                        request=record.request,
                        plan=plan,
                        device=device,
                        hooks=hooks,
                    )
                await self.store.mutate(
                    task_id,
                    lambda item: self._set_terminal(
                        item,
                        TaskStatus.SUCCEEDED,
                        result=result,
                    ),
                )
                await self.store.emit(
                    task_id,
                    EventType.TASK_SUCCEEDED,
                    message="task completed",
                    data=result,
                )
        except asyncio.CancelledError:
            if task_id not in self._cancelled:
                self._cancelled.add(task_id)
                try:
                    await self.store.mutate(
                        task_id,
                        lambda item: self._set_terminal(
                            item, TaskStatus.CANCELLED, error="cancelled"
                        ),
                    )
                    await self.store.emit(
                        task_id,
                        EventType.TASK_CANCELLED,
                        message="task cancelled",
                    )
                except TaskNotFound:
                    pass
        except TaskCancelled:
            await self.store.mutate(
                task_id,
                lambda item: self._set_terminal(item, TaskStatus.CANCELLED, error="cancelled"),
            )
            await self.store.emit(task_id, EventType.TASK_CANCELLED, message="task cancelled")
        except Exception as exc:
            message = redact_text(str(exc), 1000)
            await self.store.mutate(
                task_id,
                lambda item: self._set_terminal(item, TaskStatus.FAILED, error=message),
            )
            await self.store.emit(
                task_id,
                EventType.TASK_FAILED,
                message=message,
                data={"error_type": type(exc).__name__},
            )
        finally:
            await self._clear_gates(task_id)
            self._cancelled.discard(task_id)
            try:
                await self._write_artifact(await self.store.get(task_id))
            except Exception:
                pass

    async def _await_confirmation(
        self,
        task_id: str,
        step: ActionStep,
        decision: RiskDecision,
    ) -> None:
        token = secrets.token_urlsafe(18)
        loop = asyncio.get_running_loop()
        gate = PendingGate(token=token, future=loop.create_future())
        async with self._gate_lock:
            self._confirmations[task_id] = gate
        confirmation = {
            "type": "risk_confirmation",
            "token": token,
            "step_id": step.id,
            "risk": decision.risk.value,
            "message": step.confirmation_text or decision.reason,
        }
        await self.store.mutate(
            task_id,
            lambda record: self._set_waiting(
                record,
                TaskStatus.WAITING_CONFIRMATION,
                confirmation,
            ),
        )
        await self.store.emit(
            task_id,
            EventType.CONFIRMATION_REQUIRED,
            message=str(confirmation["message"]),
            data=confirmation,
        )
        try:
            approved = await asyncio.wait_for(
                gate.future,
                timeout=self.settings.confirmation_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ConfirmationRejected("confirmation timed out") from exc
        finally:
            async with self._gate_lock:
                self._confirmations.pop(task_id, None)
        if not approved:
            raise ConfirmationRejected("user rejected the high-risk action")
        await self.store.mutate(task_id, self._resume_running)

    async def _await_handoff(self, task_id: str, code: str, reason: str) -> None:
        token = secrets.token_urlsafe(18)
        loop = asyncio.get_running_loop()
        gate = PendingGate(token=token, future=loop.create_future())
        async with self._gate_lock:
            self._handoffs[task_id] = gate
        handoff = {
            "type": "human_handoff",
            "token": token,
            "code": code,
            "message": reason,
        }
        await self.store.mutate(
            task_id,
            lambda record: self._set_waiting(record, TaskStatus.WAITING_HANDOFF, handoff),
        )
        await self.store.emit(
            task_id,
            EventType.HANDOFF_REQUIRED,
            message=reason,
            data=handoff,
        )
        try:
            resume = await asyncio.wait_for(
                gate.future,
                timeout=self.settings.confirmation_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ConfirmationRejected("human handoff timed out") from exc
        finally:
            async with self._gate_lock:
                self._handoffs.pop(task_id, None)
        if not resume:
            raise ConfirmationRejected("human handoff was not resumed")
        await self.store.mutate(task_id, self._resume_running)

    async def _get_gate(
        self,
        gates: dict[str, PendingGate],
        task_id: str,
        expected_status: TaskStatus,
    ) -> PendingGate:
        record = await self.store.get(task_id)
        if record.status is not expected_status:
            raise InvalidTaskState(
                f"task is {record.status.value}, expected {expected_status.value}"
            )
        async with self._gate_lock:
            gate = gates.get(task_id)
            if gate is None:
                raise InvalidTaskState("task gate is no longer active")
            return gate

    async def _clear_gates(self, task_id: str) -> None:
        async with self._gate_lock:
            self._confirmations.pop(task_id, None)
            self._handoffs.pop(task_id, None)

    async def _append_trace(self, task_id: str, trace: StepTrace) -> None:
        await self.store.mutate(task_id, lambda record: record.traces.append(trace))

    async def _set_step_index(self, task_id: str, index: int) -> None:
        await self.store.mutate(
            task_id,
            lambda record: self._mark_running_step(record, index),
        )

    async def _write_artifact(self, record: TaskRecord) -> None:
        path = Path(self.settings.artifact_dir)
        path.mkdir(parents=True, exist_ok=True)
        # Context can contain tokens or personal data. Keep it out of persisted diagnostics.
        payload: dict[str, Any] = record.model_dump(mode="json")
        payload["request"]["context"] = {"redacted": True}
        payload["request"]["instruction"] = redact_text(record.request.instruction, 500)
        target = path / f"{record.id}.json"
        await asyncio.to_thread(
            target.write_text,
            json.dumps(payload, ensure_ascii=False, indent=2),
            "utf-8",
        )

    @staticmethod
    def _set_plan(record: TaskRecord, plan: Any) -> None:
        record.plan = plan
        record.status = TaskStatus.RUNNING

    @staticmethod
    def _mark_running_step(record: TaskRecord, index: int) -> None:
        record.current_step = index
        record.status = TaskStatus.RUNNING

    @staticmethod
    def _set_waiting(
        record: TaskRecord,
        status: TaskStatus,
        confirmation: dict[str, Any],
    ) -> None:
        record.status = status
        record.confirmation = confirmation

    @staticmethod
    def _resume_running(record: TaskRecord) -> None:
        record.status = TaskStatus.RUNNING
        record.confirmation = None

    @staticmethod
    def _set_terminal(
        record: TaskRecord,
        status: TaskStatus,
        *,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        record.status = status
        record.result = result
        record.error = error
        record.confirmation = None
