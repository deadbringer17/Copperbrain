"""Aggregate placement, grounding, and routing behind one PCB acceptance gate."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from copperbrain.adapters.freerouting import RoutingBackend
from copperbrain.adapters.kicad_cli import export_pcb_pdf, run_drc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    ErrorCode,
    PcbPhaseChangeRecord,
    PcbPhaseChangeSet,
    PcbPhaseRequest,
    ProjectSession,
    ValidationReport,
)
from copperbrain.services.outputs import (
    PROJECT_COPY_IGNORE,
    publish_preview,
    require_current_preview,
)
from copperbrain.services.pcb_design import PcbDesignService
from copperbrain.services.pcb_grounding import PcbGroundingService
from copperbrain.services.pcb_routing import PcbRoutingService
from copperbrain.services.projects import ProjectService, aggregate_hash, hash_file


@dataclass
class _PreparedPcbPhase:
    change_set: PcbPhaseChangeSet
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


class PcbPhaseService:
    """Compose all post-rule PCB mutations and expose one final atomic apply."""

    def __init__(
        self,
        projects: ProjectService,
        data_dir: Path,
        routing_backend: RoutingBackend,
    ) -> None:
        self.projects = projects
        self.data_dir = data_dir
        self.routing_backend = routing_backend
        self.adapter = PcbFileAdapter()
        self._changes: dict[str, _PreparedPcbPhase] = {}

    @property
    def _records_dir(self) -> Path:
        return self.data_dir / "pcb-phase-changes"

    def _record_path(self, change_set_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", change_set_id) is None:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "PCB phase identifier is invalid")
        return self._records_dir / f"{change_set_id}.json"

    @staticmethod
    def _current_hashes(session: ProjectSession) -> dict[str, str]:
        return {
            relative: hash_file(session.root / relative)
            for relative in session.hashes
            if (session.root / relative).is_file()
        }

    def _persist(self, prepared: _PreparedPcbPhase, project_root: Path) -> None:
        root = project_root.resolve()
        record = PcbPhaseChangeRecord(
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

    def _load(self, change_set_id: str) -> _PreparedPcbPhase:
        try:
            record = PcbPhaseChangeRecord.model_validate_json(
                self._record_path(change_set_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "PCB phase change set was not found"
            ) from exc
        except (OSError, ValueError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Persisted PCB phase change set is invalid",
                details={"reason": str(exc)},
            ) from exc
        workspace = record.workspace.resolve()
        private_root = (self.data_dir / "workspaces").resolve()
        if not workspace.is_relative_to(private_root) or not workspace.is_dir():
            raise CopperbrainError(ErrorCode.CONFLICT, "PCB phase workspace is unavailable")
        session = self.projects.open_project(record.project_root)
        affected = tuple(session.root / path for path in record.affected_relative_files)
        prepared = _PreparedPcbPhase(
            change_set=record.change_set.model_copy(
                update={"session_id": session.id, "affected_files": affected}
            ),
            workspace=workspace,
            snapshot=record.snapshot.resolve() if record.snapshot is not None else None,
        )
        self._changes[change_set_id] = prepared
        return prepared

    def _get(self, change_set_id: str) -> _PreparedPcbPhase:
        try:
            return self._changes[change_set_id]
        except KeyError:
            return self._load(change_set_id)

    @staticmethod
    def _new_errors(before: DrcReport, after: DrcReport) -> Counter[tuple[str | None, str]]:
        baseline = Counter(
            (item.code, item.message) for item in before.violations if item.severity == "error"
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if item.severity == "error"
        )
        return generated - baseline

    def prepare(self, session_id: str, request: PcbPhaseRequest) -> PcbPhaseChangeSet:
        """Build the complete post-rule PCB in one private, restart-safe workspace."""
        session = self.projects.get_session(session_id)
        if session.pcb_file is None or not session.pcb_file.is_file():
            raise CopperbrainError(ErrorCode.NOT_FOUND, "PCB acceptance requires an existing PCB")
        current = self._current_hashes(session)
        if current != session.hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project changed after the session was opened",
                actionable_hint="Open the project again before preparing PCB acceptance.",
            )
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(session.root, workspace, ignore=PROJECT_COPY_IGNORE)
        child_data = self.data_dir / "pcb-phase-child-state" / identifier
        child_ids: list[str] = []
        metrics_run_ids: list[str] = []
        semantic_diff: list[str] = []

        workspace_projects = ProjectService()
        workspace_session = workspace_projects.open_project(workspace)
        design = PcbDesignService(
            workspace_projects,
            child_data,
            zone_refiller=self.routing_backend.refill_zones,
            publish_artifacts=False,
        )
        grounding = PcbGroundingService(
            workspace_projects, design, child_data, publish_artifacts=False
        )
        routing = PcbRoutingService(
            workspace_projects,
            self.data_dir,
            routing_backend=self.routing_backend,
            publish_artifacts=False,
        )

        if request.placement_operations:
            placement_change = design.prepare(workspace_session.id, request.placement_operations)
            if placement_change.status is not ChangeStatus.VALIDATED:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Aggregate PCB placement did not validate",
                    details={"change_set_id": placement_change.id},
                )
            design.apply(placement_change.id, confirmed=True, editor_closed=True)
            child_ids.append(placement_change.id)
            semantic_diff.extend(placement_change.semantic_diff)
            workspace_session = workspace_projects.open_project(workspace)

        grounding_change = grounding.prepare(workspace_session.id, request.grounding)
        if grounding_change.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Aggregate PCB grounding did not validate",
                details={"change_set_id": grounding_change.id},
            )
        grounding.apply(grounding_change.id, confirmed=True, editor_closed=True)
        child_ids.append(grounding_change.id)
        semantic_diff.extend(grounding_change.semantic_diff)
        workspace_session = workspace_projects.open_project(workspace)

        for batch in request.routing_batches:
            plan = routing.propose(workspace_session.id, batch)
            if plan.metrics_run_id is not None:
                metrics_run_ids.append(plan.metrics_run_id)
            routing_change = routing.prepare(workspace_session.id, plan)
            if routing_change.status is not ChangeStatus.VALIDATED:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Aggregate PCB routing batch did not validate",
                    details={
                        "change_set_id": routing_change.id,
                        "target_nets": routing_change.plan.target_nets,
                    },
                )
            routing.apply(routing_change.id, confirmed=True, editor_closed=True)
            child_ids.append(routing_change.id)
            semantic_diff.extend(routing_change.semantic_diff)
            workspace_session = workspace_projects.open_project(workspace)

        temporary_pcb = workspace_session.pcb_file
        assert temporary_pcb is not None
        structural = self.adapter.validate(temporary_pcb)
        routing_analysis = self.adapter.analyze_routing(temporary_pcb, workspace_session.id)
        before_drc = run_drc(detect_kicad().selected_cli, session.pcb_file)
        after_drc = run_drc(detect_kicad().selected_cli, temporary_pcb)
        new_errors = self._new_errors(before_drc, after_drc)
        complete_ok = routing_analysis.complete or not request.require_board_complete
        validation = ValidationReport(
            valid=(
                structural.valid
                and after_drc.available
                and after_drc.error is None
                and not new_errors
                and complete_ok
            ),
            checks={
                **structural.checks,
                "drc_available": after_drc.available,
                "drc_command_ok": after_drc.error is None,
                "drc_no_new_errors": not new_errors,
                "board_routing_complete": routing_analysis.complete,
                "board_completion_required": request.require_board_complete,
            },
            messages=(
                *structural.messages,
                *(
                    (
                        f"Aggregate PCB leaves {routing_analysis.unrouted_connection_count} "
                        "connection(s) open",
                    )
                    if not complete_ok
                    else ()
                ),
                *(
                    f"New DRC error: {code or 'unknown'}: {message} (x{count})"
                    for (code, message), count in new_errors.items()
                ),
            ),
        )
        pdf = export_pcb_pdf(
            detect_kicad().selected_cli,
            temporary_pcb,
            workspace / "Copperbrain-PCB-acceptance-preview.pdf",
        )
        preview = publish_preview(workspace, session.root, identifier, phase="pcb")
        live_pcb = session.pcb_file
        change_set = PcbPhaseChangeSet(
            id=identifier,
            session_id=session.id,
            project_hash=aggregate_hash(current),
            request=request,
            affected_files=(live_pcb,),
            source_hashes=current,
            child_change_set_ids=tuple(child_ids),
            metrics_run_ids=tuple(metrics_run_ids),
            semantic_diff=tuple(semantic_diff),
            risks=(
                "One approval applies placement, grounding, and every reviewed routing batch",
                "A clean DRC does not certify SI, PI, EMC, thermal, impedance, or DFM behavior",
                "KiCad must be saved and closed before the aggregate apply",
            ),
            validation_report=validation,
            drc=after_drc,
            routing_analysis=routing_analysis,
            preview_directory=preview,
            preview_pdf=preview / pdf.relative_to(workspace),
            status=ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED,
        )
        prepared = _PreparedPcbPhase(change_set=change_set, workspace=workspace)
        self._changes[identifier] = prepared
        self._persist(prepared, session.root)
        return change_set

    def validate(self, change_set_id: str) -> ValidationReport:
        prepared = self._get(change_set_id)
        session = self.projects.get_session(prepared.change_set.session_id)
        if session.pcb_file is None:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "PCB phase source board is unavailable")
        pcb = prepared.workspace / session.pcb_file.relative_to(session.root)
        structural = self.adapter.validate(pcb)
        routing = self.adapter.analyze_routing(pcb, session.id)
        baseline_drc = run_drc(detect_kicad().selected_cli, session.pcb_file)
        drc = run_drc(detect_kicad().selected_cli, pcb)
        new_errors = self._new_errors(baseline_drc, drc)
        complete_ok = routing.complete or not prepared.change_set.request.require_board_complete
        return ValidationReport(
            valid=(
                structural.valid
                and drc.available
                and drc.error is None
                and not new_errors
                and complete_ok
            ),
            checks={
                **structural.checks,
                "drc_available": drc.available,
                "drc_command_ok": drc.error is None,
                "drc_no_new_errors": not new_errors,
                "board_routing_complete": routing.complete,
            },
            messages=(
                *structural.messages,
                *(
                    f"New DRC error: {code or 'unknown'}: {message} (x{count})"
                    for (code, message), count in new_errors.items()
                ),
            ),
        )

    def apply(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbPhaseChangeSet:
        """Apply the complete PCB workspace after the one allowed PCB acceptance."""
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED,
                "Final PCB acceptance is required",
            )
        prepared = self._get(change_set_id)
        change_set = prepared.change_set
        if change_set.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "PCB phase is not validated")
        require_current_preview(change_set.preview_directory, change_set.id)
        session = self.projects.get_session(change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE,
                "KiCad editor state is not safely closed",
            )
        if self._current_hashes(session) != change_set.source_hashes:
            prepared.change_set = change_set.model_copy(update={"status": ChangeStatus.STALE})
            self._persist(prepared, session.root)
            raise CopperbrainError(ErrorCode.CONFLICT, "PCB phase change set is stale")
        validation = self.validate(change_set_id)
        if not validation.valid:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "PCB phase revalidation failed")
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
    ) -> PcbPhaseChangeSet:
        """Restore the one aggregate PCB snapshot after an explicit rollback request."""
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Rollback confirmation required"
            )
        prepared = self._get(change_set_id)
        if prepared.change_set.status is not ChangeStatus.APPLIED or prepared.snapshot is None:
            raise CopperbrainError(ErrorCode.CONFLICT, "Only an applied PCB phase can roll back")
        session = self.projects.get_session(prepared.change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(ErrorCode.UNSAFE_EDITOR_STATE, "KiCad is not safely closed")
        for affected in prepared.change_set.affected_files:
            relative = affected.relative_to(session.root)
            _atomic_copy(prepared.snapshot / relative, affected)
        prepared.change_set = prepared.change_set.model_copy(
            update={"status": ChangeStatus.ROLLED_BACK}
        )
        self._persist(prepared, session.root)
        return prepared.change_set
