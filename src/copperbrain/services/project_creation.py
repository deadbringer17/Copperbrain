"""Prepare, validate, confirm, apply, and roll back empty KiCad projects."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from copperbrain.adapters.kicad_cli import run_drc, run_erc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.project_scaffold import ProjectScaffoldAdapter
from copperbrain.adapters.schematic_api import SchematicApiAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    ErrorCode,
    ProjectCreationChangeSet,
    ProjectCreationRecord,
    ProjectCreationSpec,
    ValidationReport,
)
from copperbrain.services.outputs import (
    OUTPUT_DIRECTORY,
    publish_preview,
    require_current_preview,
    require_source_project_root,
)
from copperbrain.services.projects import hash_file


@dataclass
class _PreparedCreation:
    change_set: ProjectCreationChangeSet
    workspace: Path


def _atomic_copy(source: Path, destination: Path) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ProjectCreationService:
    """Create a new empty project only through a reviewable change set."""

    def __init__(
        self,
        data_dir: Path,
        adapter: ProjectScaffoldAdapter | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.adapter = adapter or ProjectScaffoldAdapter()
        self.schematic_adapter = SchematicApiAdapter()
        self.pcb_adapter = PcbFileAdapter()
        self._changes: dict[str, _PreparedCreation] = {}

    @property
    def _manifest_directory(self) -> Path:
        return self.data_dir / "project-creations"

    def _manifest_path(self, identifier: str) -> Path:
        return self._manifest_directory / f"{identifier}.json"

    def _persist(self, prepared: _PreparedCreation) -> None:
        self._manifest_directory.mkdir(parents=True, exist_ok=True)
        record = ProjectCreationRecord(
            workspace=prepared.workspace,
            change_set=prepared.change_set,
        )
        path = self._manifest_path(prepared.change_set.id)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.close(descriptor)
        try:
            Path(temporary).write_text(record.model_dump_json(indent=2), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _load(self, change_set_id: str) -> _PreparedCreation:
        path = self._manifest_path(change_set_id)
        if not path.is_file():
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Project creation change set was not found")
        try:
            record = ProjectCreationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Project creation manifest is invalid",
                details={"reason": str(exc)},
            ) from exc
        workspace = record.workspace.resolve()
        private_root = (self.data_dir / "workspaces").resolve()
        if (
            record.change_set.id != change_set_id
            or not workspace.is_relative_to(private_root)
            or not workspace.is_dir()
        ):
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Project creation manifest escapes private storage"
            )
        prepared = _PreparedCreation(record.change_set, workspace)
        self._changes[change_set_id] = prepared
        return prepared

    @staticmethod
    def _ensure_available_target(target_root: Path) -> None:
        if not target_root.exists():
            return
        if not target_root.is_dir():
            raise CopperbrainError(ErrorCode.CONFLICT, "Project target is not a directory")
        unexpected = sorted(
            str(item) for item in target_root.iterdir() if item.name != OUTPUT_DIRECTORY
        )
        if unexpected:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project target directory is not empty",
                details={"entries": unexpected},
            )

    def _validate(self, workspace: Path, spec: ProjectCreationSpec) -> ValidationReport:
        schematic = workspace / f"{spec.name}.kicad_sch"
        pcb = workspace / f"{spec.name}.kicad_pcb"
        project = workspace / f"{spec.name}.kicad_pro"
        schematic_report = self.schematic_adapter.validate(schematic)
        pcb_report = self.pcb_adapter.validate(pcb)
        cli = detect_kicad().selected_cli
        erc = run_erc(cli, schematic)
        drc = run_drc(cli, pcb)
        checks = {
            "project_file": project.is_file(),
            **schematic_report.checks,
            **pcb_report.checks,
            "erc_available": erc.available and erc.error is None,
            "drc_available": drc.available and drc.error is None,
        }
        return ValidationReport(
            valid=all(checks.values()),
            checks=checks,
            messages=(*schematic_report.messages, *pcb_report.messages),
            erc=erc,
        )

    def prepare(self, parent: Path, spec: ProjectCreationSpec) -> ProjectCreationChangeSet:
        parent = parent.expanduser().resolve()
        if not parent.is_dir():
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Project parent directory was not found")
        target_root = require_source_project_root(parent / spec.name)
        self._ensure_available_target(target_root)
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self.adapter.create(workspace, spec)
        validation = self._validate(workspace, spec)
        preview_directory = publish_preview(workspace, target_root, identifier, phase="schematic")
        affected = tuple(
            target_root / f"{spec.name}{suffix}"
            for suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb")
        )
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        change_set = ProjectCreationChangeSet(
            id=identifier,
            spec=spec,
            target_root=target_root,
            affected_files=affected,
            semantic_diff=(
                f"create empty KiCad project {spec.name}",
                f"initialize {spec.copper_layers}-layer empty PCB",
            ),
            risks=(
                "The project contains no circuit until a separate confirmed schematic change",
                "The PCB has no outline, footprints, copper, zones, or routing",
            ),
            validation_report=validation,
            preview_directory=preview_directory,
            status=status,
        )
        self._changes[identifier] = _PreparedCreation(change_set, workspace)
        self._persist(self._changes[identifier])
        return change_set

    def _get(self, change_set_id: str) -> _PreparedCreation:
        return self._changes.get(change_set_id) or self._load(change_set_id)

    def validate(self, change_set_id: str) -> ValidationReport:
        prepared = self._get(change_set_id)
        return self._validate(prepared.workspace, prepared.change_set.spec)

    def apply(self, change_set_id: str, *, confirmed: bool) -> ProjectCreationChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        prepared = self._get(change_set_id)
        change_set = prepared.change_set
        if change_set.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED, "Project creation change set is not validated"
            )
        require_current_preview(change_set.preview_directory, change_set.id)
        self._ensure_available_target(change_set.target_root)
        change_set.target_root.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        try:
            for destination in change_set.affected_files:
                _atomic_copy(prepared.workspace / destination.name, destination)
                copied.append(destination)
        except Exception:
            for destination in copied:
                destination.unlink(missing_ok=True)
            raise
        hashes = {item.name: hash_file(item) for item in change_set.affected_files}
        prepared.change_set = change_set.model_copy(
            update={"status": ChangeStatus.APPLIED, "applied_hashes": hashes}
        )
        self._persist(prepared)
        return prepared.change_set

    def rollback(self, change_set_id: str, *, confirmed: bool) -> ProjectCreationChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Rollback confirmation is required"
            )
        prepared = self._get(change_set_id)
        change_set = prepared.change_set
        if change_set.status is not ChangeStatus.APPLIED:
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Only an applied project creation can be rolled back"
            )
        current = {
            item.name: hash_file(item) for item in change_set.affected_files if item.is_file()
        }
        if current != change_set.applied_hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Created project files changed after apply",
                actionable_hint="Preserve or review the changed files before retrying rollback.",
            )
        for item in change_set.affected_files:
            item.unlink(missing_ok=True)
        prepared.change_set = change_set.model_copy(update={"status": ChangeStatus.ROLLED_BACK})
        self._persist(prepared)
        return prepared.change_set
