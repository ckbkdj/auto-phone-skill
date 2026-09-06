from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, WithJsonSchema, field_validator, model_validator

from lobster_phone_agent.contracts import (
    ContractModel, PACKAGE_PATTERN, action_schema_rules, bounded_extension,
)


from lobster_phone_agent.apps.registry import AppRecord

CatalogEntry = Annotated[dict[str, Any], WithJsonSchema(AppRecord.model_json_schema())]

def utc_now() -> datetime:
    return datetime.now(UTC)


class ActionType(StrEnum):
    LAUNCH_APP = "launch_app"
    TAP = "tap"
    TYPE = "type"
    CLEAR = "clear"
    SWIPE = "swipe"
    BACK = "back"
    HOME = "home"
    WAIT = "wait"
    ASSERT = "assert"
    HANDOFF = "handoff"
    FINISH = "finish"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    WAITING_BRIDGE = "waiting_bridge"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_HANDOFF = "waiting_handoff"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    TASK_CREATED = "task.created"
    BRIDGE_WAITING = "bridge.waiting"
    BRIDGE_CONNECTED = "bridge.connected"
    PLAN_READY = "plan.ready"
    STEP_STARTED = "step.started"
    STEP_SUCCEEDED = "step.succeeded"
    STEP_RETRYING = "step.retrying"
    STEP_FAILED = "step.failed"
    CONFIRMATION_REQUIRED = "confirmation.required"
    HANDOFF_REQUIRED = "handoff.required"
    TASK_SUCCEEDED = "task.succeeded"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    HEARTBEAT = "heartbeat"


class ConfirmationMode(StrEnum):
    RISK_BASED = "risk_based"
    EVERY_MUTATION = "every_mutation"
    NONE = "none"


