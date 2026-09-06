"""Per-method request AND response contracts for the restricted Skill RPC."""
from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    Field, StrictBool, StrictFloat, StrictInt, StrictStr, TypeAdapter,
    ValidationError, model_validator,
)

from lobster_phone_agent.contracts import (
    ContractModel, IDENTIFIER_PATTERN, PACKAGE_PATTERN, bounded_extension,
)
from lobster_phone_agent.errors import BridgeProtocolError

Coordinate = Annotated[StrictInt, Field(ge=-100_000, le=100_000)]
Pixel = Annotated[StrictInt, Field(ge=0, le=100_000)]
Bounds = tuple[Coordinate, Coordinate, Coordinate, Coordinate]


class EmptyParams(ContractModel):
    pass


class MutationParams(ContractModel):
    operation_id: StrictStr = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9_.:-]+$")


class LaunchParams(MutationParams):
    package: StrictStr = Field(min_length=3, max_length=255, pattern=PACKAGE_PATTERN)


class TapParams(MutationParams):
    x: Pixel
    y: Pixel


class ElementHint(ContractModel):
    index: StrictInt = Field(ge=0, le=3000)
    resource_id: StrictStr = Field(default="", max_length=512)
    accessibility_id: StrictStr = Field(default="", max_length=4096)
    text: StrictStr = Field(default="", max_length=4096)
    class_name: StrictStr = Field(default="", max_length=512)
    bounds: Bounds | None = None
    path: StrictStr = Field(default="", max_length=512, pattern=r"^(?:\d+(?:/\d+)*)?$")


class ClearParams(MutationParams):
    element: ElementHint | None = None


class TypeParams(ClearParams):
    text: StrictStr = Field(max_length=4000)
    clear: StrictBool = False


class SwipeParams(MutationParams):
    direction: Literal["up", "down", "left", "right"]
    percent: StrictFloat = Field(default=0.72, ge=0.05, le=1.0)


class PerformedResult(ContractModel):
    performed: StrictBool


class ClosedResult(ContractModel):
    closed: StrictBool


class SnapshotNode(ContractModel):
    index: StrictInt = Field(ge=0, le=3000)
    class_name: StrictStr = Field(max_length=512)
    package: StrictStr = Field(max_length=255)
    text: StrictStr = Field(max_length=4096)
    content_desc: StrictStr = Field(max_length=4096)
    resource_id: StrictStr = Field(max_length=512)
    bounds: Bounds | None
    clickable: StrictBool
    enabled: StrictBool
    focusable: StrictBool
    focused: StrictBool
    scrollable: StrictBool
    selected: StrictBool
    checked: StrictBool
    password: StrictBool
    displayed: StrictBool
    depth: StrictInt = Field(ge=0, le=128)
    path: StrictStr = Field(min_length=1, max_length=512, pattern=r"^\d+(?:/\d+)*$")

    @model_validator(mode="after")
    def redact_password(self) -> SnapshotNode:
        if self.password:
            self.text = ""
            self.content_desc = ""
        return self


class SnapshotResult(ContractModel):
    package: StrictStr = Field(max_length=255)
    activity: StrictStr = Field(max_length=512)
    xml_hash: StrictStr = Field(min_length=1, max_length=128)
    fingerprint: StrictStr = Field(min_length=1, max_length=128)
    width: Pixel
    height: Pixel
    captured_at: datetime
    nodes: list[SnapshotNode] = Field(max_length=3000)

    @model_validator(mode="after")
    def unique_nodes(self) -> SnapshotResult:
        indices = [node.index for node in self.nodes]
        if len(indices) != len(set(indices)):
            raise ValueError("snapshot node indices must be unique")
        if self.captured_at.tzinfo is None:
            raise ValueError("snapshot timestamp must contain a timezone")
        return self


