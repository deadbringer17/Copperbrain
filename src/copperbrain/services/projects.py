"""Project sessions and read-only schematic analysis."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from copperbrain.adapters.kicad_cli import export_netlist, run_drc, run_erc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    DrcReport,
    ErcReport,
    ErrorCode,
    Net,
    ProjectSession,
    ProjectSummary,
)
from copperbrain.services.outputs import OUTPUT_DIRECTORY


def hash_file(path: Path) -> str:
    """Calculate a streaming SHA-256 digest."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def aggregate_hash(hashes: dict[str, str]) -> str:
    """Create a deterministic project digest from relative paths and hashes."""
    payload = "\n".join(f"{path}\0{digest}" for path, digest in sorted(hashes.items()))
    return hashlib.sha256(payload.encode()).hexdigest()


def _project_version(project_file: Path) -> str | None:
    try:
        payload = json.loads(project_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    version = payload.get("meta", {}).get("version") if isinstance(payload, dict) else None
    return str(version) if version is not None else None


class ProjectService:
    """Own in-memory project sessions; never modifies project files."""

    def __init__(self) -> None:
        self._sessions: dict[str, ProjectSession] = {}

    def open_project(self, path: Path) -> ProjectSession:
        """Validate a KiCad project, discover its files, and freeze source hashes."""
        resolved = path.expanduser().resolve()
        if resolved.is_dir():
            projects = sorted(resolved.glob("*.kicad_pro"))
            if len(projects) != 1:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Project directory must contain exactly one .kicad_pro file",
                    details={"count": len(projects), "path": str(resolved)},
                )
            project_file = projects[0]
        elif resolved.suffix.lower() == ".kicad_pro" and resolved.is_file():
            project_file = resolved
        else:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "KiCad project was not found",
                actionable_hint="Pass a .kicad_pro file or its containing directory.",
                details={"path": str(resolved)},
            )
        root = project_file.parent
        schematics = tuple(
            sorted(
                item
                for item in root.rglob("*.kicad_sch")
                if OUTPUT_DIRECTORY not in item.relative_to(root).parts
            )
        )
        if not schematics:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Project contains no .kicad_sch files")
        pcb_candidates = sorted(root.glob("*.kicad_pcb"))
        custom_rules = tuple(sorted(root.glob("*.kicad_dru")))
        affected = (project_file, *schematics, *pcb_candidates, *custom_rules)
        hashes = {str(item.relative_to(root)): hash_file(item) for item in affected}
        session = ProjectSession(
            id=uuid.uuid4().hex,
            root=root,
            project_file=project_file,
            schematic_files=schematics,
            pcb_file=pcb_candidates[0] if pcb_candidates else None,
            hashes=hashes,
            kicad_version=_project_version(project_file),
        )
        self._sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> ProjectSession:
        """Resolve a known session or return a stable not-found failure."""
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Project session was not found") from exc

    def summary(self, session_id: str) -> ProjectSummary:
        """Export a normalized summary through KiCad's read-only netlist command."""
        session = self.get_session(session_id)
        detection = detect_kicad()
        if detection.selected_cli is None:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "KiCad CLI is required to analyze the schematic",
            )
        try:
            components, nets = export_netlist(
                detection.selected_cli,
                session.schematic_files[0],
            )
        except RuntimeError as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad could not export the schematic netlist",
                details={"reason": str(exc)},
            ) from exc
        powers = tuple(
            sorted(
                {
                    component.value
                    for component in components
                    if component.reference.startswith("#PWR")
                }
            )
        )
        return ProjectSummary(
            session_id=session.id,
            sheets=tuple(str(item.relative_to(session.root)) for item in session.schematic_files),
            components=components,
            nets=nets,
            power_symbols=powers,
        )

    def trace_net(self, session_id: str, net_name: str) -> Net:
        """Return a case-sensitive net and every pin reported by KiCad."""
        summary = self.summary(session_id)
        match = next((net for net in summary.nets if net.name == net_name), None)
        if match is None:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Net was not found",
                details={"net": net_name},
            )
        return match

    def analyze(self, session_id: str) -> dict[str, object]:
        """Produce deterministic observations without electrical design guesses."""
        summary = self.summary(session_id)
        observations: list[str] = []
        unconnected = [net.name for net in summary.nets if len(net.pins) < 2]
        if unconnected:
            observations.append(f"{len(unconnected)} nets have fewer than two connected pins")
        missing_footprints = [
            item.reference
            for item in summary.components
            if not item.reference.startswith("#") and not item.footprint
        ]
        if missing_footprints:
            observations.append(f"{len(missing_footprints)} components have no footprint")
        return {
            "session_id": session_id,
            "component_count": len(summary.components),
            "net_count": len(summary.nets),
            "observations": observations,
            "evidence": {"unconnected_nets": unconnected, "missing_footprints": missing_footprints},
        }

    def run_erc(self, session_id: str) -> ErcReport:
        """Run ERC against the primary schematic without writing into the project."""
        session = self.get_session(session_id)
        return run_erc(detect_kicad().selected_cli, session.schematic_files[0])

    def run_drc(self, session_id: str) -> DrcReport:
        """Run PCB DRC without modifying the board or project."""
        session = self.get_session(session_id)
        return run_drc(detect_kicad().selected_cli, session.pcb_file)
