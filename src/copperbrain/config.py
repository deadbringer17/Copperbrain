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
    freerouting_jar: Path | None = None
    freerouting_java: Path | None = None
    freerouting_timeout_seconds: float = Field(default=900, gt=0, le=7200)
    freerouting_stall_seconds: float = Field(default=180, gt=0, le=1800)
    freerouting_normalization_limit: int = Field(default=100, ge=1, le=100_000)

    @classmethod
    def from_environment(cls) -> Settings:
        """Load supported overrides without accepting arbitrary settings."""
        defaults = cls()
        cache = os.getenv("COPPERBRAIN_CACHE_DIR")
        data = os.getenv("COPPERBRAIN_DATA_DIR")
        freerouting_jar = os.getenv("COPPERBRAIN_FREEROUTING_JAR")
        freerouting_java = os.getenv("COPPERBRAIN_FREEROUTING_JAVA")
        freerouting_timeout = os.getenv("COPPERBRAIN_FREEROUTING_TIMEOUT_SECONDS")
        freerouting_stall = os.getenv("COPPERBRAIN_FREEROUTING_STALL_SECONDS")
        freerouting_normalization_limit = os.getenv("COPPERBRAIN_FREEROUTING_NORMALIZATION_LIMIT")
        hosts = os.getenv("COPPERBRAIN_ALLOWED_HOSTS")
        allowed_hosts = defaults.allowed_download_hosts
        if hosts:
            allowed_hosts = tuple(host.strip().lower() for host in hosts.split(",") if host.strip())
        return cls(
            cache_dir=Path(cache) if cache else defaults.cache_dir,
            data_dir=Path(data) if data else defaults.data_dir,
            allowed_download_hosts=allowed_hosts,
            freerouting_jar=Path(freerouting_jar) if freerouting_jar else None,
            freerouting_java=Path(freerouting_java) if freerouting_java else None,
            freerouting_timeout_seconds=(
                float(freerouting_timeout)
                if freerouting_timeout
                else defaults.freerouting_timeout_seconds
            ),
            freerouting_stall_seconds=(
                float(freerouting_stall)
                if freerouting_stall
                else defaults.freerouting_stall_seconds
            ),
            freerouting_normalization_limit=(
                int(freerouting_normalization_limit)
                if freerouting_normalization_limit
                else defaults.freerouting_normalization_limit
            ),
        )