class InstalledApp(ContractModel):
    package: StrictStr = Field(min_length=3, max_length=255, pattern=PACKAGE_PATTERN)
    name: StrictStr | None = Field(default=None, max_length=128)
    label: StrictStr | None = Field(default=None, max_length=128)
    activity: StrictStr | None = Field(default=None, max_length=512)
    details: dict[str, Any] | None = None

    @model_validator(mode="after")
    def bounded_details(self) -> InstalledApp:
        if self.details is not None:
            bounded_extension(self.details)
        return self


class GuardedParams(ContractModel):
    fingerprint: StrictStr = Field(min_length=1, max_length=128)
    method: Literal["launch_app", "tap", "type_text", "clear_active", "swipe", "back", "home"]
    params: dict[StrictStr, Any]

    @model_validator(mode="after")
    def validate_method(self):
        self.params = validate_rpc_params(self.method, self.params)
        return self


class GuardedResult(ContractModel):
    state: Literal["executed", "not_executed", "unknown"]
    code: Literal["ok", "stale_screen", "invalid_target", "protected_surface", "outcome_unknown", "reconciled_no_effect"]

    @model_validator(mode="after")
    def consistent(self):
        allowed = {"executed": {"ok"}, "unknown": {"outcome_unknown"},
                   "not_executed": {"stale_screen", "invalid_target", "protected_surface", "reconciled_no_effect"}}
        if self.code not in allowed[self.state]:
            raise ValueError("inconsistent guarded result")
        return self


PARAM_MODELS = {
    "guarded_action": GuardedParams,
    "snapshot": EmptyParams, "launch_app": LaunchParams, "tap": TapParams,
    "type_text": TypeParams, "clear_active": ClearParams, "swipe": SwipeParams,
    "back": MutationParams, "home": MutationParams, "list_apps": EmptyParams,
    "screenshot_png": EmptyParams, "is_alive": EmptyParams, "close": EmptyParams,
}
RESULT_ADAPTERS = {
    "guarded_action": TypeAdapter(GuardedResult),
    "snapshot": TypeAdapter(SnapshotResult),
    "list_apps": TypeAdapter(Annotated[list[InstalledApp], Field(max_length=3000)]),
    "screenshot_png": TypeAdapter(Annotated[StrictStr, Field(max_length=1_800_000)]),
    "is_alive": TypeAdapter(StrictBool),
    "close": TypeAdapter(ClosedResult),
    **{name: TypeAdapter(PerformedResult) for name in (
        "launch_app", "tap", "type_text", "clear_active", "swipe", "back", "home"
    )},
}


def validate_rpc_params(method: str, params: Any) -> dict[str, Any]:
    model = PARAM_MODELS.get(method)
    if model is None:
        raise BridgeProtocolError("RPC method is not allowlisted")
    if issubclass(model, MutationParams) and isinstance(params, dict) and "operation_id" not in params:
        raise BridgeProtocolError(f"mutating RPC {method} requires operation_id")
    try:
        return model.model_validate(params).model_dump(mode="json", exclude_none=True)
    except (ValidationError, TypeError, ValueError) as exc:
        # Never put raw text, selectors, tokens or endpoints into a public error.
        raise BridgeProtocolError(f"invalid RPC parameters for {method}") from exc


def validate_rpc_result(method: str, payload: Any) -> Any:
    adapter = RESULT_ADAPTERS.get(method)
    if adapter is None:
        raise BridgeProtocolError("RPC result method is not allowlisted")
    try:
        validated = adapter.validate_python(payload)
        result = adapter.dump_python(validated, mode="json", exclude_none=method != "snapshot")
        if method == "screenshot_png":
            raw = base64.b64decode(result, validate=True)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("screenshot is not PNG")
        return result
    except (ValidationError, TypeError, ValueError, binascii.Error) as exc:
        raise BridgeProtocolError(f"invalid RPC result for {method}") from exc


def rpc_contracts() -> dict[str, Any]:
    """Export the same model instances used by both endpoints; no handwritten schema drift."""
    return {
        "protocol": "1.0",
        "methods": {
            name: {"params": model.model_json_schema(), "result": RESULT_ADAPTERS[name].json_schema()}
            for name, model in PARAM_MODELS.items()
        },
    }
