from pathlib import Path

import pytest

from copperbrain.config import Settings
from copperbrain.errors import CopperbrainError


def test_settings_environment_overrides(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COPPERBRAIN_CACHE_DIR", str(tmp_path / "cache"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_DATA_DIR", str(tmp_path / "data"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_ALLOWED_HOSTS", "example.com, assets.example.com")  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_FREEROUTING_JAR", str(tmp_path / "freerouting.jar"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_FREEROUTING_JAVA", str(tmp_path / "java.exe"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_FREEROUTING_TIMEOUT_SECONDS", "120")  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_FREEROUTING_STALL_SECONDS", "30")  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_FREEROUTING_NORMALIZATION_LIMIT", "25")  # type: ignore[attr-defined]
    settings = Settings.from_environment()
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.data_dir == tmp_path / "data"
    assert settings.allowed_download_hosts == ("example.com", "assets.example.com")
    assert settings.freerouting_jar == tmp_path / "freerouting.jar"
    assert settings.freerouting_java == tmp_path / "java.exe"
    assert settings.freerouting_timeout_seconds == 120
    assert settings.freerouting_stall_seconds == 30
    assert settings.freerouting_normalization_limit == 25


def test_settings_reject_non_numeric_environment_values(monkeypatch: object) -> None:
    monkeypatch.setenv("COPPERBRAIN_FREEROUTING_TIMEOUT_SECONDS", "not-a-number")  # type: ignore[attr-defined]
    with pytest.raises(CopperbrainError, match="must be numeric"):
        Settings.from_environment()


def test_settings_reject_non_integer_normalization_limit(monkeypatch: object) -> None:
    monkeypatch.setenv("COPPERBRAIN_FREEROUTING_NORMALIZATION_LIMIT", "25.5")  # type: ignore[attr-defined]
    with pytest.raises(CopperbrainError, match="must be numeric"):
        Settings.from_environment()
