from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable

from lobster_phone_agent.errors import InvalidTaskState, TaskNotFound
from lobster_phone_agent.schemas import EventType, TaskEvent, TaskRecord, utc_now


class InMemoryTaskStore:
    """Process-local store. Swap this interface for Redis/Postgres in multi-replica deployments."""

    def __init__(self, *, event_history_limit: int = 1000) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._events: dict[str, list[TaskEvent]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[TaskEvent]]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self.event_history_limit = event_history_limit

    @property
    def active_count(self) -> int:
        terminal = {"succeeded", "failed", "cancelled"}
        return sum(record.status.value not in terminal for record in self._tasks.values())

    async def create(self, record: TaskRecord) -> tuple[TaskRecord, bool]:
        async with self._lock:
            key = record.request.idempotency_key
            if key and key in self._idempotency:
                existing = self._tasks[self._idempotency[key]]
                if existing.request.model_dump(mode="json") != record.request.model_dump(mode="json"):
                    raise InvalidTaskState("idempotency_key is already bound to a different request")
                return existing.model_copy(deep=True), False
            self._tasks[record.id] = record
            if key:
                self._idempotency[key] = record.id
            return record.model_copy(deep=True), True

    async def get(self, task_id: str) -> TaskRecord:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFound(f"task not found: {task_id}")
            return record.model_copy(deep=True)

    async def mutate(
        self,
        task_id: str,
        callback: Callable[[TaskRecord], None],
    ) -> TaskRecord:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFound(f"task not found: {task_id}")
            callback(record)
            record.updated_at = utc_now()
            return record.model_copy(deep=True)

    async def emit(
        self,
        task_id: str,
        event_type: EventType,
        *,
        message: str = "",
        data: dict[str, object] | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            task_id=task_id,
            type=event_type,
            message=message,
            data=dict(data or {}),
        )
        async with self._lock:
            if task_id not in self._tasks:
                raise TaskNotFound(f"task not found: {task_id}")
            history = self._events[task_id]
            history.append(event)
            if len(history) > self.event_history_limit:
                del history[: len(history) - self.event_history_limit]
            subscribers = list(self._subscribers[task_id])
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow client must not stall device execution.
                pass
        return event

    async def event_history(self, task_id: str) -> list[TaskEvent]:
        await self.get(task_id)
        async with self._lock:
            return [item.model_copy(deep=True) for item in self._events[task_id]]

    async def subscribe(
        self,
        task_id: str,
        *,
        replay: bool = True,
        queue_size: int = 128,
    ) -> AsyncIterator[TaskEvent]:
        await self.get(task_id)
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue(maxsize=queue_size)
        async with self._lock:
            if replay:
                for event in self._events[task_id][-queue_size:]:
                    queue.put_nowait(event)
            self._subscribers[task_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers[task_id].discard(queue)
