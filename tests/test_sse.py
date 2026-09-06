from __future__ import annotations

import asyncio

import pytest

from lobster_phone_agent.api.sse import EventSourceResponse


def test_sse_event_framing_sanitizes_headers_and_preserves_multiline_data() -> None:
    encoded = EventSourceResponse.encode_event(
        {
            "id": "event\n1",
            "event": "task\rfailed",
            "data": "first\r\nsecond",
        }
    )
    assert encoded == (
        b"id: event1\n"
        b"event: taskfailed\n"
        b"data: first\n"
        b"data: second\n\n"
    )


@pytest.mark.asyncio
async def test_sse_stream_emits_heartbeat_while_source_is_idle() -> None:
    release = asyncio.Event()

    async def source():
        await release.wait()
        yield {"event": "done", "data": "ok"}

    response = EventSourceResponse(source(), ping=0.05)
    iterator = response.body_iterator
    heartbeat = await asyncio.wait_for(anext(iterator), timeout=0.2)
    assert heartbeat == b": ping\n\n"
    release.set()
    event = await asyncio.wait_for(anext(iterator), timeout=0.2)
    assert event == b"event: done\ndata: ok\n\n"
    await iterator.aclose()
