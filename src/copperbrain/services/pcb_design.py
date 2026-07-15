"""PCB inspection, deterministic placement, preview, and safe mutation workflow."""

from __future__ import annotations

import os
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
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    ErrorCode,
    PcbBounds,
    PcbFootprintPlacement,
    PcbNetInspection,
    PcbPlacementChangeSet,
    PcbSummary,
    PlacementAnalysis,
    PlacementIssue,
    PlacementOperation,
    PlacementProposal,
    PlacementRequest,
    ProjectSession,
    ValidationReport,
)
from copperbrain.services.outputs import PROJECT_COPY_IGNORE, publish_preview
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


def _translated_bounds(
    footprint: PcbFootprintPlacement, x: float, y: float, rotation: float
) -> PcbBounds:
    width = footprint.bounds.max_x_mm - footprint.bounds.min_x_mm
    height = footprint.bounds.max_y_mm - footprint.bounds.min_y_mm
    normalized = abs((rotation - footprint.rotation_deg) % 180)
    if 45 < normalized < 135:
        width, height = height, width
    return PcbBounds(
        min_x_mm=x - width / 2,
        min_y_mm=y - height / 2,
        max_x_mm=x + width / 2,
        max_y_mm=y + height / 2,
    )


class PcbDesignService:
    """Own read-only PCB queries and confirmed placement change sets."""

    def __init__(
        self,
        projects: ProjectService,
        data_dir: Path,
        adapter: PcbFileAdapter | None = None,
        drc_runner: Callable[[Path | None], DrcReport] | None = None,
        pdf_exporter: Callable[[Path, Path], Path] | None = None,
    ) -> None:
        self.projects = projects
        self.data_dir = data_dir
        self.adapter = adapter or PcbFileAdapter()
        self.drc_runner = drc_runner or (lambda pcb: run_drc(detect_kicad().selected_cli, pcb))
        self.pdf_exporter = pdf_exporter or (
            lambda pcb, destination: export_pcb_pdf(detect_kicad().selected_cli, pcb, destination)
        )
        self._changes: dict[str, _PreparedPlacement] = {}

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
    def _analyze_summary(summary: PcbSummary) -> PlacementAnalysis:
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
                if _overlap(left.bounds, right.bounds):
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
            assumptions=assumptions,
        )

    def analyze_placement(self, session_id: str) -> PlacementAnalysis:
        return self._analyze_summary(self.summary(session_id))

    def propose(self, session_id: str, request: PlacementRequest) -> PlacementProposal:
        summary = self.summary(session_id)
        by_reference = {item.reference: item for item in summary.footprints}
        missing = sorted(set(request.references) - set(by_reference))
        locked = sorted(
            reference
            for reference in request.references
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
        region = request.region or summary.board_bounds
        if region is None:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Placement requires explicit bounds when Edge.Cuts cannot be detected",
            )
        selected = set(request.references)
        occupied = [item.bounds for item in summary.footprints if item.reference not in selected]
        operations: list[PlacementOperation] = []
        proposed_bounds: list[PcbBounds] = []
        step = (
            request.grid_mm
            if request.strategy == "compact"
            else max(request.grid_mm, request.spacing_mm)
        )
        for reference in request.references:
            footprint = by_reference[reference]
            width = footprint.bounds.max_x_mm - footprint.bounds.min_x_mm
            height = footprint.bounds.max_y_mm - footprint.bounds.min_y_mm
            if abs((request.rotation_deg - footprint.rotation_deg) % 180 - 90) < 45:
                width, height = height, width
            x = region.min_x_mm + width / 2
            placed: PcbBounds | None = None
            while x + width / 2 <= region.max_x_mm + 1e-9 and placed is None:
                y = region.min_y_mm + height / 2
                while y + height / 2 <= region.max_y_mm + 1e-9:
                    candidate = PcbBounds(
                        min_x_mm=x - width / 2,
                        min_y_mm=y - height / 2,
                        max_x_mm=x + width / 2,
                        max_y_mm=y + height / 2,
                    )
                    if not any(
                        _overlap(candidate, other, request.spacing_mm)
                        for other in (*occupied, *proposed_bounds)
                    ):
                        placed = candidate
                        break
                    y += step
                x += step
            if placed is None:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Requested footprints do not fit in the placement region",
                    details={"reference": reference},
                )
            center_x = (placed.min_x_mm + placed.max_x_mm) / 2
            center_y = (placed.min_y_mm + placed.max_y_mm) / 2
            operations.append(
                PlacementOperation(
                    reference=reference,
                    x_mm=round(center_x, 6),
                    y_mm=round(center_y, 6),
                    rotation_deg=request.rotation_deg,
                    layer=footprint.layer,
                )
            )
            proposed_bounds.append(placed)

        predicted = tuple(
            item.model_copy(
                update={
                    "x_mm": operation.x_mm,
                    "y_mm": operation.y_mm,
                    "rotation_deg": operation.rotation_deg,
                    "bounds": _translated_bounds(
                        item, operation.x_mm, operation.y_mm, operation.rotation_deg
                    ),
                }
            )
            if (
                operation := next((op for op in operations if op.reference == item.reference), None)
            )
            else item
            for item in summary.footprints
        )
        after_summary = summary.model_copy(update={"footprints": predicted})
        return PlacementProposal(
            session_id=session_id,
            request=request,
            operations=tuple(operations),
            analysis_before=self._analyze_summary(summary),
            analysis_after=self._analyze_summary(after_summary),
            evidence=(
                "Footprint sizes were derived from embedded pad/courtyard geometry",
                "Candidates were scanned deterministically from the region minimum coordinates",
            ),
            assumptions=(
                "The proposal optimizes collision-free packing, not signal integrity or "
                "mechanical intent",
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
        self.adapter.apply_placement(temporary_pcb, operations)
        validation, drc = self._validate_workspace(session, temporary_pcb)
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
        self._changes[identifier] = _PreparedPlacement(change_set, workspace)
        return change_set

    def _get(self, change_set_id: str) -> _PreparedPlacement:
        try:
            return self._changes[change_set_id]
        except KeyError as exc:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "Placement change set was not found"
            ) from exc

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
