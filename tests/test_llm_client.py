from __future__ import annotations

import json

import httpx
import pytest

from lobster_phone_agent.config import Settings
from lobster_phone_agent.errors import PlanningError
from lobster_phone_agent.llm.client import OpenAICompatibleClient


@pytest.mark.asyncio
async def test_llm_client_falls_back_from_json_schema_to_json_object() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": "json_schema unsupported"})
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"steps":[]}'}}]},
        )

    settings = Settings(llm_model="test-model", llm_base_url="http://llm.local/v1")
    client = OpenAICompatibleClient(settings)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://llm.local/v1/",
    )
    result = await client.complete_json(
        system="system",
        user="user",
        schema_name="plan",
        schema={"type": "object"},
    )
    assert result == {"steps": []}
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[1]["response_format"] == {"type": "json_object"}
    await client.close()


@pytest.mark.asyncio
async def test_llm_client_wraps_non_json_response_as_planning_error() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, text="not-json")
    )
    settings = Settings(llm_model="test-model", llm_base_url="http://llm.local/v1")
    client = OpenAICompatibleClient(settings)
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=transport, base_url="http://llm.local/v1/")
    with pytest.raises(PlanningError, match="invalid JSON response"):
        await client.complete_json(
            system="system",
            user="user",
            schema_name="plan",
            schema={"type": "object"},
        )
    await client.close()
