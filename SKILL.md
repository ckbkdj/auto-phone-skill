---
name: auto-phone-skill
description: Control a caller-selected Android phone using live Appium semantic elements. Self-starting Python Skill with an optional MCP stdio entry; no Docker or public API deployment.
user-invocable: true
metadata:
  openclaw:
    requires:
      anyBins: [python3, python]
---

# Auto Phone Skill

Use the Python interpreter available on the host (Python 3.11 or newer). All Python code is included in this directory; do not run pip, Docker, git pull, or a separate HTTP gateway.

Entry: `python3 {baseDir}/scripts/phone_agent.py`. On Windows use `py -3` or the configured Python executable. Run with the host/node that can reach the assigned phone, not an unrelated sandbox.

## First use

Run `doctor --json '{}'`. Device allocation belongs to Lobster/OpenClaw. An operator supplies `AUTO_PHONE_UDID` and optionally `AUTO_PHONE_DEVICE_ID` / `AUTO_PHONE_APPIUM_URL`, or the private JSON mapping described in `{baseDir}/config.example.json`. Never invent a UDID, change a device mapping, or put Appium endpoints in tool arguments.

The runtime reuses an existing Appium. For a missing local Appium it can install into the user's private runtime directory and start a loopback-only Appium child automatically. Node/npm, Java and Android SDK are host prerequisites for that path. Do not silently install OS toolchains or require sudo. Read `{baseDir}/docs/USAGE.md` for bootstrap errors.

## Default: use the host model, one decision per screen

1. Call `begin` with a goal, an assigned device_id and one stable idempotency_key for this user turn.
2. Read the returned observation. Decide **exactly one** current action. Do not generate a workflow list or predict future controls.
3. Call `step` with task_id, observation_id, a stable operation_id for this one action, and a decision object. Use a target ref actually present in this observation. No x/y, shell, arbitrary code, XPath or model-generated selectors are accepted.
4. Read the new observation and the last receipt before deciding the next action. Do not reuse an old observation after another observe/step call.
5. Use `finish` only with positive success evidence visible on the phone. A dispatched command is not proof of a successful ride, payment or order.

Preserve the user goal and at most three compact receipts between turns. Discard speculative future steps and verbose reasoning. The full runtime contracts are printed by `contracts`; see `{baseDir}/docs/USAGE.md` for examples.

```bash
python3 {baseDir}/scripts/phone_agent.py begin --json '{"goal":"打开系统设置","device_id":"cloud-1","idempotency_key":"conversation-1:turn-1","success":[{"kind":"package_is","value":"com.android.settings"}]}'
```

All operational commands accept a single strict JSON object through `--json` or stdin. Prefer the host's argument-array/JSON facilities; never concatenate untrusted text into a shell command. Match shell quoting to the OS.

## Non-negotiable state handling

- `needs_decision`: examine the new screen and decide one next action, not a stored plan tail.
- `waiting_confirmation`: show the exact gate message and actual target. Only after explicit user approval, repeat the **same step request and operation_id** with its confirmation_token. Never let an autonomous loop approve.
- `waiting_handoff`: let the user complete the protected phone operation. Call `resume` with the current gate token only after the user says it is complete. Resume only observes; it never repeats the previous action.
- `outcome_unknown`: stop all mutations. Ask the user to inspect the phone and verify the previous remote operation has ended. Never invent a new operation_id to retry it. The operator may resume with the current token after reconciliation.
- `SCREEN_CHANGED_REPLAN`: nothing was dispatched; use the newly returned observation and reconsider.
- `REPEATED_ACTION_BLOCKED`: do not keep clicking the same unchanged page. Request human help.
- `succeeded`: report only the observed result. Do not claim untested App versions or an entire Top 500 catalog are certified.

Passwords, OTPs, biometrics, CAPTCHA, security verification and sensitive input are manual. User data is local in `~/.auto-phone-skill/state.sqlite3`; do not copy it or Appium logs to public repositories.

## Optional autonomous LLM / MCP

`run` uses a separately configured OpenAI-compatible model, with **fresh system + current-screen messages for each single decision**. It has no expanding chat history and cannot supply confirmation/resume tokens. The default host-driven mode needs no second LLM key.

MCP hosts start `python3 {baseDir}/scripts/phone_agent.py mcp` themselves. The process exposes only doctor/begin/observe/step/status/resume/cancel tools over stdin/stdout; no listening port and no daemon setup. See `{baseDir}/mcp.example.json`.
