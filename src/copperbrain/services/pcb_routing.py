"""External-backend PCB routing proposals and safe copper mutation workflow."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from copperbrain.adapters.kicad_cli import export_pcb_pdf, run_drc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.kicad_routing_tools import KiCadRoutingToolsAdapter
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.pcb_rules import (
    read_managed_roles,
    read_managed_widths,
    read_netclasses,
    stage_router_project,
)
from copperbrain.adapters.routing_backend import (
    RoutedBoardCandidate,
    RoutingBackend,
    RoutingStrategy,
)
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    ConnectivityMetricRecord,
    ConnectivityMetricRunSummary,
    DrcReport,
    ErrorCode,
    PcbPadInspection,
    PcbRoutingChangeSet,
    ProjectSession,
    RouteSegment,
    RouteVia,
    RoutingAnalysis,
    RoutingBackendStatus,
    RoutingCandidateEvaluation,
    RoutingChangeRecord,
    RoutingHotspot,
    RoutingPassMetric,
    RoutingPlan,
    RoutingRequest,
    RoutingReviewSummary,
    RoutingSnapshotRestoreResult,
    UnroutedConnection,
    ValidationReport,
    utc_now,
)
from copperbrain.services.connectivity_metrics import ConnectivityMetricsStore
from copperbrain.services.outputs import PROJECT_COPY_IGNORE, publish_preview
from copperbrain.services.projects import ProjectService, aggregate_hash, hash_file


@dataclass
class _PreparedRouting:
    change_set: PcbRoutingChangeSet
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


def _segment_is_covered(
    segment: RouteSegment,
    candidates: tuple[RouteSegment, ...] | list[RouteSegment],
    tolerance_mm: float,
) -> bool:
    """Return whether collinear candidate copper covers the complete segment."""

    start = (segment.start_x_mm, segment.start_y_mm)
    end = (segment.end_x_mm, segment.end_y_mm)
    length = math.dist(start, end)
    if length <= tolerance_mm:
        return any(
            math.isclose(segment.width_mm, candidate.width_mm, abs_tol=tolerance_mm)
            and math.dist(start, (candidate.start_x_mm, candidate.start_y_mm)) <= tolerance_mm
            and math.dist(end, (candidate.end_x_mm, candidate.end_y_mm)) <= tolerance_mm
            for candidate in candidates
        )

    direction_x = (end[0] - start[0]) / length
    direction_y = (end[1] - start[1]) / length
    intervals: list[tuple[float, float]] = []
    for candidate in candidates:
        if not math.isclose(segment.width_mm, candidate.width_mm, abs_tol=tolerance_mm):
            continue
        points = (
            (candidate.start_x_mm, candidate.start_y_mm),
            (candidate.end_x_mm, candidate.end_y_mm),
        )
        if any(
            abs((point[0] - start[0]) * direction_y - (point[1] - start[1]) * direction_x)
            > tolerance_mm
            for point in points
        ):
            continue
        projections = tuple(
            (point[0] - start[0]) * direction_x + (point[1] - start[1]) * direction_y
            for point in points
        )
        lower, upper = sorted(projections)
        if upper < -tolerance_mm or lower > length + tolerance_mm:
            continue
        intervals.append((max(0.0, lower), min(length, upper)))

    if not intervals:
        return False
    intervals.sort()
    if intervals[0][0] > tolerance_mm:
        return False
    covered_until = intervals[0][1]
    for lower, upper in intervals[1:]:
        if lower > covered_until + tolerance_mm:
            return False
        covered_until = max(covered_until, upper)
        if covered_until >= length - tolerance_mm:
            return True
    return covered_until >= length - tolerance_mm


class PcbRoutingService:
    """Orchestrate external routing, deterministic evaluation, preview, and safe apply."""

    def __init__(
        self,
        projects: ProjectService,
        data_dir: Path,
        adapter: PcbFileAdapter | None = None,
        drc_runner: Callable[[Path | None], DrcReport] | None = None,
        pdf_exporter: Callable[[Path, Path], Path] | None = None,
        routing_backend: RoutingBackend | None = None,
        publish_artifacts: bool = True,
    ) -> None:
        self.projects = projects
        self.data_dir = data_dir
        self.adapter = adapter or PcbFileAdapter()
        self.drc_runner = drc_runner or (lambda pcb: run_drc(detect_kicad().selected_cli, pcb))
        self.pdf_exporter = pdf_exporter or (
            lambda pcb, destination: export_pcb_pdf(detect_kicad().selected_cli, pcb, destination)
        )
        self.routing_backend = routing_backend or KiCadRoutingToolsAdapter.discover(data_dir)
        self.publish_artifacts = publish_artifacts
        self.metrics = ConnectivityMetricsStore(data_dir)
        self._changes: dict[str, _PreparedRouting] = {}

    @property
    def _records_dir(self) -> Path:
        return self.data_dir / "routing-changes"

    def _record_path(self, change_set_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", change_set_id) is None:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Routing change identifier is invalid")
        return self._records_dir / f"{change_set_id}.json"

    def _persist(self, prepared: _PreparedRouting, project_root: Path) -> None:
        """Atomically persist enough typed state to resume after an MCP restart."""
        change = prepared.change_set
        record = RoutingChangeRecord(
            project_root=project_root.resolve(),
            workspace=prepared.workspace.resolve(),
            affected_relative_files=tuple(
                path.resolve().relative_to(project_root.resolve()) for path in change.affected_files
            ),
            change_set=change,
            snapshot=prepared.snapshot.resolve() if prepared.snapshot is not None else None,
        )
        path = self._record_path(change.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(record.model_dump(mode="json"), stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _require_child(path: Path, parent: Path, label: str) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(parent.resolve())
        except ValueError as exc:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                f"Persisted {label} path is outside Copperbrain private storage",
            ) from exc
        return resolved

    def _load(self, change_set_id: str) -> _PreparedRouting:
        path = self._record_path(change_set_id)
        try:
            record = RoutingChangeRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Routing change set was not found") from exc
        except (OSError, ValueError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Persisted routing change set is invalid",
                actionable_hint="Prepare the routing change again from the source project.",
                details={"reason": str(exc)},
            ) from exc

        workspace = self._require_child(
            record.workspace, self.data_dir / "workspaces", "routing workspace"
        )
        if not workspace.is_dir():
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Persisted routing workspace was not found",
                actionable_hint="Prepare the routing change again from the source project.",
            )
        snapshot = None
        if record.snapshot is not None:
            snapshot = self._require_child(
                record.snapshot, self.data_dir / "snapshots", "routing snapshot"
            )
            if not snapshot.is_dir():
                raise CopperbrainError(
                    ErrorCode.NOT_FOUND, "Persisted routing snapshot was not found"
                )

        session = self.projects.open_project(record.project_root)
        affected = tuple(session.root / item for item in record.affected_relative_files)
        if any(not item.is_file() for item in affected):
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "A source file referenced by the routing change was not found"
            )
        old = record.change_set
        plan = old.plan.model_copy(
            update={
                "session_id": session.id,
                "analysis_before": old.plan.analysis_before.model_copy(
                    update={"session_id": session.id}
                ),
            }
        )
        change = old.model_copy(
            update={
                "session_id": session.id,
                "plan": plan,
                "affected_files": affected,
                "routing_analysis": old.routing_analysis.model_copy(
                    update={"session_id": session.id}
                ),
            }
        )
        prepared = _PreparedRouting(change, workspace, snapshot)
        self._changes[change_set_id] = prepared
        return prepared

    def _session_pcb(self, session_id: str) -> tuple[ProjectSession, Path]:
        session = self.projects.get_session(session_id)
        if session.pcb_file is None or not session.pcb_file.is_file():
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Project contains no PCB file",
                actionable_hint="Prepare and apply a PCB layout before routing.",
            )
        return session, session.pcb_file

    def backend_status(self) -> RoutingBackendStatus:
        return self.routing_backend.status()

    def metrics_for_run(self, run_id: str) -> ConnectivityMetricRunSummary:
        """Return a sanitized private-metrics summary for MCP optimization clients."""
        return self.metrics.read_run(run_id)

    def _write_lifecycle_metric(
        self,
        change: PcbRoutingChangeSet,
        phase: str,
        *,
        started_at: datetime,
        started_monotonic: float,
        outcome: str = "success",
        error_code: ErrorCode | None = None,
    ) -> None:
        """Correlate prepare/validate/apply/rollback evidence to the proposal run."""
        _, pcb = self._session_pcb(change.session_id)
        final = change.routing_analysis
        baseline = change.plan.analysis_before
        self.metrics.write(
            ConnectivityMetricRecord(
                run_id=uuid.uuid4().hex,
                parent_run_id=change.plan.metrics_run_id,
                operation="routing_change",
                phase=phase,  # type: ignore[arg-type]
                outcome=outcome,  # type: ignore[arg-type]
                started_at=started_at,
                finished_at=utc_now(),
                duration_seconds=time.monotonic() - started_monotonic,
                project_fingerprint=hash_file(pcb),
                source_hashes=dict(sorted(change.source_hashes.items())),
                backend="Copperbrain",
                backend_version=change.plan.backend_version,
                effective_configuration=self._routing_configuration(change.plan.request),
                requested_net_count=len(change.plan.target_nets),
                requested_net_role_counts=self._requested_net_role_counts(
                    change.plan.target_nets, change.plan.request.net_roles
                ),
                baseline_routed_net_count=baseline.routed_net_count,
                baseline_unrouted_net_count=baseline.unrouted_net_count,
                final_routed_net_count=final.routed_net_count,
                final_unrouted_net_count=final.unrouted_net_count,
                baseline_open_connection_count=baseline.unrouted_connection_count,
                final_open_connection_count=final.unrouted_connection_count,
                open_connection_delta=(
                    baseline.unrouted_connection_count - final.unrouted_connection_count
                ),
                segment_count=len(change.plan.segments),
                via_count=len(change.plan.vias),
                routed_length_mm=round(
                    sum(
                        math.dist(
                            (item.start_x_mm, item.start_y_mm),
                            (item.end_x_mm, item.end_y_mm),
                        )
                        for item in change.plan.segments
                    ),
                    6,
                ),
                final_drc_error_count=self._drc_error_count(change.drc),
                final_drc_warning_count=self._drc_warning_count(change.drc),
                error_code=error_code,
            )
        )

    def _write_plan_lifecycle_failure(
        self,
        session_id: str,
        plan: RoutingPlan,
        phase: str,
        *,
        started_at: datetime,
        started_monotonic: float,
        error_code: ErrorCode,
    ) -> None:
        """Flush best-known lifecycle evidence when no change set could be produced."""
        session, pcb = self._session_pcb(session_id)
        baseline = plan.analysis_before
        self.metrics.write(
            ConnectivityMetricRecord(
                run_id=uuid.uuid4().hex,
                parent_run_id=plan.metrics_run_id,
                operation="routing_change",
                phase=phase,  # type: ignore[arg-type]
                outcome="failure",
                started_at=started_at,
                finished_at=utc_now(),
                duration_seconds=time.monotonic() - started_monotonic,
                project_fingerprint=hash_file(pcb),
                source_hashes=dict(sorted(session.hashes.items())),
                backend="Copperbrain",
                backend_version=plan.backend_version,
                effective_configuration=self._routing_configuration(plan.request),
                requested_net_count=len(plan.target_nets),
                requested_net_role_counts=self._requested_net_role_counts(
                    plan.target_nets, plan.request.net_roles
                ),
                baseline_routed_net_count=baseline.routed_net_count,
                baseline_unrouted_net_count=baseline.unrouted_net_count,
                final_routed_net_count=baseline.routed_net_count,
                final_unrouted_net_count=baseline.unrouted_net_count,
                baseline_open_connection_count=baseline.unrouted_connection_count,
                final_open_connection_count=baseline.unrouted_connection_count,
                open_connection_delta=0,
                error_code=error_code,
                diagnostic_only=True,
                applicable=False,
            )
        )

    def analyze(self, session_id: str, net_names: tuple[str, ...] = ()) -> RoutingAnalysis:
        _, pcb = self._session_pcb(session_id)
        return self.adapter.analyze_routing(pcb, session_id, net_names)

    def _routing_delta(
        self,
        source: Path,
        routed: Path,
        target_nets: tuple[str, ...],
    ) -> tuple[tuple[RouteSegment, ...], tuple[RouteVia, ...]]:
        before_segments, before_vias = self.adapter.routing_items(source)
        after_segments, after_vias = self.adapter.routing_items(routed)
        coordinate_tolerance_mm = 0.001
        before_segment_buckets: dict[tuple[str, str], list[RouteSegment]] = {}
        for item in before_segments:
            before_segment_buckets.setdefault((item.net, item.layer), []).append(item)
        after_segment_buckets: dict[tuple[str, str], list[RouteSegment]] = {}
        for item in after_segments:
            after_segment_buckets.setdefault((item.net, item.layer), []).append(item)
        removed_segments = [
            item
            for item in before_segments
            if not _segment_is_covered(
                item,
                after_segment_buckets.get((item.net, item.layer), ()),
                coordinate_tolerance_mm,
            )
        ]

        via_buckets: dict[tuple[str, tuple[str, ...]], list[RouteVia]] = {}
        for via_item in after_vias:
            via_buckets.setdefault((via_item.net, via_item.layers), []).append(via_item)
        removed_vias: list[RouteVia] = []
        for via_item in before_vias:
            via_candidates = via_buckets.get((via_item.net, via_item.layers), [])
            match = next(
                (
                    index
                    for index, via_candidate in enumerate(via_candidates)
                    if math.dist(
                        (via_item.x_mm, via_item.y_mm),
                        (via_candidate.x_mm, via_candidate.y_mm),
                    )
                    <= coordinate_tolerance_mm
                    and math.isclose(
                        via_item.diameter_mm,
                        via_candidate.diameter_mm,
                        abs_tol=coordinate_tolerance_mm,
                    )
                    and math.isclose(
                        via_item.drill_mm,
                        via_candidate.drill_mm,
                        abs_tol=coordinate_tolerance_mm,
                    )
                ),
                None,
            )
            if match is None:
                removed_vias.append(via_item)
            else:
                via_candidates.pop(match)
        if removed_segments or removed_vias:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "The autorouter changed or removed existing copper",
                actionable_hint=(
                    "Route from a clean board or preserve existing tracks in the router."
                ),
                details={
                    "removed_segments": len(removed_segments),
                    "removed_vias": len(removed_vias),
                    "round_trip_tolerance_mm": coordinate_tolerance_mm,
                },
            )
        target = set(target_nets)
        added_segments = tuple(
            item
            for item in after_segments
            if not _segment_is_covered(
                item,
                before_segment_buckets.get((item.net, item.layer), ()),
                coordinate_tolerance_mm,
            )
        )
        added_vias = tuple(item for items in via_buckets.values() for item in items)
        foreign_segments = tuple(item for item in added_segments if item.net not in target)
        foreign_vias = tuple(item for item in added_vias if item.net not in target)
        if foreign_segments or foreign_vias:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "The autorouter added copper outside the requested routing scope",
                details={
                    "foreign_segment_count": len(foreign_segments),
                    "foreign_via_count": len(foreign_vias),
                },
            )
        segments = tuple(
            sorted(
                (item for item in added_segments if item.net in target),
                key=lambda item: item.model_dump_json(),
            )
        )
        vias = tuple(
            sorted(
                (item for item in added_vias if item.net in target),
                key=lambda item: item.model_dump_json(),
            )
        )
        if not segments and not vias:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "The autorouter produced no copper for the requested nets",
            )
        return segments, vias

    def _stage_router_input(
        self,
        pcb: Path,
        workspace: Path,
        target_nets: tuple[str, ...],
        *,
        context_pcb: Path | None = None,
        add_escape_stubs: bool = False,
        default_width_mm: float | None = None,
        default_clearance_mm: float = 0.2,
        via_diameter_mm: float = 0.6,
        via_drill_mm: float = 0.3,
        seed_segments: tuple[RouteSegment, ...] = (),
        seed_vias: tuple[RouteVia, ...] = (),
    ) -> Path:
        """Stage preferred netclasses and deterministic fine-pitch escape geometry."""
        workspace.mkdir(parents=True, exist_ok=False)
        context = context_pcb or pcb
        staged_pcb = workspace / context.name
        shutil.copy2(pcb, staged_pcb)
        source_rules = context.with_suffix(".kicad_dru")
        preferred_classes, fanout_limits = read_managed_widths(source_rules)
        if source_rules.is_file():
            shutil.copy2(source_rules, workspace / source_rules.name)
        source_project = context.with_suffix(".kicad_pro")
        if source_project.is_file():
            stage_router_project(
                source_project,
                workspace / source_project.name,
                preferred_classes,
            )
        _, assignments = read_netclasses(source_project) if source_project.is_file() else ((), ())
        net_classes = {item.net: item.netclass for item in assignments}
        net_widths = {
            net: preferred_classes[netclass]
            for net, netclass in net_classes.items()
            if netclass in preferred_classes
        }
        if default_width_mm is not None:
            for net in target_nets:
                net_widths.setdefault(net, default_width_mm)
        if not fanout_limits or not net_widths:
            return staged_pcb

        parsed = self.adapter.parse(staged_pcb)
        bounds_by_reference = {item.reference: item.bounds for item in parsed.summary.footprints}
        stubs: list[RouteSegment] = []
        escape_vias: list[RouteVia] = []
        fine_pads: list[tuple[PcbPadInspection, float, tuple[float, float]]] = []
        for pad in parsed.pads:
            preferred = net_widths.get(pad.net)
            configured_limit = fanout_limits.get(pad.reference)
            bounds = bounds_by_reference.get(pad.reference)
            if (
                pad.net not in target_nets
                or preferred is None
                or configured_limit is None
                or bounds is None
                or configured_limit > 0.5
            ):
                continue
            pad_limit = round(
                min(
                    preferred,
                    configured_limit,
                    min(pad.width_mm, pad.height_mm) * 0.8,
                ),
                6,
            )
            if pad_limit >= preferred and not add_escape_stubs:
                continue
            center_x = (bounds.min_x_mm + bounds.max_x_mm) / 2
            center_y = (bounds.min_y_mm + bounds.max_y_mm) / 2
            horizontal = abs(pad.x_mm - center_x) / max(
                (bounds.max_x_mm - bounds.min_x_mm) / 2, 1e-9
            )
            vertical = abs(pad.y_mm - center_y) / max((bounds.max_y_mm - bounds.min_y_mm) / 2, 1e-9)
            margin = pad_limit / 2 + 0.01
            if horizontal >= vertical:
                end_x = (
                    bounds.max_x_mm + margin if pad.x_mm >= center_x else bounds.min_x_mm - margin
                )
                end_y = pad.y_mm
            else:
                end_x = pad.x_mm
                end_y = (
                    bounds.max_y_mm + margin if pad.y_mm >= center_y else bounds.min_y_mm - margin
                )
            if (pad.x_mm, pad.y_mm) == (end_x, end_y):
                continue
            escape = (round(end_x, 6), round(end_y, 6))
            fine_pads.append((pad, pad_limit, escape))
        seeded_pad_keys: set[tuple[str, str, str, str]] = set()
        for index, (left_pad, left_width, _) in enumerate(fine_pads):
            for right_pad, right_width, _ in fine_pads[index + 1 :]:
                if (
                    not add_escape_stubs
                    or left_pad.reference != right_pad.reference
                    or left_pad.net != right_pad.net
                    or left_pad.layers[0] != right_pad.layers[0]
                    or (left_pad.x_mm, left_pad.y_mm) == (right_pad.x_mm, right_pad.y_mm)
                    or math.dist(
                        (left_pad.x_mm, left_pad.y_mm),
                        (right_pad.x_mm, right_pad.y_mm),
                    )
                    > 1.0
                ):
                    continue
                stubs.append(
                    RouteSegment(
                        net=left_pad.net,
                        start_x_mm=left_pad.x_mm,
                        start_y_mm=left_pad.y_mm,
                        end_x_mm=right_pad.x_mm,
                        end_y_mm=right_pad.y_mm,
                        width_mm=min(left_width, right_width),
                        layer=left_pad.layers[0],
                    )
                )
                seeded_pad_keys.add(
                    (left_pad.reference, left_pad.number, left_pad.net, left_pad.layers[0])
                )
                seeded_pad_keys.add(
                    (right_pad.reference, right_pad.number, right_pad.net, right_pad.layers[0])
                )
        groups: dict[
            tuple[str, str, str],
            list[tuple[PcbPadInspection, float, tuple[float, float]]],
        ] = {}
        for item in fine_pads:
            pad = item[0]
            groups.setdefault((pad.reference, pad.net, pad.layers[0]), []).append(item)
        for (reference, net, layer), group in groups.items():
            if not add_escape_stubs and net_widths.get(net, 0) < 1.0:
                continue
            nearby: list[
                tuple[
                    float,
                    PcbPadInspection,
                    float,
                    tuple[float, float],
                    PcbPadInspection,
                ]
            ] = []
            for pad, width, escape in group:
                for target in parsed.pads:
                    if (
                        target.reference == reference
                        or target.net != net
                        or layer not in target.layers
                    ):
                        continue
                    distance = math.dist(
                        (pad.x_mm, pad.y_mm),
                        (target.x_mm, target.y_mm),
                    )
                    if distance <= 5.0:
                        nearby.append((distance, pad, width, escape, target))
            if not nearby:
                opposite_layer: list[
                    tuple[
                        float,
                        PcbPadInspection,
                        float,
                        tuple[float, float],
                        PcbPadInspection,
                    ]
                ] = []
                if add_escape_stubs:
                    for pad, width, escape in group:
                        for target in parsed.pads:
                            if (
                                target.reference == reference
                                or target.net != net
                                or layer in target.layers
                            ):
                                continue
                            distance = math.dist(
                                (pad.x_mm, pad.y_mm),
                                (target.x_mm, target.y_mm),
                            )
                            if distance <= 10.0:
                                opposite_layer.append((distance, pad, width, escape, target))
                if not opposite_layer:
                    continue
                _, pad, width, escape, target = min(
                    opposite_layer,
                    key=lambda item: (
                        item[0],
                        item[1].number,
                        item[4].reference,
                        item[4].number,
                    ),
                )
                delta_x = escape[0] - pad.x_mm
                delta_y = escape[1] - pad.y_mm
                length = math.hypot(delta_x, delta_y)
                if length <= 1e-9:
                    continue
                via_offset = via_diameter_mm / 2 + default_clearance_mm
                via_x = round(escape[0] + delta_x / length * via_offset, 6)
                via_y = round(escape[1] + delta_y / length * via_offset, 6)
                target_layer = target.layers[0]
                connection_width = round(
                    min(
                        net_widths[net],
                        min(target.width_mm, target.height_mm) * 0.8,
                    ),
                    6,
                )
                target_bounds = bounds_by_reference.get(target.reference)
                target_escape = (target.x_mm, target.y_mm)
                if target_bounds is not None:
                    target_center_x = (target_bounds.min_x_mm + target_bounds.max_x_mm) / 2
                    target_center_y = (target_bounds.min_y_mm + target_bounds.max_y_mm) / 2
                    target_horizontal = abs(target.x_mm - target_center_x) / max(
                        (target_bounds.max_x_mm - target_bounds.min_x_mm) / 2,
                        1e-9,
                    )
                    target_vertical = abs(target.y_mm - target_center_y) / max(
                        (target_bounds.max_y_mm - target_bounds.min_y_mm) / 2,
                        1e-9,
                    )
                    target_margin = connection_width / 2 + 0.01
                    if target_horizontal >= target_vertical:
                        target_escape = (
                            target_bounds.max_x_mm + target_margin
                            if target.x_mm >= target_center_x
                            else target_bounds.min_x_mm - target_margin,
                            target.y_mm,
                        )
                    else:
                        target_escape = (
                            target.x_mm,
                            target_bounds.max_y_mm + target_margin
                            if target.y_mm >= target_center_y
                            else target_bounds.min_y_mm - target_margin,
                        )
                    target_escape = (
                        round(target_escape[0], 6),
                        round(target_escape[1], 6),
                    )
                stubs.append(
                    RouteSegment(
                        net=net,
                        start_x_mm=pad.x_mm,
                        start_y_mm=pad.y_mm,
                        end_x_mm=via_x,
                        end_y_mm=via_y,
                        width_mm=width,
                        layer=layer,
                    )
                )
                dogleg = (target_escape[0], via_y)
                if (via_x, via_y) != dogleg:
                    stubs.append(
                        RouteSegment(
                            net=net,
                            start_x_mm=via_x,
                            start_y_mm=via_y,
                            end_x_mm=dogleg[0],
                            end_y_mm=dogleg[1],
                            width_mm=connection_width,
                            layer=target_layer,
                        )
                    )
                if dogleg != target_escape:
                    stubs.append(
                        RouteSegment(
                            net=net,
                            start_x_mm=dogleg[0],
                            start_y_mm=dogleg[1],
                            end_x_mm=target_escape[0],
                            end_y_mm=target_escape[1],
                            width_mm=connection_width,
                            layer=target_layer,
                        )
                    )
                if target_escape != (target.x_mm, target.y_mm):
                    stubs.append(
                        RouteSegment(
                            net=net,
                            start_x_mm=target_escape[0],
                            start_y_mm=target_escape[1],
                            end_x_mm=target.x_mm,
                            end_y_mm=target.y_mm,
                            width_mm=connection_width,
                            layer=target_layer,
                        )
                    )
                escape_vias.append(
                    RouteVia(
                        net=net,
                        x_mm=via_x,
                        y_mm=via_y,
                        diameter_mm=via_diameter_mm,
                        drill_mm=via_drill_mm,
                        layers=(layer, target_layer),
                    )
                )
                seeded_pad_keys.add((pad.reference, pad.number, pad.net, layer))
                continue
            _, pad, width, escape, target = min(
                nearby,
                key=lambda item: (
                    item[0],
                    item[1].number,
                    item[4].reference,
                    item[4].number,
                ),
            )
            connection_width = round(
                min(
                    net_widths[net],
                    min(target.width_mm, target.height_mm) * 0.8,
                ),
                6,
            )
            if add_escape_stubs:
                connection_stubs = [
                    RouteSegment(
                        net=net,
                        start_x_mm=pad.x_mm,
                        start_y_mm=pad.y_mm,
                        end_x_mm=target.x_mm,
                        end_y_mm=target.y_mm,
                        width_mm=min(width, connection_width),
                        layer=layer,
                    )
                ]
                seeded_pad_keys.add((pad.reference, pad.number, pad.net, layer))
            else:
                connection_stubs = [
                    RouteSegment(
                        net=net,
                        start_x_mm=escape[0],
                        start_y_mm=escape[1],
                        end_x_mm=target.x_mm,
                        end_y_mm=target.y_mm,
                        width_mm=connection_width,
                        layer=layer,
                    )
                ]
                connection_stubs.insert(
                    0,
                    RouteSegment(
                        net=net,
                        start_x_mm=pad.x_mm,
                        start_y_mm=pad.y_mm,
                        end_x_mm=escape[0],
                        end_y_mm=escape[1],
                        width_mm=width,
                        layer=layer,
                    ),
                )
            stubs.extend(connection_stubs)
        if add_escape_stubs:
            for pad, width, escape in fine_pads:
                key = (pad.reference, pad.number, pad.net, pad.layers[0])
                if key in seeded_pad_keys:
                    continue
                stubs.append(
                    RouteSegment(
                        net=pad.net,
                        start_x_mm=pad.x_mm,
                        start_y_mm=pad.y_mm,
                        end_x_mm=escape[0],
                        end_y_mm=escape[1],
                        width_mm=width,
                        layer=pad.layers[0],
                    )
                )
        staged_segments = (*seed_segments, *stubs)
        staged_vias = (*seed_vias, *escape_vias)
        if staged_segments or staged_vias:
            self.adapter.apply_routing(staged_pcb, staged_segments, staged_vias)
        return staged_pcb

    @staticmethod
    def _stage_candidate_rules(source_pcb: Path, candidate_pcb: Path) -> None:
        """Give standalone candidate DRC the same same-stem custom rules as the project."""
        source_rules = source_pcb.with_suffix(".kicad_dru")
        if source_rules.is_file():
            shutil.copy2(source_rules, candidate_pcb.with_suffix(".kicad_dru"))

    @staticmethod
    def _new_drc_errors(before: DrcReport, after: DrcReport) -> int | None:
        if not before.available or not after.available or before.error or after.error:
            return None
        baseline = Counter(
            (item.code, item.message) for item in before.violations if item.severity == "error"
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if item.severity == "error"
        )
        return sum((generated - baseline).values())

    @staticmethod
    def _drc_error_count(report: DrcReport) -> int | None:
        if not report.available or report.error is not None:
            return None
        return sum(item.severity == "error" for item in report.violations)

    @staticmethod
    def _drc_warning_count(report: DrcReport) -> int | None:
        if not report.available or report.error is not None:
            return None
        return sum(item.severity == "warning" for item in report.violations)

    @staticmethod
    def _new_drc_warnings(before: DrcReport, after: DrcReport) -> int | None:
        if not before.available or not after.available or before.error or after.error:
            return None
        baseline = Counter(
            (item.code, item.message) for item in before.violations if item.severity == "warning"
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if item.severity == "warning"
        )
        return sum((generated - baseline).values())

    @staticmethod
    def _requested_net_role_counts(
        net_names: tuple[str, ...], explicit_roles: Mapping[str, str] | None = None
    ) -> dict[str, int]:
        counts: Counter[str] = Counter()
        explicit_roles = explicit_roles or {}
        normalized_names = {name.upper().strip("/") for name in net_names}
        for name in net_names:
            normalized = name.upper().strip("/")
            if name in explicit_roles:
                role = explicit_roles[name]
            elif re.search(r"(?:^|_)(?:GND|AGND|DGND|PGND)(?:$|_)", normalized):
                role = "ground"
            elif re.search(r"(?:^|_)(?:PHASE_[ABC]|MOTOR_[ABC]|OUT[ABC])(?:$|_)", normalized):
                role = "motor_phase"
            elif re.search(r"(?:^|_)(?:PWM|GATE|GH[123]|GL[123]|SW|LX)(?:$|_)", normalized):
                role = "switching"
            elif re.search(r"(?:^|_)(?:VBAT|VIN|VOUT|VCC|VDD|VM|POWER)(?:$|_)", normalized):
                role = "power"
            elif (
                (normalized.endswith("_P") and f"{normalized[:-2]}_N" in normalized_names)
                or (normalized.endswith("_N") and f"{normalized[:-2]}_P" in normalized_names)
                or (normalized.endswith("+") and f"{normalized[:-1]}-" in normalized_names)
                or (normalized.endswith("-") and f"{normalized[:-1]}+" in normalized_names)
            ):
                role = "differential_candidate"
            else:
                role = "signal"
            counts[role] += 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _rule_derived_roles(session: ProjectSession, net_names: tuple[str, ...]) -> dict[str, str]:
        """Resolve roles from typed managed rules, leaving names as an explicit fallback."""
        _, assignments = read_netclasses(session.project_file)
        rule_file = session.root / f"{session.project_file.stem}.kicad_dru"
        class_roles = read_managed_roles(rule_file)
        assignment_by_net = {item.net: item.netclass for item in assignments}
        return {
            net: class_roles[assignment_by_net[net]]
            for net in net_names
            if net in assignment_by_net and assignment_by_net[net] in class_roles
        }

    @staticmethod
    def _routing_hotspots(
        pads: tuple[PcbPadInspection, ...], analysis: RoutingAnalysis
    ) -> tuple[RoutingHotspot, ...]:
        """Cluster open airwires into deterministic 10 mm local placement regions."""
        pad_by_key = {(item.reference, item.number): item for item in pads}
        clusters: dict[tuple[int, int], list[tuple[UnroutedConnection, float, float]]] = {}
        for connection in analysis.unrouted_connections:
            start = pad_by_key.get((connection.start_reference, connection.start_pad))
            end = pad_by_key.get((connection.end_reference, connection.end_pad))
            if start is None or end is None:
                continue
            x = (start.x_mm + end.x_mm) / 2
            y = (start.y_mm + end.y_mm) / 2
            clusters.setdefault((math.floor(x / 10), math.floor(y / 10)), []).append(
                (connection, x, y)
            )
        hotspots: list[RoutingHotspot] = []
        for items in clusters.values():
            if len(items) < 2:
                continue
            center_x = sum(item[1] for item in items) / len(items)
            center_y = sum(item[2] for item in items) / len(items)
            references = tuple(
                sorted(
                    {
                        reference
                        for connection, _, _ in items
                        for reference in (
                            connection.start_reference,
                            connection.end_reference,
                        )
                    }
                )[:12]
            )
            total_length = sum(connection.distance_mm for connection, _, _ in items)
            radius = max(
                1.0,
                max(math.dist((center_x, center_y), (x, y)) for _, x, y in items),
            )
            hotspots.append(
                RoutingHotspot(
                    references=references,
                    connection_count=len(items),
                    total_airwire_length_mm=round(total_length, 6),
                    center_x_mm=round(center_x, 6),
                    center_y_mm=round(center_y, 6),
                    radius_mm=round(radius, 6),
                    recommendation=(
                        "Prepare a reviewed local placement iteration for these footprints; "
                        "reduce crossing airwires and reopen a routing corridor before retrying."
                    ),
                )
            )
        return tuple(
            sorted(
                hotspots,
                key=lambda item: (-item.connection_count, -item.total_airwire_length_mm),
            )[:5]
        )

    @staticmethod
    def _pass_optimization_summary(
        metrics: tuple[RoutingPassMetric, ...],
    ) -> tuple[int | None, int, int, float | None, float | None]:
        best_pass: int | None = None
        best_open: int | None = None
        stagnation = 0
        previous_open: int | None = None
        for item in metrics:
            current = (
                item.board_unrouted_count
                if item.board_unrouted_count is not None
                else item.board_incomplete_count
            )
            if current is not None and (best_open is None or current < best_open):
                best_open = current
                best_pass = item.pass_number
            if current is not None and previous_open is not None and current >= previous_open:
                stagnation += 1
            if current is not None:
                previous_open = current
        cpu_values = tuple(item.cpu_seconds for item in metrics if item.cpu_seconds is not None)
        memory_values = tuple(
            item.allocated_memory_gb for item in metrics if item.allocated_memory_gb is not None
        )
        return (
            best_pass,
            sum(item.failure_count for item in metrics),
            stagnation,
            max(cpu_values, default=None),
            max(memory_values, default=None),
        )

    @staticmethod
    def _routing_metrics_from_error(
        exc: CopperbrainError,
    ) -> tuple[RoutingPassMetric, ...]:
        raw = exc.error.details.get("routing_pass_metrics", ())
        if not isinstance(raw, (list, tuple)):
            return ()
        metrics: list[RoutingPassMetric] = []
        for item in raw:
            try:
                metrics.append(RoutingPassMetric.model_validate(item))
            except (TypeError, ValueError):
                continue
        return tuple(metrics)

    @staticmethod
    def _normalization_count_from_error(exc: CopperbrainError) -> int:
        value = exc.error.details.get("normalization_count", 0)
        return value if isinstance(value, int) and value >= 0 else 0

    def _routing_configuration(
        self,
        request: RoutingRequest,
        strategy: RoutingStrategy | None = None,
    ) -> dict[str, str | int | float | bool]:
        configuration: dict[str, str | int | float | bool] = {
            "preferred_layer": request.preferred_layer,
            "track_width_mm": request.default_track_width_mm,
            "clearance_mm": request.default_clearance_mm,
            "via_diameter_mm": request.via_diameter_mm,
            "via_drill_mm": request.via_drill_mm,
            "grid_mm": request.grid_mm,
            "allow_vias": request.allow_vias,
            "max_iterations": request.max_iterations,
            "max_probe_iterations": request.max_probe_iterations,
            "heuristic_weight": request.heuristic_weight,
            "via_cost": request.via_cost,
            "max_ripup": request.max_ripup,
            "existing_copper_policy": request.existing_copper_policy,
            "maximum_autorouting_attempts": 3,
            "requested_candidate_count": request.candidate_count,
            "excluded_plane_net_count": len(request.excluded_plane_nets),
            "fine_pitch_escape_stubs": request.allow_fine_pitch_escape_stubs,
            "seed_segment_count": len(request.seed_segments),
            "seed_via_count": len(request.seed_vias),
        }
        for attribute, name in (
            ("timeout_seconds", "wall_time_budget_seconds"),
            ("stall_seconds", "stall_time_budget_seconds"),
        ):
            value = getattr(self.routing_backend, attribute, None)
            if isinstance(value, int | float) and not isinstance(value, bool):
                configuration[name] = value
        if strategy is not None:
            configuration["attempt_configuration"] = strategy
            configuration["kicad_routing_tools_ordering"] = strategy
        return configuration

    def propose(self, session_id: str, request: RoutingRequest) -> RoutingPlan:
        proposal_started_at = utc_now()
        proposal_started = time.monotonic()
        run_id = uuid.uuid4().hex
        session, pcb = self._session_pcb(session_id)
        analysis = self.adapter.analyze_routing(pcb, session_id, request.nets)
        if analysis.complete:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Selected PCB nets are already fully routed",
                details={"nets": list(request.nets)},
            )
        existing_segments, existing_vias = self.adapter.routing_items(pcb)
        if request.existing_copper_policy == "reject" and (existing_segments or existing_vias):
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Incremental autorouting is disabled for a board that already contains copper",
                actionable_hint=(
                    "Route from the clean placed board, or explicitly use "
                    "existing_copper_policy='preserve' with watchdog protection."
                ),
                details={
                    "existing_segment_count": len(existing_segments),
                    "existing_via_count": len(existing_vias),
                },
            )
        status = self.routing_backend.status()
        if not status.available:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                f"The selected {status.name} backend is unavailable",
                actionable_hint=(
                    "Run 'uv run python scripts/setup_dependencies.py', or set "
                    "COPPERBRAIN_KICAD_ROUTING_TOOLS_ROOT, "
                    "then restart Copperbrain."
                ),
                details={"reason": status.reason or "unknown"},
            )
        target_nets = tuple(sorted({item.net for item in analysis.unrouted_connections}))
        excluded_plane_nets = request.excluded_plane_nets
        if not excluded_plane_nets:
            excluded_plane_nets = self.adapter.zone_net_names(pcb)
        target_nets = tuple(net for net in target_nets if net not in excluded_plane_nets)
        if not target_nets:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "No ordinary routing targets remain after reviewed plane-net exclusion",
                details={"excluded_plane_nets": list(excluded_plane_nets)},
            )
        analysis = self.adapter.analyze_routing(pcb, session_id, target_nets)
        explicit_roles = self._rule_derived_roles(session, target_nets)
        explicit_roles.update(
            {net: role for net, role in request.net_roles.items() if net in target_nets}
        )
        request = request.model_copy(
            update={
                "nets": target_nets,
                "net_roles": explicit_roles,
                "excluded_plane_nets": excluded_plane_nets,
            }
        )
        board_analysis = self.adapter.analyze_routing(pcb, session_id)
        parsed = self.adapter.parse(pcb, session_id)
        bounds = parsed.summary.board_bounds
        board_width = bounds.max_x_mm - bounds.min_x_mm if bounds is not None else None
        board_height = bounds.max_y_mm - bounds.min_y_mm if bounds is not None else None
        board_area = board_width * board_height if board_width and board_height else None
        placement_area = sum(
            (item.bounds.max_x_mm - item.bounds.min_x_mm)
            * (item.bounds.max_y_mm - item.bounds.min_y_mm)
            for item in parsed.summary.footprints
        )
        placement_density = (
            min(100.0, round(placement_area / board_area * 100, 6))
            if board_area is not None
            else None
        )
        source_hashes = dict(sorted(session.hashes.items()))
        requested_net_role_counts = self._requested_net_role_counts(target_nets, explicit_roles)
        baseline_drc = self.drc_runner(pcb)
        project_fingerprint = hash_file(pcb)
        routing_hotspots = self._routing_hotspots(parsed.pads, analysis)
        baseline_finished_at = utc_now()
        self.metrics.write(
            ConnectivityMetricRecord(
                run_id=run_id,
                phase="baseline",
                outcome="success",
                started_at=proposal_started_at,
                finished_at=baseline_finished_at,
                duration_seconds=time.monotonic() - proposal_started,
                project_fingerprint=project_fingerprint,
                source_hashes=source_hashes,
                board_width_mm=board_width,
                board_height_mm=board_height,
                copper_layer_count=len(parsed.copper_layers),
                footprint_count=len(parsed.summary.footprints),
                pad_count=len(parsed.pads),
                placement_density_percent=placement_density,
                backend=status.name,
                backend_version=status.version,
                effective_configuration=self._routing_configuration(request),
                requested_net_count=len(target_nets),
                requested_net_role_counts=requested_net_role_counts,
                baseline_routed_net_count=analysis.routed_net_count,
                baseline_unrouted_net_count=analysis.unrouted_net_count,
                final_routed_net_count=analysis.routed_net_count,
                final_unrouted_net_count=analysis.unrouted_net_count,
                baseline_open_connection_count=analysis.unrouted_connection_count,
                final_open_connection_count=analysis.unrouted_connection_count,
                open_connection_delta=0,
                board_baseline_open_connection_count=board_analysis.unrouted_connection_count,
                board_final_open_connection_count=board_analysis.unrouted_connection_count,
                baseline_drc_error_count=self._drc_error_count(baseline_drc),
                final_drc_error_count=self._drc_error_count(baseline_drc),
                baseline_drc_warning_count=self._drc_warning_count(baseline_drc),
                final_drc_warning_count=self._drc_warning_count(baseline_drc),
            )
        )
        strategies = self.routing_backend.strategies(request)
        evaluated: list[
            tuple[
                tuple[int, int, int, int, float, int],
                tuple[RouteSegment, ...],
                tuple[RouteVia, ...],
                RoutingCandidateEvaluation,
            ]
        ] = []
        diagnostic_candidates: list[RoutingCandidateEvaluation] = []
        failures: list[str] = []
        seen_fingerprints: dict[str, RoutingStrategy] = {}
        seed_only_candidate = False
        proposal_root = self.data_dir / "routing-proposals"
        proposal_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="kicad-routing-tools-", dir=proposal_root
        ) as directory:
            root = Path(directory)
            router_input = self._stage_router_input(
                pcb,
                root / "input",
                target_nets,
                add_escape_stubs=request.allow_fine_pitch_escape_stubs,
                default_width_mm=request.default_track_width_mm,
                default_clearance_mm=request.default_clearance_mm,
                via_diameter_mm=request.via_diameter_mm,
                via_drill_mm=request.via_drill_mm,
                seed_segments=request.seed_segments,
                seed_vias=request.seed_vias,
            )
            seeded_candidate: RoutedBoardCandidate | None = None
            if request.allow_fine_pitch_escape_stubs or request.seed_segments or request.seed_vias:
                seeded_analysis = self.adapter.analyze_routing(
                    router_input, session_id, target_nets
                )
                if seeded_analysis.complete:
                    self.routing_backend.refill_zones(router_input)
                    seeded_candidate = RoutedBoardCandidate(
                        strategy="mps",
                        pcb=router_input,
                        elapsed_seconds=0,
                    )
                    strategies = ("mps",)
                    seed_only_candidate = True
            for strategy_index, strategy in enumerate(strategies):
                candidate_started_at = utc_now()
                candidate_started = time.monotonic()
                candidate = None
                try:
                    candidate = seeded_candidate or self.routing_backend.route(
                        router_input,
                        root / strategy,
                        request,
                        strategy,
                    )
                    segments, vias = self._routing_delta(pcb, candidate.pcb, target_nets)
                    if not request.allow_vias and vias:
                        raise CopperbrainError(
                            ErrorCode.VALIDATION_FAILED,
                            "KiCadRoutingTools used vias although this request forbids them",
                            details={"via_count": len(vias)},
                        )
                    routed_analysis = self.adapter.analyze_routing(
                        candidate.pcb, session_id, target_nets
                    )
                    board_routed_analysis = self.adapter.analyze_routing(candidate.pcb, session_id)
                    self._stage_candidate_rules(pcb, candidate.pcb)
                    candidate_drc = self.drc_runner(candidate.pcb)
                    new_errors = self._new_drc_errors(baseline_drc, candidate_drc)
                    new_warnings = self._new_drc_warnings(baseline_drc, candidate_drc)
                    track_length = round(
                        sum(
                            math.dist(
                                (item.start_x_mm, item.start_y_mm),
                                (item.end_x_mm, item.end_y_mm),
                            )
                            for item in segments
                        ),
                        6,
                    )
                    fingerprint_payload = "\n".join(
                        sorted(
                            [item.model_dump_json() for item in segments]
                            + [item.model_dump_json() for item in vias]
                        )
                    )
                    fingerprint = hashlib.sha256(fingerprint_payload.encode()).hexdigest()
                    duplicate_of = seen_fingerprints.get(fingerprint)
                    seen_fingerprints.setdefault(fingerprint, strategy)
                    evaluation = RoutingCandidateEvaluation(
                        strategy=strategy,
                        complete=routed_analysis.complete,
                        unrouted_connection_count=routed_analysis.unrouted_connection_count,
                        drc_available=candidate_drc.available and candidate_drc.error is None,
                        new_drc_error_count=new_errors,
                        segment_count=len(segments),
                        via_count=len(vias),
                        track_length_mm=track_length,
                        fingerprint=fingerprint,
                        duplicate_of=duplicate_of,
                        backend_elapsed_seconds=candidate.elapsed_seconds,
                        routing_pass_metrics=candidate.pass_metrics,
                        normalization_count=candidate.normalization_count,
                        applicable=candidate.watchdog_reason is None,
                        diagnostic_only=candidate.watchdog_reason is not None,
                        failure_reason=candidate.watchdog_reason,
                        copper_produced_per_second=(
                            round(track_length / candidate.elapsed_seconds, 6)
                            if candidate.elapsed_seconds > 0
                            else 0
                        ),
                        connections_resolved_per_pass=round(
                            max(
                                0,
                                analysis.unrouted_connection_count
                                - routed_analysis.unrouted_connection_count,
                            )
                            / max(1, len(candidate.pass_metrics)),
                            6,
                        ),
                    )
                    rank = (
                        0 if evaluation.complete else 1,
                        new_errors if new_errors is not None else 1_000_000,
                        evaluation.unrouted_connection_count,
                        evaluation.via_count,
                        evaluation.track_length_mm,
                        strategy_index,
                    )
                    if evaluation.applicable:
                        evaluated.append((rank, segments, vias, evaluation))
                    else:
                        diagnostic_candidates.append(evaluation)
                    candidate_finished_at = utc_now()
                    (
                        best_pass,
                        failed_route_count,
                        stagnation_count,
                        cpu_seconds,
                        peak_memory_gb,
                    ) = self._pass_optimization_summary(candidate.pass_metrics)
                    self.metrics.write(
                        ConnectivityMetricRecord(
                            run_id=run_id,
                            phase="candidate",
                            outcome="success" if evaluation.applicable else "failure",
                            started_at=candidate_started_at,
                            finished_at=candidate_finished_at,
                            duration_seconds=time.monotonic() - candidate_started,
                            project_fingerprint=project_fingerprint,
                            source_hashes=source_hashes,
                            board_width_mm=board_width,
                            board_height_mm=board_height,
                            copper_layer_count=len(parsed.copper_layers),
                            footprint_count=len(parsed.summary.footprints),
                            pad_count=len(parsed.pads),
                            placement_density_percent=placement_density,
                            backend=status.name,
                            backend_version=status.version,
                            strategy=strategy,
                            effective_configuration=self._routing_configuration(request, strategy),
                            requested_net_count=len(target_nets),
                            requested_net_role_counts=requested_net_role_counts,
                            baseline_routed_net_count=analysis.routed_net_count,
                            baseline_unrouted_net_count=analysis.unrouted_net_count,
                            final_routed_net_count=routed_analysis.routed_net_count,
                            final_unrouted_net_count=routed_analysis.unrouted_net_count,
                            baseline_open_connection_count=analysis.unrouted_connection_count,
                            final_open_connection_count=routed_analysis.unrouted_connection_count,
                            open_connection_delta=(
                                analysis.unrouted_connection_count
                                - routed_analysis.unrouted_connection_count
                            ),
                            board_baseline_open_connection_count=(
                                board_analysis.unrouted_connection_count
                            ),
                            board_final_open_connection_count=(
                                board_routed_analysis.unrouted_connection_count
                            ),
                            segment_count=len(segments),
                            via_count=len(vias),
                            routed_length_mm=track_length,
                            baseline_drc_error_count=self._drc_error_count(baseline_drc),
                            final_drc_error_count=self._drc_error_count(candidate_drc),
                            new_drc_error_count=new_errors,
                            baseline_drc_warning_count=self._drc_warning_count(baseline_drc),
                            final_drc_warning_count=self._drc_warning_count(candidate_drc),
                            new_drc_warning_count=new_warnings,
                            routing_pass_metrics=candidate.pass_metrics,
                            normalization_count=candidate.normalization_count,
                            best_pass_number=best_pass,
                            failed_route_count=failed_route_count,
                            stagnation_count=stagnation_count,
                            cpu_seconds=cpu_seconds,
                            peak_memory_gb=peak_memory_gb,
                            watchdog_reason=candidate.watchdog_reason,
                            copper_produced_per_second=evaluation.copper_produced_per_second,
                            connections_resolved_per_pass=(
                                evaluation.connections_resolved_per_pass
                            ),
                            diagnostic_only=evaluation.diagnostic_only,
                            applicable=evaluation.applicable,
                        )
                    )
                except CopperbrainError as exc:
                    failures.append(f"{strategy}: {exc}")
                    pass_metrics = (
                        candidate.pass_metrics
                        if candidate is not None
                        else self._routing_metrics_from_error(exc)
                    )
                    normalization_count = (
                        candidate.normalization_count
                        if candidate is not None
                        else self._normalization_count_from_error(exc)
                    )
                    candidate_finished_at = utc_now()
                    (
                        best_pass,
                        failed_route_count,
                        stagnation_count,
                        cpu_seconds,
                        peak_memory_gb,
                    ) = self._pass_optimization_summary(pass_metrics)
                    self.metrics.write(
                        ConnectivityMetricRecord(
                            run_id=run_id,
                            phase="candidate",
                            outcome="failure",
                            started_at=candidate_started_at,
                            finished_at=candidate_finished_at,
                            duration_seconds=time.monotonic() - candidate_started,
                            project_fingerprint=project_fingerprint,
                            source_hashes=source_hashes,
                            board_width_mm=board_width,
                            board_height_mm=board_height,
                            copper_layer_count=len(parsed.copper_layers),
                            footprint_count=len(parsed.summary.footprints),
                            pad_count=len(parsed.pads),
                            placement_density_percent=placement_density,
                            backend=status.name,
                            backend_version=status.version,
                            strategy=strategy,
                            effective_configuration=self._routing_configuration(request, strategy),
                            requested_net_count=len(target_nets),
                            requested_net_role_counts=requested_net_role_counts,
                            baseline_routed_net_count=analysis.routed_net_count,
                            baseline_unrouted_net_count=analysis.unrouted_net_count,
                            baseline_open_connection_count=analysis.unrouted_connection_count,
                            board_baseline_open_connection_count=(
                                board_analysis.unrouted_connection_count
                            ),
                            baseline_drc_error_count=self._drc_error_count(baseline_drc),
                            baseline_drc_warning_count=self._drc_warning_count(baseline_drc),
                            error_code=exc.error.code,
                            watchdog_reason=(
                                str(exc.error.details["watchdog"])
                                if "watchdog" in exc.error.details
                                else None
                            ),
                            routing_pass_metrics=pass_metrics,
                            normalization_count=normalization_count,
                            best_pass_number=best_pass,
                            failed_route_count=failed_route_count,
                            stagnation_count=stagnation_count,
                            cpu_seconds=cpu_seconds,
                            peak_memory_gb=peak_memory_gb,
                            connections_resolved_per_pass=(
                                round(
                                    sum(item.connections_resolved for item in pass_metrics)
                                    / max(1, len(pass_metrics)),
                                    6,
                                )
                            ),
                            diagnostic_only=True,
                            applicable=False,
                        )
                    )
                    diagnostic_candidates.append(
                        RoutingCandidateEvaluation(
                            strategy=strategy,
                            complete=False,
                            unrouted_connection_count=analysis.unrouted_connection_count,
                            drc_available=False,
                            segment_count=0,
                            via_count=0,
                            track_length_mm=0,
                            fingerprint=hashlib.sha256(
                                f"{run_id}:{strategy}:diagnostic".encode()
                            ).hexdigest(),
                            backend_elapsed_seconds=time.monotonic() - candidate_started,
                            routing_pass_metrics=pass_metrics,
                            normalization_count=normalization_count,
                            applicable=False,
                            diagnostic_only=True,
                            failure_reason=str(exc),
                            connections_resolved_per_pass=round(
                                sum(item.connections_resolved for item in pass_metrics)
                                / max(1, len(pass_metrics)),
                                6,
                            ),
                        )
                    )
        if not evaluated:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCadRoutingTools produced no usable routing candidate",
                actionable_hint="Review placement and design rules, then retry.",
                details={
                    "failures": failures,
                    "partial_candidate_diagnostics": [
                        item.model_dump(mode="json") for item in diagnostic_candidates
                    ],
                    "routing_hotspots": [item.model_dump(mode="json") for item in routing_hotspots],
                    "placement_rework_recommended": bool(routing_hotspots),
                    "metrics_run_id": run_id,
                },
            )
        evaluated.sort(key=lambda item: item[0])
        _, segments, vias, selected = evaluated[0]
        evaluations = tuple(
            item[3].model_copy(update={"selected": item[3].strategy == selected.strategy})
            for item in evaluated
        ) + tuple(diagnostic_candidates)
        return RoutingPlan(
            session_id=session_id,
            request=request,
            segments=segments,
            vias=vias,
            target_nets=target_nets,
            analysis_before=analysis,
            predicted_complete=selected.complete,
            backend="kicad_routing_tools",
            backend_version=status.version,
            metrics_run_id=run_id,
            candidate_evaluations=evaluations,
            routing_hotspots=routing_hotspots,
            placement_rework_recommended=bool(routing_hotspots),
            evidence=(
                (
                    "Copper geometry was completed by explicitly supplied typed seeds and/or "
                    "enabled geometry-derived fine-pitch escapes before a router process "
                    "was needed"
                    if seed_only_candidate
                    else "Copper geometry was generated by the managed local "
                    "KiCadRoutingTools Rust-accelerated A* backend"
                ),
                f"Evaluated {len(evaluations)} deterministic candidate configuration(s)",
                "Autorouting stopped after the configured candidates, with a hard maximum of "
                "three attempts",
                f"Observed {len(seen_fingerprints)} unique copper candidate(s)",
                f"Selected {selected.strategy}: {selected.segment_count} segments, "
                f"{selected.via_count} vias, {selected.track_length_mm:g} mm routed length",
            ),
            assumptions=(
                "KiCadRoutingTools consumes the reviewed widths, clearances, vias, and board "
                "geometry supplied by Copperbrain",
                "The selected candidate is still subject to the authoritative prepared-workspace "
                "connectivity and comparative KiCad DRC gates",
                "Only the selected candidate is applied to the prepared PCB; final acceptance "
                "and engineering completion remain with the user",
                "Routing does not certify SI, PI, EMC, thermal, or impedance behavior",
            ),
        )

    def _current_hashes(self, session: ProjectSession) -> dict[str, str]:
        return {
            relative: hash_file(session.root / relative)
            for relative in session.hashes
            if (session.root / relative).is_file()
        }

    def _validate_workspace(
        self,
        session: ProjectSession,
        temporary_pcb: Path,
        target_nets: tuple[str, ...],
        require_complete: bool,
    ) -> tuple[ValidationReport, DrcReport, RoutingAnalysis]:
        structural = self.adapter.validate(temporary_pcb)
        routing = self.adapter.analyze_routing(temporary_pcb, session.id, target_nets)
        before = self.drc_runner(session.pcb_file)
        after = self.drc_runner(temporary_pcb)
        baseline = Counter(
            (item.code, item.message) for item in before.violations if item.severity == "error"
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if item.severity == "error"
        )
        new_errors = generated - baseline
        complete_ok = routing.complete or not require_complete
        drc_ok = after.available and after.error is None and not new_errors
        messages = list(structural.messages)
        if not complete_ok:
            messages.append(
                f"Prepared routing leaves {routing.unrouted_connection_count} connection(s) open"
            )
        if after.error is not None:
            messages.append(after.error.message)
        messages.extend(
            f"New DRC error: {code or 'unknown'}: {message} (x{count})"
            for (code, message), count in new_errors.items()
        )
        validation = ValidationReport(
            valid=structural.valid and complete_ok and drc_ok,
            checks={
                **structural.checks,
                "routing_complete": routing.complete,
                "routing_completion_required": require_complete,
                "drc_available": after.available,
                "drc_command_ok": after.error is None,
                "drc_no_new_errors": not new_errors,
            },
            messages=tuple(messages),
        )
        return validation, after, routing

    def prepare(self, session_id: str, plan: RoutingPlan) -> PcbRoutingChangeSet:
        failure_started_at = utc_now()
        failure_started = time.monotonic()
        try:
            return self._prepare(session_id, plan)
        except CopperbrainError as exc:
            with suppress(CopperbrainError):
                self._write_plan_lifecycle_failure(
                    session_id,
                    plan,
                    "prepare",
                    started_at=failure_started_at,
                    started_monotonic=failure_started,
                    error_code=exc.error.code,
                )
            raise

    def _prepare(self, session_id: str, plan: RoutingPlan) -> PcbRoutingChangeSet:
        metric_started_at = utc_now()
        metric_started = time.monotonic()
        if plan.session_id != session_id:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT, "Routing plan belongs to another session"
            )
        session, pcb = self._session_pcb(session_id)
        current = self._current_hashes(session)
        if current != session.hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project changed after the session was opened",
                actionable_hint="Open the project again before preparing routing changes.",
            )
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(session.root, workspace, ignore=PROJECT_COPY_IGNORE)
        temporary_pcb = workspace / pcb.relative_to(session.root)
        self.adapter.apply_routing(temporary_pcb, plan.segments, plan.vias)
        self.routing_backend.refill_zones(temporary_pcb)
        validation, drc, routing = self._validate_workspace(
            session, temporary_pcb, plan.target_nets, plan.request.require_complete
        )
        preview_directory = workspace
        preview_pdf = None
        if self.publish_artifacts:
            pdf = self.pdf_exporter(
                temporary_pcb, workspace / "Copperbrain-PCB-routing-preview.pdf"
            )
            preview_directory = publish_preview(workspace, session.root, identifier)
            preview_pdf = preview_directory / pdf.relative_to(workspace)
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        change_set = PcbRoutingChangeSet(
            id=identifier,
            session_id=session_id,
            project_hash=aggregate_hash(current),
            plan=plan,
            affected_files=(pcb,),
            source_hashes=current,
            semantic_diff=(
                *(
                    f"route {item.net} on {item.layer}: "
                    f"({item.start_x_mm:g}, {item.start_y_mm:g}) to "
                    f"({item.end_x_mm:g}, {item.end_y_mm:g}), "
                    f"width {item.width_mm:g} mm"
                    for item in plan.segments
                ),
                *(
                    f"via {item.net} at ({item.x_mm:g}, {item.y_mm:g}), "
                    f"{item.diameter_mm:g}/{item.drill_mm:g} mm"
                    for item in plan.vias
                ),
            ),
            risks=(
                "A clean DRC does not certify signal integrity, power integrity, EMC, "
                "thermal, or impedance behavior",
                "KiCad may overwrite external changes if an editor has unsaved state",
            ),
            validation_report=validation,
            drc=drc,
            routing_analysis=routing,
            preview_directory=preview_directory,
            preview_pdf=preview_pdf,
            status=status,
        )
        self._changes[identifier] = _PreparedRouting(change_set, workspace)
        self._persist(self._changes[identifier], session.root)
        self._write_lifecycle_metric(
            change_set,
            "prepare",
            started_at=metric_started_at,
            started_monotonic=metric_started,
        )
        return change_set

    def _get(self, change_set_id: str) -> _PreparedRouting:
        try:
            return self._changes[change_set_id]
        except KeyError:
            return self._load(change_set_id)

    def change_set(self, change_set_id: str) -> PcbRoutingChangeSet:
        """Return a routing change, resuming it from private storage when necessary."""
        return self._get(change_set_id).change_set

    def review(self, change_set_id: str) -> RoutingReviewSummary:
        """Return concise decision evidence without serializing every route operation."""
        change = self.change_set(change_set_id)
        errors = sum(item.severity == "error" for item in change.drc.violations)
        warnings = sum(item.severity == "warning" for item in change.drc.violations)
        return RoutingReviewSummary(
            change_set_id=change.id,
            status=change.status,
            target_nets=change.plan.target_nets,
            validation_valid=change.validation_report.valid,
            routing_complete=change.routing_analysis.complete,
            unrouted_connection_count=change.routing_analysis.unrouted_connection_count,
            segment_count=len(change.plan.segments),
            via_count=len(change.plan.vias),
            drc_error_count=errors,
            drc_warning_count=warnings,
            preview_directory=change.preview_directory,
            preview_pdf=change.preview_pdf,
            risks=change.risks,
        )

    def validate(self, change_set_id: str) -> tuple[ValidationReport, DrcReport, RoutingAnalysis]:
        metric_started_at = utc_now()
        metric_started = time.monotonic()
        prepared = self._get(change_set_id)
        change = prepared.change_set
        session, pcb = self._session_pcb(change.session_id)
        validation, drc, routing = self._validate_workspace(
            session,
            prepared.workspace / pcb.relative_to(session.root),
            change.plan.target_nets,
            change.plan.request.require_complete,
        )
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        prepared.change_set = change.model_copy(
            update={
                "validation_report": validation,
                "drc": drc,
                "routing_analysis": routing,
                "status": status,
            }
        )
        self._persist(prepared, session.root)
        self._write_lifecycle_metric(
            prepared.change_set,
            "validate",
            started_at=metric_started_at,
            started_monotonic=metric_started,
            outcome="success" if validation.valid else "failure",
            error_code=None if validation.valid else ErrorCode.VALIDATION_FAILED,
        )
        return validation, drc, routing

    def apply(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbRoutingChangeSet:
        metric_started_at = utc_now()
        metric_started = time.monotonic()
        prepared = self._get(change_set_id)
        change = prepared.change_set
        if not confirmed:
            self._write_lifecycle_metric(
                change,
                "apply",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.CONFIRMATION_REQUIRED,
            )
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        if change.status is not ChangeStatus.VALIDATED:
            self._write_lifecycle_metric(
                change,
                "apply",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.VALIDATION_FAILED,
            )
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Routing is not validated")
        session, pcb = self._session_pcb(change.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            self._write_lifecycle_metric(
                change,
                "apply",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.UNSAFE_EDITOR_STATE,
            )
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE,
                "KiCad editor state is not safely closed",
                actionable_hint="Save and close PCB Editor, then retry.",
            )
        if self._current_hashes(session) != change.source_hashes:
            prepared.change_set = change.model_copy(update={"status": ChangeStatus.STALE})
            self._persist(prepared, session.root)
            self._write_lifecycle_metric(
                prepared.change_set,
                "apply",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.CONFLICT,
            )
            raise CopperbrainError(ErrorCode.CONFLICT, "Routing change set is stale")
        validation, drc, routing = self._validate_workspace(
            session,
            prepared.workspace / pcb.relative_to(session.root),
            change.plan.target_nets,
            change.plan.request.require_complete,
        )
        if not validation.valid:
            prepared.change_set = change.model_copy(
                update={
                    "validation_report": validation,
                    "drc": drc,
                    "routing_analysis": routing,
                    "status": ChangeStatus.PREPARED,
                }
            )
            self._persist(prepared, session.root)
            self._write_lifecycle_metric(
                prepared.change_set,
                "apply",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.VALIDATION_FAILED,
            )
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Routing failed validation immediately before apply",
                actionable_hint="Review the persisted preview and prepare the routing again.",
            )
        change = change.model_copy(
            update={
                "validation_report": validation,
                "drc": drc,
                "routing_analysis": routing,
            }
        )
        snapshot_id = uuid.uuid4().hex
        snapshot = self.data_dir / "snapshots" / snapshot_id
        snapshot.mkdir(parents=True, exist_ok=False)
        for affected in change.affected_files:
            relative = affected.relative_to(session.root)
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(affected, destination)
        try:
            for affected in change.affected_files:
                relative = affected.relative_to(session.root)
                _atomic_copy(prepared.workspace / relative, affected)
        except Exception:
            for affected in change.affected_files:
                relative = affected.relative_to(session.root)
                _atomic_copy(snapshot / relative, affected)
            self._write_lifecycle_metric(
                change,
                "apply",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.INTERNAL_ERROR,
            )
            raise
        prepared.snapshot = snapshot
        prepared.change_set = change.model_copy(
            update={"status": ChangeStatus.APPLIED, "snapshot_id": snapshot_id}
        )
        self._persist(prepared, session.root)
        self._write_lifecycle_metric(
            prepared.change_set,
            "apply",
            started_at=metric_started_at,
            started_monotonic=metric_started,
        )
        return prepared.change_set

    def rollback(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbRoutingChangeSet:
        metric_started_at = utc_now()
        metric_started = time.monotonic()
        prepared = self._get(change_set_id)
        change = prepared.change_set
        if not confirmed:
            self._write_lifecycle_metric(
                change,
                "rollback",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.CONFIRMATION_REQUIRED,
            )
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        if change.status is not ChangeStatus.APPLIED or prepared.snapshot is None:
            self._write_lifecycle_metric(
                change,
                "rollback",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.CONFLICT,
            )
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Only an applied routing change can be rolled back"
            )
        session, _ = self._session_pcb(change.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            self._write_lifecycle_metric(
                change,
                "rollback",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.UNSAFE_EDITOR_STATE,
            )
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE, "KiCad editor state is not safely closed"
            )
        try:
            for affected in change.affected_files:
                relative = affected.relative_to(session.root)
                _atomic_copy(prepared.snapshot / relative, affected)
        except Exception:
            self._write_lifecycle_metric(
                change,
                "rollback",
                started_at=metric_started_at,
                started_monotonic=metric_started,
                outcome="failure",
                error_code=ErrorCode.INTERNAL_ERROR,
            )
            raise
        prepared.change_set = change.model_copy(update={"status": ChangeStatus.ROLLED_BACK})
        self._persist(prepared, session.root)
        self._write_lifecycle_metric(
            prepared.change_set,
            "rollback",
            started_at=metric_started_at,
            started_monotonic=metric_started,
        )
        return prepared.change_set

    def restore_snapshot(
        self,
        session_id: str,
        snapshot_id: str,
        *,
        confirmed: bool,
        editor_closed: bool,
    ) -> RoutingSnapshotRestoreResult:
        """Restore a private routing snapshot after binding it to the current board."""
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        if re.fullmatch(r"[0-9a-f]{32}", snapshot_id) is None:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Snapshot identifier is invalid")
        session, pcb = self._session_pcb(session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE, "KiCad editor state is not safely closed"
            )
        snapshot_root = (self.data_dir / "snapshots" / snapshot_id).resolve()
        snapshots_root = (self.data_dir / "snapshots").resolve()
        if snapshot_root.parent != snapshots_root:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Snapshot path is invalid")
        relative_pcb = pcb.relative_to(session.root)
        snapshot_pcb = snapshot_root / relative_pcb
        if not snapshot_pcb.is_file():
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Routing snapshot was not found for this PCB",
                details={"snapshot_id": snapshot_id, "pcb": str(relative_pcb)},
            )

        current_board = self.adapter.parse(pcb)
        snapshot_board = self.adapter.parse(snapshot_pcb)
        excluded_summary = {
            "session_id",
            "pcb_file",
            "track_count",
            "via_count",
            "ipc",
            "warnings",
        }
        current_identity = (
            current_board.summary.model_dump(mode="json", exclude=excluded_summary),
            tuple(sorted(item.model_dump_json() for item in current_board.pads)),
        )
        snapshot_identity = (
            snapshot_board.summary.model_dump(mode="json", exclude=excluded_summary),
            tuple(sorted(item.model_dump_json() for item in snapshot_board.pads)),
        )
        if current_identity != snapshot_identity:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Routing snapshot does not belong to the current PCB geometry",
                actionable_hint="Select a snapshot created from this exact placed board.",
            )

        validation = self.adapter.validate(snapshot_pcb)
        drc = self.drc_runner(snapshot_pcb)
        if not validation.valid or not drc.available or drc.error is not None:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Routing snapshot could not be validated before restoration",
                details={
                    "validation_messages": list(validation.messages),
                    "drc_error": drc.error.model_dump(mode="json") if drc.error else None,
                },
            )

        recovery_snapshot_id = uuid.uuid4().hex
        recovery_root = self.data_dir / "snapshots" / recovery_snapshot_id
        recovery_pcb = recovery_root / relative_pcb
        recovery_pcb.parent.mkdir(parents=True, exist_ok=False)
        shutil.copy2(pcb, recovery_pcb)
        try:
            _atomic_copy(snapshot_pcb, pcb)
        except Exception:
            _atomic_copy(recovery_pcb, pcb)
            raise
        return RoutingSnapshotRestoreResult(
            restored_snapshot_id=snapshot_id,
            recovery_snapshot_id=recovery_snapshot_id,
            affected_file=pcb,
            validation_report=validation,
            drc=drc,
        )
