from pathlib import Path

from copperbrain.config import Settings


def test_settings_environment_overrides(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("COPPERBRAIN_CACHE_DIR", str(tmp_path / "cache"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_DATA_DIR", str(tmp_path / "data"))  # type: ignore[attr-defined]
    monkeypatch.setenv("COPPERBRAIN_ALLOWED_HOSTS", "example.com, assets.example.com")  # type: ignore[attr-defined]
    settings = Settings.from_environment()
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.data_dir == tmp_path / "data"
    assert settings.allowed_download_hosts == ("example.com", "assets.example.com")
