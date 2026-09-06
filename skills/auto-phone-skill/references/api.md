# Private Skill API used by Lobster

The caller base URL is `PHONE_SKILL_URL`, normally `http://127.0.0.1:8790`. It is loopback-only. Authenticate with `Authorization: Bearer $PHONE_SKILL_TOKEN` or `X-API-Key`.

## Start and optionally wait

`POST /v1/execute?wait_seconds=30`

```json
{
  "instruction": "用美团打车去北京南站",
  "device_id": "cloud-1",
  "location": {
    "address": "北京市朝阳区...",
    "latitude": 39.9,
    "longitude": 116.4,
    "coordinate_system": "gcj02"
  },
  "context": {},
  "policy": {
    "confirmation_mode": "risk_based",
    "allow_irreversible": true,
    "allow_messages": false,
    "allow_purchases": false,
    "allow_payments": false
  },
  "idempotency_key": "conversation-id:user-turn-id"
}
```

The local Skill replaces `device_id` with the public routing identity and forwards the request over authenticated HTTPS. It rejects `device`, `bridge_id`, Appium, ADB, capability, and bridge-secret fields from Lobster.

## Observe and control

- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/confirm` with `{"approved":true,"token":"..."}`
- `POST /v1/tasks/{task_id}/resume` with `{"resume":true,"token":"..."}`
- `POST /v1/tasks/{task_id}/cancel`

The public control-plane SSE endpoint is intentionally not proxied through the first Skill release. Poll the local task endpoint or use `/v1/execute` with a bounded wait.
