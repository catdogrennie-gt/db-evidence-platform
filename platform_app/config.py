"""Private runtime configuration. No repository-local credential defaults."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ControlDBConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=13306, ge=1, le=65535)
    database: str
    user: str
    password: SecretStr = Field(repr=False)


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    control_db: ControlDBConfig
    private_dir: Path
    fernet_key_path: Path
    secrets_dir: Path
    known_hosts_path: Path
    public_origin: str = "http://127.0.0.1:18080"
    allowed_networks: list[str] = Field(default_factory=lambda: ["127.0.0.0/8", "::1/128"])
    ssh_profiles: list[dict[str, Any]] = Field(default_factory=list)
    model: dict[str, Any] = Field(default_factory=dict, repr=False)
    demo: dict[str, Any] = Field(default_factory=dict, repr=False)
    prometheus_url: str = "http://127.0.0.1:9090"
    collection_mode: Literal["zabbix_only", "legacy"] = "zabbix_only"
    session_hours: int = Field(default=8, ge=1, le=24)

    @model_validator(mode="after")
    def validate_origin(self):
        origin = urlparse(self.public_origin)
        if origin.scheme not in ("http", "https") or not origin.hostname or origin.username:
            raise ValueError("public_origin must be an HTTP(S) origin")
        if origin.scheme == "http" and origin.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("HTTP development is limited to loopback")
        if origin.path not in ("", "/") or origin.query or origin.fragment:
            raise ValueError("public_origin cannot include a path, query or fragment")
        for field in ("private_dir", "fernet_key_path", "secrets_dir", "known_hosts_path"):
            if not getattr(self, field).is_absolute():
                raise ValueError(f"{field} must be an absolute private path")
        from .demo import settings

        settings(self)
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.public_origin.startswith("https://")


def load_config(path: str | Path | None = None) -> PlatformConfig:
    selected = path or os.environ.get("DBA_PLATFORM_CONFIG")
    if not selected:
        raise RuntimeError("DBA_PLATFORM_CONFIG must identify a private configuration file")
    # Avoid logging raw config or validation input containing secrets.
    try:
        raw = json.loads(Path(selected).read_text(encoding="utf-8-sig"))
        return PlatformConfig.model_validate(raw)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Private platform configuration is unavailable or invalid") from None


def zabbix_only(config=None, *, db=None):
    if config is None and db is not None:
        config = db.info.get("platform_config")
    return getattr(config, "collection_mode", "zabbix_only") != "legacy"


def require_legacy(config=None, *, db=None):
    if zabbix_only(config, db=db):
        from .common import PlatformError

        raise PlatformError(
            410,
            "collection_mode_retired",
            "旧直连、自定义采集和单主机入口已退役，请使用 Zabbix 联合分析对象。",
        )


def group_collection_allowed(db, instance, config=None, *, collector=None):
    if not zabbix_only(config, db=db):
        return True
    from .monitoring import mapping_for

    mapping = mapping_for(db, instance.id) if instance else None
    return bool(
        instance
        and instance.connection_mode == "zabbix"
        and mapping
        and mapping.layer == "group"
        and mapping.selected
        and (collector is None or collector == "monitoring")
    )
