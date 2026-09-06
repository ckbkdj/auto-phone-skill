from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from time import perf_counter
from typing import Any

import websockets

from lobster_phone_agent.bridge.protocol import (
    AdvertisedDevice,
    BridgeMessageType,
    HeartbeatAckMessage,
    HeartbeatMessage,
    HelloAckMessage,
    HelloMessage,
    RpcRequestMessage,
    RpcResultMessage,
    message_type,
)
from lobster_phone_agent.skill.dispatcher import SkillRpcDispatcher
from lobster_phone_agent.skill.models import SkillConfig
from lobster_phone_agent.contracts import strict_json_object

logger = logging.getLogger(__name__)


class SkillBridgeClient:
    def __init__(self, config: SkillConfig, dispatcher: SkillRpcDispatcher) -> None:
        self.config = config
        self.dispatcher = dispatcher
        self.connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._response_cache: OrderedDict[str, RpcResultMessage] = OrderedDict()
        self._tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        self._last_sequence = 0

    async def run_forever(self) -> None:
        delay = self.config.reconnect_min_seconds
        while not self._stop.is_set():
            try:
                await self._run_connection()
                delay = self.config.reconnect_min_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected.clear()
                logger.warning("skill bridge connection failed: %s", exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, self.config.reconnect_max_seconds)

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_connection(self) -> None:
        headers = {"Authorization": f"Bearer {self.config.bridge_token}"}
        async with websockets.connect(
            self.config.websocket_url,
            additional_headers=headers,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=self.config.runtime.bridge_max_message_bytes,
        ) as websocket:
            hello = HelloMessage(
                bridge_id=self.config.bridge_id,
                devices=[
                    AdvertisedDevice(id=item.id, metadata=item.advertised_metadata())
                    for item in self.config.devices
                ],
            )
            await self._send(websocket, hello.model_dump_json())
            ack_raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            ack = HelloAckMessage.model_validate(self._parse_payload(ack_raw))
            logger.info("private skill bridge connected: session=%s", ack.session_id)
            self._last_sequence = 0
            self.connected.set()
            try:
                async for raw in websocket:
                    payload = self._parse_payload(raw)
                    kind = message_type(payload)
                    if kind is BridgeMessageType.RPC_REQUEST:
                        request = RpcRequestMessage.model_validate(payload)
                        expected_sequence = self._last_sequence + 1
                        if request.sequence != expected_sequence:
                            raise ValueError(
                                "non-monotonic bridge RPC sequence: "
                                f"expected {expected_sequence}, got {request.sequence}"
                            )
                        self._last_sequence = request.sequence
                        task = asyncio.create_task(
                            self._handle_rpc(websocket, request),
                            name=f"skill-rpc:{request.device_id}:{request.method}",
                        )
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                    elif kind is BridgeMessageType.HEARTBEAT:
                        heartbeat = HeartbeatMessage.model_validate(payload)
                        await self._send(
                            websocket, HeartbeatAckMessage(id=heartbeat.id).model_dump_json()
                        )
                    else:
                        raise ValueError(f"unexpected control-plane message: {kind.value}")
            finally:
                self.connected.clear()

    async def _handle_rpc(self, websocket, request: RpcRequestMessage) -> None:
        cached = self._response_cache.get(request.id)
        if cached is not None:
            await self._send(websocket, cached.model_dump_json())
            return
        started = perf_counter()
        try:
            result = await asyncio.wait_for(
                self.dispatcher.dispatch(request.device_id, request.method, request.params),
                timeout=request.timeout_ms / 1000,
            )
            response = RpcResultMessage(
                id=request.id,
                sequence=request.sequence,
                ok=True,
                result=result,
                duration_ms=(perf_counter() - started) * 1000,
            )
        except Exception as exc:
            response = RpcResultMessage(
                id=request.id,
                sequence=request.sequence,
                ok=False,
                error=f"private operation failed: {type(exc).__name__}",
                duration_ms=(perf_counter() - started) * 1000,
            )
        self._response_cache[request.id] = response
        self._response_cache.move_to_end(request.id)
        while len(self._response_cache) > 512:
            self._response_cache.popitem(last=False)
        try:
            await self._send(websocket, response.model_dump_json())
        except Exception:
            # The action may have completed after the network dropped. The control plane will
            # re-observe state before retrying, rather than blindly duplicating the mutation.
            pass

    async def _send(self, websocket: Any, payload: str) -> None:
        async with self._send_lock:
            await websocket.send(payload)

    @staticmethod
    def _parse_payload(raw: Any) -> dict[str, Any]:
        return strict_json_object(raw)
