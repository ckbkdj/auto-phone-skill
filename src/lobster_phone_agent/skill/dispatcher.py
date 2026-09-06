from __future__ import annotations

import asyncio
import base64
import json
from collections import OrderedDict
from time import monotonic
from typing import Any

from lobster_phone_agent.bridge.protocol import ALLOWED_RPC_METHODS
from lobster_phone_agent.bridge.contracts import validate_rpc_params, validate_rpc_result
from lobster_phone_agent.errors import BridgeProtocolError
from lobster_phone_agent.skill.pool import LocalAppiumPool

_MUTATING_METHODS = frozenset(
    {
        "launch_app",
        "tap",
        "type_text",
        "clear_active",
        "swipe",
        "back",
        "home",
    }
)


class SkillRpcDispatcher:
    """Strict RPC contracts and bounded duplicate suppression inside one Skill process."""

    def __init__(self, pool: LocalAppiumPool) -> None:
        self.pool = pool
        self._operation_results: OrderedDict[
            tuple[str, str, str], tuple[str, Any]
        ] = OrderedDict()
        self._operation_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
        self._cache_guard = asyncio.Lock()
        self._max_cache_entries = pool.settings.operation_cache_entries
        self._app_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}

    async def dispatch(self, device_id: str, method: str, params: dict[str, Any]) -> Any:
        if method not in ALLOWED_RPC_METHODS:
            raise BridgeProtocolError(f"RPC method is not allowlisted: {method}")
        params = validate_rpc_params(method, params)

        operation_id = str(params.get("operation_id") or "")
        if method not in _MUTATING_METHODS:
            result = await self._dispatch_uncached(device_id, method, params)
            return validate_rpc_result(method, result)
        if not operation_id:
            raise BridgeProtocolError(f"mutating RPC {method} requires operation_id")

        key = (device_id, method, operation_id)
        fingerprint = json.dumps(
            params, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        async with self._cache_guard:
            cached = self._operation_results.get(key)
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != fingerprint:
                    raise BridgeProtocolError(
                        "operation_id was reused with different RPC parameters"
                    )
                self._operation_results.move_to_end(key)
                return cached_result
            lock = self._operation_locks.setdefault(key, asyncio.Lock())

        try:
            async with lock:
                async with self._cache_guard:
                    cached = self._operation_results.get(key)
                    if cached is not None:
                        cached_fingerprint, cached_result = cached
                        if cached_fingerprint != fingerprint:
                            raise BridgeProtocolError(
                                "operation_id was reused with different RPC parameters"
                            )
                        self._operation_results.move_to_end(key)
                        return cached_result
                result = await self._dispatch_uncached(device_id, method, params)
                result = validate_rpc_result(method, result)
                async with self._cache_guard:
                    self._operation_results[key] = (fingerprint, result)
                    self._operation_results.move_to_end(key)
                    while len(self._operation_results) > self._max_cache_entries:
                        self._operation_results.popitem(last=False)
                return result
        finally:
            async with self._cache_guard:
                if not lock.locked():
                    self._operation_locks.pop(key, None)

    async def _dispatch_uncached(
        self,
        device_id: str,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        operation_id = str(params.get("operation_id") or "") or None
        async with self.pool.lease(device_id) as device:
            if method == "snapshot":
                return (await device.snapshot()).to_payload()
            if method == "launch_app":
                await device.launch_app(str(params["package"]), operation_id=operation_id)
                return {"performed": True}
            if method == "tap":
                await device.tap(
                    int(params["x"]),
                    int(params["y"]),
                    operation_id=operation_id,
                )
                return {"performed": True}
            if method == "type_text":
                await device.type_text(
                    str(params["text"]),
                    clear=bool(params.get("clear", False)),
                    element=params.get("element")
                    if isinstance(params.get("element"), dict)
                    else None,
                    operation_id=operation_id,
                )
                return {"performed": True}
            if method == "clear_active":
                await device.clear_active(
                    element=params.get("element")
                    if isinstance(params.get("element"), dict)
                    else None,
                    operation_id=operation_id,
                )
                return {"performed": True}
            if method == "swipe":
                await device.swipe(
                    str(params["direction"]),
                    percent=float(params.get("percent", 0.72)),
                    operation_id=operation_id,
                )
                return {"performed": True}
            if method == "back":
                await device.back(operation_id=operation_id)
                return {"performed": True}
            if method == "home":
                await device.home(operation_id=operation_id)
                return {"performed": True}
            if method == "list_apps":
                ttl = self.pool.settings.installed_apps_cache_seconds
                cached = self._app_cache.get(device_id)
                if cached is not None and ttl > 0 and monotonic() - cached[0] < ttl:
                    return [dict(item) for item in cached[1]]
                apps = await device.list_apps()
                normalized = [dict(item) for item in apps]
                if ttl > 0:
                    self._app_cache[device_id] = (monotonic(), normalized)
                return [dict(item) for item in normalized]
            if method == "screenshot_png":
                return base64.b64encode(await device.screenshot_png()).decode("ascii")
            if method == "is_alive":
                return await device.is_alive()
            if method == "close":
                await device.close()
                self._app_cache.pop(device_id, None)
                return {"closed": True}
        raise BridgeProtocolError(f"unhandled RPC method: {method}")

    @staticmethod
    def _validate_params(method: str, params: dict[str, Any]) -> None:
        validate_rpc_params(method, params)
