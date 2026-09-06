"""Private execution boundary: current-screen checks and durable uncertain-outcome fencing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from lobster_phone_agent.agent.dialogs import CommonDialogHandler
from lobster_phone_agent.device.matcher import SemanticMatcher
from lobster_phone_agent.errors import BridgeProtocolError
from lobster_phone_agent.stepwise.models import AtomicRequest, AtomicResult


class OperationJournal:
    """Write intent before effect; never replay an unresolved intent after a restart.

    This is at-most-once dispatch, NOT exactly-once business completion. Do not remove this
    database until all recorded uncertain operations have been reconciled by an operator.
    """

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (device TEXT, operation TEXT, digest TEXT NOT NULL, result TEXT, PRIMARY KEY(device,operation))")
        os.chmod(path, 0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def digest(request: AtomicRequest) -> str:
        raw = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def lookup(self, device: str, request: AtomicRequest) -> AtomicResult | None:
        with self._connect() as db:
            row = db.execute("SELECT digest,result FROM operations WHERE device=? AND operation=?",
                             (device, request.operation_id)).fetchone()
        if row is None:
            return None
        if row[0] != self.digest(request):
            raise BridgeProtocolError("operation identity conflicts with prior input")
        return (AtomicResult.model_validate_json(row[1]) if row[1]
                else AtomicResult(state="unknown", code="execution_uncertain"))

    def blocked(self, device: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM operations WHERE device=? AND (result IS NULL OR result LIKE '%\"unknown\"%') LIMIT 1", (device,)).fetchone() is not None

    def reserve(self, device: str, request: AtomicRequest) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM operations WHERE device=? AND (result IS NULL OR result LIKE '%\"unknown\"%') LIMIT 1", (device,)).fetchone():
                return False
            return db.execute("INSERT OR IGNORE INTO operations VALUES(?,?,?,NULL)",
                              (device, request.operation_id, self.digest(request))).rowcount == 1

    def reconcile(self, device: str, operation: str, *, executed: bool):
        # Local operator only: no HTTP/MCP/LLM route can call this method.
        result = AtomicResult(state="executed", code="ok") if executed else AtomicResult(state="not_executed", code="reconciled_no_effect")
        with self._connect() as db:
            if db.execute("UPDATE operations SET result=? WHERE device=? AND operation=?", (result.model_dump_json(), device, operation)).rowcount != 1:
                raise ValueError("operation does not exist")


    def finish(self, device: str, request: AtomicRequest, result: AtomicResult):
        with self._connect() as db:
            db.execute("UPDATE operations SET result=? WHERE device=? AND operation=?",
                       (result.model_dump_json(), device, request.operation_id))


class AtomicDispatcher:
    def __init__(self, pool, journal: OperationJournal):
        self.pool, self.journal = pool, journal
        self.dialogs = CommonDialogHandler(SemanticMatcher())
        self._locks = {key: asyncio.Lock() for key in pool.device_ids}

    async def dispatch(self, device_id: str, payload: dict) -> dict:
        request = AtomicRequest.model_validate(payload)
        if device_id not in self._locks:
            raise BridgeProtocolError("unknown private device")
        async with self._locks[device_id]:
            saved = self.journal.lookup(device_id, request)
            if saved:
                return saved.model_dump(mode="json")
            if self.journal.blocked(device_id):
                return AtomicResult(state="unknown", code="execution_uncertain").model_dump()
            async with self.pool.lease(device_id) as device:
                current = await device.snapshot()
                if current.fingerprint != request.fingerprint:
                    return AtomicResult(state="not_executed", code="stale_screen").model_dump()
                if self.dialogs.detect_blocking(current) or any(n.password and n.displayed for n in current.nodes):
                    return AtomicResult(state="not_executed", code="blocked_surface").model_dump()
                action = request.action
                node = next((n for n in current.nodes if n.index == action.node_id), None)
                if action.node_id is not None:
                    valid = (node is not None and node.enabled and node.displayed and not node.password
                             and node.bounds and node.bounds.intersects(current.width, current.height))
                    if not valid or (action.kind == "tap" and not node.clickable) or (
                        action.kind in {"type", "clear"} and not node.editable
                    ):
                        return AtomicResult(state="not_executed", code="invalid_target").model_dump()
                if not self.journal.reserve(device_id, request):
                    return (self.journal.lookup(device_id, request) or AtomicResult(state="unknown", code="execution_uncertain")).model_dump(mode="json")
                try:
                    op = request.operation_id
                    if action.kind == "tap":
                        await device.tap(*node.bounds.clamped_center(current.width, current.height), operation_id=op)
                    elif action.kind == "type":
                        # One semantic replacement, no blind append or locator guessing.
                        await device.type_text(action.text, clear=True, element=node.interaction_payload(), operation_id=op)
                    elif action.kind == "clear":
                        await device.clear_active(element=node.interaction_payload(), operation_id=op)
                    elif action.kind == "launch_app":
                        await device.launch_app(action.package, operation_id=op)
                    elif action.kind == "swipe":
                        await device.swipe(action.direction, operation_id=op)
                    elif action.kind == "back":
                        await device.back(operation_id=op)
                    elif action.kind == "home":
                        await device.home(operation_id=op)
                    else:
                        await asyncio.sleep(action.wait_ms / 1000)
                except asyncio.CancelledError:
                    # Keep unresolved intent durable. A cancellation is not proof of no effect.
                    raise
                except Exception:
                    result = AtomicResult(state="unknown", code="execution_uncertain")
                else:
                    result = AtomicResult(state="executed", code="ok")
                self.journal.finish(device_id, request, result)
                return result.model_dump(mode="json")
