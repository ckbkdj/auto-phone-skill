from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass

from lobster_phone_agent.device.matcher import MatchResult
from lobster_phone_agent.device.ui import ScreenSnapshot
from lobster_phone_agent.schemas import SemanticTarget


@dataclass(slots=True, frozen=True)
class SelectorEntry:
    node_stable_key: str
    score: float
    reason: str


class SelectorMemory:
    """Caches a successful semantic selector only for an exact screen fingerprint."""

    def __init__(self, max_entries: int = 4096) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, SelectorEntry] = OrderedDict()

    def recall(
        self,
        snapshot: ScreenSnapshot,
        target: SemanticTarget,
    ) -> MatchResult | None:
        key = self._key(snapshot, target)
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        for node in snapshot.nodes:
            if node.stable_key == entry.node_stable_key and node.displayed and node.enabled:
                return MatchResult(node=node, score=entry.score, reason=f"memory, {entry.reason}")
        self._entries.pop(key, None)
        return None

    def remember(
        self,
        snapshot: ScreenSnapshot,
        target: SemanticTarget,
        match: MatchResult,
    ) -> None:
        key = self._key(snapshot, target)
        self._entries[key] = SelectorEntry(
            node_stable_key=match.node.stable_key,
            score=match.score,
            reason=match.reason,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    @staticmethod
    def _key(snapshot: ScreenSnapshot, target: SemanticTarget) -> str:
        raw = json.dumps(
            {
                "package": snapshot.package,
                "fingerprint": snapshot.fingerprint,
                "target": target.model_dump(mode="json", exclude_none=True),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()
