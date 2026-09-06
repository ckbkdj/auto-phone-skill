from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")


class AtomicAction(Closed):
    """Exactly one semantic UI operation. Unused fields MUST be null."""

    kind: Literal["launch_app", "tap", "type", "clear", "swipe", "back", "home", "wait"]
    node_id: StrictInt | None = Field(ge=0, le=2999)
    text: StrictStr | None = Field(max_length=1000)
    package: StrictStr | None = Field(max_length=255, pattern=r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
    direction: Literal["up", "down", "left", "right"] | None
    wait_ms: StrictInt | None = Field(ge=50, le=1500)

    @model_validator(mode="after")
    def shape(self):
        requirements = {
            "launch_app": {"package"}, "tap": {"node_id"}, "type": {"node_id", "text"},
            "clear": {"node_id"}, "swipe": {"direction"}, "back": set(), "home": set(),
            "wait": {"wait_ms"},
        }
        provided = {key for key in ("node_id", "text", "package", "direction", "wait_ms")
                    if getattr(self, key) is not None}
        if provided != requirements[self.kind]:
            raise ValueError("action fields do not match the selected kind")
        return self

    @classmethod
    def make(cls, kind: str, **values):
        return cls.model_validate({"kind": kind, "node_id": None, "text": None,
                                   "package": None, "direction": None, "wait_ms": None, **values})


class Evidence(Closed):
    kind: Literal["text_present", "text_absent", "resource_present", "package_is", "activity_contains", "screen_changed"]
    value: StrictStr = Field(max_length=256)

    @model_validator(mode="after")
    def shape(self):
        if self.kind == "screen_changed":
            if self.value != "":
                raise ValueError("screen_changed must use an empty value")
        elif not self.value.strip():
            raise ValueError("evidence value must not be blank")
        return self


class Decision(Closed):
    version: Literal["2.0"]
    observation_id: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")
    status: Literal["act", "done", "handoff"]
    action: AtomicAction | None
    checks: list[Evidence] = Field(max_length=4)
    summary: StrictStr = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def shape(self):
        if (self.status == "act") != (self.action is not None):
            raise ValueError("only act may contain exactly one action")
        if self.status == "act" and not self.checks:
            raise ValueError("an action needs an observable postcondition")
        if self.status == "done" and (not self.checks or any(
            item.kind in {"screen_changed", "text_absent"} for item in self.checks
        )):
            raise ValueError("completion requires positive current-screen evidence")
        if self.status == "handoff" and self.checks:
            raise ValueError("handoff cannot claim verified completion")
        return self


class AtomicRequest(Closed):
    operation_id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    fingerprint: StrictStr = Field(min_length=1, max_length=128)
    action: AtomicAction


class AtomicResult(Closed):
    state: Literal["executed", "not_executed", "unknown"]
    code: Literal["ok", "stale_screen", "invalid_target", "blocked_surface", "execution_uncertain", "reconciled_no_effect"]

    @model_validator(mode="after")
    def consistent(self):
        allowed = {"executed": {"ok"}, "not_executed": {"stale_screen", "invalid_target", "blocked_surface", "reconciled_no_effect"},
                   "unknown": {"execution_uncertain"}}
        if self.code not in allowed[self.state]:
            raise ValueError("inconsistent operation outcome")
        return self
