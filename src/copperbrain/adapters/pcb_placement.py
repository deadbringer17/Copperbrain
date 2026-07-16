"""Fixed-command KiCad API adapter for coordinated footprint side changes."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode, PlacementOperation


class KiCadPlacementAdapter:
    """Move, rotate, and flip footprints through KiCad's bundled pcbnew API."""

    def __init__(
        self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
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
                "KiCad Python runtime is required for footprint side changes",
                actionable_hint="Install a complete KiCad distribution and retry detection.",
            )
        return candidate

    def apply(self, pcb: Path, operations: tuple[PlacementOperation, ...]) -> None:
        if not operations:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "At least one placement is required")
        worker = Path(__file__).with_name("kicad_project_worker.py")
        manifest_fd, manifest_name = tempfile.mkstemp(
            prefix=".copperbrain-placement-", suffix=".json", dir=pcb.parent
        )
        os.close(manifest_fd)
        output_fd, output_name = tempfile.mkstemp(
            prefix=f".{pcb.stem}-placed-", suffix=".kicad_pcb", dir=pcb.parent
        )
        os.close(output_fd)
        manifest, output = Path(manifest_name), Path(output_name)
        output.unlink()
        try:
            manifest.write_text(
                json.dumps([item.model_dump(mode="json") for item in operations]),
                encoding="utf-8",
            )
            try:
                result = self.runner(
                    [
                        str(self._kicad_python()),
                        str(worker),
                        "apply-placement",
                        str(pcb),
                        str(output),
                        str(manifest),
                    ],
                    cwd=pcb.parent,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CopperbrainError(
                    ErrorCode.INTEGRATION_UNAVAILABLE,
                    "KiCad failed to apply footprint side changes",
                    details={"reason": str(exc)},
                ) from exc
            if result.returncode != 0 or not output.is_file():
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "KiCad failed to apply footprint side changes",
                    details={"reason": (result.stderr or result.stdout)[-4000:]},
                )
            os.replace(output, pcb)
        finally:
            manifest.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
