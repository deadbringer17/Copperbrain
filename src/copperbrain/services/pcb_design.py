"""PCB inspection, deterministic placement, preview, and safe mutation workflow."""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from copperbrain.adapters.kicad_cli import export_pcb_pdf, run_drc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.pcb_placement import KiCadPlacementAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    ErrorCode,
    PcbBounds,
    PcbFootprintPlacement,
    PcbNetInspection,
    PcbPadInspection,
    PcbPlacementChangeRecord,
    PcbPlacementChangeSet,
    PcbSummary,
    PlacementAnalysis,
    PlacementIssue,
    PlacementOperation,
    PlacementProposal,
    PlacementRequest,
    ProjectSession,
    RouteSegment,
    RouteVia,
    ValidationReport,
)
from copperbrain.services.outputs import PROJECT_COPY_IGNORE, publish_preview
from copperbrain.services.placement_optimizer import (
    copper_anchored_references,
    copper_conflicting_references,
    optimize_placement,
    project_placement,
)
from copperbrain.services.projects import ProjectService, aggregate_hash, hash_file


@dataclass
class _PreparedPlacement:
    change_set: PcbPlacementChangeSet
    workspace: Path
    snapshot: Path | None = None


def _editor_lock_exists(root: Path) -> bool:
    return any(root.glob("*.lck")) or any(root.glob(".*.lck"))


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


def _overlap(left: PcbBounds, right: PcbBounds, margin: float = 0) -> bool:
    return not (
        left.max_x_mm + margin <= right.min_x_mm
        or right.max_x_mm + margin <= left.min_x_mm
        or left.max_y_mm + margin <= right.min_y_mm
        or right.max_y_mm + margin <= left.min_y_mm
    )


def _inside(inner: PcbBounds, outer: PcbBounds) -> bool:
    return (
        inner.min_x_mm >= outer.min_x_mm
        and inner.min_y_mm >= outer.min_y_mm
        and inner.max_x_mm <= outer.max_x_mm
        and inner.max_y_mm <= outer.max_y_mm
    )


