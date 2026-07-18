"""Post-placement ground-plane planning and safe PCB mutation workflow."""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from copperbrain.adapters.kicad_cli import export_pcb_pdf, run_drc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.pcb_grounding import KiCadGroundingAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    ErrorCode,
    GroundBridge,
    GroundDomainAnalysis,
    GroundDomainPlan,
    GroundDomainRequest,
    GroundingAnalysis,
    GroundingPlan,
    GroundingRequest,
    GroundZoneRegion,
    PcbBounds,
    PcbGroundingChangeRecord,
    PcbGroundingChangeSet,
    PcbPadInspection,
    ProjectSession,
    RouteSegment,
    RouteVia,
    ValidationReport,
)
from copperbrain.services.outputs import PROJECT_COPY_IGNORE, publish_preview
from copperbrain.services.pcb_design import PcbDesignService
from copperbrain.services.projects import ProjectService, aggregate_hash, hash_file


@dataclass
class _PreparedGrounding:
    change_set: PcbGroundingChangeSet
    workspace: Path
    snapshot: Path | None = None


def _editor_lock_exists(root: Path) -> bool:
    return any(root.glob("*.kicad_pcb.lck")) or any(root.glob(".*.kicad_pcb.lck"))


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _is_ground_net(name: str) -> bool:
    token = name.strip().upper().rsplit("/", 1)[-1].replace("-", "_")
    return token in {"GND", "AGND", "DGND", "PGND", "SGND", "GNDA", "GNDD", "0V", "VSS"} or (
        token.startswith("GND_") or token.endswith("_GND")
    )


def _is_power_ground(name: str) -> bool:
    token = name.strip().upper().rsplit("/", 1)[-1].replace("-", "_")
    return token in {"PGND", "POWER_GND", "PWR_GND"} or token.startswith("PGND_")


def _point_segment_distance(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    squared = dx * dx + dy * dy
    if squared == 0:
        return math.dist(point, start)
    ratio = max(
        0.0,
        min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / squared),
    )
    return math.dist(point, (start[0] + ratio * dx, start[1] + ratio * dy))


