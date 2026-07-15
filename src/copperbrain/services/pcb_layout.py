"""Headless schematic-to-PCB initialization with preview, validation, and rollback."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from copperbrain.adapters.kicad_cli import (
    export_netlist,
    export_pcb_pdf,
    run_drc,
    run_erc,
    upgrade_pcb,
)
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.pcb_layout import PcbLayoutAdapter
from copperbrain.adapters.pcb_rules import migrate_managed_pair_rules
from copperbrain.adapters.schematic_api import SchematicApiAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeOperation,
    ChangeStatus,
    DrcReport,
    ErcReport,
    ErrorCode,
    PcbLayoutChangeSet,
    PcbLayoutPlan,
    PlacementAnalysis,
    ProjectSession,
    ValidationReport,
)
from copperbrain.services.outputs import PROJECT_COPY_IGNORE, publish_preview
from copperbrain.services.pcb_design import PcbDesignService
from copperbrain.services.projects import ProjectService, aggregate_hash, hash_file


@dataclass
class _PreparedLayout:
    change_set: PcbLayoutChangeSet
    workspace: Path
    snapshot: Path | None = None


def _editor_lock_exists(root: Path) -> bool:
    return any(root.glob("*.lck")) or any(root.glob(".*.lck"))


def _atomic_copy(source: Path, destination: Path) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class PcbLayoutService:
    """Prepare a complete unrouted PCB skeleton without GUI automation."""

    def __init__(self, projects: ProjectService, data_dir: Path) -> None:
        self.projects = projects
        self.data_dir = data_dir
        self.layout_adapter = PcbLayoutAdapter()
        self.pcb_adapter = PcbFileAdapter()
        self.schematic_adapter = SchematicApiAdapter()
        self._changes: dict[str, _PreparedLayout] = {}

    @staticmethod
    def _current_hashes(session: ProjectSession) -> dict[str, str]:
        return {
            relative: hash_file(session.root / relative)
            for relative in session.hashes
            if (session.root / relative).is_file()
        }

    @staticmethod
    def _new_errors(before: object, after: object) -> Counter[tuple[str | None, str]]:
        baseline = Counter(
            (item.code, item.message)
            for item in before.violations  # type: ignore[attr-defined]
            if item.severity == "error"
        )
        generated = Counter(
            (item.code, item.message)
            for item in after.violations  # type: ignore[attr-defined]
            if item.severity == "error"
        )
        return generated - baseline

    def _validate_workspace(
        self, session: ProjectSession, schematic: Path, pcb: Path
    ) -> tuple[ValidationReport, ErcReport, DrcReport, PlacementAnalysis]:
        parser = self.schematic_adapter.validate(schematic)
        pcb_parser = self.pcb_adapter.validate(pcb)
        erc_before, erc_after = (
            run_erc(detect_kicad().selected_cli, session.schematic_files[0]),
            run_erc(detect_kicad().selected_cli, schematic),
        )
        drc_before, drc_after = (
            run_drc(detect_kicad().selected_cli, session.pcb_file),
            run_drc(detect_kicad().selected_cli, pcb),
        )
        erc_new = self._new_errors(erc_before, erc_after)
        drc_new = self._new_errors(drc_before, drc_after)
        analysis = PcbDesignService._analyze_summary(self.pcb_adapter.summary(pcb, session.id))
        messages = [*parser.messages, *pcb_parser.messages]
        messages.extend(
            f"New ERC error: {code or 'unknown'}: {message} (x{count})"
            for (code, message), count in erc_new.items()
        )
        messages.extend(
            f"New DRC error: {code or 'unknown'}: {message} (x{count})"
            for (code, message), count in drc_new.items()
        )
        messages.extend(item.message for item in analysis.issues)
        checks = {
            **parser.checks,
            **{f"pcb_{key}": value for key, value in pcb_parser.checks.items()},
            "erc_available": erc_after.available and erc_after.error is None,
            "erc_no_new_errors": not erc_new,
            "drc_available": drc_after.available and drc_after.error is None,
            "drc_no_new_errors": not drc_new,
            "placement_score_100": analysis.score == 100,
        }
        return (
            ValidationReport(valid=all(checks.values()), checks=checks, messages=tuple(messages)),
            erc_after,
            drc_after,
            analysis,
        )

    def prepare(self, session_id: str, plan: PcbLayoutPlan) -> PcbLayoutChangeSet:
        session = self.projects.get_session(session_id)
        if session.pcb_file is None:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Project contains no PCB file")
        current = self._current_hashes(session)
        if current != session.hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project changed after the session was opened",
                actionable_hint="Open the project again before preparing the layout.",
            )
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(session.root, workspace, ignore=PROJECT_COPY_IGNORE)
        relative_schematic = session.schematic_files[0].relative_to(session.root)
        relative_pcb = session.pcb_file.relative_to(session.root)
        schematic, pcb = workspace / relative_schematic, workspace / relative_pcb
        source_rule_file = session.root / f"{session.project_file.stem}.kicad_dru"
        rule_file_changed = False
        if source_rule_file.is_file():
            relative_rule_file = source_rule_file.relative_to(session.root)
            rule_file_changed = migrate_managed_pair_rules(workspace / relative_rule_file)
        if plan.footprint_overrides:
            operations = tuple(
                ChangeOperation(
                    kind="update_property",
                    target=reference,
                    parameters={"name": "Footprint", "value": footprint, "hidden": True},
                )
                for reference, footprint in sorted(plan.footprint_overrides.items())
            )
            self.schematic_adapter.apply(schematic, operations)
        cli = detect_kicad().selected_cli
        upgrade_pcb(cli, pcb)
        components, nets = export_netlist(cli, schematic)  # type: ignore[arg-type]
        self.layout_adapter.compose(pcb, workspace, components, nets, plan)
        upgrade_pcb(cli, pcb)
        validation, erc, drc, analysis = self._validate_workspace(session, schematic, pcb)
        pdf = export_pcb_pdf(cli, pcb, workspace / "Copperbrain-PCB-layout-preview.pdf")
        preview_directory = publish_preview(workspace, session.root, identifier)
        preview_pdf = preview_directory / pdf.relative_to(workspace)
        affected: tuple[Path, ...] = (session.pcb_file,)
        if plan.footprint_overrides:
            affected = (session.schematic_files[0], session.pcb_file)
        if rule_file_changed:
            affected = (*affected, source_rule_file)
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        change_set = PcbLayoutChangeSet(
            id=identifier,
            session_id=session.id,
            project_hash=aggregate_hash(current),
            plan=plan,
            affected_files=affected,
            source_hashes=current,
            semantic_diff=(
                f"initialize rectangular PCB {plan.outline.width_mm:g} x "
                f"{plan.outline.height_mm:g} mm",
                f"place {len(plan.placements)} schematic footprints",
                f"add {len(plan.mounting_holes)} mounting holes",
                *(
                    ("scope managed pair rules to different parent footprints",)
                    if rule_file_changed
                    else ()
                ),
                *(
                    f"override {ref} footprint: {fp}"
                    for ref, fp in plan.footprint_overrides.items()
                ),
            ),
            risks=(
                "The generated PCB is intentionally unrouted",
                "Placement validation does not prove thermal, EMC, SI, PI, or mechanical fitness",
                "Routing, copper zones, and keepouts remain outside this change set",
            ),
            validation_report=validation,
            erc=erc,
            drc=drc,
            placement_analysis=analysis,
            preview_directory=preview_directory,
            preview_pdf=preview_pdf,
            status=status,
        )
        self._changes[identifier] = _PreparedLayout(change_set, workspace)
        return change_set

    def _get(self, change_set_id: str) -> _PreparedLayout:
        try:
            return self._changes[change_set_id]
        except KeyError as exc:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "PCB layout change set was not found"
            ) from exc

    def validate(
        self, change_set_id: str
    ) -> tuple[ValidationReport, ErcReport, DrcReport, PlacementAnalysis]:
        prepared = self._get(change_set_id)
        session = self.projects.get_session(prepared.change_set.session_id)
        schematic = prepared.workspace / session.schematic_files[0].relative_to(session.root)
        assert session.pcb_file is not None
        pcb = prepared.workspace / session.pcb_file.relative_to(session.root)
        return self._validate_workspace(session, schematic, pcb)

    def apply(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbLayoutChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        prepared = self._get(change_set_id)
        change_set = prepared.change_set
        if change_set.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "PCB layout is not validated")
        session = self.projects.get_session(change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE, "KiCad editor state is not safely closed"
            )
        if self._current_hashes(session) != change_set.source_hashes:
            prepared.change_set = change_set.model_copy(update={"status": ChangeStatus.STALE})
            raise CopperbrainError(ErrorCode.CONFLICT, "PCB layout change set is stale")
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
    ) -> PcbLayoutChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Rollback confirmation is required"
            )
        prepared = self._get(change_set_id)
        if prepared.change_set.status is not ChangeStatus.APPLIED or prepared.snapshot is None:
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Only an applied PCB layout can be rolled back"
            )
        session = self.projects.get_session(prepared.change_set.session_id)
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
