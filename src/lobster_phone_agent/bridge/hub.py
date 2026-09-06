from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from lobster_phone_agent.bridge.protocol import (
    ALLOWED_RPC_METHODS,
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
from lobster_phone_agent.bridge.contracts import validate_rpc_result
from lobster_phone_agent.contracts import strict_json_object
from lobster_phone_agent.config import Settings
from lobster_phone_agent.errors import (
    BridgeProtocolError,
    BridgeUnavailable,
    ExecutionError,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BridgeSession:
    bridge_id: str
    session_id: str
    instance_id: str
    websocket: WebSocket
    devices: dict[str, AdvertisedDevice]
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, asyncio.Future[RpcResultMessage]] = field(default_factory=dict)
    sequence: int = 0
    connected_at: float = field(default_factory=monotonic)
    last_seen: float = field(default_factory=monotonic)
    closed: bool = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.closed:
            raise BridgeUnavailable(f"bridge {self.bridge_id} is disconnected")
        async with self.send_lock:
            await self.websocket.send_json(payload)

    def fail_pending(self, reason: str) -> None:
        self.closed = True
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(BridgeUnavailable(reason))
        self.pending.clear()


class BridgeHub:
    """Tracks private skill bridges that connect outward to the public control plane."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()
        self._connected_events: dict[str, asyncio.Event] = {}

    @property
    def connected_count(self) -> int:
        return sum(not session.closed for session in self._sessions.values())

    @property
    def device_count(self) -> int:
        return sum(
            len(session.devices)
            for session in self._sessions.values()
            if not session.closed
        )

    def is_connected(self, bridge_id: str, device_id: str | None = None) -> bool:
        session = self._sessions.get(bridge_id)
        if session is None or session.closed:
            return False
        return device_id is None or device_id in session.devices

    def advertised_devices(self, bridge_id: str) -> list[AdvertisedDevice]:
        session = self._sessions.get(bridge_id)
        if session is None or session.closed:
            return []
        return list(session.devices.values())

    def public_status(self) -> dict[str, object]:
        bridges: list[dict[str, object]] = []
        for bridge_id, session in sorted(self._sessions.items()):
            if session.closed:
                continue
            bridges.append(
                {
                    "bridge_id": bridge_id,
                    "instance_id": session.instance_id,
                    "session_id": session.session_id,
                    "connected_seconds": max(0.0, monotonic() - session.connected_at),
                    "last_seen_seconds": max(0.0, monotonic() - session.last_seen),
                    "devices": [
                        device.model_dump(mode="json")
                        for device in sorted(session.devices.values(), key=lambda item: item.id)
                    ],
                }
            )
        return {
            "connected_bridges": len(bridges),
            "advertised_devices": sum(len(item["devices"]) for item in bridges),
            "bridges": bridges,
        }

    async def wait_connected(
        self,
        bridge_id: str,
        device_id: str,
        *,
        timeout: float | None = None,
    ) -> None:
        if self.is_connected(bridge_id, device_id):
            return
        async with self._lock:
            event = self._connected_events.setdefault(bridge_id, asyncio.Event())
        try:
            await asyncio.wait_for(
                event.wait(),
                timeout=timeout or self.settings.bridge_connect_grace_seconds,
            )
        except TimeoutError as exc:
            raise BridgeUnavailable(
                f"private skill bridge {bridge_id!r} is offline"
            ) from exc
        if not self.is_connected(bridge_id, device_id):
            raise BridgeUnavailable(
                f"bridge {bridge_id!r} does not advertise device {device_id!r}"
            )

    async def serve(self, websocket: WebSocket, *, bridge_id: str) -> None:
        await websocket.accept()
        session: BridgeSession | None = None
        heartbeat: asyncio.Task[None] | None = None
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=8.0)
            raw = strict_json_object(raw, max_bytes=self.settings.bridge_max_message_bytes)
            hello = HelloMessage.model_validate(raw)
            if hello.bridge_id != bridge_id:
                raise BridgeProtocolError("bridge_id does not match authenticated URL")
            devices = {device.id: device for device in hello.devices}
            if not devices:
                raise BridgeProtocolError("bridge must advertise at least one device")
            session = BridgeSession(
                bridge_id=bridge_id,
                session_id=uuid4().hex,
                instance_id=hello.instance_id,
                websocket=websocket,
                devices=devices,
            )
            old = await self._register(session)
            if old is not None:
                old.fail_pending("bridge connection was replaced by a new instance")
                try:
                    await old.websocket.close(code=1012, reason="replaced")
                except Exception:
                    pass
            await session.send_json(
                HelloAckMessage(
                    session_id=session.session_id,
                    heartbeat_seconds=self.settings.bridge_heartbeat_seconds,
                ).model_dump(mode="json")
            )
            heartbeat = asyncio.create_task(
                self._heartbeat_loop(session), name=f"bridge-heartbeat:{bridge_id}"
            )
            while True:
                payload = await websocket.receive_text()
                payload = strict_json_object(payload, max_bytes=self.settings.bridge_max_message_bytes)
                session.last_seen = monotonic()
                kind = message_type(payload)
                if kind is BridgeMessageType.RPC_RESULT:
                    result = RpcResultMessage.model_validate(payload)
                    future = session.pending.pop(result.id, None)
                    if future is None:
                        logger.warning(
                            "late or unknown bridge response",
                            extra={"bridge_id": bridge_id, "rpc_id": result.id},
                        )
                        continue
                    if not future.done():
                        future.set_result(result)
                elif kind is BridgeMessageType.HEARTBEAT_ACK:
                    HeartbeatAckMessage.model_validate(payload)
                elif kind is BridgeMessageType.HEARTBEAT:
                    heartbeat_request = HeartbeatMessage.model_validate(payload)
                    await session.send_json(
                        HeartbeatAckMessage(id=heartbeat_request.id).model_dump(mode="json")
                    )
                else:
                    raise BridgeProtocolError(f"unexpected bridge message: {kind.value}")
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("bridge disconnected: %s", exc, extra={"bridge_id": bridge_id})
            try:
                await websocket.close(code=1008, reason="invalid bridge protocol")
            except Exception:
                pass
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if session is not None:
                await self._unregister(session)
                session.fail_pending(f"bridge {bridge_id} disconnected")

    async def _register(self, session: BridgeSession) -> BridgeSession | None:
        async with self._lock:
            old = self._sessions.get(session.bridge_id)
            self._sessions[session.bridge_id] = session
            event = self._connected_events.setdefault(session.bridge_id, asyncio.Event())
            event.set()
            return old

    async def _unregister(self, session: BridgeSession) -> None:
        async with self._lock:
            if self._sessions.get(session.bridge_id) is session:
                self._sessions.pop(session.bridge_id, None)
                event = self._connected_events.setdefault(session.bridge_id, asyncio.Event())
                event.clear()

    async def _heartbeat_loop(self, session: BridgeSession) -> None:
        interval = max(5, self.settings.bridge_heartbeat_seconds)
        while not session.closed:
            await asyncio.sleep(interval)
            if monotonic() - session.last_seen > interval * 3:
                try:
                    await session.websocket.close(code=1011, reason="heartbeat timeout")
                finally:
                    return
            await session.send_json(HeartbeatMessage().model_dump(mode="json"))

    async def call(
        self,
        *,
        bridge_id: str,
        device_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        if method not in ALLOWED_RPC_METHODS:
            raise BridgeProtocolError(f"RPC method is not allowlisted: {method}")
        await self.wait_connected(bridge_id, device_id, timeout=timeout)
        session = self._sessions.get(bridge_id)
        if session is None or session.closed:
            raise BridgeUnavailable(f"bridge {bridge_id!r} disconnected")
        if device_id not in session.devices:
            raise BridgeUnavailable(
                f"bridge {bridge_id!r} does not advertise device {device_id!r}"
            )
        session.sequence += 1
        request = RpcRequestMessage(
            sequence=session.sequence,
            device_id=device_id,
            method=method,
            params=params or {},
            timeout_ms=int((timeout or self.settings.bridge_rpc_timeout_seconds) * 1000),
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[RpcResultMessage] = loop.create_future()
        session.pending[request.id] = future
        try:
            await session.send_json(request.model_dump(mode="json"))
            result = await asyncio.wait_for(
                future,
                timeout=timeout or self.settings.bridge_rpc_timeout_seconds,
            )
        except TimeoutError as exc:
            session.pending.pop(request.id, None)
            raise BridgeUnavailable(
                f"bridge RPC timed out: {bridge_id}/{device_id} {method}"
            ) from exc
        except Exception:
            session.pending.pop(request.id, None)
            raise
        if result.sequence != request.sequence:
            raise BridgeProtocolError(
                "bridge RPC sequence mismatch: "
                f"expected {request.sequence}, got {result.sequence}"
            )
        if not result.ok:
            raise ExecutionError(result.error or f"private skill failed RPC {method}")
        return validate_rpc_result(method, result.result)

    def _validate_message_size(self, payload: Any) -> None:
        try:
            size = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        except Exception as exc:
            raise BridgeProtocolError("bridge message is not JSON serializable") from exc
        if size > self.settings.bridge_max_message_bytes:
            raise BridgeProtocolError(
                f"bridge message exceeds {self.settings.bridge_max_message_bytes} bytes"
            )

    async def close(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for event in self._connected_events.values():
                event.clear()
        for session in sessions:
            session.fail_pending("control plane shutting down")
            try:
                await session.websocket.close(code=1001, reason="server shutdown")
            except Exception:
                pass
