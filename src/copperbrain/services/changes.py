"""Prepare, validate, confirm, atomically apply, and roll back schematic changes."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from copperbrain.adapters.kicad_cli import export_schematic_pdf, run_erc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.schematic_api import SchematicApiAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeOperation,
    ChangeSet,
    ChangeStatus,
    ErcReport,
    ErrorCode,
    ProjectSession,
    ValidationReport,
)
from copperbrain.services.outputs import PROJECT_COPY_IGNORE, publish_preview
from copperbrain.services.projects import ProjectService, aggregate_hash, hash_file


@dataclass
class _PreparedChange:
    change_set: ChangeSet
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


class ChangeService:
    """Stateful application service enforcing the mutation safety workflow."""

    def __init__(
        self,
        projects: ProjectService,
        data_dir: Path,
        adapter: SchematicApiAdapter | None = None,
        erc_runner: Callable[[Path], ErcReport] | None = None,
        pdf_exporter: Callable[[Path, Path], Path] | None = None,
    ) -> None:
        self.projects = projects
        self.data_dir = data_dir
        self.adapter = adapter or SchematicApiAdapter()
        self.erc_runner = erc_runner or (
            lambda schematic: run_erc(detect_kicad().selected_cli, schematic)
        )
        self.pdf_exporter = pdf_exporter or (
            lambda schematic, destination: export_schematic_pdf(
                detect_kicad().selected_cli, schematic, destination
            )
        )
        self._changes: dict[str, _PreparedChange] = {}

    def _validate_workspace(
        self,
        session: ProjectSession,
        temporary_schematic: Path,
    ) -> ValidationReport:
        parser_report = self.adapter.validate(temporary_schematic)
        before = self.erc_runner(session.schematic_files[0])
        after = self.erc_runner(temporary_schematic)
        baseline = Counter(
            (item.code, item.message) for item in before.violations if item.severity == "error"
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if item.severity == "error"
        )
        new_errors = generated - baseline
        erc_available = after.available and after.error is None
        erc_regression_free = not new_errors
        messages = list(parser_report.messages)
        messages.extend(
            f"New ERC error: {code or 'unknown'}: {message} (x{count})"
            for (code, message), count in new_errors.items()
        )
        return ValidationReport(
            valid=parser_report.valid and erc_available and erc_regression_free,
            checks={
                **parser_report.checks,
                "erc_available": erc_available,
                "erc_no_new_errors": erc_regression_free,
            },
            messages=tuple(messages),
            erc=after,
        )

    def _current_hashes(self, session: ProjectSession) -> dict[str, str]:
        return {
            relative: hash_file(session.root / relative)
            for relative in session.hashes
            if (session.root / relative).is_file()
        }

    def prepare(
        self,
        session_id: str,
        operations: tuple[ChangeOperation, ...],
    ) -> ChangeSet:
        """Apply semantic operations only to a private workspace and validate them."""
        if not operations:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "At least one operation is required")
        session = self.projects.get_session(session_id)
        current = self._current_hashes(session)
        if current != session.hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project changed after the session was opened",
                actionable_hint="Open the project again before preparing a change.",
            )
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            session.root,
            workspace,
            ignore=PROJECT_COPY_IGNORE,
        )
        relative_schematic = session.schematic_files[0].relative_to(session.root)
        temporary_schematic = workspace / relative_schematic
        self.adapter.apply(temporary_schematic, operations)
        validation = self._validate_workspace(session, temporary_schematic)
        self.pdf_exporter(temporary_schematic, workspace / "Copperbrain-preview.pdf")
        preview_directory = publish_preview(workspace, session.root, identifier)
        semantic_diff = tuple(
            f"{operation.kind}: {operation.target} ({', '.join(sorted(operation.parameters))})"
            for operation in operations
        )
        risks = (
            "KiCad may overwrite external changes if the editor has unsaved state",
            "ERC and electrical intent must be reviewed before confirmation",
        )
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        change_set = ChangeSet(
            id=identifier,
            session_id=session.id,
            project_hash=aggregate_hash(current),
            operations=operations,
            affected_files=(session.schematic_files[0],),
            source_hashes=current,
            semantic_diff=semantic_diff,
            risks=risks,
            validation_report=validation,
            preview_directory=preview_directory,
            status=status,
        )
        self._changes[identifier] = _PreparedChange(change_set=change_set, workspace=workspace)
        return change_set

    def validate(self, change_set_id: str) -> ValidationReport:
        """Revalidate the prepared temporary schematic, never the live project."""
        prepared = self._get(change_set_id)
        session = self.projects.get_session(prepared.change_set.session_id)
        relative = session.schematic_files[0].relative_to(session.root)
        return self._validate_workspace(session, prepared.workspace / relative)

    def _get(self, change_set_id: str) -> _PreparedChange:
        try:
            return self._changes[change_set_id]
        except KeyError as exc:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Change set was not found") from exc

    def apply(
        self,
        change_set_id: str,
        *,
        confirmed: bool,
        editor_closed: bool,
    ) -> ChangeSet:
        """Apply a validated set after explicit confirmation and fresh hash checks."""
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        prepared = self._get(change_set_id)
        change_set = prepared.change_set
        if change_set.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Change set is not validated")
        session = self.projects.get_session(change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE,
                "KiCad editor state is not safely closed",
                actionable_hint="Save and close the schematic editor, then retry.",
            )
        current = self._current_hashes(session)
        if current != change_set.source_hashes:
            prepared.change_set = change_set.model_copy(update={"status": ChangeStatus.STALE})
            raise CopperbrainError(ErrorCode.CONFLICT, "Change set is stale; project files changed")
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
            update={
                "status": ChangeStatus.APPLIED,
                "snapshot_id": snapshot_id,
            }
        )
        return prepared.change_set

    def rollback(
        self,
        change_set_id: str,
        *,
        confirmed: bool,
        editor_closed: bool,
    ) -> ChangeSet:
        """Restore the exact snapshot after a second explicit confirmation."""
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Rollback confirmation is required"
            )
        prepared = self._get(change_set_id)
        if prepared.change_set.status is not ChangeStatus.APPLIED or prepared.snapshot is None:
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Only an applied change set can be rolled back"
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
