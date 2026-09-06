"""Private guarded RPC: re-observe under the device lease and persist intent before effect."""
from __future__ import annotations

import asyncio

from lobster_phone_agent.agent.dialogs import CommonDialogHandler
from lobster_phone_agent.bridge.contracts import GuardedParams, GuardedResult
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.skill.journal import OperationJournal


class GuardedDispatcher:
    def __init__(self, pool, journal: OperationJournal):
        self.pool, self.journal = pool, journal
        self.locks = {device: asyncio.Lock() for device in pool.device_ids}
        self.dialogs = CommonDialogHandler(SemanticMatcher())

    @staticmethod
    def receipt(state, code):
        return GuardedResult(state=state, code=code).model_dump(mode="json")

    async def dispatch(self, device_id: str, payload: dict) -> dict:
        request = GuardedParams.model_validate(payload)
        if device_id not in self.locks:
            return self.receipt("not_executed", "invalid_target")
        params = request.params
        operation = params["operation_id"]
        digest = self.journal.digest(request.model_dump(mode="json"))
        async with self.locks[device_id]:
            saved = self.journal.lookup(device_id, operation, digest)
            if saved:
                return self.receipt(saved, "ok" if saved == "executed" else "outcome_unknown" if saved == "unknown" else "reconciled_no_effect")
            if self.journal.blocked(device_id):
                return self.receipt("unknown", "outcome_unknown")
            async with self.pool.lease(device_id) as device:
                current = await device.snapshot()
                if current.fingerprint != request.fingerprint:
                    return self.receipt("not_executed", "stale_screen")
                if self.dialogs.detect_blocking(current) or any(n.displayed and n.password for n in current.nodes):
                    return self.receipt("not_executed", "protected_surface")
                method = request.method
                if method == "tap":
                    x, y = params["x"], params["y"]
                    visible = [n for n in current.nodes if n.enabled and n.displayed and n.clickable and not n.password and n.bounds and n.bounds.intersects(current.width, current.height)]
                    if not any(n.bounds.clamped_center(current.width, current.height) == (x, y) and n.bounds.area <= current.width * current.height * .8 for n in visible):
                        return self.receipt("not_executed", "invalid_target")
                elif method in {"type_text", "clear_active"}:
                    hint = params.get("element")
                    if not hint:
                        return self.receipt("not_executed", "invalid_target")
                    node = next((n for n in current.nodes if n.index == hint["index"]), None)
                    if node is None or not node.enabled or not node.displayed or node.password or not node.editable or not node.bounds:
                        return self.receipt("not_executed", "invalid_target")
                    actual = node.interaction_payload()
                    if any(actual.get(k) != v and not (k == "bounds" and tuple(actual[k]) == tuple(v)) for k, v in hint.items()):
                        return self.receipt("not_executed", "invalid_target")
                    if method == "type_text" and params.get("clear") is not True:
                        return self.receipt("not_executed", "invalid_target")
                if not self.journal.reserve(device_id, operation, digest):
                    return self.receipt("unknown", "outcome_unknown")
                try:
                    if method == "tap":
                        await device.tap(params["x"], params["y"], operation_id=operation)
                    elif method == "launch_app":
                        await device.launch_app(params["package"], operation_id=operation)
                    elif method == "type_text":
                        await device.type_text(params["text"], clear=True, element=params["element"], operation_id=operation)
                    elif method == "clear_active":
                        await device.clear_active(element=params["element"], operation_id=operation)
                    elif method == "swipe":
                        await device.swipe(params["direction"], percent=params["percent"], operation_id=operation)
                    elif method == "back":
                        await device.back(operation_id=operation)
                    elif method == "home":
                        await device.home(operation_id=operation)
                    self.journal.finish(device_id, operation)
                except asyncio.CancelledError:
                    # The effect may still be in flight. Keep the durable unresolved intent.
                    raise
                except Exception:
                    return self.receipt("unknown", "outcome_unknown")
                return self.receipt("executed", "ok")
