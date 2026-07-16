"""Fixed-command KiCad adapter for typed shaped ground regions and stitching vias."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from copperbrain.adapters.pcb_placement import KiCadPlacementAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode, GroundingPlan


class KiCadGroundingAdapter:
    """Relayer and fill planner-derived ground regions through bundled pcbnew."""

    def __init__(
        self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    ) -> None:
        self.runner = runner

    @staticmethod
    def _kicad_python() -> Path:
        return KiCadPlacementAdapter._kicad_python()

    def apply(self, pcb: Path, plan: GroundingPlan) -> None:
        if not plan.domains:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT, "At least one ground domain is required"
            )
        request = plan.request
        manifest_payload = {
            "copper_layers": request.copper_layers,
            "domains": [
                {
                    "net_name": domain.net_name,
                    "layers": list(domain.plane_layers),
                    "regions": [item.model_dump(mode="json") for item in domain.regions],
                    "pad_connection": domain.pad_connection,
                    "fanouts": [
                        {
                            "start_x_mm": item.start_x_mm,
                            "start_y_mm": item.start_y_mm,
                            "end_x_mm": item.end_x_mm,
                            "end_y_mm": item.end_y_mm,
                            "width_mm": item.width_mm,
                            "layer": item.layer,
                        }
                        for item in domain.fanout_segments
                    ],
                    "vias": [
                        {
                            "x_mm": item.x_mm,
                            "y_mm": item.y_mm,
                            "diameter_mm": item.diameter_mm,
                            "drill_mm": item.drill_mm,
                        }
                        for item in domain.vias
                    ],
                }
                for domain in plan.domains
            ],
            "replace_existing_planes": request.replace_existing_planes,
            "edge_clearance_mm": request.edge_clearance_mm,
            "clearance_mm": request.clearance_mm,
            "min_thickness_mm": request.min_thickness_mm,
            "thermal_gap_mm": request.thermal_gap_mm,
            "thermal_spoke_width_mm": request.thermal_spoke_width_mm,
        }
        worker = Path(__file__).with_name("kicad_project_worker.py")
        manifest_fd, manifest_name = tempfile.mkstemp(
            prefix=".copperbrain-grounding-", suffix=".json", dir=pcb.parent
        )
        os.close(manifest_fd)
        output_fd, output_name = tempfile.mkstemp(
            prefix=f".{pcb.stem}-grounded-", suffix=".kicad_pcb", dir=pcb.parent
        )
        os.close(output_fd)
        manifest, output = Path(manifest_name), Path(output_name)
        output.unlink()
        try:
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            try:
                result = self.runner(
                    [
                        str(self._kicad_python()),
                        str(worker),
                        "apply-grounding",
                        str(pcb),
                        str(output),
                        str(manifest),
                    ],
                    cwd=pcb.parent,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CopperbrainError(
                    ErrorCode.INTEGRATION_UNAVAILABLE,
                    "KiCad failed to create ground planes",
                    details={"reason": str(exc)},
                ) from exc
            if result.returncode != 0 or not output.is_file():
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "KiCad failed to create ground planes",
                    details={"reason": (result.stderr or result.stdout)[-4000:]},
                )
            os.replace(output, pcb)
        finally:
            manifest.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
