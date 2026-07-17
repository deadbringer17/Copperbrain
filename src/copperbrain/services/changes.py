"""Prepare, validate, confirm, atomically apply, and roll back schematic changes."""

from __future__ import annotations

import json
import os
import re
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
from copperbrain.adapters.schematic_readability import analyze_schematic_readability
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeOperation,
    ChangeSet,
    ChangeStatus,
    ErcReport,
    ErrorCode,
    ProjectSession,
    SchematicChangeRecord,
    SchematicReadabilityReport,
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
        readability_analyzer: Callable[[Path], SchematicReadabilityReport] | None = None,
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
        self.readability_analyzer = readability_analyzer or analyze_schematic_readability
        self._changes: dict[str, _PreparedChange] = {}

    @property
    def _records_dir(self) -> Path:
        return self.data_dir / "schematic-changes"

    def _record_path(self, change_set_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", change_set_id) is None:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT, "Schematic change identifier is invalid"
            )
        return self._records_dir / f"{change_set_id}.json"

    def _persist(self, prepared: _PreparedChange, project_root: Path) -> None:
        """Atomically persist typed state so review and apply survive an MCP restart."""
        change = prepared.change_set
        resolved_root = project_root.resolve()
        record = SchematicChangeRecord(
            project_root=resolved_root,
            workspace=prepared.workspace.resolve(),
            affected_relative_files=tuple(
                path.resolve().relative_to(resolved_root) for path in change.affected_files
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

    def _load(self, change_set_id: str) -> _PreparedChange:
        path = self._record_path(change_set_id)
        try:
            record = SchematicChangeRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "Schematic change set was not found"
            ) from exc
        except (OSError, ValueError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Persisted schematic change set is invalid",
                actionable_hint="Prepare the schematic change again from the source project.",
                details={"reason": str(exc)},
            ) from exc

        workspace = self._require_child(
            record.workspace, self.data_dir / "workspaces", "schematic workspace"
        )
        if not workspace.is_dir():
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Persisted schematic workspace was not found",
                actionable_hint="Prepare the schematic change again from the source project.",
            )
        snapshot = None
        if record.snapshot is not None:
            snapshot = self._require_child(
                record.snapshot, self.data_dir / "snapshots", "schematic snapshot"
            )
            if not snapshot.is_dir():
                raise CopperbrainError(
                    ErrorCode.NOT_FOUND, "Persisted schematic snapshot was not found"
                )

        session = self.projects.open_project(record.project_root)
        affected = tuple(session.root / item for item in record.affected_relative_files)
        if any(not item.is_file() for item in affected):
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "A source file referenced by the schematic change was not found",
            )
        change = record.change_set.model_copy(
            update={"session_id": session.id, "affected_files": affected}
        )
        prepared = _PreparedChange(change_set=change, workspace=workspace, snapshot=snapshot)
        self._changes[change_set_id] = prepared
        return prepared

    def _validate_workspace(
        self,
        session: ProjectSession,
        temporary_schematic: Path,
        *,
        readability_required: bool = False,
    ) -> tuple[ValidationReport, SchematicReadabilityReport]:
        parser_report = self.adapter.validate(temporary_schematic)
        readability = self.readability_analyzer(temporary_schematic)
        before = self.erc_runner(session.schematic_files[0])
        after = self.erc_runner(temporary_schematic)

        def blocking(violation: object) -> bool:
            severity = getattr(violation, "severity", None)
            code = getattr(violation, "code", None)
            return severity == "error" or code == "multiple_net_names"

        baseline = Counter(
            (item.code, item.message) for item in before.violations if blocking(item)
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if blocking(item)
        )
        new_errors = generated - baseline
        erc_available = after.available and after.error is None
        erc_regression_free = not new_errors
        messages = list(parser_report.messages)
        messages.extend(
            f"New blocking ERC violation: {code or 'unknown'}: {message} (x{count})"
            for (code, message), count in new_errors.items()
        )
        readability_ok = readability.valid or not readability_required
        return ValidationReport(
            valid=(
                parser_report.valid and erc_available and erc_regression_free and readability_ok
            ),
            checks={
                **parser_report.checks,
                "erc_available": erc_available,
                "erc_no_new_errors": erc_regression_free,
                "schematic_readability": readability.valid,
                "schematic_readability_required": readability_required,
            },
            messages=tuple(
                [
                    *messages,
                    *(
                        f"Readability: {message}"
                        for message in readability.messages
                        if readability_required
                    ),
                ]
            ),
            erc=after,
        ), readability

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
        readability_required = any(
            operation.kind in {"move_component", "relayout_pin_label"} for operation in operations
        )
        validation, readability = self._validate_workspace(
            session,
            temporary_schematic,
            readability_required=readability_required,
        )
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
            readability_report=readability,
            preview_directory=preview_directory,
            status=status,
        )
        prepared = _PreparedChange(change_set=change_set, workspace=workspace)
        self._changes[identifier] = prepared
        self._persist(prepared, session.root)
        return change_set

    def validate(self, change_set_id: str) -> ValidationReport:
        """Revalidate the prepared temporary schematic, never the live project."""
        prepared = self._get(change_set_id)
        session = self.projects.get_session(prepared.change_set.session_id)
        relative = session.schematic_files[0].relative_to(session.root)
        validation, readability = self._validate_workspace(
            session,
            prepared.workspace / relative,
            readability_required=any(
                operation.kind in {"move_component", "relayout_pin_label"}
                for operation in prepared.change_set.operations
            ),
        )
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        prepared.change_set = prepared.change_set.model_copy(
            update={
                "validation_report": validation,
                "readability_report": readability,
                "status": status,
            }
        )
        self._persist(prepared, session.root)
        return validation

    def _get(self, change_set_id: str) -> _PreparedChange:
        try:
            return self._changes[change_set_id]
        except KeyError:
            return self._load(change_set_id)

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
            self._persist(prepared, session.root)
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
        self._persist(prepared, session.root)
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
        self._persist(prepared, session.root)
        return prepared.change_set
