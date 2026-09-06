from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_config_dir() -> Path:
    project_config = Path("config")
    if project_config.exists():
        return project_config
    return Path(__file__).resolve().parent / "resources" / "config"


class Settings(BaseSettings):
    """Public control-plane settings.

    Appium/ADB/device connection details intentionally do not belong here. They stay in the
    private skill process and are never submitted to the public API.
    """

    model_config = SettingsConfigDict(
        env_prefix="PHONE_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = Field(default=8788, ge=1024, le=65535)
    public_url: str = "http://127.0.0.1:8788"
    api_key: str | None = Field(default=None, repr=False)
    bridge_tokens: dict[str, str] = Field(default_factory=dict, repr=False)
    bridge_token: str | None = Field(default=None, repr=False)
    log_level: str = "INFO"
    production_mode: bool = False

    llm_base_url: str = "http://127.0.0.1:18080/v1"
    llm_api_key: str = Field(default="local-key", repr=False)
    llm_model: str | None = None
    llm_timeout_seconds: float = 20.0
    llm_max_output_tokens: int = 1600
    vision_fallback_enabled: bool = False
    vision_model: str | None = None
    vision_timeout_seconds: float = 25.0
    vision_min_confidence: float = 0.82

    action_settle_ms: int = 120
    post_action_timeout_ms: int = 2500
    snapshot_stable_interval_ms: int = 80
    snapshot_stable_timeout_ms: int = 700
    condition_poll_ms: int = 160
    confirmation_timeout_seconds: int = 300
    bridge_rpc_timeout_seconds: float = 15.0
    bridge_connect_grace_seconds: float = 5.0
    bridge_task_wait_seconds: float = 120.0
    bridge_max_message_bytes: int = 2_000_000
    bridge_heartbeat_seconds: int = 20
    max_steps: int = 40
    max_repairs: int = 3
    max_scroll_searches: int = Field(default=2, ge=0, le=10)
    max_safe_dialog_dismissals: int = Field(default=3, ge=0, le=6)

    enable_a2a: bool = False
    a2a_wait_timeout_seconds: int = 900
    reaper_interval_seconds: int = 60
    config_dir: Path = Field(default_factory=default_config_dir)
    artifact_dir: Path = Field(default=Path("artifacts"))
    edge_model_dir: Path | None = None

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_model)

    def token_for_bridge(self, bridge_id: str) -> str | None:
        return self.bridge_tokens.get(bridge_id) or self.bridge_token

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("public_url must be an http(s) URL with a hostname")
        if parsed.username or parsed.password:
            raise ValueError("credentials must not be embedded in public_url")
        return value

    @model_validator(mode="after")
    def validate_production_security(self) -> Settings:
        if not self.production_mode:
            return self
        if not self.public_url.startswith("https://"):
            raise ValueError("production_mode requires an https public_url")
        if not self.api_key or len(self.api_key) < 24:
            raise ValueError("production_mode requires an API key of at least 24 characters")
        bridge_secrets = [*self.bridge_tokens.values()]
        if self.bridge_token:
            bridge_secrets.append(self.bridge_token)
        if not bridge_secrets or any(len(item) < 24 for item in bridge_secrets):
            raise ValueError("production_mode requires bridge tokens of at least 24 characters")
        if self.api_key in bridge_secrets:
            raise ValueError("the public API key and bridge token must be different")
        mapped_tokens = list(self.bridge_tokens.values())
        if len(mapped_tokens) != len(set(mapped_tokens)):
            raise ValueError("each named bridge must use a unique token")
        return self

    @field_validator("bridge_tokens", mode="before")
    @classmethod
    def parse_bridge_tokens(cls, value):
        if value is None or value == "":
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            import json

            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("bridge_tokens must be a JSON object")
            return {str(key): str(item) for key, item in parsed.items()}
        raise ValueError("bridge_tokens must be a mapping")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
