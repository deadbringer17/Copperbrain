from pathlib import Path

from copperbrain.adapters import kicad_detection


def test_version_from_cli_parses_output(monkeypatch: object, tmp_path: Path) -> None:
    class Result:
        stdout = "10.0.4"
        stderr = ""

    monkeypatch.setattr(kicad_detection.subprocess, "run", lambda *args, **kwargs: Result())  # type: ignore[attr-defined]
    assert kicad_detection._version_from_cli(tmp_path / "kicad-cli.exe") == "10.0.4"


def test_version_from_cli_handles_os_error(monkeypatch: object, tmp_path: Path) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("missing")

    monkeypatch.setattr(kicad_detection.subprocess, "run", fail)  # type: ignore[attr-defined]
    assert kicad_detection._version_from_cli(tmp_path / "missing.exe") is None


def test_detect_kicad_uses_discovered_values(monkeypatch: object, tmp_path: Path) -> None:
    installation = kicad_detection.IntegrationStatus(
        name="KiCad", available=True, path=tmp_path / "kicad-cli.exe", version="10.0.4"
    )
    monkeypatch.setattr(kicad_detection, "_find_installations", lambda: (installation,))  # type: ignore[attr-defined]
    monkeypatch.setattr(kicad_detection, "_user_data_directories", lambda: (tmp_path,))  # type: ignore[attr-defined]
    monkeypatch.setattr(kicad_detection, "_detect_plugins", lambda dirs: ())  # type: ignore[attr-defined]
    result = kicad_detection.detect_kicad()
    assert result.selected_cli == installation.path
    assert result.user_data_directories == (tmp_path,)


def test_version_key_sorts_kicad_10_after_9() -> None:
    assert kicad_detection._version_key("10.0.1") > kicad_detection._version_key("9.0.6")


def test_user_data_directories_prefer_numerically_newest(
    monkeypatch: object, tmp_path: Path
) -> None:
    root = tmp_path / "kicad"
    for name in ("9.0", "10.0", "8.0"):
        (root / name).mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(tmp_path))  # type: ignore[attr-defined]
    result = kicad_detection._user_data_directories()
    assert [path.name for path in result] == ["10.0", "9.0", "8.0"]
