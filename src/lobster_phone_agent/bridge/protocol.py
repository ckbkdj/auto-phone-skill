from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

from lobster_phone_agent.contracts import ContractModel
from lobster_phone_agent.bridge.contracts import validate_rpc_params

PROTOCOL_VERSION = "1.0"


class BridgeMessageType(StrEnum):
    HELLO = "hello"
    HELLO_ACK = "hello_ack"
    RPC_REQUEST = "rpc_request"
    RPC_RESULT = "rpc_result"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_ACK = "heartbeat_ack"
    ERROR = "error"


ALLOWED_RPC_METHODS = frozenset(
    {
        "atomic_action",
        "snapshot",
        "launch_app",
        "tap",
        "type_text",
        "clear_active",
        "swipe",
        "back",
        "home",
        "list_apps",
        "screenshot_png",
        "is_alive",
        "close",
    }
)

IDEMPOTENT_RPC_METHODS = frozenset(
    {"snapshot", "list_apps", "screenshot_png", "is_alive", "close"}
)


class AdvertisedDevice(ContractModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    platform: Literal["android"] = "android"
    metadata: dict[StrictStr, Any] = Field(default_factory=dict)


class HelloMessage(ContractModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[BridgeMessageType.HELLO] = BridgeMessageType.HELLO
    protocol: Literal["1.0"] = PROTOCOL_VERSION
    bridge_id: StrictStr = Field(min_length=1, max_length=128)
    instance_id: StrictStr = Field(default_factory=lambda: uuid4().hex)
    devices: list[AdvertisedDevice] = Field(min_length=1, max_length=512)

    @field_validator("devices")
    @classmethod
    def unique_device_ids(
        cls, values: list[AdvertisedDevice]
    ) -> list[AdvertisedDevice]:
        ids = [item.id for item in values]
        if len(ids) != len(set(ids)):
            raise ValueError("advertised device ids must be unique")
        return values


class HelloAckMessage(ContractModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[BridgeMessageType.HELLO_ACK] = BridgeMessageType.HELLO_ACK
    protocol: Literal["1.0"] = PROTOCOL_VERSION
    session_id: StrictStr
    heartbeat_seconds: StrictInt = Field(ge=1, le=300)


class RpcRequestMessage(ContractModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[BridgeMessageType.RPC_REQUEST] = BridgeMessageType.RPC_REQUEST
    id: StrictStr = Field(default_factory=lambda: uuid4().hex)
    sequence: StrictInt = Field(ge=1)
    device_id: StrictStr = Field(min_length=1, max_length=128)
    method: StrictStr = Field(min_length=1, max_length=64)
    params: dict[StrictStr, Any] = Field(default_factory=dict)
    timeout_ms: StrictInt = Field(default=15_000, ge=100, le=120_000)


    @model_validator(mode="after")
    def method_params_contract(self) -> RpcRequestMessage:
        self.params = validate_rpc_params(self.method, self.params)
        return self


class RpcResultMessage(ContractModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[BridgeMessageType.RPC_RESULT] = BridgeMessageType.RPC_RESULT
    id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    sequence: StrictInt = Field(ge=1)
    ok: StrictBool
    result: Any = None
    error: StrictStr | None = Field(default=None, max_length=4000)
    duration_ms: StrictFloat = Field(default=0.0, ge=0.0)


    @model_validator(mode="after")
    def outcome_contract(self) -> RpcResultMessage:
        if self.ok and self.error is not None:
            raise ValueError("successful result cannot contain an error")
        if not self.ok and (not self.error or self.result is not None):
            raise ValueError("failed result must contain only an error")
        return self


class HeartbeatMessage(ContractModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[BridgeMessageType.HEARTBEAT] = BridgeMessageType.HEARTBEAT
    id: StrictStr = Field(default_factory=lambda: uuid4().hex)


class HeartbeatAckMessage(ContractModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[BridgeMessageType.HEARTBEAT_ACK] = BridgeMessageType.HEARTBEAT_ACK
    id: StrictStr = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


def message_type(payload: dict[str, Any]) -> BridgeMessageType:
    return BridgeMessageType(str(payload.get("type", "")))
