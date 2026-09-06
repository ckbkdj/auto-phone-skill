from __future__ import annotations

import asyncio
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaError

from lobster_phone_agent.contracts import strict_json_object

from lobster_phone_agent.config import Settings
from lobster_phone_agent.errors import PlanningError
from lobster_phone_agent.util.json import extract_json_object


class OpenAICompatibleClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        if not self.settings.llm_model:
            raise PlanningError("LLM planner is disabled")

        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_output_tokens or self.settings.llm_max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }

        try:
            response = await self._post_bounded(payload)
            if response.status_code in {400, 422} and any(
                marker in response.text.lower()
                for marker in ("json_schema", "response_format", "structured output")
            ):
                # Transport downgrade never disables local schema/semantic validation.
                payload["response_format"] = {"type": "json_object"}
                response = await self._post_bounded(payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PlanningError(f"LLM request failed: {type(exc).__name__}") from exc

        try:
            data = strict_json_object(response.content)
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("exactly one LLM completion is required")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("incomplete or non-text LLM completion")
            message = choice["message"]
            if message.get("refusal") or message.get("tool_calls"):
                raise ValueError("refusal or tool calls cannot be an action plan")
            content = message["content"]
            if not isinstance(content, str):
                raise ValueError("LLM content must be one JSON text object")
            result = extract_json_object(content)
            Draft202012Validator(schema).validate(result)
            return result
        except (KeyError, IndexError, TypeError, ValueError, JsonSchemaError) as exc:
            raise PlanningError("LLM returned an invalid JSON response or output contract") from exc

    async def _post_bounded(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                async with self._client.stream("POST", "chat/completions", json=payload) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            raise PlanningError("LLM response exceeds byte limit")
                    return httpx.Response(response.status_code, headers=response.headers,
                                          content=bytes(body), request=response.request)
        except TimeoutError as exc:
            raise PlanningError("LLM response deadline exceeded") from exc

    async def close(self) -> None:
        await self._client.aclose()
