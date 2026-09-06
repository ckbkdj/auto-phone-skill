from __future__ import annotations

import asyncio
from time import monotonic

from lobster_phone_agent.device.base import DeviceAdapter
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.device.ui import ScreenSnapshot
from lobster_phone_agent.schemas import ConditionKind, ScreenCondition


class ConditionEvaluator:
    def __init__(self, matcher: SemanticMatcher, *, poll_ms: int = 160) -> None:
        self.matcher = matcher
        self.poll_ms = max(20, poll_ms)

    def evaluate(
        self,
        condition: ScreenCondition,
        snapshot: ScreenSnapshot,
        *,
        previous: ScreenSnapshot | None = None,
    ) -> bool:
        kind = condition.kind
        value = condition.value or ""
        if kind is ConditionKind.TEXT_PRESENT:
            return snapshot.contains_text(value)
        if kind is ConditionKind.TEXT_ABSENT:
            return not snapshot.contains_text(value)
        if kind is ConditionKind.ELEMENT_PRESENT:
            return bool(condition.target and self.matcher.best(snapshot, condition.target))
        if kind is ConditionKind.ELEMENT_ABSENT:
            return not bool(condition.target and self.matcher.best(snapshot, condition.target))
        if kind is ConditionKind.PACKAGE_IS:
            return snapshot.package == value
        if kind is ConditionKind.PACKAGE_CONTAINS:
            return value.lower() in snapshot.package.lower()
        if kind is ConditionKind.ACTIVITY_CONTAINS:
            return value.lower() in snapshot.activity.lower()
        if kind is ConditionKind.SCREEN_CHANGED:
            return previous is not None and snapshot.fingerprint != previous.fingerprint
        if kind is ConditionKind.KEYBOARD_VISIBLE:
            expected = value.lower() not in {"false", "0", "no"}
            return snapshot.keyboard_visible is expected
        if kind is ConditionKind.ANY:
            return any(
                self.evaluate(item, snapshot, previous=previous)
                for item in condition.children
            )
        if kind is ConditionKind.ALL:
            return all(
                self.evaluate(item, snapshot, previous=previous)
                for item in condition.children
            )
        return False

    def all_met(
        self,
        conditions: list[ScreenCondition],
        snapshot: ScreenSnapshot,
        *,
        previous: ScreenSnapshot | None = None,
    ) -> bool:
        return all(
            self.evaluate(condition, snapshot, previous=previous)
            for condition in conditions
        )

    async def wait(
        self,
        device: DeviceAdapter,
        conditions: list[ScreenCondition],
        *,
        timeout_ms: int,
        previous: ScreenSnapshot | None = None,
        poll_ms: int | None = None,
    ) -> ScreenSnapshot:
        deadline = monotonic() + timeout_ms / 1000.0
        last = await device.snapshot()
        while True:
            if self.all_met(conditions, last, previous=previous):
                return last
            if monotonic() >= deadline:
                return last
            await asyncio.sleep((poll_ms or self.poll_ms) / 1000.0)
            last = await device.snapshot()
