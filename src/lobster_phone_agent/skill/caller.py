"""Validated loopback-only command client used by the installable Agent Skill."""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import sys
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError

from lobster_phone_agent.api.contracts import ErrorDetail, ErrorResponse
from lobster_phone_agent.contracts import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, strict_json_object
from lobster_phone_agent.schemas import ConfirmationRequest, HandoffResumeRequest, LocalTaskRequest, TaskRecord


def loopback_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("Skill URL must be an HTTP loopback origin")
    # An IP literal avoids DNS resolution of localhost to an unexpected address.
    if not ipaddress.ip_address(parsed.hostname).is_loopback:
        raise ValueError("Skill URL must use a loopback IP literal")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid port")
    return value.rstrip("/")


def task_path(task_id: str) -> str:
    if re.fullmatch(r"[a-f0-9]{32}", task_id) is None:
        raise ValueError("task_id must be 32 lowercase hexadecimal characters")
    return f"/v1/tasks/{task_id}"


def request(method: str, path: str, body: Any = None, *, timeout: float = 45) -> dict[str, Any]:
    base = loopback_base_url(os.environ.get("PHONE_SKILL_URL", "http://127.0.0.1:8790"))
    token = os.environ.get("PHONE_SKILL_TOKEN", "")
    if not 24 <= len(token) <= 4096 or any(ord(char) < 33 for char in token):
        raise ValueError("PHONE_SKILL_TOKEN must be configured")
    if not path.startswith("/v1/") or ".." in path or "#" in path:
        raise ValueError("invalid Skill endpoint")
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    if data is not None and len(data) > MAX_REQUEST_BYTES:
        raise ValueError("request body exceeds contract limit")
    # Neither environment proxies nor redirects may receive the private Skill token.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
        with client.stream(method, base + path, content=data, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json",
        }) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError(f"Skill request failed with HTTP {response.status_code}")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("Skill response exceeds output limit")
    parsed = strict_json_object(bytes(content))
    return TaskRecord.model_validate(parsed).model_dump(mode="json")


def json_arg(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    # Wrapping preserves duplicate-key and depth checks for array-valued catalog arguments.
    return strict_json_object('{"value":' + raw + '}', max_bytes=MAX_REQUEST_BYTES)["value"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Call the private auto-phone-skill gateway")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("instruction")
    for name in ("device-id", "location-json", "context-json", "policy-json", "app-package", "app-catalog-json", "idempotency-key"):
        submit.add_argument("--" + name)
    submit.add_argument("--wait", type=float, default=30)
    for name in ("get", "confirm", "resume", "cancel"):
        command = sub.add_parser(name)
        command.add_argument("task_id")
        if name in {"confirm", "resume"}:
            command.add_argument("--token", required=True)
        if name == "confirm":
            command.add_argument("--reject", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "submit":
            if not math.isfinite(args.wait) or not 0 <= args.wait <= 600:
                raise ValueError("wait_seconds must be between zero and 600")
            payload = LocalTaskRequest.model_validate({
                "instruction": args.instruction,
                "device_id": args.device_id or os.environ.get("PHONE_SKILL_DEVICE_ID", ""),
                "location": json_arg(args.location_json, None),
                "context": json_arg(args.context_json, {}),
                "policy": json_arg(args.policy_json, {}),
                "app_package": args.app_package,
                "app_catalog": json_arg(args.app_catalog_json, []),
                "idempotency_key": args.idempotency_key,
            }).model_dump(mode="json")
            result = request("POST", f"/v1/execute?wait_seconds={args.wait}", payload, timeout=max(45, args.wait + 15))
        else:
            path = task_path(args.task_id)
            if args.command == "get":
                result = request("GET", path)
            elif args.command == "confirm":
                body = ConfirmationRequest(approved=not args.reject, token=args.token)
                result = request("POST", path + "/confirm", body.model_dump(mode="json"))
            elif args.command == "resume":
                body = HandoffResumeRequest(resume=True, token=args.token)
                result = request("POST", path + "/resume", body.model_dump(mode="json"))
            else:
                result = request("POST", path + "/cancel", {})
    except (ValueError, ValidationError, httpx.HTTPError, KeyError) as exc:
        # Machine-readable, non-echoing output; only the exception class is exposed.
        error = ErrorResponse(error=ErrorDetail(code="EXECUTION_ERROR", message=f"Skill call failed: {type(exc).__name__}", request_id=uuid4().hex))
        print(error.model_dump_json())
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
