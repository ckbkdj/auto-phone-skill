from __future__ import annotations

from dataclasses import dataclass, field

from lobster_phone_agent.device.ui import ScreenSnapshot, parse_uiautomator_xml


def screen(package: str, *nodes: dict[str, object], activity: str = ".Main") -> ScreenSnapshot:
    xml_nodes = []
    for index, node in enumerate(nodes):
        attrs = {
            "index": str(index),
            "text": str(node.get("text", "")),
            "resource-id": str(node.get("resource_id", "")),
            "class": str(node.get("class_name", "android.widget.TextView")),
            "package": package,
            "content-desc": str(node.get("content_desc", "")),
            "checkable": "false",
            "checked": "false",
            "clickable": str(bool(node.get("clickable", True))).lower(),
            "enabled": "true",
            "focusable": str(bool(node.get("focusable", False))).lower(),
            "focused": str(bool(node.get("focused", False))).lower(),
            "scrollable": str(bool(node.get("scrollable", False))).lower(),
            "long-clickable": "false",
            "password": str(bool(node.get("password", False))).lower(),
            "selected": "false",
            "bounds": str(node.get("bounds", "[10,10][500,120]")),
            "displayed": "true",
        }
        encoded = " ".join(f'{key}="{value}"' for key, value in attrs.items())
        xml_nodes.append(f"<node {encoded} />")
    xml = '<?xml version="1.0" encoding="UTF-8"?><hierarchy>' + "".join(xml_nodes) + "</hierarchy>"
    return parse_uiautomator_xml(
        xml,
        package=package,
        activity=activity,
        window_size=(1080, 2400),
    )


@dataclass
class FakeDevice:
    states: list[ScreenSnapshot]
    installed_apps: list[dict[str, object]] = field(default_factory=list)
    device_id: str = "fake-device"
    state_index: int = 0
    actions: list[tuple[str, object]] = field(default_factory=list)
    alive: bool = True

    async def snapshot(self) -> ScreenSnapshot:
        return self.states[min(self.state_index, len(self.states) - 1)]

    def advance(self) -> None:
        if self.state_index < len(self.states) - 1:
            self.state_index += 1

    async def launch_app(
        self, package: str, *, operation_id: str | None = None
    ) -> None:
        del operation_id
        self.actions.append(("launch", package))
        self.advance()

    async def tap(
        self, x: int, y: int, *, operation_id: str | None = None
    ) -> None:
        del operation_id
        self.actions.append(("tap", (x, y)))
        self.advance()

    async def type_text(
        self,
        text: str,
        *,
        clear: bool = False,
        element: dict[str, object] | None = None,
        operation_id: str | None = None,
    ) -> None:
        del operation_id
        self.actions.append(("type", {"text": text, "clear": clear, "element": element}))
        self.advance()

    async def clear_active(
        self,
        *,
        element: dict[str, object] | None = None,
        operation_id: str | None = None,
    ) -> None:
        del operation_id
        self.actions.append(("clear", element))
        self.advance()

    async def swipe(
        self,
        direction: str,
        *,
        percent: float = 0.72,
        operation_id: str | None = None,
    ) -> None:
        del operation_id
        self.actions.append(("swipe", {"direction": direction, "percent": percent}))
        self.advance()

    async def back(self, *, operation_id: str | None = None) -> None:
        del operation_id
        self.actions.append(("back", None))
        self.advance()

    async def home(self, *, operation_id: str | None = None) -> None:
        del operation_id
        self.actions.append(("home", None))
        self.advance()

    async def list_apps(self) -> list[dict[str, object]]:
        return list(self.installed_apps)

    async def screenshot_png(self) -> bytes:
        return b"fake-png"

    async def is_alive(self) -> bool:
        return self.alive

    async def close(self) -> None:
        self.alive = False
