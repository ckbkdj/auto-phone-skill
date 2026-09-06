from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

from lobster_phone_agent.util.text import compact_text, normalize_text, redact_text

_BOUNDS = re.compile(r"\[(?P<x1>-?\d+),(?P<y1>-?\d+)]\[(?P<x2>-?\d+),(?P<y2>-?\d+)]")
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(slots=True, frozen=True)
class Bounds:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(0, self.y2 - self.y1)

    @property
    def center(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    @property
    def area(self) -> int:
        return self.width * self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)

    def intersects(self, width: int, height: int) -> bool:
        if self.area <= 0 or self.x2 <= 0 or self.y2 <= 0:
            return False
        if width <= 0 or height <= 0:
            return True
        return self.x1 < width and self.y1 < height

    def clamped_center(self, width: int, height: int) -> tuple[int, int]:
        x, y = self.center
        return (
            min(max(x, 0), max(width - 1, 0)),
            min(max(y, 0), max(height - 1, 0)),
        )


@dataclass(slots=True)
class UiNode:
    index: int
    class_name: str
    package: str
    text: str
    content_desc: str
    resource_id: str
    bounds: Bounds | None
    clickable: bool
    enabled: bool
    focusable: bool
    focused: bool
    scrollable: bool
    selected: bool
    checked: bool
    password: bool
    displayed: bool
    depth: int
    path: str

    @property
    def label(self) -> str:
        candidates = (self.text, self.content_desc, self.resource_id.rsplit("/", 1)[-1])
        return next((value for value in candidates if value), "")

    @property
    def role(self) -> str:
        name = self.class_name.rsplit(".", 1)[-1].lower()
        if "button" in name:
            return "button"
        if any(part in name for part in ("edittext", "textfield", "autocomplete")):
            return "input"
        if "checkbox" in name:
            return "checkbox"
        if "radiobutton" in name:
            return "radio"
        if "switch" in name:
            return "switch"
        if "image" in name:
            return "image"
        if "text" in name:
            return "text"
        if self.scrollable:
            return "scrollable"
        return "view"

    @property
    def editable(self) -> bool:
        return self.role == "input" or (self.focusable and "edit" in self.class_name.lower())

    @property
    def stable_key(self) -> str:
        raw = "|".join(
            (
                self.package,
                self.resource_id,
                compact_text(self.text),
                compact_text(self.content_desc),
                self.class_name,
                str(self.bounds.as_tuple() if self.bounds else ""),
                self.path,
            )
        )
        return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    def compact(self) -> str:
        flags = "".join(
            (
                "C" if self.clickable else "",
                "E" if self.enabled else "",
                "F" if self.focused else "",
                "S" if self.scrollable else "",
                "K" if self.checked else "",
            )
        ) or "-"
        bounds = self.bounds.as_tuple() if self.bounds else "-"
        text = "<password>" if self.password else redact_text(self.text, 80)
        desc = redact_text(self.content_desc, 80)
        rid = self.resource_id[-90:]
        return (
            f"n={self.index} role={self.role} flags={flags} b={bounds} "
            f"text={text!r} desc={desc!r} id={rid!r}"
        )

    def interaction_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "resource_id": self.resource_id,
            "accessibility_id": self.content_desc,
            "text": self.text,
            "class_name": self.class_name,
            "bounds": self.bounds.as_tuple() if self.bounds else None,
            "path": self.path,
        }


