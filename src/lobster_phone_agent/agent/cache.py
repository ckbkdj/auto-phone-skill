from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic

from lobster_phone_agent.schemas import ActionPlan


@dataclass(slots=True)
class CacheEntry:
    plan: ActionPlan
    expires_at: float


class PlanCache:
    """Small in-memory LRU cache for normalized, reusable semantic plans."""

    def __init__(self, *, max_entries: int = 512, ttl_seconds: int = 3600) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> ActionPlan | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= monotonic():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry.plan.model_copy(deep=True)

    async def put(self, key: str, plan: ActionPlan) -> None:
        async with self._lock:
            self._entries[key] = CacheEntry(
                plan=plan.model_copy(deep=True),
                expires_at=monotonic() + self.ttl_seconds,
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
