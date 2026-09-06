from __future__ import annotations

import json
from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState

from lobster_phone_agent.agent.service import TaskService
from lobster_phone_agent.schemas import TaskRequest, TaskStatus, TaskRecord
from lobster_phone_agent.contracts import MAX_REQUEST_BYTES, strict_json_object
from lobster_phone_agent.api.contracts import ErrorDetail, ErrorResponse
from uuid import uuid4


class PhoneControlA2AExecutor(AgentExecutor):
    """A2A bridge. The request text is a TaskRequest JSON envelope."""

    def __init__(self, service: TaskService, *, wait_timeout_seconds: int = 900) -> None:
        self.service = service
        self.wait_timeout_seconds = wait_timeout_seconds
        self._task_map: dict[str, str] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id,
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Phone agent is planning the task."),
        )

        query = get_message_text(context.message)
        try:
            payload = strict_json_object(query or "", max_bytes=MAX_REQUEST_BYTES)
            request = TaskRequest.model_validate(payload)
        except Exception as exc:
            await updater.add_artifact(
                parts=[
                    new_text_part(
                        text=ErrorResponse(error=ErrorDetail(
                            code="INVALID_REQUEST",
                            message="A2A payload does not match the task input contract.",
                            request_id=uuid4().hex,
                        )).model_dump_json(),
                        media_type="application/json",
                    )
                ]
            )
            await updater.update_status(
                state=TaskState.TASK_STATE_FAILED,
                message=new_text_message("Invalid phone-agent request."),
            )
            return

        record = await self.service.submit(request)
        self._task_map[task.id] = record.id
        record = await self.service.wait(record.id, timeout=self.wait_timeout_seconds)
        await updater.add_artifact(
            parts=[
                new_text_part(
                    text=TaskRecord.model_validate(record).model_dump_json(),
                    media_type="application/json",
                )
            ]
        )
        if record.status is TaskStatus.SUCCEEDED:
            state = TaskState.TASK_STATE_COMPLETED
            message = "Phone task completed."
        elif record.status in {
            TaskStatus.WAITING_CONFIRMATION,
            TaskStatus.WAITING_HANDOFF,
            TaskStatus.RUNNING,
            TaskStatus.PLANNING,
        }:
            state = TaskState.TASK_STATE_INPUT_REQUIRED
            message = (
                "Phone task needs confirmation, handoff, or more time. "
                "Use the REST task endpoints."
            )
        elif record.status is TaskStatus.CANCELLED:
            state = TaskState.TASK_STATE_CANCELED
            message = "Phone task was cancelled."
        else:
            state = TaskState.TASK_STATE_FAILED
            message = record.error or "Phone task failed."
        await updater.update_status(state=state, message=new_text_message(message))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.current_task:
            return
        phone_task_id = self._task_map.get(context.current_task.id)
        if phone_task_id:
            await self.service.cancel(phone_task_id)
