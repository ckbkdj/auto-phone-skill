from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Path as ApiPath, Query, Request, status
from lobster_phone_agent.api.sse import EventSourceResponse

from lobster_phone_agent import __version__
from lobster_phone_agent.api.contracts import ERROR_RESPONSES
from lobster_phone_agent.agent.service import TaskService
from lobster_phone_agent.apps.registry import AppRegistry
from lobster_phone_agent.bridge.hub import BridgeHub
from lobster_phone_agent.config import Settings
from lobster_phone_agent.errors import InvalidTaskState, PhoneAgentError, TaskNotFound
from lobster_phone_agent.schemas import (
    ActionPlan, AppCatalogResponse, EmptyRequest, ReadyResponse,
    ConfirmationRequest,
    EventType,
    HandoffResumeRequest,
    HealthResponse,
    TaskRecord,
    TaskRequest,
)

TaskId = Annotated[str, ApiPath(pattern=r"^[a-f0-9]{32}$")]

_TERMINAL_EVENTS = {
    EventType.TASK_SUCCEEDED,
    EventType.TASK_FAILED,
    EventType.TASK_CANCELLED,
}


def create_router(
    *,
    settings: Settings,
    service: TaskService,
    registry: AppRegistry,
    hub: BridgeHub,
    auth_dependency,
) -> APIRouter:
    router = APIRouter(responses=ERROR_RESPONSES)
    protected = [Depends(auth_dependency)]

    @router.get("/healthz", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            version=__version__,
            llm_enabled=settings.llm_enabled,
            active_tasks=service.store.active_count,
            active_devices=service.pool.active_count,
            connected_bridges=hub.connected_count,
        )

    @router.get("/readyz", response_model=ReadyResponse)
    async def ready() -> dict[str, object]:
        return {"ready": True, "llm_enabled": settings.llm_enabled}

    @router.post(
        "/v1/tasks",
        response_model=TaskRecord,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=protected,
    )
    async def create_task(payload: TaskRequest) -> TaskRecord:
        return await service.submit(payload)

    @router.post(
        "/v1/execute",
        response_model=TaskRecord,
        dependencies=protected,
    )
    async def execute_task(
        payload: TaskRequest,
        wait_seconds: Annotated[float, Query(ge=0, le=600, allow_inf_nan=False)] = 30,
    ) -> TaskRecord:
        record = await service.submit(payload)
        if wait_seconds <= 0:
            return record
        return await service.wait(record.id, timeout=wait_seconds)

    @router.get(
        "/v1/tasks/{task_id}",
        response_model=TaskRecord,
        dependencies=protected,
    )
    async def get_task(task_id: TaskId) -> TaskRecord:
        return await service.get(task_id)

    @router.get("/v1/tasks/{task_id}/events", dependencies=protected)
    async def task_events(task_id: TaskId, request: Request) -> EventSourceResponse:
        await service.get(task_id)

        async def stream():
            async for event in service.store.subscribe(task_id, replay=True):
                if await request.is_disconnected():
                    return
                yield {
                    "id": event.id,
                    "event": event.type.value,
                    "data": json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
                }
                if event.type in _TERMINAL_EVENTS:
                    return

        return EventSourceResponse(stream(), ping=15)

    @router.post(
        "/v1/tasks/{task_id}/confirm",
        response_model=TaskRecord,
        dependencies=protected,
    )
    async def confirm_task(task_id: TaskId, payload: ConfirmationRequest) -> TaskRecord:
        return await service.confirm(task_id, payload)

    @router.post(
        "/v1/tasks/{task_id}/resume",
        response_model=TaskRecord,
        dependencies=protected,
    )
    async def resume_task(task_id: TaskId, payload: HandoffResumeRequest) -> TaskRecord:
        return await service.resume_handoff(task_id, payload)

    @router.post(
        "/v1/tasks/{task_id}/cancel",
        response_model=TaskRecord,
        dependencies=protected,
    )
    async def cancel_task(task_id: TaskId, payload: EmptyRequest | None = None) -> TaskRecord:
        return await service.cancel(task_id)

    @router.get("/v1/bridges", dependencies=protected)
    async def list_bridges() -> dict[str, object]:
        return hub.public_status()

    @router.get("/v1/apps", response_model=AppCatalogResponse, dependencies=protected)
    async def list_apps() -> dict[str, object]:
        return {
            "count": len(registry.records),
            "apps": [record.model_dump(mode="json") for record in registry.records],
            "note": (
                "This is the built-in alias seed. Task-level catalogs and "
                "installed-device apps are merged dynamically."
            ),
        }

    @router.get("/v1/contracts/action-plan", dependencies=protected)
    async def action_plan_schema() -> dict[str, object]:
        return ActionPlan.model_json_schema()

    @router.get("/v1/contracts/next-decision", dependencies=protected)
    async def next_decision_schema() -> dict[str, object]:
        from lobster_phone_agent.stepwise.models import Decision
        return Decision.model_json_schema()

    return router


def install_error_handlers(app) -> None:
    from lobster_phone_agent.api.contracts import install_contract_handlers
    install_contract_handlers(app)