def _segment_distance(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> float:
    """Return zero for crossing segments and endpoint distance otherwise."""

    def orientation(
        first: tuple[float, float],
        second: tuple[float, float],
        third: tuple[float, float],
    ) -> float:
        return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (
            third[0] - first[0]
        )

    values = (
        orientation(first_start, first_end, second_start),
        orientation(first_start, first_end, second_end),
        orientation(second_start, second_end, first_start),
        orientation(second_start, second_end, first_end),
    )
    if values[0] * values[1] <= 0 and values[2] * values[3] <= 0:
        return 0.0
    return min(
        _point_segment_distance(first_start, second_start, second_end),
        _point_segment_distance(first_end, second_start, second_end),
        _point_segment_distance(second_start, first_start, first_end),
        _point_segment_distance(second_end, first_start, first_end),
    )


def _pad_local_point(point: tuple[float, float], pad: PcbPadInspection) -> tuple[float, float]:
    angle = math.radians(-pad.rotation_deg)
    dx, dy = point[0] - pad.x_mm, point[1] - pad.y_mm
    return (
        dx * math.cos(angle) - dy * math.sin(angle),
        dx * math.sin(angle) + dy * math.cos(angle),
    )


def _point_pad_distance(point: tuple[float, float], pad: PcbPadInspection) -> float:
    local_x, local_y = _pad_local_point(point, pad)
    dx = max(abs(local_x) - pad.width_mm / 2, 0.0)
    dy = max(abs(local_y) - pad.height_mm / 2, 0.0)
    return math.hypot(dx, dy)


def _segment_pad_distance(
    start: tuple[float, float], end: tuple[float, float], pad: PcbPadInspection
) -> float:
    local_start = _pad_local_point(start, pad)
    local_end = _pad_local_point(end, pad)
    half_width, half_height = pad.width_mm / 2, pad.height_mm / 2
    if (abs(local_start[0]) <= half_width and abs(local_start[1]) <= half_height) or (
        abs(local_end[0]) <= half_width and abs(local_end[1]) <= half_height
    ):
        return 0.0
    corners = (
        (-half_width, -half_height),
        (half_width, -half_height),
        (half_width, half_height),
        (-half_width, half_height),
    )
    return min(
        _segment_distance(local_start, local_end, corners[index], corners[(index + 1) % 4])
        for index in range(4)
    )


class PcbGroundingService:
    """Prepare safe single- or multi-domain grounding from validated placement geometry."""

    def __init__(
        self,
        projects: ProjectService,
        design: PcbDesignService,
        data_dir: Path,
        adapter: PcbFileAdapter | None = None,
        grounding_adapter: KiCadGroundingAdapter | None = None,
        drc_runner: Callable[[Path | None], DrcReport] | None = None,
        pdf_exporter: Callable[[Path, Path], Path] | None = None,
        publish_artifacts: bool = True,
    ) -> None:
        self.projects = projects
        self.design = design
        self.data_dir = data_dir
        self.adapter = adapter or PcbFileAdapter()
        self.grounding_adapter = grounding_adapter or KiCadGroundingAdapter()
        self.drc_runner = drc_runner or (lambda pcb: run_drc(detect_kicad().selected_cli, pcb))
        self.pdf_exporter = pdf_exporter or (
            lambda pcb, destination: export_pcb_pdf(detect_kicad().selected_cli, pcb, destination)
        )
        self.publish_artifacts = publish_artifacts
        self._changes: dict[str, _PreparedGrounding] = {}

    @property
    def _records_dir(self) -> Path:
        return self.data_dir / "pcb-grounding-changes"

    def _record_path(self, change_set_id: str) -> Path:
        if len(change_set_id) != 32 or any(
            character not in "0123456789abcdef" for character in change_set_id
        ):
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Grounding identifier is invalid")
        return self._records_dir / f"{change_set_id}.json"

    def _persist(self, prepared: _PreparedGrounding, project_root: Path) -> None:
        root = project_root.resolve()
        record = PcbGroundingChangeRecord(
            project_root=root,
            workspace=prepared.workspace.resolve(),
            affected_relative_files=tuple(
                path.resolve().relative_to(root) for path in prepared.change_set.affected_files
            ),
            change_set=prepared.change_set,
            snapshot=prepared.snapshot.resolve() if prepared.snapshot is not None else None,
        )
        path = self._record_path(prepared.change_set.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(record.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _load(self, change_set_id: str) -> _PreparedGrounding:
        try:
            record = PcbGroundingChangeRecord.model_validate_json(
                self._record_path(change_set_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "Grounding change set was not found"
            ) from exc
        except (OSError, ValueError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Persisted grounding change set is invalid",
                details={"reason": str(exc)},
            ) from exc
        workspace = record.workspace.resolve()
        private_root = (self.data_dir / "workspaces").resolve()
        if not workspace.is_relative_to(private_root) or not workspace.is_dir():
            raise CopperbrainError(ErrorCode.CONFLICT, "Grounding workspace is unavailable")
        session = self.projects.open_project(record.project_root)
        affected = tuple(session.root / path for path in record.affected_relative_files)
        prepared = _PreparedGrounding(
            change_set=record.change_set.model_copy(
                update={"session_id": session.id, "affected_files": affected}
            ),
            workspace=workspace,
            snapshot=record.snapshot.resolve() if record.snapshot is not None else None,
        )
        self._changes[change_set_id] = prepared
        return prepared

    def _session_pcb(self, session_id: str) -> tuple[ProjectSession, Path]:
        session = self.projects.get_session(session_id)
        if session.pcb_file is None or not session.pcb_file.is_file():
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Project contains no PCB file",
                actionable_hint="Apply the PCB layout and placement before grounding.",
            )
        return session, session.pcb_file

    def _current_hashes(self, session: ProjectSession) -> dict[str, str]:
        return {
            relative: hash_file(session.root / relative)
            for relative in session.hashes
            if (session.root / relative).is_file()
        }

    @staticmethod
    def _via_candidates(
        pads: tuple[PcbPadInspection, ...],
        segments: tuple[RouteSegment, ...],
        existing_vias: tuple[RouteVia, ...],
        request: GroundingRequest,
        bounds: PcbBounds,
        target_net: str,
    ) -> tuple[RouteVia, ...]:
        min_x = bounds.min_x_mm
        min_y = bounds.min_y_mm
        max_x = bounds.max_x_mm
        max_y = bounds.max_y_mm
        radius = request.via_diameter_mm / 2
        margin = request.edge_clearance_mm + radius + request.clearance_mm
        result: list[RouteVia] = []
        y = min_y + margin
        while y <= max_y - margin + 1e-9 and len(result) < request.max_stitching_vias:
            x = min_x + margin
            while x <= max_x - margin + 1e-9 and len(result) < request.max_stitching_vias:
                point = (round(x, 6), round(y, 6))
                blocked_by_pad = any(
                    math.dist(point, (pad.x_mm, pad.y_mm))
                    < math.hypot(pad.width_mm, pad.height_mm) / 2 + radius + request.clearance_mm
                    for pad in pads
                )
                blocked_by_via = any(
                    math.dist(point, (via.x_mm, via.y_mm))
                    < (via.diameter_mm + request.via_diameter_mm) / 2 + request.clearance_mm
                    for via in existing_vias
                )
                blocked_by_track = any(
                    _point_segment_distance(
                        point,
                        (segment.start_x_mm, segment.start_y_mm),
                        (segment.end_x_mm, segment.end_y_mm),
                    )
                    < segment.width_mm / 2 + radius + request.clearance_mm
                    for segment in segments
                )
                if not (blocked_by_pad or blocked_by_via or blocked_by_track):
                    result.append(
                        RouteVia(
                            net=target_net,
                            x_mm=point[0],
                            y_mm=point[1],
                            diameter_mm=request.via_diameter_mm,
                            drill_mm=request.via_drill_mm,
                        )
                    )
                x += request.via_spacing_mm
            y += request.via_spacing_mm
        return tuple(result)

    @staticmethod
    def _target_nets(request: GroundingRequest, candidates: tuple[str, ...]) -> tuple[str, ...]:
        if request.domains:
            selected = tuple(item.net_name for item in request.domains)
        elif request.net_name is not None:
            selected = (request.net_name,)
        else:
            selected = candidates
        if not selected:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "No component ground net was detected")
        missing = tuple(item for item in selected if item not in candidates)
        if missing:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "Requested nets are not recognized component ground domains",
                details={"nets": missing, "detected_ground_nets": candidates},
            )
        return selected

    @staticmethod
    def _domain_layers(
        request: GroundingRequest,
        target_nets: tuple[str, ...],
        available_layers: tuple[str, ...],
    ) -> dict[str, tuple[str, ...]]:
        target_layers = (
            ("F.Cu", "B.Cu") if request.copper_layers == 2 else ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        )
        explicit = {item.net_name: item.layers for item in request.domains if item.layers}
        if explicit:
            if len(explicit) != len(target_nets):
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Every selected ground domain requires explicit layers when one is specified",
                )
            missing_layers = tuple(
                layer
                for layers in explicit.values()
                for layer in layers
                if layer not in target_layers
            )
            if missing_layers:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Requested ground domain layers are not enabled in the target stackup",
                    details={
                        "layers": missing_layers,
                        "target_layers": target_layers,
                    },
                )
            if request.copper_layers == 2 and any(len(items) != 1 for items in explicit.values()):
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Two-layer shaped grounding requires one primary layer per domain",
                )
            if request.copper_layers == 2 and len(set(explicit.values())) != len(explicit):
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Two-layer ground domains require different primary layers",
                )
            return explicit
        if len(target_nets) == 1:
            if request.layers:
                missing_layers = tuple(
                    layer for layer in request.layers if layer not in target_layers
                )
                if missing_layers:
                    raise CopperbrainError(
                        ErrorCode.INVALID_INPUT,
                        "Requested ground plane layers are not enabled in the target stackup",
                        details={
                            "layers": missing_layers,
                            "target_layers": target_layers,
                        },
                    )
                return {target_nets[0]: request.layers}
            selected = (
                ("F.Cu", "B.Cu") if request.copper_layers == 2 else ("F.Cu", "In1.Cu", "B.Cu")
            )
            return {target_nets[0]: selected}
        if request.layers:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "Multiple ground domains require per-domain layer assignments",
                actionable_hint="Use domains=[{net_name, layers}, ...] for a reviewed split.",
            )
        if len(target_nets) != 2:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Automatic grounding supports exactly two bridge-connected domains",
                actionable_hint="Provide explicit reviewed layer assignments for every domain.",
            )
        power = tuple(item for item in target_nets if _is_power_ground(item))
        logic = tuple(item for item in target_nets if not _is_power_ground(item))
        if len(power) != 1 or len(logic) != 1:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Automatic layer separation requires one PGND-like and one logic ground domain",
                actionable_hint="Provide explicit domain layers after engineering review.",
            )
        if request.copper_layers == 2:
            return {logic[0]: ("B.Cu",), power[0]: ("F.Cu",)}
        inner = tuple(layer for layer in target_layers if layer.startswith("In"))
        logic_layers = [layer for index, layer in enumerate(inner) if index % 2 == 0]
        power_layers = [layer for index, layer in enumerate(inner) if index % 2 == 1]
        logic_layers.append("B.Cu")
        power_layers.insert(0, "F.Cu")
        return {logic[0]: tuple(logic_layers), power[0]: tuple(power_layers)}

    @staticmethod
    def _bridges(
        request: GroundingRequest,
        target_nets: tuple[str, ...],
        pads: tuple[PcbPadInspection, ...],
    ) -> tuple[GroundBridge, ...]:
        if len(target_nets) == 1:
            return ()
        by_reference: dict[str, list[PcbPadInspection]] = {}
        for pad in pads:
            by_reference.setdefault(pad.reference, []).append(pad)
        candidates = {
            reference: tuple(sorted(items, key=lambda item: (item.net, item.number)))
            for reference, items in by_reference.items()
            if len(items) == 2
            and len({item.net for item in items}) == 2
            and {item.net for item in items}.issubset(target_nets)
        }
        if not request.bridge_references:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Multiple ground domains require explicit reviewed bridge references",
                actionable_hint="Set bridge_references to reviewed 0-ohm or net-tie parts.",
                details={
                    "bridge_candidates": tuple(sorted(candidates)),
                    "ground_nets": target_nets,
                },
            )
        missing = tuple(item for item in request.bridge_references if item not in candidates)
        if missing:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "Reviewed ground bridge references are not two-terminal domain bridges",
                details={"references": missing, "candidates": tuple(sorted(candidates))},
            )
        selected = request.bridge_references
        bridges = tuple(
            GroundBridge(
                reference=reference,
                net_a=candidates[reference][0].net,
                pad_a=candidates[reference][0].number,
                net_b=candidates[reference][1].net,
                pad_b=candidates[reference][1].number,
            )
            for reference in selected
        )
        connected = {target_nets[0]}
        while True:
            expanded = (
                connected
                | {bridge.net_b for bridge in bridges if bridge.net_a in connected}
                | {bridge.net_a for bridge in bridges if bridge.net_b in connected}
            )
            if expanded == connected:
                break
            connected = expanded
        if connected != set(target_nets):
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Reviewed ground bridges do not connect every selected domain",
                details={"connected": tuple(sorted(connected)), "ground_nets": target_nets},
            )
        return bridges

    @staticmethod
    def _fanout_for_pad(
        pad: PcbPadInspection,
        all_pads: tuple[PcbPadInspection, ...],
        segments: tuple[RouteSegment, ...],
        vias: tuple[RouteVia, ...],
        request: GroundingRequest,
        bounds: PcbBounds,
    ) -> tuple[RouteSegment | None, RouteVia | None]:
        layer: Literal["F.Cu", "B.Cu"]
        if "F.Cu" in pad.layers:
            layer = "F.Cu"
        elif "B.Cu" in pad.layers:
            layer = "B.Cu"
        else:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "A ground pad that misses its dedicated plane has no outer-layer fanout path",
                details={"reference": pad.reference, "pad": pad.number, "layers": pad.layers},
            )
        pad_limit = min(pad.width_mm, pad.height_mm) * 0.8
        if request.fanout_width_mm > pad_limit + 1e-9:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "Ground fanout width exceeds the 80% pad-size safety limit",
                actionable_hint="Use a reviewed smaller fanout width or a larger footprint pad.",
                details={
                    "reference": pad.reference,
                    "pad": pad.number,
                    "requested_width_mm": request.fanout_width_mm,
                    "maximum_width_mm": round(pad_limit, 6),
                },
            )
        if request.allow_via_in_pad and min(pad.width_mm, pad.height_mm) >= (
            request.via_diameter_mm + 2 * request.clearance_mm
        ):
            via = RouteVia(
                net=pad.net,
                x_mm=pad.x_mm,
                y_mm=pad.y_mm,
                diameter_mm=request.via_diameter_mm,
                drill_mm=request.via_drill_mm,
            )
            blocked = any(
                not (
                    other.reference == pad.reference
                    and other.number == pad.number
                    and other.net == pad.net
                )
                and _point_pad_distance((pad.x_mm, pad.y_mm), other)
                < request.via_diameter_mm / 2 + request.clearance_mm
                for other in all_pads
            ) or any(
                math.dist((pad.x_mm, pad.y_mm), (item.x_mm, item.y_mm))
                < (item.diameter_mm + request.via_diameter_mm) / 2 + request.clearance_mm
                for item in vias
            )
            if not blocked:
                return None, via
        for shared_via in vias:
            if shared_via.net != pad.net:
                continue
            destination_pad = next(
                (
                    other
                    for other in all_pads
                    if other.reference == pad.reference
                    and other.net == pad.net
                    and other.number != pad.number
                    and abs(shared_via.x_mm - other.x_mm) <= other.width_mm / 2
                    and abs(shared_via.y_mm - other.y_mm) <= other.height_mm / 2
                ),
                None,
            )
            if destination_pad is None or layer not in destination_pad.layers:
                continue
            destination = (shared_via.x_mm, shared_via.y_mm)
            blocked = any(
                other.net != pad.net
                and layer in other.layers
                and _segment_pad_distance((pad.x_mm, pad.y_mm), destination, other)
                < request.fanout_width_mm / 2 + request.clearance_mm
                for other in all_pads
            )
            if not blocked:
                return (
                    RouteSegment(
                        net=pad.net,
                        start_x_mm=pad.x_mm,
                        start_y_mm=pad.y_mm,
                        end_x_mm=destination[0],
                        end_y_mm=destination[1],
                        width_mm=request.fanout_width_mm,
                        layer=layer,
                    ),
                    None,
                )
        directions = (
            (1.0, 0.0),
            (-1.0, 0.0),
            (0.0, 1.0),
            (0.0, -1.0),
            (math.sqrt(0.5), math.sqrt(0.5)),
            (-math.sqrt(0.5), math.sqrt(0.5)),
            (math.sqrt(0.5), -math.sqrt(0.5)),
            (-math.sqrt(0.5), -math.sqrt(0.5)),
        )
        start = (pad.x_mm, pad.y_mm)
        pad_radius = math.hypot(pad.width_mm, pad.height_mm) / 2
        via_radius = request.via_diameter_mm / 2
        base_distance = pad_radius + via_radius + request.clearance_mm
        edge_margin = request.edge_clearance_mm + via_radius
        best: tuple[float, int, RouteSegment, RouteVia] | None = None
        for multiplier in (1.0, 1.5, 2.0, 3.0):
            for index, (dx, dy) in enumerate(directions):
                point = (
                    round(start[0] + dx * base_distance * multiplier, 6),
                    round(start[1] + dy * base_distance * multiplier, 6),
                )
                margins = [
                    point[0] - bounds.min_x_mm - edge_margin,
                    bounds.max_x_mm - point[0] - edge_margin,
                    point[1] - bounds.min_y_mm - edge_margin,
                    bounds.max_y_mm - point[1] - edge_margin,
                ]
                for other in all_pads:
                    if (
                        other.reference == pad.reference
                        and other.number == pad.number
                        and other.net == pad.net
                    ):
                        continue
                    margins.append(
                        _point_pad_distance(point, other) - via_radius - request.clearance_mm
                    )
                    if layer in other.layers:
                        margins.append(
                            _segment_pad_distance(start, point, other)
                            - request.fanout_width_mm / 2
                            - request.clearance_mm
                        )
                for existing_via in vias:
                    margins.append(
                        math.dist(point, (existing_via.x_mm, existing_via.y_mm))
                        - (existing_via.diameter_mm + request.via_diameter_mm) / 2
                        - request.clearance_mm
                    )
                for existing_segment in segments:
                    if existing_segment.layer != layer:
                        continue
                    existing_start = (
                        existing_segment.start_x_mm,
                        existing_segment.start_y_mm,
                    )
                    existing_end = (
                        existing_segment.end_x_mm,
                        existing_segment.end_y_mm,
                    )
                    margins.append(
                        _point_segment_distance(point, existing_start, existing_end)
                        - via_radius
                        - existing_segment.width_mm / 2
                        - request.clearance_mm
                    )
                    margins.append(
                        _segment_distance(start, point, existing_start, existing_end)
                        - request.fanout_width_mm / 2
                        - existing_segment.width_mm / 2
                        - request.clearance_mm
                    )
                score = min(margins)
                if score < -1e-9:
                    continue
                segment = RouteSegment(
                    net=pad.net,
                    start_x_mm=start[0],
                    start_y_mm=start[1],
                    end_x_mm=point[0],
                    end_y_mm=point[1],
                    width_mm=request.fanout_width_mm,
                    layer=layer,
                )
                via = RouteVia(
                    net=pad.net,
                    x_mm=point[0],
                    y_mm=point[1],
                    diameter_mm=request.via_diameter_mm,
                    drill_mm=request.via_drill_mm,
                )
                candidate = (score, -index, segment, via)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
            if best is not None:
                break
        if best is None:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "No clearance-safe fanout via position was found for a ground pad",
                actionable_hint="Revise placement, via dimensions, clearance, or domain layers.",
                details={"reference": pad.reference, "pad": pad.number, "net": pad.net},
            )
        return best[2], best[3]

    @staticmethod
    def _zone_regions(
        request: GroundingRequest,
        assigned_layers: tuple[str, ...],
        target_pads: tuple[PcbPadInspection, ...],
        domain_segments: tuple[RouteSegment, ...],
        bounds: PcbBounds,
        *,
        shaped_two_domain: bool,
    ) -> tuple[str, tuple[str, ...], tuple[GroundZoneRegion, ...]]:
        primary_layer = assigned_layers[0]
        if not shaped_two_domain:
            return (
                primary_layer,
                assigned_layers,
                tuple(GroundZoneRegion(layer=layer, kind="board") for layer in assigned_layers),
            )
        secondary_layer = "B.Cu" if primary_layer == "F.Cu" else "F.Cu"
        regions: list[GroundZoneRegion] = [GroundZoneRegion(layer=primary_layer, kind="board")]
        inset = request.edge_clearance_mm
        seen: set[tuple[float, float, float, float]] = set()
        for pad in sorted(
            (item for item in target_pads if secondary_layer in item.layers),
            key=lambda item: (item.reference, item.number, item.x_mm, item.y_mm),
        ):
            xs = [pad.x_mm - pad.width_mm / 2, pad.x_mm + pad.width_mm / 2]
            ys = [pad.y_mm - pad.height_mm / 2, pad.y_mm + pad.height_mm / 2]
            for segment in domain_segments:
                start = (segment.start_x_mm, segment.start_y_mm)
                end = (segment.end_x_mm, segment.end_y_mm)
                if (
                    min(
                        math.dist((pad.x_mm, pad.y_mm), start), math.dist((pad.x_mm, pad.y_mm), end)
                    )
                    <= 1e-6
                ):
                    xs.extend((segment.start_x_mm, segment.end_x_mm))
                    ys.extend((segment.start_y_mm, segment.end_y_mm))
            min_x = max(bounds.min_x_mm + inset, min(xs) - request.region_margin_mm)
            min_y = max(bounds.min_y_mm + inset, min(ys) - request.region_margin_mm)
            max_x = min(bounds.max_x_mm - inset, max(xs) + request.region_margin_mm)
            max_y = min(bounds.max_y_mm - inset, max(ys) + request.region_margin_mm)
            key: tuple[float, float, float, float] = (
                round(min_x, 6),
                round(min_y, 6),
                round(max_x, 6),
                round(max_y, 6),
            )
            if min_x >= max_x or min_y >= max_y or key in seen:
                continue
            seen.add(key)
            regions.append(
                GroundZoneRegion(
                    layer=secondary_layer,
                    kind="local",
                    min_x_mm=min_x,
                    min_y_mm=min_y,
                    max_x_mm=max_x,
                    max_y_mm=max_y,
                )
            )
        plane_layers = tuple(
            layer for layer in ("F.Cu", "B.Cu") if any(item.layer == layer for item in regions)
        )
        return primary_layer, plane_layers, tuple(regions)

    def plan(self, session_id: str, request: GroundingRequest) -> GroundingPlan:
        """Derive shaped, bridge-connected ground domains from placed geometry."""
        _, pcb = self._session_pcb(session_id)
        placement = self.design.analyze_placement(session_id)
        placement_errors = tuple(
            item.message for item in placement.issues if item.severity == "error"
        )
        if placement_errors:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "PCB grounding requires a valid optimized placement",
                actionable_hint=(
                    "Resolve placement errors and apply placement before grounding_pcb."
                ),
                details={"placement_errors": placement_errors},
            )
        parsed = self.adapter.parse(pcb, session_id)
        if parsed.summary.board_bounds is None:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED, "A closed Edge.Cuts outline is required for grounding"
            )
        available_layers = self.adapter.copper_layers(pcb)
        candidates = tuple(
            sorted(
                name
                for name, item in parsed.nets.items()
                if item.pad_count and _is_ground_net(name)
            )
        )
        target_nets = self._target_nets(request, candidates)
        domain_layers = self._domain_layers(request, target_nets, available_layers)
        shaped_two_domain = request.copper_layers == 2 and len(target_nets) == 2
        bridges = self._bridges(request, target_nets, parsed.pads)
        existing_by_net = {net: self.adapter.ground_plane_layers(pcb, net) for net in target_nets}
        if any(existing_by_net.values()) and not request.replace_existing_planes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Selected ground domains already contain zones",
                actionable_hint=(
                    "Set replace_existing_planes=true only after reviewing the replacement preview."
                ),
                details={"existing_layers": existing_by_net},
            )
        segments, existing_vias = self.adapter.routing_items(pcb)
        planned_segments = list(segments)
        planned_vias = list(existing_vias)
        domain_plans: list[GroundDomainPlan] = []
        requested_domains = {item.net_name: item for item in request.domains}
        for target_net in target_nets:
            target_pads = tuple(item for item in parsed.pads if item.net == target_net)
            assigned_layers = domain_layers[target_net]
            desired_layers = set(assigned_layers)
            fanouts: list[RouteSegment] = []
            vias: list[RouteVia] = []
            existing_routing = self.adapter.analyze_routing(pcb, session_id, (target_net,))
            reuses_complete_plane = (
                request.replace_existing_planes
                and existing_routing.complete
                and desired_layers.issubset(set(existing_by_net[target_net]))
            )
            missing_direct = (
                ()
                if reuses_complete_plane
                else tuple(
                    pad for pad in target_pads if not desired_layers.intersection(pad.layers)
                )
            )
            if missing_direct and not request.allow_vias:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Ground pads outside their dedicated plane layers require fanout vias",
                    details={
                        "net": target_net,
                        "references": tuple(sorted({item.reference for item in missing_direct})),
                    },
                )
            for pad in sorted(
                missing_direct, key=lambda item: (item.reference, item.number, item.x_mm, item.y_mm)
            ):
                segment, via = self._fanout_for_pad(
                    pad,
                    parsed.pads,
                    tuple(planned_segments),
                    tuple(planned_vias),
                    request,
                    parsed.summary.board_bounds,
                )
                if segment is not None:
                    fanouts.append(segment)
                    planned_segments.append(segment)
                if via is not None:
                    vias.append(via)
                    planned_vias.append(via)
            if len(planned_vias) - len(existing_vias) > request.max_stitching_vias:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Ground fanout via count exceeds the configured limit",
                    details={"limit": request.max_stitching_vias},
                )
            primary_layer, plane_layers, regions = self._zone_regions(
                request,
                assigned_layers,
                target_pads,
                tuple(item for item in planned_segments if item.net == target_net),
                parsed.summary.board_bounds,
                shaped_two_domain=shaped_two_domain,
            )
            actual_layers = set(plane_layers)
            through_pad = any(actual_layers.issubset(set(pad.layers)) for pad in target_pads)
            target_existing_vias = tuple(item for item in existing_vias if item.net == target_net)
            if len(actual_layers) > 1 and not through_pad and not target_existing_vias and not vias:
                if not request.allow_vias:
                    raise CopperbrainError(
                        ErrorCode.INVALID_INPUT,
                        "Multiple same-domain planes require a through connection",
                    )
                stitching = self._via_candidates(
                    parsed.pads,
                    tuple(planned_segments),
                    tuple(planned_vias),
                    request,
                    parsed.summary.board_bounds,
                    target_net,
                )
                if not stitching:
                    raise CopperbrainError(
                        ErrorCode.VALIDATION_FAILED,
                        "No clearance-safe ground stitching via position was found",
                    )
                vias.extend(stitching[:1])
                planned_vias.extend(stitching[:1])
            planes_connected = (
                len(actual_layers) == 1 or through_pad or bool(target_existing_vias or vias)
            )
            domain_plans.append(
                GroundDomainPlan(
                    net_name=target_net,
                    primary_layer=primary_layer,
                    plane_layers=plane_layers,
                    regions=regions,
                    pad_connection=(
                        requested_domains[target_net].pad_connection
                        if target_net in requested_domains
                        else "solid"
                        if _is_power_ground(target_net)
                        else "thermal"
                    ),
                    replaced_plane_layers=existing_by_net[target_net],
                    fanout_segments=tuple(fanouts),
                    vias=tuple(vias),
                    target_pad_count=len(target_pads),
                    target_references=tuple(sorted({item.reference for item in target_pads})),
                    planes_connected=planes_connected,
                )
            )
        if len(target_nets) == 1:
            target_net = target_nets[0]
            if request.domains:
                request = request.model_copy(
                    update={
                        "domains": (
                            GroundDomainRequest(
                                net_name=target_net,
                                layers=domain_layers[target_net],
                                pad_connection=domain_plans[0].pad_connection,
                            ),
                        ),
                        "layers": (),
                    }
                )
            else:
                request = request.model_copy(update={"layers": domain_layers[target_net]})
        else:
            request = request.model_copy(
                update={
                    "domains": tuple(
                        GroundDomainRequest(
                            net_name=net,
                            layers=domain_layers[net],
                            pad_connection=next(
                                item.pad_connection for item in domain_plans if item.net_name == net
                            ),
                        )
                        for net in target_nets
                    ),
                    "layers": (),
                }
            )
        return GroundingPlan(
            session_id=session_id,
            request=request,
            domains=tuple(domain_plans),
            bridges=bridges,
            evidence=(
                f"Planned {request.copper_layers}-layer grounding for "
                f"{len(target_nets)} exact domain(s)",
                "Derived board and local shaped regions from Edge.Cuts, pads, fanouts, and vias "
                f"after placement score {placement.score}",
                "Validated bridge topology through "
                f"{', '.join(item.reference for item in bridges) or 'none'}",
                f"Selected {sum(len(item.vias) for item in domain_plans)} "
                "clearance-screened via(s)",
            ),
            assumptions=(
                "Different-net shaped regions are explicitly clipped apart on shared layers",
                "Ground domains connect only through reviewed two-terminal bridges",
                "Local shaped regions use solid pad connections and are refilled by KiCad",
                "DRC validation does not replace SI, PI, EMC, thermal, or manufacturer review",
            ),
        )

    def _analysis(self, pcb: Path, session_id: str, plan: GroundingPlan) -> GroundingAnalysis:
        all_pads = self.adapter.pads(pcb)
        domain_results: list[GroundDomainAnalysis] = []
        for domain in plan.domains:
            pads = tuple(item for item in all_pads if item.net == domain.net_name)
            zone_layers = self.adapter.ground_plane_layers(pcb, domain.net_name)
            desired = set(domain.plane_layers)
            routing = self.adapter.analyze_routing(pcb, session_id, (domain.net_name,))
            inspection = self.adapter.inspect_net(pcb, session_id, domain.net_name)
            missing = tuple(
                sorted(
                    {item.start_reference for item in routing.unrouted_connections}
                    | {item.end_reference for item in routing.unrouted_connections}
                )
            )
            through_pad = any(desired.issubset(set(pad.layers)) for pad in pads)
            planes_connected = len(desired) == 1 or through_pad or inspection.via_count > 0
            complete = desired.issubset(zone_layers) and routing.complete and planes_connected
            domain_results.append(
                GroundDomainAnalysis(
                    net_name=domain.net_name,
                    complete=complete,
                    target_pad_count=len(pads),
                    connected_references=(
                        tuple(sorted({item.reference for item in pads}))
                        if routing.complete
                        else tuple(sorted({item.reference for item in pads} - set(missing)))
                    ),
                    zone_layers=tuple(
                        layer for layer in domain.plane_layers if layer in zone_layers
                    ),
                    via_count=inspection.via_count,
                    fanout_segment_count=len(domain.fanout_segments),
                    unrouted_connection_count=routing.unrouted_connection_count,
                    missing_pad_references=missing,
                    planes_connected=planes_connected,
                )
            )
        connected_by_net = {
            item.net_name: set(item.connected_references) for item in domain_results
        }
        bridge_pads = {(item.reference, item.number, item.net) for item in all_pads}
        bridges_connected = all(
            (bridge.reference, bridge.pad_a, bridge.net_a) in bridge_pads
            and (bridge.reference, bridge.pad_b, bridge.net_b) in bridge_pads
            and bridge.reference in connected_by_net[bridge.net_a]
            and bridge.reference in connected_by_net[bridge.net_b]
            for bridge in plan.bridges
        )
        if not plan.bridges:
            bridges_connected = len(plan.domains) == 1
        return GroundingAnalysis(
            session_id=session_id,
            complete=all(item.complete for item in domain_results) and bridges_connected,
            domains=tuple(domain_results),
            bridge_references=tuple(item.reference for item in plan.bridges),
            bridges_connected=bridges_connected,
            assumptions=plan.assumptions,
        )

    def _validate_workspace(
        self, session: ProjectSession, temporary_pcb: Path, plan: GroundingPlan
    ) -> tuple[ValidationReport, DrcReport, GroundingAnalysis]:
        structural = self.adapter.validate(temporary_pcb)
        before = self.drc_runner(session.pcb_file)
        after = self.drc_runner(temporary_pcb)
        baseline = Counter(
            (item.code, item.message) for item in before.violations if item.severity == "error"
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if item.severity == "error"
        )
        new_errors = generated - baseline
        analysis = self._analysis(temporary_pcb, session.id, plan)
        drc_ok = after.available and after.error is None and not new_errors
        messages = list(structural.messages)
        if after.error is not None:
            messages.append(after.error.message)
        messages.extend(
            f"New DRC error: {code or 'unknown'}: {message} (x{count})"
            for (code, message), count in new_errors.items()
        )
        if not analysis.complete:
            messages.append(
                "Ground domains do not connect every selected pad, layer, and reviewed bridge"
            )
        domain_checks = {
            f"ground_domain_{index}_complete": item.complete
            for index, item in enumerate(analysis.domains)
        }
        validation = ValidationReport(
            valid=structural.valid and drc_ok and analysis.complete,
            checks={
                **structural.checks,
                "drc_available": after.available,
                "drc_no_new_errors": not new_errors,
                "drc_command_ok": after.error is None,
                "all_ground_domains_complete": all(item.complete for item in analysis.domains),
                "ground_bridges_connected": analysis.bridges_connected,
                **domain_checks,
            },
            messages=tuple(messages),
        )
        return validation, after, analysis

    def prepare(self, session_id: str, request: GroundingRequest) -> PcbGroundingChangeSet:
        """Run grounding_pcb in a private workspace immediately after placement."""
        plan = self.plan(session_id, request)
        session, pcb = self._session_pcb(session_id)
        current = self._current_hashes(session)
        if current != session.hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project changed after the session was opened",
                actionable_hint="Open the project again after applying placement.",
            )
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(session.root, workspace, ignore=PROJECT_COPY_IGNORE)
        temporary_pcb = workspace / pcb.relative_to(session.root)
        self.grounding_adapter.apply(temporary_pcb, plan)
        before_segments, before_vias = self.adapter.routing_items(pcb)
        after_segments, after_vias = self.adapter.routing_items(temporary_pcb)
        actual_segments = tuple(
            sorted(
                (Counter(after_segments) - Counter(before_segments)).elements(),
                key=lambda item: item.model_dump_json(),
            )
        )
        actual_vias = tuple(
            sorted(
                (Counter(after_vias) - Counter(before_vias)).elements(),
                key=lambda item: item.model_dump_json(),
            )
        )
        updated_domains: list[GroundDomainPlan] = []
        for domain in plan.domains:
            domain_segments = tuple(item for item in actual_segments if item.net == domain.net_name)
            domain_vias = tuple(item for item in actual_vias if item.net == domain.net_name)
            if len(domain_segments) != len(domain.fanout_segments):
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "KiCad did not preserve every planned ground fanout",
                    details={
                        "net": domain.net_name,
                        "planned": len(domain.fanout_segments),
                        "actual": len(domain_segments),
                    },
                )
            desired_layers = set(domain.plane_layers)
            target_pads = self.adapter.pads(pcb, (domain.net_name,))
            through_pad = any(desired_layers.issubset(set(pad.layers)) for pad in target_pads)
            existing_target_via = any(item.net == domain.net_name for item in before_vias)
            planes_connected = (
                len(desired_layers) == 1 or through_pad or existing_target_via or bool(domain_vias)
            )
            if not planes_connected:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "A ground domain has no through connection between its planes",
                    details={"net": domain.net_name},
                )
            updated_domains.append(
                domain.model_copy(
                    update={
                        "fanout_segments": domain_segments,
                        "vias": domain_vias,
                        "planes_connected": planes_connected,
                    }
                )
            )
        plan = plan.model_copy(
            update={
                "domains": tuple(updated_domains),
                "evidence": (
                    *plan.evidence[:-1],
                    f"KiCad inserted {len(actual_segments)} fanout segment(s) and "
                    f"{len(actual_vias)} via(s)",
                ),
            }
        )
        validation, drc, analysis = self._validate_workspace(session, temporary_pcb, plan)
        preview_directory = workspace
        preview_pdf = None
        if self.publish_artifacts:
            pdf = self.pdf_exporter(temporary_pcb, workspace / "Copperbrain-grounding-preview.pdf")
            preview_directory = publish_preview(workspace, session.root, identifier)
            preview_pdf = preview_directory / pdf.relative_to(workspace)
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        change_set = PcbGroundingChangeSet(
            id=identifier,
            session_id=session_id,
            project_hash=aggregate_hash(current),
            plan=plan,
            affected_files=(pcb,),
            source_hashes=current,
            semantic_diff=(
                f"set target stackup to {plan.request.copper_layers} copper layers",
                *(
                    item
                    for domain in plan.domains
                    for item in (
                        (
                            f"replace {domain.net_name} planes on "
                            f"{', '.join(domain.replaced_plane_layers)}"
                            if domain.replaced_plane_layers
                            else f"add new {domain.net_name} plane set"
                        ),
                        f"assign {domain.net_name} primary region to {domain.primary_layer} and "
                        f"{len(domain.regions) - 1} local shaped region(s)",
                        f"use {domain.pad_connection} pad connection for {domain.net_name}",
                        f"connect {domain.target_pad_count} pad(s) across "
                        f"{len(domain.target_references)} component(s)",
                        f"add {len(domain.fanout_segments)} fanout segment(s) and "
                        f"{len(domain.vias)} through via(s)",
                    )
                ),
                *(
                    f"bridge {item.net_a} to {item.net_b} only through {item.reference}"
                    for item in plan.bridges
                ),
            ),
            risks=(
                "Named ground domains remain separate except through reviewed bridge components",
                "Shaped-region and primary-plane assignment changes return-current paths",
                "Via-in-pad operations require explicit fabrication-process review when enabled",
                "Copper pours require thermal, return-path, EMC, SI/PI, and DFM engineering review",
                "KiCad may overwrite external changes if an editor has unsaved state",
            ),
            validation_report=validation,
            drc=drc,
            grounding_analysis=analysis,
            preview_directory=preview_directory,
            preview_pdf=preview_pdf,
            status=status,
        )
        prepared = _PreparedGrounding(change_set, workspace)
        self._changes[identifier] = prepared
        self._persist(prepared, session.root)
        return change_set

    def _get(self, change_set_id: str) -> _PreparedGrounding:
        try:
            return self._changes[change_set_id]
        except KeyError:
            return self._load(change_set_id)

    def validate(self, change_set_id: str) -> tuple[ValidationReport, DrcReport, GroundingAnalysis]:
        prepared = self._get(change_set_id)
        session, pcb = self._session_pcb(prepared.change_set.session_id)
        temporary = prepared.workspace / pcb.relative_to(session.root)
        return self._validate_workspace(session, temporary, prepared.change_set.plan)

    def apply(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbGroundingChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        prepared = self._get(change_set_id)
        change_set = prepared.change_set
        if change_set.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "PCB grounding is not validated")
        session, _ = self._session_pcb(change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE,
                "KiCad editor state is not safely closed",
                actionable_hint="Save and close PCB Editor, then retry.",
            )
        if self._current_hashes(session) != change_set.source_hashes:
            prepared.change_set = change_set.model_copy(update={"status": ChangeStatus.STALE})
            self._persist(prepared, session.root)
            raise CopperbrainError(ErrorCode.CONFLICT, "Grounding change set is stale")
        snapshot_id = uuid.uuid4().hex
        snapshot = self.data_dir / "snapshots" / snapshot_id
        snapshot.mkdir(parents=True, exist_ok=False)
        for affected in change_set.affected_files:
            relative = affected.relative_to(session.root)
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(affected, destination)
        try:
            for affected in change_set.affected_files:
                relative = affected.relative_to(session.root)
                _atomic_copy(prepared.workspace / relative, affected)
        except Exception:
            for affected in change_set.affected_files:
                relative = affected.relative_to(session.root)
                _atomic_copy(snapshot / relative, affected)
            raise
        prepared.snapshot = snapshot
        prepared.change_set = change_set.model_copy(
            update={"status": ChangeStatus.APPLIED, "snapshot_id": snapshot_id}
        )
        self._persist(prepared, session.root)
        return prepared.change_set

    def rollback(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbGroundingChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Rollback confirmation is required"
            )
        prepared = self._get(change_set_id)
        if prepared.change_set.status is not ChangeStatus.APPLIED or prepared.snapshot is None:
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Only an applied PCB grounding can be rolled back"
            )
        session, _ = self._session_pcb(prepared.change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE, "KiCad editor state is not safely closed"
            )
        for affected in prepared.change_set.affected_files:
            relative = affected.relative_to(session.root)
            _atomic_copy(prepared.snapshot / relative, affected)
        prepared.change_set = prepared.change_set.model_copy(
            update={"status": ChangeStatus.ROLLED_BACK}
        )
        self._persist(prepared, session.root)
        return prepared.change_set
