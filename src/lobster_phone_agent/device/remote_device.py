from __future__ import annotations

import base64
from typing import Any

from lobster_phone_agent.bridge.hub import BridgeHub
from lobster_phone_agent.device.ui import ScreenSnapshot
from lobster_phone_agent.schemas import DeviceDescriptor


class RemoteDevice:
    """Control-plane proxy whose commands are executed by an outbound private skill."""

    def __init__(self, descriptor: DeviceDescriptor, hub: BridgeHub) -> None:
        self.descriptor = descriptor
        self.device_id = descriptor.id
        self._hub = hub

    async def _call(self, method: str, **params: Any) -> Any:
        return await self._hub.call(
            bridge_id=self.descriptor.bridge_id,
            device_id=self.descriptor.id,
            method=method,
            params=params,
        )

    async def snapshot(self) -> ScreenSnapshot:
        payload = await self._call("snapshot")
        if not isinstance(payload, dict):
            raise TypeError("bridge snapshot result must be an object")
        return ScreenSnapshot.from_payload(payload)

    @staticmethod
    def _require_performed(method: str, result: Any) -> None:
        if not isinstance(result, dict) or result.get("performed") is not True:
            raise RuntimeError(f"private skill did not perform {method}: {result!r}")

    async def launch_app(
        self, package: str, *, operation_id: str | None = None
    ) -> None:
        self._require_performed(
            "launch_app",
            await self._call(
                "launch_app", package=package, operation_id=operation_id
            )
        )

    async def tap(
        self, x: int, y: int, *, operation_id: str | None = None
    ) -> None:
        self._require_performed(
            "tap",
            await self._call(
                "tap", x=int(x), y=int(y), operation_id=operation_id
            )
        )

    async def type_text(
        self,
        text: str,
        *,
        clear: bool = False,
        element: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> None:
        self._require_performed(
            "type_text",
            await self._call(
                "type_text",
                text=text,
                clear=clear,
                element=element,
                operation_id=operation_id,
            ),
        )

    async def clear_active(
        self,
        *,
        element: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> None:
        self._require_performed(
            "clear_active",
            await self._call(
                "clear_active", element=element, operation_id=operation_id
            )
        )

    async def swipe(
        self,
        direction: str,
        *,
        percent: float = 0.72,
        operation_id: str | None = None,
    ) -> None:
        self._require_performed(
            "swipe",
            await self._call(
                "swipe",
                direction=direction,
                percent=percent,
                operation_id=operation_id,
            )
        )

    async def back(self, *, operation_id: str | None = None) -> None:
        self._require_performed(
            "back", await self._call("back", operation_id=operation_id)
        )

    async def home(self, *, operation_id: str | None = None) -> None:
        self._require_performed(
            "home", await self._call("home", operation_id=operation_id)
        )

    async def list_apps(self) -> list[dict[str, object]]:
        result = await self._call("list_apps")
        if not isinstance(result, list):
            raise TypeError("bridge list_apps result must be a list")
        return [dict(item) for item in result if isinstance(item, dict)]

    async def screenshot_png(self) -> bytes:
        result = await self._call("screenshot_png")
        if not isinstance(result, str):
            raise TypeError("bridge screenshot result must be base64 text")
        return base64.b64decode(result, validate=True)

    async def is_alive(self) -> bool:
        return bool(await self._call("is_alive"))

    async def close(self) -> None:
        # The private skill owns and reuses the real Appium session.
        return None
