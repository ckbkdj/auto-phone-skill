"""Shared, fail-closed limits for public JSON contracts (not private credentials)."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

MAX_REQUEST_BYTES = 262_144
MAX_RESPONSE_BYTES = 2_000_000
MAX_JSON_DEPTH = 16
PRIVATE_KEYS = frozenset({
    "appiumurl", "udid", "devicename", "systemport", "capabilities", "bridgetoken",
    "adbhost", "adbport", "adbaddress", "password", "passwd", "apikey", "apitoken",
    "accesstoken", "authorization", "privatekey", "secret", "secrets", "token",
})
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
PACKAGE_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$"


def validate_json_tree(
    value: Any, *, max_depth: int = MAX_JSON_DEPTH, max_nodes: int = 20_000,
    forbid_private: bool = False,
) -> Any:
    """Reject non-JSON values, non-finite numbers, deep trees and private extension keys."""
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > max_nodes or depth > max_depth:
            raise ValueError("JSON structure limit exceeded")
        if item is None or isinstance(item, (str, bool, int)):
            if isinstance(item, str) and any(ord(c) < 32 and c not in "\t\n\r" for c in item):
                raise ValueError("JSON string contains unsupported control characters")
            if isinstance(item, str):
                try:
                    item.encode("utf-8", errors="strict")
                except UnicodeError as exc:
                    raise ValueError("JSON string is not valid Unicode") from exc
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite numbers are forbidden")
            continue
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise ValueError("JSON keys must be nonempty strings of at most 128 characters")
                normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
                if forbid_private and normalized in PRIVATE_KEYS:
                    raise ValueError("private connection or credential field in public extension")
                stack.append((child, depth + 1))
            continue
        if isinstance(item, (list, tuple)):
            stack.extend((child, depth + 1) for child in item)
            continue
        raise ValueError("non-JSON value is forbidden")
    return value


def bounded_extension(value: Any, *, max_bytes: int = 16_384) -> Any:
    validate_json_tree(value, max_depth=8, max_nodes=2048, forbid_private=True)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > max_bytes:
        raise ValueError("public extension exceeds byte limit")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object keys are forbidden")
        result[key] = value
    return result


def strict_json_object(raw: str | bytes, *, max_bytes: int = MAX_RESPONSE_BYTES) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise ValueError("JSON body exceeds byte limit")
        raw = raw.decode("utf-8", errors="strict")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > max_bytes:
        raise ValueError("JSON body exceeds byte limit")
    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite numbers are forbidden")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("JSON structure limit exceeded") from exc
    if not isinstance(value, dict):
        raise ValueError("body must contain exactly one JSON object")
    return validate_json_tree(value)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, str_max_length=4096,
        revalidate_instances="always",
    )

    @field_validator("*", mode="before")
    @classmethod
    def bounded_maps(cls, value: Any, info: ValidationInfo) -> Any:
        if info.field_name in {"metadata", "context"}:
            bounded_extension(value)
        elif info.field_name in {"data", "result", "selected_node", "confirmation"} and value is not None:
            validate_json_tree(value)
        return value


def action_schema_rules(schema: dict[str, Any]) -> None:
    """Mirror action-specific runtime checks in exported JSON Schema."""
    rules: list[dict[str, Any]] = []
    def rule(actions: list[str], then: dict[str, Any]) -> None:
        rules.append({"if": {"properties": {"action": {"enum": actions}}, "required": ["action"]},
                      "then": then})
    rule(["type", "launch_app"], {"required": ["value"], "properties": {"value": {"type": "string", "maxLength": 4000}}})
    rule(["launch_app"], {"properties": {"value": {"minLength": 1, "maxLength": 255}}})
    rule(["wait"], {"required": ["value"], "properties": {"value": {"type": "integer", "minimum": 0, "maximum": 60000}}})
    rule(["tap", "type", "clear"], {"required": ["target"], "properties": {"target": {"type": "object"}}})
    rule(["tap", "clear", "swipe", "back", "home", "assert"], {"properties": {"value": {"type": "null"}}})
    rule(["swipe"], {"required": ["direction"], "properties": {"direction": {"type": "string"}}})
    rule(["launch_app", "swipe", "back", "home", "wait", "handoff", "finish"], {"properties": {"target": {"type": "null"}}})
    rule(["launch_app", "tap", "type", "clear", "back", "home", "wait", "assert", "handoff", "finish"], {"properties": {"direction": {"type": "null"}}})
    rule(["handoff"], {"properties": {"value": {"type": ["string", "null"]}}})
    rule(["finish"], {"properties": {"value": {"type": ["object", "string", "null"]}}})
    schema["allOf"] = rules
    schema["properties"]["metadata"] = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "screen_fingerprint": {"type": "string", "minLength": 1, "maxLength": 128},
            **{key: {"type": "boolean"} for key in (
                "clear_first", "require_screen_change", "allow_noop", "allow_scroll_search"
            )},
            "scroll_direction": {"enum": ["up", "down", "left", "right"]},
        },
    }
