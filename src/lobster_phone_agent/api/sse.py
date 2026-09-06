from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import suppress
from typing import Any

from starlette.responses import StreamingResponse


class EventSourceResponse(StreamingResponse):
    """Small dependency-free SSE response with idle heartbeat comments.

    The task route already stops its source generator when the client disconnects. This wrapper
    adds correct SSE framing, anti-buffering headers, and periodic comments so long-lived streams
    survive ordinary reverse-proxy idle timeouts without requiring a second SSE package.
    """

    def __init__(
        self,
        content: AsyncIterable[dict[str, Any]],
        ping: float = 15,
        headers: dict[str, str] | None = None,
    ) -> None:
        response_headers = {
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            **(headers or {}),
        }
        super().__init__(
            self._stream(content, ping=max(0.05, float(ping))),
            media_type="text/event-stream",
            headers=response_headers,
        )

    @classmethod
    async def _stream(
        cls,
        content: AsyncIterable[dict[str, Any]],
        *,
        ping: float,
    ) -> AsyncIterator[bytes]:
        iterator = content.__aiter__()
        pending: asyncio.Task[dict[str, Any]] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait({pending}, timeout=ping)
                if not done:
                    yield b": ping\n\n"
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield cls.encode_event(event)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            close = getattr(iterator, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()

    @staticmethod
    def encode_event(event: dict[str, Any]) -> bytes:
        chunks: list[str] = []
        event_id = str(event.get("id") or "").replace("\r", "").replace("\n", "")
        event_name = str(event.get("event") or "").replace("\r", "").replace("\n", "")
        if event_id:
            chunks.append(f"id: {event_id}")
        if event_name:
            chunks.append(f"event: {event_name}")
        data = str(event.get("data") or "")
        for line in data.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            chunks.append(f"data: {line}")
        return ("\n".join(chunks) + "\n\n").encode("utf-8")