class DeviceDescriptor(ContractModel):
    """Public, non-secret reference to a device registered by a private skill bridge."""

    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    bridge_id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    metadata: dict[StrictStr, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_connection_secrets(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        forbidden = {
            "appium_url",
            "udid",
            "device_name",
            "system_port",
            "capabilities",
            "bridge_token",
            "adb_host",
            "adb_port",
        }
        present = sorted(forbidden.intersection(value))
        if present:
            raise ValueError(
                "device connection details are private to the skill bridge and are forbidden in "
                f"the public API: {', '.join(present)}"
            )
        return value


class LocationContext(ContractModel):
    address: StrictStr | None = Field(default=None, max_length=1000)
    latitude: StrictFloat | None = Field(default=None, ge=-90, le=90)
    longitude: StrictFloat | None = Field(default=None, ge=-180, le=180)
    coordinate_system: Literal["wgs84", "gcj02", "bd09", "unknown"] = "unknown"


    @model_validator(mode="after")
    def coordinate_pair(self) -> LocationContext:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class ExecutionPolicy(ContractModel):
    confirmation_mode: ConfirmationMode = ConfirmationMode.RISK_BASED
    preauthorized_risks: set[RiskLevel] = Field(default_factory=set)
    allow_irreversible: StrictBool = False
    allow_messages: StrictBool = False
    allow_purchases: StrictBool = False
    allow_payments: StrictBool = False
    max_steps: StrictInt | None = Field(default=None, ge=1, le=200)
    max_repairs: StrictInt | None = Field(default=None, ge=0, le=20)


class TaskInput(ContractModel):
    instruction: StrictStr = Field(min_length=1, max_length=4000)
    location: LocationContext | None = None
    locale: StrictStr = Field(default="zh-CN", max_length=35, pattern=r"^[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*$")
    timezone: StrictStr = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    app_package: StrictStr | None = Field(default=None, max_length=255, pattern=PACKAGE_PATTERN)
    app_catalog: list[CatalogEntry] = Field(default_factory=list, max_length=500)
    context: dict[StrictStr, Any] = Field(default_factory=dict)
    policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    idempotency_key: StrictStr | None = Field(default=None, max_length=200)


    @field_validator("instruction")
    @classmethod
    def nonblank_instruction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instruction must not be whitespace")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @field_validator("app_catalog")
    @classmethod
    def catalog_contract(cls, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from lobster_phone_agent.apps.registry import AppRecord
        for value in values:
            bounded_extension(value)
            AppRecord.model_validate(value)
        return values


class TaskRequest(TaskInput):
    device: DeviceDescriptor


class LocalTaskRequest(TaskInput):
    device_id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class SemanticTarget(ContractModel):
    text: StrictStr | None = None
    aliases: list[StrictStr] = Field(default_factory=list, max_length=32)
    resource_id: StrictStr | None = None
    accessibility_id: StrictStr | None = None
    role: Literal["button", "input", "checkbox", "radio", "switch", "image", "text", "scrollable", "view"] | None = None
    package: StrictStr | None = None
    index: StrictInt | None = Field(default=None, ge=0, le=3000)
    near_text: StrictStr | None = None
    coordinates: tuple[StrictInt, StrictInt] | None = None
    allow_partial: StrictBool = True

    @field_validator("aliases")
    @classmethod
    def unique_aliases(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("coordinates")
    @classmethod
    def bounded_coordinates(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is not None and any(item < 0 or item > 100_000 for item in value):
            raise ValueError("coordinates are outside supported range")
        return value

    @model_validator(mode="after")
    def nonempty_target(self) -> SemanticTarget:
        if not any((self.text, self.aliases, self.resource_id, self.accessibility_id,
                    self.role, self.coordinates)):
            raise ValueError("target must specify a semantic locator or coordinates")
        return self

    def labels(self) -> list[str]:
        result: list[StrictStr] = []
        if self.text:
            result.append(self.text)
        result.extend(self.aliases)
        if self.accessibility_id:
            result.append(self.accessibility_id)
        return list(dict.fromkeys(result))


class ConditionKind(StrEnum):
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    ELEMENT_PRESENT = "element_present"
    ELEMENT_ABSENT = "element_absent"
    PACKAGE_IS = "package_is"
    PACKAGE_CONTAINS = "package_contains"
    ACTIVITY_CONTAINS = "activity_contains"
    SCREEN_CHANGED = "screen_changed"
    KEYBOARD_VISIBLE = "keyboard_visible"
    ANY = "any"
    ALL = "all"


class ScreenCondition(ContractModel):
    kind: ConditionKind
    value: StrictStr | None = None
    target: SemanticTarget | None = None
    children: list[ScreenCondition] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_payload(self) -> ScreenCondition:
        composite = self.kind in {ConditionKind.ANY, ConditionKind.ALL}
        if composite:
            if not self.children or self.value is not None or self.target is not None:
                raise ValueError("composite condition requires only children")
        elif self.children:
            raise ValueError("leaf conditions must not have children")
        if self.kind in {ConditionKind.ELEMENT_PRESENT, ConditionKind.ELEMENT_ABSENT}:
            if self.target is None or self.value is not None:
                raise ValueError("element condition requires only target")
        elif self.target is not None:
            raise ValueError("target is only valid for element conditions")
        if self.kind in {ConditionKind.TEXT_PRESENT, ConditionKind.TEXT_ABSENT,
                         ConditionKind.PACKAGE_IS, ConditionKind.PACKAGE_CONTAINS,
                         ConditionKind.ACTIVITY_CONTAINS} and not (self.value or "").strip():
            raise ValueError("text/package/activity condition requires a nonblank value")
        if self.kind is ConditionKind.KEYBOARD_VISIBLE and self.value not in {None, "true", "false"}:
            raise ValueError("keyboard condition value must be true or false")
        if self.kind is ConditionKind.SCREEN_CHANGED and self.value is not None:
            raise ValueError("screen_changed does not accept value")
        return self


class RetryPolicy(ContractModel):
    max_attempts: StrictInt = Field(default=2, ge=1, le=8)
    delay_ms: StrictInt = Field(default=180, ge=0, le=5000)
    allow_repair: StrictBool = True


class ActionStep(ContractModel):
    model_config = ConfigDict(json_schema_extra=action_schema_rules)

    id: StrictStr = Field(
        default_factory=lambda: f"step_{uuid4().hex[:8]}",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    action: ActionType
    description: StrictStr = ""
    target: SemanticTarget | None = None
    value: StrictStr | StrictInt | StrictFloat | dict[StrictStr, Any] | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    preconditions: list[ScreenCondition] = Field(default_factory=list, max_length=32)
    expect: list[ScreenCondition] = Field(default_factory=list, max_length=32)
    optional: StrictBool = False
    timeout_ms: StrictInt = Field(default=2500, ge=100, le=60000)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    risk: RiskLevel = RiskLevel.LOW
    confirmation_text: StrictStr | None = None
    metadata: dict[StrictStr, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action_fields(self) -> ActionStep:
        if self.action in {ActionType.TAP, ActionType.TYPE, ActionType.CLEAR} and not self.target:
            raise ValueError(f"{self.action} requires target")
        if self.action is ActionType.LAUNCH_APP and not self.value:
            raise ValueError("launch_app requires value")
        if self.action in {ActionType.TYPE, ActionType.LAUNCH_APP} and not isinstance(self.value, str):
            raise ValueError("type and launch_app require a string value")
        if self.action is ActionType.LAUNCH_APP and (not self.value.strip() or len(self.value) > 255):
            raise ValueError("launch_app requires a bounded nonblank app name")
        if self.action is ActionType.WAIT and (type(self.value) is not int or not 0 <= self.value <= 60_000):
            raise ValueError("wait requires an integer duration between 0 and 60000 ms")
        if self.action in {ActionType.TAP, ActionType.CLEAR, ActionType.SWIPE, ActionType.BACK,
                          ActionType.HOME, ActionType.ASSERT} and self.value is not None:
            raise ValueError("this action does not accept value")
        if self.action not in {ActionType.TAP, ActionType.TYPE, ActionType.CLEAR, ActionType.ASSERT} and self.target is not None:
            raise ValueError("this action does not accept target")
        if self.action is not ActionType.SWIPE and self.direction is not None:
            raise ValueError("direction is only valid for swipe")
        if self.action is ActionType.FINISH and self.value is not None:
            if not isinstance(self.value, (dict, str)):
                raise ValueError("finish requires an object, string, or null value")
            bounded_extension(self.value)
        if self.action is ActionType.HANDOFF and self.value is not None and not isinstance(self.value, str):
            raise ValueError("handoff value must be a string")
        if self.action is ActionType.ASSERT and not (self.target or self.expect or self.preconditions):
            raise ValueError("assert requires a condition or target")
        if self.target and self.target.coordinates is not None and not self.metadata.get("screen_fingerprint"):
            raise ValueError("coordinate actions require a screen_fingerprint")
        allowed_metadata = {"screen_fingerprint", "clear_first", "require_screen_change",
                            "allow_noop", "allow_scroll_search", "scroll_direction"}
        if set(self.metadata) - allowed_metadata:
            raise ValueError("unknown action metadata field")
        for key in {"clear_first", "require_screen_change", "allow_noop", "allow_scroll_search"}:
            if key in self.metadata and type(self.metadata[key]) is not bool:
                raise ValueError("action metadata flags require JSON booleans")
        if "screen_fingerprint" in self.metadata and (not isinstance(self.metadata["screen_fingerprint"], str) or not 1 <= len(self.metadata["screen_fingerprint"]) <= 128):
            raise ValueError("invalid screen_fingerprint")
        if "scroll_direction" in self.metadata and self.metadata["scroll_direction"] not in {"up", "down", "left", "right"}:
            raise ValueError("invalid scroll_direction")
        if self.action is ActionType.SWIPE and not self.direction:
            raise ValueError("swipe requires direction")
        return self


class ActionPlan(ContractModel):
    version: Literal["1.0"] = "1.0"
    goal: StrictStr = Field(min_length=1, max_length=4000)
    target_app: StrictStr | None = None
    target_package: StrictStr | None = Field(default=None, max_length=255, pattern=PACKAGE_PATTERN)
    planner: Literal["recipe", "llm", "repair", "manual"] = "manual"
    steps: list[ActionStep] = Field(min_length=1, max_length=200)
    success: list[ScreenCondition] = Field(default_factory=list, max_length=32)
    metadata: dict[StrictStr, Any] = Field(default_factory=dict)

    @field_validator("steps")
    @classmethod
    def nonempty_unique_steps(cls, steps: list[ActionStep]) -> list[ActionStep]:
        if not steps:
            raise ValueError("plan must contain at least one step")
        ids = [step.id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        return steps


class TaskEvent(ContractModel):
    id: StrictStr = Field(default_factory=lambda: uuid4().hex)
    task_id: StrictStr
    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    message: StrictStr = ""
    data: dict[StrictStr, Any] = Field(default_factory=dict)


class StepTrace(ContractModel):
    step_id: StrictStr
    action: ActionType
    started_at: datetime
    ended_at: datetime | None = None
    dispatch_ms: StrictFloat | None = None
    observation_ms: StrictFloat | None = None
    selected_node: dict[StrictStr, Any] | None = None
    attempts: StrictInt = Field(default=0, ge=0, le=1000)
    success: StrictBool = False
    error: StrictStr | None = None


class TaskRecord(ContractModel):
    id: StrictStr = Field(default_factory=lambda: uuid4().hex)
    request: TaskRequest
    status: TaskStatus = TaskStatus.QUEUED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    plan: ActionPlan | None = None
    current_step: StrictInt = Field(default=0, ge=0, le=200)
    result: dict[StrictStr, Any] | None = None
    error: StrictStr | None = None
    confirmation: dict[StrictStr, Any] | None = None
    traces: list[StepTrace] = Field(default_factory=list)


class ConfirmationRequest(ContractModel):
    approved: StrictBool
    token: StrictStr = Field(min_length=8, max_length=200)
    note: StrictStr | None = Field(default=None, max_length=1000)


class HandoffResumeRequest(ContractModel):
    resume: StrictBool = True
    token: StrictStr = Field(min_length=8, max_length=200)
    note: StrictStr | None = Field(default=None, max_length=1000)


class HealthResponse(ContractModel):
    status: Literal["ok"] = "ok"
    version: StrictStr
    llm_enabled: StrictBool
    active_tasks: StrictInt = Field(ge=0)
    active_devices: StrictInt = Field(ge=0)
    connected_bridges: StrictInt = Field(default=0, ge=0)


class EmptyRequest(ContractModel):
    pass


class ReadyResponse(ContractModel):
    ready: Literal[True] = True
    llm_enabled: StrictBool


class AppCatalogResponse(ContractModel):
    count: StrictInt = Field(ge=0)
    apps: list[AppRecord]
    note: StrictStr


class SkillHealthResponse(ContractModel):
    status: Literal["ok"] = "ok"
    bridge_connected: StrictBool
    bridge_id: StrictStr
    devices: list[StrictStr]
    scope: Literal["loopback-only"] = "loopback-only"
