#!/usr/bin/env python3
"""Non-destructive real-device smoke test through the loopback Skill gateway.

Required environment:
  PHONE_SKILL_URL=http://127.0.0.1:8790
  PHONE_SKILL_TOKEN=<local skill token>
  PHONE_SKILL_DEVICE_ID=cloud-1
Optional:
  PHONE_AGENT_LIVE_INSTRUCTION=打开系统设置
  PHONE_AGENT_LIVE_APP_PACKAGE=com.android.settings

This script never auto-confirms payments, rides, purchases, messages, or handoffs.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx


def main() -> int:
    base = os.environ.get("PHONE_SKILL_URL", "").rstrip("/")
    token = os.environ.get("PHONE_SKILL_TOKEN", "")
    device = os.environ.get("PHONE_SKILL_DEVICE_ID", "")
    if not all((base, token, device)):
        raise SystemExit("PHONE_SKILL_URL, PHONE_SKILL_TOKEN, and PHONE_SKILL_DEVICE_ID are required")
    instruction = os.environ.get("PHONE_AGENT_LIVE_INSTRUCTION", "打开系统设置")
    package = os.environ.get("PHONE_AGENT_LIVE_APP_PACKAGE", "com.android.settings")
    report: dict[str, object] = {
        "device_id": device,
        "instruction": instruction,
        "package": package,
        "status": "running",
    }
    started = time.monotonic()
    with httpx.Client(base_url=base, timeout=30, headers={"Authorization": f"Bearer {token}"}) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        if not health.json().get("bridge_connected"):
            raise SystemExit("private Skill is not connected to the public control plane")
        response = client.post(
            "/v1/tasks",
            json={
                "instruction": instruction,
                "device_id": device,
                "app_package": package,
                "idempotency_key": "release-smoke-" + secrets.token_hex(12),
                "policy": {
                    "allow_irreversible": False,
                    "allow_messages": False,
                    "allow_purchases": False,
                    "allow_payments": False,
                },
            },
        )
        response.raise_for_status()
        task = response.json()
        task_id = task["id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = client.get(f"/v1/tasks/{task_id}")
            response.raise_for_status()
            task = response.json()
            state = task.get("status")
            if state in {"succeeded", "failed", "cancelled", "waiting_confirmation", "waiting_handoff"}:
                break
            time.sleep(0.2)
        report["task"] = task
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        report["status"] = "passed" if task.get("status") == "succeeded" else "failed"
    output = Path("artifacts/live-smoke.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
