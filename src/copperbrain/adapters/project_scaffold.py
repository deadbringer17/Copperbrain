"""Typed creation of empty KiCad project files through supported APIs."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import kicad_sch_api

from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode, ProjectCreationSpec


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    try:
        Path(temporary).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ProjectScaffoldAdapter:
    """Create only an empty project; no raw schematic or PCB syntax is accepted."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.runner = runner

    @staticmethod
    def _kicad_python() -> Path:
        cli = detect_kicad().selected_cli
        names = ("python.exe",) if os.name == "nt" else ("python3", "python")
        candidate = (
            next((cli.parent / name for name in names if (cli.parent / name).is_file()), None)
            if cli is not None
            else None
        )
        if candidate is None:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "KiCad Python runtime is unavailable",
                actionable_hint="Install a complete KiCad distribution and retry detection.",
            )
        return candidate

    def create(self, destination: Path, spec: ProjectCreationSpec) -> tuple[Path, Path, Path]:
        if destination.exists():
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Private project scaffold destination already exists"
            )
        destination.mkdir(parents=True)
        project = destination / f"{spec.name}.kicad_pro"
        schematic = destination / f"{spec.name}.kicad_sch"
        pcb = destination / f"{spec.name}.kicad_pcb"

        created = kicad_sch_api.create_schematic(spec.name)
        created.save(schematic)
        _atomic_json(
            project,
            {
                "meta": {"filename": project.name, "version": 3},
                "text_variables": {},
            },
        )

        worker = Path(__file__).with_name("kicad_project_worker.py")
        try:
            result = self.runner(
                [
                    str(self._kicad_python()),
                    str(worker),
                    "create-board",
                    str(pcb),
                    str(spec.copper_layers),
                ],
                cwd=destination,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "KiCad failed to create an empty PCB",
                details={"reason": str(exc)},
            ) from exc
        if result.returncode != 0 or not pcb.is_file():
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad failed to create an empty PCB",
                details={"reason": (result.stderr or result.stdout)[-4000:]},
            )
        return project, schematic, pcb