class PcbDesignService:
    """Own read-only PCB queries and confirmed placement change sets."""

    def __init__(
        self,
        projects: ProjectService,
        data_dir: Path,
        adapter: PcbFileAdapter | None = None,
        placement_adapter: KiCadPlacementAdapter | None = None,
        drc_runner: Callable[[Path | None], DrcReport] | None = None,
        pdf_exporter: Callable[[Path, Path], Path] | None = None,
        zone_refiller: Callable[[Path], None] | None = None,
        publish_artifacts: bool = True,
    ) -> None:
        self.projects = projects
        self.data_dir = data_dir
        self.adapter = adapter or PcbFileAdapter()
        self.placement_adapter = placement_adapter or KiCadPlacementAdapter()
        self.drc_runner = drc_runner or (lambda pcb: run_drc(detect_kicad().selected_cli, pcb))
        self.pdf_exporter = pdf_exporter or (
            lambda pcb, destination: export_pcb_pdf(detect_kicad().selected_cli, pcb, destination)
        )
        self.zone_refiller = zone_refiller
        self.publish_artifacts = publish_artifacts
        self._changes: dict[str, _PreparedPlacement] = {}

    @property
    def _records_dir(self) -> Path:
        return self.data_dir / "pcb-placement-changes"

    def _record_path(self, change_set_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", change_set_id) is None:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Placement identifier is invalid")
        return self._records_dir / f"{change_set_id}.json"

    def _persist(self, prepared: _PreparedPlacement, project_root: Path) -> None:
        root = project_root.resolve()
        record = PcbPlacementChangeRecord(
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

    def _load(self, change_set_id: str) -> _PreparedPlacement:
        try:
            record = PcbPlacementChangeRecord.model_validate_json(
                self._record_path(change_set_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "Placement change set was not found"
            ) from exc
        except (OSError, ValueError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Persisted placement change set is invalid",
                details={"reason": str(exc)},
            ) from exc
        workspace = record.workspace.resolve()
        private_root = (self.data_dir / "workspaces").resolve()
        if not workspace.is_relative_to(private_root) or not workspace.is_dir():
            raise CopperbrainError(ErrorCode.CONFLICT, "Placement workspace is unavailable")
        session = self.projects.open_project(record.project_root)
        affected = tuple(session.root / path for path in record.affected_relative_files)
        prepared = _PreparedPlacement(
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
                actionable_hint="Create or synchronize the PCB in KiCad before placement analysis.",
            )
        return session, session.pcb_file

    def summary(self, session_id: str) -> PcbSummary:
        _, pcb = self._session_pcb(session_id)
        return self.adapter.summary(pcb, session_id)

    def inspect_net(self, session_id: str, net_name: str) -> PcbNetInspection:
        _, pcb = self._session_pcb(session_id)
        return self.adapter.inspect_net(pcb, session_id, net_name)

    def footprint(self, session_id: str, reference: str) -> PcbFootprintPlacement:
        summary = self.summary(session_id)
        match = next((item for item in summary.footprints if item.reference == reference), None)
        if match is None:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "PCB footprint was not found",
                details={"reference": reference},
            )
        return match

    @staticmethod
    def _analyze_summary(
        summary: PcbSummary, pads: tuple[PcbPadInspection, ...] = ()
    ) -> PlacementAnalysis:
        overlaps: list[tuple[str, str]] = []
        outside: list[str] = []
        issues: list[PlacementIssue] = []
        footprints = summary.footprints
        if not footprints:
            issues.append(
                PlacementIssue(
                    kind="empty_board",
                    severity="warning",
                    references=(),
                    message="PCB contains no footprints to analyze",
                )
            )
        if summary.board_bounds is None:
            issues.append(
                PlacementIssue(
                    kind="missing_outline",
                    severity="error",
                    references=(),
                    message="A closed Edge.Cuts outline is required for placement validation",
                )
            )
        for index, left in enumerate(footprints):
            for right in footprints[index + 1 :]:
                shares_physical_side = (
                    left.layer == right.layer
                    or left.mount_type != "smd"
                    or right.mount_type != "smd"
                )
                if shares_physical_side and _overlap(left.bounds, right.bounds):
                    first, second = sorted((left.reference, right.reference))
                    pair = (first, second)
                    overlaps.append(pair)
                    issues.append(
                        PlacementIssue(
                            kind="overlap",
                            severity="error",
                            references=pair,
                            message=f"Footprint courtyards overlap: {pair[0]} and {pair[1]}",
                        )
                    )
            if summary.board_bounds is not None and not _inside(left.bounds, summary.board_bounds):
                outside.append(left.reference)
                issues.append(
                    PlacementIssue(
                        kind="outside_board",
                        severity="error",
                        references=(left.reference,),
                        message=f"Footprint lies outside Edge.Cuts bounds: {left.reference}",
                    )
                )
        deduction = min(
            100,
            len(overlaps) * 15 + len(outside) * 20 + (50 if summary.board_bounds is None else 0),
        )
        score = 0 if not footprints else 100 - deduction
        placement_area = 0.0
        compactness = 0.0
        if footprints:
            min_x = min(item.bounds.min_x_mm for item in footprints)
            min_y = min(item.bounds.min_y_mm for item in footprints)
            max_x = max(item.bounds.max_x_mm for item in footprints)
            max_y = max(item.bounds.max_y_mm for item in footprints)
            placement_area = (max_x - min_x) * (max_y - min_y)
            footprint_area = sum(
                (item.bounds.max_x_mm - item.bounds.min_x_mm)
                * (item.bounds.max_y_mm - item.bounds.min_y_mm)
                for item in footprints
            )
            compactness = min(100.0, 100 * footprint_area / placement_area)
        by_net: dict[str, dict[str, list[tuple[float, float]]]] = {}
        for pad in pads:
            if not pad.net:
                continue
            by_net.setdefault(pad.net, {}).setdefault(pad.reference, []).append(
                (pad.x_mm, pad.y_mm)
            )
        wire_length = 0.0
        cross_layer = 0
        layer_by_reference = {item.reference: item.layer for item in footprints}
        for references in by_net.values():
            points = [
                (
                    sum(x for x, _ in positions) / len(positions),
                    sum(y for _, y in positions) / len(positions),
                )
                for positions in references.values()
            ]
            if len({layer_by_reference.get(reference) for reference in references}) > 1:
                cross_layer += 1
            if len(points) < 2:
                continue
            connected = {0}
            while len(connected) < len(points):
                distance, target = min(
                    (math.dist(points[left], points[right]), right)
                    for left in connected
                    for right in range(len(points))
                    if right not in connected
                )
                wire_length += distance
                connected.add(target)
        assumptions = (
            "Overlap checks use embedded courtyard geometry when present and conservative "
            "bounds otherwise",
            "Edge.Cuts validation uses the rectangular extent of detected outline primitives",
        )
        return PlacementAnalysis(
            session_id=summary.session_id,
            score=score,
            issues=tuple(issues),
            overlap_pairs=tuple(sorted(set(overlaps))),
            outside_board=tuple(sorted(set(outside))),
            footprint_count=len(footprints),
            estimated_wire_length_mm=round(wire_length, 6),
            placement_area_mm2=round(placement_area, 6),
            compactness_percent=round(compactness, 3),
            cross_layer_net_count=cross_layer,
            top_footprint_count=sum(item.layer == "F.Cu" for item in footprints),
            bottom_footprint_count=sum(item.layer == "B.Cu" for item in footprints),
            assumptions=assumptions,
        )

    def analyze_placement(self, session_id: str) -> PlacementAnalysis:
        _, pcb = self._session_pcb(session_id)
        return self._analyze_summary(self.summary(session_id), self.adapter.pads(pcb))

    def propose(self, session_id: str, request: PlacementRequest) -> PlacementProposal:
        _, pcb = self._session_pcb(session_id)
        summary = self.summary(session_id)
        pads = self.adapter.pads(pcb)
        by_reference = {item.reference: item for item in summary.footprints}
        missing = sorted(set(request.references) - set(by_reference))
        anchored: tuple[str, ...] = ()
        effective_request = request
        segments: tuple[RouteSegment, ...] = ()
        vias: tuple[RouteVia, ...] = ()
        if request.existing_copper_policy == "preserve_anchors":
            segments, vias = self.adapter.routing_items(pcb)
            ignored_anchor_nets = (
                frozenset({"GND", "/GND", "PGND", "/PGND"})
                if not request.anchor_ground_copper
                else frozenset()
            )
            anchored = tuple(
                reference
                for reference in copper_anchored_references(
                    pads, segments, vias, ignored_nets=ignored_anchor_nets
                )
                if reference in request.references
            )
            movable = tuple(
                reference for reference in request.references if reference not in anchored
            )
            if not movable:
                raise CopperbrainError(
                    ErrorCode.CONFLICT,
                    "All requested footprints are anchored by existing copper",
                    details={"anchored_references": list(anchored)},
                )
            effective_request = request.model_copy(update={"references": movable})
        locked = sorted(
            reference
            for reference in effective_request.references
            if reference in by_reference and by_reference[reference].locked
        )
        if missing:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Placement references were not found",
                details={"references": missing},
            )
        if locked:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Locked footprints cannot be placed automatically",
                details={"references": locked},
            )
        while True:
            operations = optimize_placement(summary, pads, effective_request)
            predicted, predicted_pads = project_placement(summary, pads, operations)
            if request.existing_copper_policy != "preserve_anchors":
                break
            conflicts = set(copper_conflicting_references(predicted_pads, segments, vias)) & set(
                effective_request.references
            )
            if not conflicts:
                break
            anchored = tuple(sorted(set(anchored) | conflicts))
            movable = tuple(
                reference for reference in request.references if reference not in anchored
            )
            if not movable:
                raise CopperbrainError(
                    ErrorCode.CONFLICT,
                    "Existing copper blocks every requested placement",
                    details={"anchored_references": list(anchored)},
                )
            effective_request = request.model_copy(update={"references": movable})
        after_summary = summary.model_copy(update={"footprints": predicted})
        return PlacementProposal(
            session_id=session_id,
            request=request,
            operations=operations,
            analysis_before=self._analyze_summary(summary, pads),
            analysis_after=self._analyze_summary(after_summary, predicted_pads),
            evidence=(
                "Footprint sizes and pad anchors were derived from PCB geometry",
                "Shared-net distance, placement envelope, edge affinity, rotation, and side "
                "were scored deterministically",
                "Only small SMD passives are eligible for automatic bottom-side placement",
                *(
                    (
                        "Routing-coherent placement increased power/critical-net locality and "
                        "penalized long or footprint-obstructed connection corridors",
                    )
                    if request.strategy == "routing_coherent"
                    else ()
                ),
                *(
                    (f"Preserved {len(anchored)} footprint(s) attached to existing copper",)
                    if anchored
                    else ()
                ),
            ),
            assumptions=(
                "Net distance is a pre-routing estimate, not SI/PI/EMC or thermal validation",
                "Connectors are biased toward board edges; high-fanout nets are down-weighted",
            ),
        )

    def _current_hashes(self, session: ProjectSession) -> dict[str, str]:
        return {
            relative: hash_file(session.root / relative)
            for relative in session.hashes
            if (session.root / relative).is_file()
        }

    def _validate_workspace(
        self, session: ProjectSession, temporary_pcb: Path
    ) -> tuple[ValidationReport, DrcReport]:
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
        drc_ok = after.available and after.error is None and not new_errors
        messages = list(structural.messages)
        if after.error is not None:
            messages.append(after.error.message)
        messages.extend(
            f"New DRC error: {code or 'unknown'}: {message} (x{count})"
            for (code, message), count in new_errors.items()
        )
        return (
            ValidationReport(
                valid=structural.valid and drc_ok,
                checks={
                    **structural.checks,
                    "drc_available": after.available,
                    "drc_no_new_errors": not new_errors,
                    "drc_command_ok": after.error is None,
                },
                messages=tuple(messages),
            ),
            after,
        )

    def prepare(
        self, session_id: str, operations: tuple[PlacementOperation, ...]
    ) -> PcbPlacementChangeSet:
        if not operations:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "At least one placement is required")
        references = [item.reference for item in operations]
        if len(references) != len(set(references)):
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT, "Each footprint may be placed only once per change set"
            )
        session, pcb = self._session_pcb(session_id)
        current = self._current_hashes(session)
        if current != session.hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project changed after the session was opened",
                actionable_hint="Open the project again before preparing placement changes.",
            )
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(session.root, workspace, ignore=PROJECT_COPY_IGNORE)
        temporary_pcb = workspace / pcb.relative_to(session.root)
        current_placements = {
            item.reference: item for item in self.adapter.summary(pcb, session_id).footprints
        }
        api_transform = any(
            (item.layer is not None and item.layer != current_placements[item.reference].layer)
            or not math.isclose(
                item.rotation_deg % 360,
                current_placements[item.reference].rotation_deg % 360,
                abs_tol=1e-9,
            )
            for item in operations
        )
        if api_transform:
            self.placement_adapter.apply(temporary_pcb, operations)
        else:
            self.adapter.apply_placement(temporary_pcb, operations)
        if self.zone_refiller is not None:
            self.zone_refiller(temporary_pcb)
        validation, drc = self._validate_workspace(session, temporary_pcb)
        preview_directory = workspace
        preview_pdf = None
        if self.publish_artifacts:
            pdf = self.pdf_exporter(temporary_pcb, workspace / "Copperbrain-PCB-preview.pdf")
            preview_directory = publish_preview(workspace, session.root, identifier)
            preview_pdf = preview_directory / pdf.relative_to(workspace)
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        change_set = PcbPlacementChangeSet(
            id=identifier,
            session_id=session_id,
            project_hash=aggregate_hash(current),
            operations=operations,
            affected_files=(pcb,),
            source_hashes=current,
            semantic_diff=tuple(
                f"place {item.reference} at ({item.x_mm:g}, {item.y_mm:g}) mm, "
                f"rotation {item.rotation_deg:g} deg, layer {item.layer or 'unchanged'}"
                for item in operations
            ),
            risks=(
                "Placement quality does not prove electrical, thermal, RF, or mechanical "
                "suitability",
                "KiCad may overwrite external changes if an editor has unsaved state",
            ),
            validation_report=validation,
            drc=drc,
            preview_directory=preview_directory,
            preview_pdf=preview_pdf,
            status=status,
        )
        prepared = _PreparedPlacement(change_set, workspace)
        self._changes[identifier] = prepared
        self._persist(prepared, session.root)
        return change_set

    def _get(self, change_set_id: str) -> _PreparedPlacement:
        try:
            return self._changes[change_set_id]
        except KeyError:
            return self._load(change_set_id)

    def validate(self, change_set_id: str) -> tuple[ValidationReport, DrcReport]:
        prepared = self._get(change_set_id)
        session, pcb = self._session_pcb(prepared.change_set.session_id)
        temporary = prepared.workspace / pcb.relative_to(session.root)
        return self._validate_workspace(session, temporary)

    def apply(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbPlacementChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        prepared = self._get(change_set_id)
        change_set = prepared.change_set
        if change_set.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Placement is not validated")
        session, _ = self._session_pcb(change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE,
                "KiCad editor state is not safely closed",
                actionable_hint="Save and close PCB Editor, then retry.",
            )
        current = self._current_hashes(session)
        if current != change_set.source_hashes:
            prepared.change_set = change_set.model_copy(update={"status": ChangeStatus.STALE})
            self._persist(prepared, session.root)
            raise CopperbrainError(ErrorCode.CONFLICT, "Placement change set is stale")
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
    ) -> PcbPlacementChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Rollback confirmation is required"
            )
        prepared = self._get(change_set_id)
        if prepared.change_set.status is not ChangeStatus.APPLIED or prepared.snapshot is None:
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Only an applied placement can be rolled back"
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

    def export_preview(self, session_id: str) -> Path:
        session, pcb = self._session_pcb(session_id)
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "previews" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(session.root, workspace, ignore=PROJECT_COPY_IGNORE)
        temporary_pcb = workspace / pcb.relative_to(session.root)
        self.pdf_exporter(temporary_pcb, workspace / "Copperbrain-PCB-preview.pdf")
        published = publish_preview(workspace, session.root, identifier)
        return published / "Copperbrain-PCB-preview.pdf"
