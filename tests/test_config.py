from pathlib import Path

import pytest

from copperbrain.config import Settings
from copperbrain.errors import CopperbrainError


def test_settings_environment_overrides(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COPPERBRAIN_CACHE_DIR", str(tmp_path / "cache"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_DATA_DIR", str(tmp_path / "data"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_ALLOWED_HOSTS", "example.com, assets.example.com")  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_KICAD_ROUTING_TOOLS_ROOT", str(tmp_path / "router"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_KICAD_ROUTING_TOOLS_PYTHON", str(tmp_path / "python.exe"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_ROUTING_TIMEOUT_SECONDS", "120")  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_ROUTING_STALL_SECONDS", "30")  # type: ignore[attr-defined]
    settings = Settings.from_environment()
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.data_dir == tmp_path / "data"
    assert settings.allowed_download_hosts == ("example.com", "assets.example.com")
    assert settings.kicad_routing_tools_root == tmp_path / "router"
    assert settings.kicad_routing_tools_python == tmp_path / "python.exe"
    assert settings.routing_timeout_seconds == 120
    assert settings.routing_stall_seconds == 30


def test_settings_reject_non_numeric_environment_values(monkeypatch: object) -> None:
    monkeypatch.setenv("COPPERBRAIN_ROUTING_TIMEOUT_SECONDS", "not-a-number")  # type: ignore[attr-defined]
    with pytest.raises(CopperbrainError, match="must be numeric"):
        Settings.from_environment()


def test_settings_reject_out_of_range_stall(monkeypatch: object) -> None:
    monkeypatch.setenv("COPPERBRAIN_ROUTING_STALL_SECONDS", "0")  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        Settings.from_environment()
