from __future__ import annotations

from typing import Any, Protocol

from lobster_phone_agent.device.ui import ScreenSnapshot


class DeviceAdapter(Protocol):
    device_id: str

    async def snapshot(self) -> ScreenSnapshot: ...

    async def launch_app(self, package: str, *, operation_id: str | None = None) -> None: ...

    async def tap(self, x: int, y: int, *, operation_id: str | None = None) -> None: ...

    async def type_text(
        self,
        text: str,
        *,
        clear: bool = False,
        element: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> None: ...

    async def clear_active(
        self,
        *,
        element: dict[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> None: ...

    async def swipe(
        self,
        direction: str,
        *,
        percent: float = 0.72,
        operation_id: str | None = None,
    ) -> None: ...

    async def back(self, *, operation_id: str | None = None) -> None: ...

    async def home(self, *, operation_id: str | None = None) -> None: ...

    async def list_apps(self) -> list[dict[str, object]]: ...

    async def screenshot_png(self) -> bytes: ...

    async def is_alive(self) -> bool: ...

    async def close(self) -> None: ...
