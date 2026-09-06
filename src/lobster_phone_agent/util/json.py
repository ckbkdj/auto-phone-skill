from __future__ import annotations

from typing import Any

from lobster_phone_agent.contracts import strict_json_object


def extract_json_object(text: str) -> dict[str, Any]:
    """Compatibility name; deliberately DOES NOT extract from fences or prose anymore."""
    return strict_json_object(text, max_bytes=262_144)
