from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SkillRuntimeConfig(BaseModel):
    """Private runtime tuning used only by the Lobster skill process."""

    model_config = ConfigDict(extra="forbid")

    operation_journal_path: Path = Path("artifacts/private-operations.sqlite3")

    appium_wait_for_idle_timeout_ms: int = Field(default=500, ge=0, le=10_000)
    appium_wait_for_selector_timeout_ms: int = Field(default=1500, ge=0, le=30_000)
    appium_enable_multi_windows: bool = False
    appium_command_timeout_seconds: float = Field(default=20.0, ge=1.0, le=120.0)
    appium_session_ttl_seconds: int = Field(default=1800, ge=60, le=86_400)
    reaper_interval_seconds: int = Field(default=60, ge=5, le=3600)
    bridge_max_message_bytes: int = Field(default=2_000_000, ge=64_000, le=8_000_000)
    operation_cache_entries: int = Field(default=4096, ge=128, le=65_536)
    installed_apps_cache_seconds: int = Field(default=300, ge=0, le=86_400)


class LocalDeviceDescriptor(BaseModel):
    """Private device configuration. It never crosses the public API boundary."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    appium_url: str
    udid: str | None = None
    device_name: str | None = None
    platform_version: str | None = None
    system_port: int | None = Field(default=None, ge=1024, le=65535)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("appium_url")
    @classmethod
    def validate_appium_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("appium_url must be an http(s) URL with a hostname")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in appium_url")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and address.is_unspecified:
            raise ValueError("appium_url must not use an unspecified address")
        return value

    @field_validator("capabilities")
    @classmethod
    def protect_session_invariants(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden = {
            "platformName",
            "appium:automationName",
            "automationName",
            "appium:udid",
            "udid",
            "appium:deviceName",
            "deviceName",
            "appium:systemPort",
            "systemPort",
            "appium:noReset",
            "noReset",
            "appium:fullReset",
            "fullReset",
            "appium:skipUnlock",
            "skipUnlock",
            "appium:settings",
            "settings",
        }
        present = sorted(forbidden.intersection(value))
        if present:
            raise ValueError(
                "capabilities may not override protected session fields: "
                + ", ".join(present)
            )
        return value

    @model_validator(mode="after")
    def ensure_device_target(self) -> LocalDeviceDescriptor:
        if not self.udid and not self.device_name:
            raise ValueError("local device requires udid or device_name")
        return self

    def advertised_metadata(self) -> dict[str, Any]:
        # Explicitly exclude Appium/ADB identifiers and capabilities.
        allowed = {"name", "region", "provider", "android_version", "model"}
        return {key: value for key, value in self.metadata.items() if key in allowed}


class SkillConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_url: str
    allow_insecure_http: bool = False
    bridge_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    bridge_token: str = Field(min_length=24, repr=False)
    api_token: str = Field(min_length=24, repr=False)
    local_host: str = "127.0.0.1"
    local_port: int = Field(default=8790, ge=1024, le=65535)
    local_token: str = Field(min_length=24, repr=False)
    reconnect_min_seconds: float = Field(default=0.5, ge=0.1, le=30)
    reconnect_max_seconds: float = Field(default=15.0, ge=1, le=120)
    runtime: SkillRuntimeConfig = Field(default_factory=SkillRuntimeConfig)
    devices: list[LocalDeviceDescriptor]

    @field_validator("server_url")
    @classmethod
    def normalize_server_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("server_url must be an http(s) URL with a hostname")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in server_url")
        return value

    @model_validator(mode="after")
    def validate_security_and_devices(self) -> SkillConfig:
        if self.server_url.startswith("http://") and not self.allow_insecure_http:
            raise ValueError(
                "public control-plane server_url must use https; set allow_insecure_http=true "
                "only for local tests"
            )
        ids = [item.id for item in self.devices]
        if not ids:
            raise ValueError("at least one private device is required")
        if len(ids) != len(set(ids)):
            raise ValueError("device ids must be unique")
        targets = [item.udid or item.device_name for item in self.devices]
        if len(targets) != len(set(targets)):
            raise ValueError("each private device must resolve to a unique Appium target")
        by_server: dict[str, list[LocalDeviceDescriptor]] = {}
        for item in self.devices:
            by_server.setdefault(item.appium_url, []).append(item)
        for appium_url, devices in by_server.items():
            if len(devices) <= 1:
                continue
            ports = [item.system_port for item in devices]
            if any(port is None for port in ports) or len(ports) != len(set(ports)):
                raise ValueError(
                    "multiple devices on one Appium server require unique explicit system_port "
                    f"values: {appium_url}"
                )
        secrets = [self.bridge_token, self.api_token, self.local_token]
        if len(secrets) != len(set(secrets)):
            raise ValueError("bridge_token, api_token, and local_token must be different")
        if self.reconnect_min_seconds > self.reconnect_max_seconds:
            raise ValueError("reconnect_min_seconds may not exceed reconnect_max_seconds")
        return self

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.server_url.startswith("https://") else "ws"
        base = self.server_url.split("://", 1)[1]
        return f"{scheme}://{base}/v1/bridge/ws/{quote(self.bridge_id, safe='')}"

    @classmethod
    def from_yaml(cls, path: Path) -> SkillConfig:
        payload = yaml.safe_load(path.read_text("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("skill config must be a YAML object")
        return cls.model_validate(payload)
