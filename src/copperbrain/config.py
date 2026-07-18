"""Explicit runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_cache_path, user_data_path
from pydantic import BaseModel, ConfigDict, Field

from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode


def _numeric_env(name: str, raw: str | None, default: float, *, integer: bool = False) -> float:
    if not raw:
        return default
    try:
        return int(raw) if integer else float(raw)
    except ValueError as exc:
        raise CopperbrainError(
            ErrorCode.INVALID_INPUT,
            f"Environment variable {name} must be numeric",
            details={"value": raw},
        ) from exc


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_dir: Path = Field(default_factory=lambda: user_cache_path("Copperbrain"))
    data_dir: Path = Field(default_factory=lambda: user_data_path("Copperbrain"))
    allowed_download_hosts: tuple[str, ...] = (
        "jlcpcb.com",
        "lcsc.com",
        "easyeda.com",
        "wmsc.lcsc.com",
    )
    connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    read_timeout_seconds: float = Field(default=20, gt=0, le=120)
    max_download_bytes: int = Field(default=25_000_000, gt=0)
    kicad_routing_tools_root: Path | None = None
    kicad_routing_tools_python: Path | None = None
    routing_timeout_seconds: float = Field(default=900, gt=0, le=7200)
    routing_stall_seconds: float = Field(default=180, gt=0, le=1800)

    @classmethod
    def from_environment(cls) -> Settings:
        """Load supported overrides without accepting arbitrary settings."""
        defaults = cls()
        cache = os.getenv("COPPERBRAIN_CACHE_DIR")
        data = os.getenv("COPPERBRAIN_DATA_DIR")
        routing_root = os.getenv("COPPERBRAIN_KICAD_ROUTING_TOOLS_ROOT")
        routing_python = os.getenv("COPPERBRAIN_KICAD_ROUTING_TOOLS_PYTHON")
        routing_timeout = os.getenv("COPPERBRAIN_ROUTING_TIMEOUT_SECONDS")
        routing_stall = os.getenv("COPPERBRAIN_ROUTING_STALL_SECONDS")
        hosts = os.getenv("COPPERBRAIN_ALLOWED_HOSTS")
        allowed_hosts = defaults.allowed_download_hosts
        if hosts:
            allowed_hosts = tuple(host.strip().lower() for host in hosts.split(",") if host.strip())
        return cls(
            cache_dir=Path(cache) if cache else defaults.cache_dir,
            data_dir=Path(data) if data else defaults.data_dir,
            allowed_download_hosts=allowed_hosts,
            kicad_routing_tools_root=Path(routing_root) if routing_root else None,
            kicad_routing_tools_python=Path(routing_python) if routing_python else None,
            routing_timeout_seconds=_numeric_env(
                "COPPERBRAIN_ROUTING_TIMEOUT_SECONDS",
                routing_timeout,
                defaults.routing_timeout_seconds,
            ),
            routing_stall_seconds=_numeric_env(
                "COPPERBRAIN_ROUTING_STALL_SECONDS",
                routing_stall,
                defaults.routing_stall_seconds,
            ),
        )
