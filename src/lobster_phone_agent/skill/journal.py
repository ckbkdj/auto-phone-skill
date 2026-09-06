"""Durable intent-before-effect log. Unknown outcomes require local reconciliation."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from lobster_phone_agent.errors import BridgeProtocolError, ExecutionError


class OperationJournal:
    """At-most-once semantic dispatch, not exactly-once business completion.

    The journal stores only identifiers, a request digest and an outcome. Keep this file on
    persistent private storage. A NULL outcome fences the entire device, including new IDs.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (device TEXT, operation TEXT, digest TEXT NOT NULL, outcome TEXT, PRIMARY KEY(device,operation))")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def digest(payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def lookup(self, device: str, operation: str, digest: str) -> str | None:
        with self.connection() as db:
            row = db.execute("SELECT digest,outcome FROM operations WHERE device=? AND operation=?", (device, operation)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise BridgeProtocolError("operation_id conflicts with its recorded request")
        return row[1] or "unknown"

    def blocked(self, device: str) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM operations WHERE device=? AND outcome IS NULL LIMIT 1", (device,)).fetchone() is not None

    def reserve(self, device: str, operation: str, digest: str) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM operations WHERE device=? AND outcome IS NULL LIMIT 1", (device,)).fetchone():
                return False
            return db.execute("INSERT OR IGNORE INTO operations VALUES(?,?,?,NULL)", (device, operation, digest)).rowcount == 1

    def finish(self, device: str, operation: str) -> None:
        with self.connection() as db:
            if db.execute("UPDATE operations SET outcome='executed' WHERE device=? AND operation=? AND outcome IS NULL", (device, operation)).rowcount != 1:
                raise ExecutionError("operation journal state conflict")

    def reconcile(self, device: str, operation: str, *, executed: bool) -> None:
        # No HTTP/MCP/LLM route calls this. Stop Skill and inspect real phone/business state first.
        with self.connection() as db:
            if db.execute("UPDATE operations SET outcome=? WHERE device=? AND operation=? AND outcome IS NULL", ("executed" if executed else "not_executed", device, operation)).rowcount != 1:
                raise ValueError("operation is absent or already resolved")