@dataclass(slots=True)
class ScreenSnapshot:
    package: str
    activity: str
    nodes: list[UiNode]
    xml_hash: str
    fingerprint: str
    width: int
    height: int
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def keyboard_visible(self) -> bool:
        return any(
            any(
                marker in node.package.lower()
                for marker in ("inputmethod", "keyboard", "latin", "sogou", "baidu.input")
            )
            for node in self.nodes
        )

    def labels(self) -> list[str]:
        return [node.label for node in self.nodes if node.label and not node.password]

    def contains_text(self, value: str) -> bool:
        needle = compact_text(value)
        return bool(needle) and any(
            needle in compact_text(candidate)
            for node in self.nodes
            for candidate in (node.text, node.content_desc, node.resource_id)
        )

    def compact(self, max_nodes: int = 90) -> str:
        relevant = [
            node
            for node in self.nodes
            if node.displayed
            and (
                node.clickable
                or node.focusable
                or node.scrollable
                or bool(node.text)
                or bool(node.content_desc)
                or bool(node.resource_id)
            )
        ]
        relevant.sort(
            key=lambda node: (
                not node.clickable,
                not node.focusable,
                not bool(node.text or node.content_desc),
                node.depth,
                node.index,
            )
        )
        lines = [
            f"package={self.package} activity={self.activity} size={self.width}x{self.height}",
            f"fingerprint={self.fingerprint} keyboard={self.keyboard_visible}",
        ]
        lines.extend(node.compact() for node in relevant[:max_nodes])
        if len(relevant) > max_nodes:
            lines.append(f"... {len(relevant) - max_nodes} nodes omitted")
        return "\n".join(lines)

    def diff(self, previous: ScreenSnapshot | None, max_items: int = 40) -> str:
        if previous is None:
            return self.compact(max_nodes=max_items)
        old = {node.stable_key: node for node in previous.nodes}
        new = {node.stable_key: node for node in self.nodes}
        added = [new[key] for key in new.keys() - old.keys()]
        removed = [old[key] for key in old.keys() - new.keys()]
        lines = [
            f"package:{previous.package}->{self.package}",
            f"activity:{previous.activity}->{self.activity}",
            f"fingerprint:{previous.fingerprint}->{self.fingerprint}",
        ]
        lines.extend(f"+ {node.compact()}" for node in added[: max_items // 2])
        lines.extend(f"- {node.compact()}" for node in removed[: max_items // 2])
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "activity": self.activity,
            "xml_hash": self.xml_hash,
            "fingerprint": self.fingerprint,
            "width": self.width,
            "height": self.height,
            "captured_at": self.captured_at.isoformat(),
            "nodes": [
                {
                    **{key: value for key, value in asdict(node).items() if key != "bounds"},
                    "bounds": node.bounds.as_tuple() if node.bounds else None,
                }
                for node in self.nodes
            ],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ScreenSnapshot:
        raw_nodes = payload.get("nodes", [])
        if not isinstance(raw_nodes, list):
            raise ValueError("snapshot nodes must be a list")
        if len(raw_nodes) > 3000:
            raise ValueError("snapshot contains more than 3000 nodes")
        nodes = []
        for item in raw_nodes:
            if not isinstance(item, dict):
                raise ValueError("snapshot node must be an object")
            data = dict(item)
            bounds = data.get("bounds")
            data["bounds"] = Bounds(*map(int, bounds)) if bounds else None
            nodes.append(UiNode(**data))
        captured = payload.get("captured_at")
        captured_at = datetime.fromisoformat(captured) if captured else datetime.now(UTC)
        return cls(
            package=str(payload.get("package", "")),
            activity=str(payload.get("activity", "")),
            nodes=nodes,
            xml_hash=str(payload.get("xml_hash", "")),
            fingerprint=str(payload.get("fingerprint", "")),
            width=int(payload.get("width", 0)),
            height=int(payload.get("height", 0)),
            captured_at=captured_at,
        )


def parse_bounds(value: str | None) -> Bounds | None:
    if not value:
        return None
    match = _BOUNDS.fullmatch(value.strip())
    if not match:
        return None
    return Bounds(*(int(match.group(key)) for key in ("x1", "y1", "x2", "y2")))


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() == "true"


def _attr(attrs: dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        if name in attrs:
            return attrs[name]
    return default


def parse_uiautomator_xml(
    xml: str,
    *,
    package: str = "",
    activity: str = "",
    window_size: tuple[int, int] | None = None,
) -> ScreenSnapshot:
    if len(xml.encode("utf-8")) > 2_000_000 or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise ValueError("untrusted or oversized XML hierarchy")
    cleaned = _INVALID_XML.sub("", xml)
    if not cleaned.strip():
        raise ValueError("empty UI hierarchy")
    root = ET.fromstring(cleaned)
    nodes: list[UiNode] = []

    def walk(element: ET.Element, depth: int, path: str) -> None:
        if depth > 128 or len(nodes) >= 3000:
            raise ValueError("UI hierarchy limit exceeded")
        attrs = element.attrib
        node_bounds = parse_bounds(_attr(attrs, "bounds", "rect"))
        if element.tag == "node" or any(
            key in attrs
            for key in (
                "class",
                "className",
                "resource-id",
                "resourceId",
                "text",
                "content-desc",
                "contentDescription",
            )
        ):
            nodes.append(
                UiNode(
                    index=len(nodes),
                    class_name=_attr(attrs, "class", "className", default=element.tag),
                    package=_attr(attrs, "package", default=package),
                    text="" if _as_bool(_attr(attrs, "password")) else _attr(attrs, "text"),
                    content_desc="" if _as_bool(_attr(attrs, "password")) else _attr(attrs, "content-desc", "contentDescription", "name"),
                    resource_id=_attr(attrs, "resource-id", "resourceId"),
                    bounds=node_bounds,
                    clickable=_as_bool(_attr(attrs, "clickable")),
                    enabled=_as_bool(_attr(attrs, "enabled", default="true"), True),
                    focusable=_as_bool(_attr(attrs, "focusable")),
                    focused=_as_bool(_attr(attrs, "focused")),
                    scrollable=_as_bool(_attr(attrs, "scrollable")),
                    selected=_as_bool(_attr(attrs, "selected")),
                    checked=_as_bool(_attr(attrs, "checked")),
                    password=_as_bool(_attr(attrs, "password")),
                    displayed=not (_attr(attrs, "displayed", "visible") == "false"),
                    depth=depth,
                    path=path,
                )
            )
        for child_index, child in enumerate(element):
            walk(child, depth + 1, f"{path}/{child_index}")

    walk(root, 0, "0")
    if window_size and window_size[0] > 0 and window_size[1] > 0:
        width, height = window_size
    else:
        bounds = [node.bounds for node in nodes if node.bounds]
        width = max((bound.x2 for bound in bounds), default=0)
        height = max((bound.y2 for bound in bounds), default=0)

    resolved_package = package or next((node.package for node in nodes if node.package), "")
    xml_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
    meaningful = sorted(
        {
            "|".join(
                (
                    str(node.index), node.path, node.class_name,
                    "1" if node.enabled else "0",
                    "1" if node.password else "0",
                    node.resource_id,
                    compact_text(node.text),
                    compact_text(node.content_desc),
                    node.role,
                    str(node.bounds.as_tuple() if node.bounds else ""),
                    "1" if node.clickable else "0",
                    "1" if node.focused else "0",
                    "1" if node.checked else "0",
                    "1" if node.selected else "0",
                )
            )
            for node in nodes
            if node.displayed
            and (node.resource_id or node.text or node.content_desc or node.clickable)
        }
    )
    fingerprint_raw = (
        f"{resolved_package}|{activity}|{width}x{height}|"
        + "\n".join(meaningful)
    )
    fingerprint = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()[:16]
    return ScreenSnapshot(
        package=resolved_package,
        activity=activity,
        nodes=nodes,
        xml_hash=xml_hash,
        fingerprint=fingerprint,
        width=width,
        height=height,
    )


def iter_ancestors(node: UiNode, nodes: Iterable[UiNode]) -> Iterable[UiNode]:
    prefixes = node.path.split("/")
    ancestor_paths = {"/".join(prefixes[:index]) for index in range(1, len(prefixes))}
    candidates = [candidate for candidate in nodes if candidate.path in ancestor_paths]
    yield from sorted(candidates, key=lambda candidate: candidate.depth, reverse=True)


def iter_descendants(node: UiNode, nodes: Iterable[UiNode]) -> Iterable[UiNode]:
    prefix = f"{node.path}/"
    candidates = [candidate for candidate in nodes if candidate.path.startswith(prefix)]
    yield from sorted(candidates, key=lambda candidate: candidate.depth)
