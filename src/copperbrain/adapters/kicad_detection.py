"""Runtime detection for KiCad and optional JLC integrations."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from platformdirs import user_documents_path

from copperbrain.models import IntegrationStatus, KicadDetection


def _candidate_install_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        if base := os.getenv(variable):
            roots.append(Path(base) / "KiCad")
    return tuple(dict.fromkeys(roots))


def _version_from_cli(cli: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(cli), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\d+(?:\.\d+){1,3}", result.stdout or result.stderr)
    return match.group(0) if match else None


def _find_installations() -> tuple[IntegrationStatus, ...]:
    candidates: set[Path] = set()
    if executable := shutil.which("kicad-cli"):
        candidates.add(Path(executable).resolve())
    for root in _candidate_install_roots():
        if root.is_dir():
            candidates.update(path for path in root.glob("*/bin/kicad-cli.exe") if path.is_file())
    found = [
        IntegrationStatus(
            name="KiCad",
            available=True,
            path=path,
            version=_version_from_cli(path),
        )
        for path in candidates
    ]
    return tuple(sorted(found, key=lambda item: _version_key(item.version), reverse=True))


def _version_key(version: str | None) -> tuple[int, ...]:
    """Compare dotted KiCad versions numerically (10.x must sort after 9.x)."""
    if not version:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _user_data_directories() -> tuple[Path, ...]:
    appdata = os.getenv("APPDATA")
    if not appdata:
        return ()
    root = Path(appdata) / "kicad"
    if not root.is_dir():
        return ()
    return tuple(sorted((path for path in root.iterdir() if path.is_dir()), reverse=True))


def _detect_plugins(data_dirs: tuple[Path, ...]) -> tuple[IntegrationStatus, ...]:
    roots = list(data_dirs)
    appdata = os.getenv("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "kicad")
    documents = user_documents_path()
    roots.extend(path for path in (documents / "KiCad").glob("*/3rdparty/plugins") if path.is_dir())
    definitions = {
        "JLCImport": ("jlcimport", "JLCImport"),
        "JLCPCB Tools": ("jlcpcb", "JLCPCB"),
    }
    statuses: list[IntegrationStatus] = []
    for name, needles in definitions.items():
        match = next(
            (
                path
                for root in roots
                if root.exists()
                for path in root.rglob("*")
                if any(needle.lower() in path.name.lower() for needle in needles)
            ),
            None,
        )
        statuses.append(IntegrationStatus(name=name, available=match is not None, path=match))
    return tuple(statuses)


def detect_kicad() -> KicadDetection:
    """Detect supported KiCad installations and optional plugins without fixed paths."""
    installations = _find_installations()
    data_dirs = _user_data_directories()
    selected = installations[0].path if installations else None
    return KicadDetection(
        installations=installations,
        selected_cli=selected,
        user_data_directories=data_dirs,
        plugins=_detect_plugins(data_dirs),
    )
