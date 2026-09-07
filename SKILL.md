---
name: auto-phone-skill
description: Observe and control a host-assigned Android phone one action at a time. Self-starting ZIP Skill and MCP stdio with bounded setup diagnostics; no Docker or public gateway.
user-invocable: true
metadata:
  openclaw:
    requires:
      anyBins: [python3, python]
---

# Auto Phone Skill 0.3.1

The entry is `python3 {baseDir}/scripts/phone_agent.py` (Python 3.11+; Windows: `py -3`). `{baseDir}` is the directory containing **this actual loaded SKILL.md**. Never append `auto-phone-skill-main` or another copy of the directory name. No pip, Docker, Git pull, HTTP gateway or public port is needed. Run on the host/node that can reach the assigned phone.

## Setup: bounded commands, not an investigation loop

1. Run `doctor --json '{}'` once. `ok:true` means the diagnostic ran, NOT that the phone is ready. Read `report.issues`, `config_source`, `config_path`, `architecture`, `jdk_ready`, `sdk_ready` and Node/npm versions.
2. The host must supply its already-assigned real device ID/UDID. Do not enumerate or select another phone. For approved first-time provisioning, use the host-only `setup` command with this assignment. It writes the correct private config. `install_jdk:true` is an explicit user-home JDK-install opt-in; never download a random JRE/CPU archive yourself.
3. For an existing configuration, run `prepare --json '{"device_id":"cloud-1"}'`. This prepares Appium without allocating a task. `appium_ready` does not yet certify the phone session.
4. On failure, show the error and `hint`, or obtain `setup_status --json '{}'`. **Stop after one failed setup attempt.** Do not read source files, grep all directories, search memories for configuration, run sudo/apt, guess new URLs, or repeatedly call begin. A denial from exec/network policy requires administrator approval; never switch tools to bypass it.

Private config is `~/.auto-phone-skill/config.json`; `--config` or `AUTO_PHONE_CONFIG` has priority and must exist. A config beside this Skill is read only as backward compatibility when no private config exists. Do not put credentials into model context. Operator details: `{baseDir}/docs/USAGE.md`.

## Execute one observed step

`begin` accepts goal, assigned device_id, stable idempotency_key and optional success conditions. Read the returned observation, decide exactly ONE action, and call `step` with task_id, observation_id, stable operation_id and decision. References such as `n0` are valid only for the returned observation. Do not generate future steps, coordinates, Shell, ADB, XPath or arbitrary scripts. `type` replaces the entire field; it is not append.

After each step, inspect the new observation and receipt before deciding again. Retain the goal, task ID and at most three receipts; do not carry verbose reasoning. `finish` requires positive observable evidence. Opening an app is not evidence of completing a compound goal such as browsing three videos: track that count and actual distinct content observations, and hand off when evidence is unavailable. Do not count timer changes as new videos.

## Recover without repeating effects

- `initialization_failed`: no user action was dispatched; device occupancy is released. After setup is fixed, reuse the same begin request/key. Old initialization-only leftovers are reclaimed only with no observation, receipts or operation intents.
- `waiting_confirmation`: show the gate message. Only after explicit user approval repeat the SAME step/operation_id with its confirmation_token. Never approve automatically.
- `waiting_handoff` / `outcome_unknown`: stop mutations. User must inspect/complete the phone operation and ensure any remote command ended before `resume`. Resume only re-observes; it never repeats the action. Do not delete state.sqlite3.
- `SCREEN_CHANGED_REPLAN`: nothing dispatched; decide from the new observation.
- `REPEATED_ACTION_BLOCKED`: do not invent another ID to keep clicking.
- `succeeded`: report only observed completion, not app-wide certification.

`tasks --json '{}'` lists at most 16 task IDs/statuses without UI or goals. `status` omits the previous UI; `observe` explicitly fetches a new screen. Passwords, OTPs, CAPTCHA and biometrics stay manual.

If the host reports auto-compaction failure, do not launch a replacement phone task. In a refreshed host session recover task ID/status using `tasks` and `status` first. This Skill cannot repair the host's compaction engine or silently change its reserve-token settings.

## Modes

Default mode uses the host model; no second model key. Optional `run` uses a configured LLM with fresh system + current-screen messages each round and cannot approve gates. Optional MCP: host launches `python3 {baseDir}/scripts/phone_agent.py mcp`; stdin/stdout only. Host-only setup/installation are NOT MCP tools. `contracts` prints the closed schemas only when developing an integration, not every task.
