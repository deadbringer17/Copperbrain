"""Explicit runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_cache_path, user_data_path
from pydantic import BaseModel, ConfigDict, Field


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

    @classmethod
    def from_environment(cls) -> Settings:
        """Load supported overrides without accepting arbitrary settings."""
        defaults = cls()
        cache = os.getenv("COPPERBRAIN_CACHE_DIR")
        data = os.getenv("COPPERBRAIN_DATA_DIR")
        hosts = os.getenv("COPPERBRAIN_ALLOWED_HOSTS")
        allowed_hosts = defaults.allowed_download_hosts
        if hosts:
            allowed_hosts = tuple(host.strip().lower() for host in hosts.split(",") if host.strip())
        return cls(
            cache_dir=Path(cache) if cache else defaults.cache_dir,
            data_dir=Path(data) if data else defaults.data_dir,
            allowed_download_hosts=allowed_hosts,
        )
