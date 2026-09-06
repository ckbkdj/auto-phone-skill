---
name: auto-phone-skill
description: Control a selected Android cloud phone through the private Lobster Skill gateway for app launch, search, navigation, ride preparation, and visible UI workflows.
user-invocable: true
metadata:
  openclaw:
    skillKey: auto-phone-skill
    primaryEnv: PHONE_SKILL_TOKEN
    requires:
      bins:
        - python3
      env:
        - PHONE_SKILL_URL
        - PHONE_SKILL_TOKEN
        - PHONE_SKILL_DEVICE_ID
---

# Auto Phone Skill

Run `{baseDir}/scripts/phone_agent.py`. The helper is deliberately restricted to the loopback Skill API; it must never call the public Docker domain or Appium directly.

## Boundary

- Lobster talks to `PHONE_SKILL_URL`, normally `http://127.0.0.1:8790`.
- Send only a stable `device_id`. The private Skill owns the Appium URL, ADB serial, capabilities, sessions, and outbound WebSocket.
- The public Docker control plane is reached only by the Skill. Do not put its API key, bridge token, URL, Appium details, or ADB details in prompts.
- Keep one stable idempotency key for retries of the same user turn.

## Start a task

```bash
python3 {baseDir}/scripts/phone_agent.py submit '打开美团' \
  --idempotency-key 'conversation-id:turn-id'
```

For trusted location context already held by Lobster:

```bash
python3 {baseDir}/scripts/phone_agent.py submit \
  '用美团打车去北京南站' \
  --location-json '{"address":"北京市朝阳区...","latitude":39.9,"longitude":116.4,"coordinate_system":"gcj02"}' \
  --policy-json '{"allow_irreversible":true}' \
  --idempotency-key 'conversation-id:turn-id'
```

`allow_irreversible` permits the workflow to reach the risk gate; it does not approve the final action.

## Handle states

- `succeeded`: report the result.
- `queued`, `planning`, `waiting_bridge`, or `running`: retain the task ID and query it; do not submit a new irreversible task.
- `waiting_confirmation`: show the confirmation message. Call `confirm` only after explicit user approval.
- `waiting_handoff`: ask the user to operate the visible phone for login, password, OTP, CAPTCHA, face, fingerprint, or another protected surface. Call `resume` only after the user says it is complete.
- `failed`: report the error. Do not silently resubmit a ride, order, payment, message, deletion, or publication action.

```bash
python3 {baseDir}/scripts/phone_agent.py get TASK_ID
python3 {baseDir}/scripts/phone_agent.py confirm TASK_ID --token CONFIRMATION_TOKEN
python3 {baseDir}/scripts/phone_agent.py resume TASK_ID --token HANDOFF_TOKEN
python3 {baseDir}/scripts/phone_agent.py cancel TASK_ID
```

## Safety and token discipline

Never approve on the user's behalf. Never place passwords, OTPs, payment credentials, cookies, API keys, screenshots, Appium XML, or coordinates chosen by the main model into the instruction or context. The phone sub-agent observes and compresses the UI locally and refuses ambiguous controls.

Read `{baseDir}/references/api.md` when implementing another local caller.

## Strict contract

Install the matching candidate Python package in the Python interpreter used by this skill before running the helper. `python3 -m pip install '.[skill]'` from the repository installs it.

The helper validates `LocalTaskRequest` before sending and `TaskRecord` after receiving. Unknown fields, numeric booleans, string numbers, duplicate JSON keys, nested device credentials, and oversized JSON are rejected. Successful calls print one TaskRecord JSON object; runtime failures print the versioned ErrorResponse JSON object and exit nonzero. CLI `--help` and argument usage errors are human-readable. Do not scrape fields out of malformed output or retry uncertain irreversible actions.

Use the IP literal `http://127.0.0.1:8790` (or `[::1]`), not a public URL. The helper disables environment HTTP proxies and refuses redirects to avoid forwarding its local token.
