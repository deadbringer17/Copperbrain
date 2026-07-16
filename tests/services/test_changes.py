import json
from pathlib import Path

import pytest

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeOperation,
    ChangeStatus,
    ErcReport,
    ErcViolation,
    ValidationReport,
)
from copperbrain.services.changes import ChangeService
from copperbrain.services.projects import ProjectService


class FakeAdapter:
    def apply(self, schematic_path: Path, operations: tuple[ChangeOperation, ...]) -> None:
        schematic_path.write_text("changed", encoding="utf-8")

    def validate(self, schematic_path: Path) -> ValidationReport:
        return ValidationReport(
            valid=schematic_path.read_text(encoding="utf-8") == "changed", checks={"parse": True}
        )


def export_pdf(schematic: Path, destination: Path) -> Path:
    destination.write_bytes(b"preview-pdf")
    return destination


def setup(tmp_path: Path) -> tuple[ChangeService, Path, str]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "demo.kicad_pro").write_text(json.dumps({"meta": {"version": 1}}), encoding="utf-8")
    schematic = project / "demo.kicad_sch"
    schematic.write_text("original", encoding="utf-8")
    projects = ProjectService()
    session = projects.open_project(project)
    service = ChangeService(
        projects,
        tmp_path / "data",
        FakeAdapter(),  # type: ignore[arg-type]
        lambda path: ErcReport(available=True),
        export_pdf,
    )
    return service, schematic, session.id


def operation() -> ChangeOperation:
    return ChangeOperation(
        kind="update_property", target="R1", parameters={"name": "LCSC", "value": "C1"}
    )


def test_prepare_does_not_touch_source_and_validates(tmp_path: Path) -> None:
    service, schematic, session = setup(tmp_path)
    change = service.prepare(session, (operation(),))
    assert change.status is ChangeStatus.VALIDATED
    assert schematic.read_text(encoding="utf-8") == "original"
    assert change.preview_directory.parent.parent.name == "copperbrain-output"
    assert (change.preview_directory / "demo.kicad_sch").read_text(encoding="utf-8") == "changed"
    assert (change.preview_directory / "Copperbrain-preview.pdf").is_file()
    assert service.validate(change.id).valid


def test_apply_requires_confirmation_and_closed_editor(tmp_path: Path) -> None:
    service, _, session = setup(tmp_path)
    change = service.prepare(session, (operation(),))
    with pytest.raises(CopperbrainError, match="confirmation"):
        service.apply(change.id, confirmed=False, editor_closed=True)
    with pytest.raises(CopperbrainError, match="not safely closed"):
        service.apply(change.id, confirmed=True, editor_closed=False)


def test_apply_and_rollback_are_byte_exact(tmp_path: Path) -> None:
    service, schematic, session = setup(tmp_path)
    original = schematic.read_bytes()
    change = service.prepare(session, (operation(),))
    applied = service.apply(change.id, confirmed=True, editor_closed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert schematic.read_text(encoding="utf-8") == "changed"
    rolled_back = service.rollback(change.id, confirmed=True, editor_closed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert schematic.read_bytes() == original


def test_apply_refuses_stale_change(tmp_path: Path) -> None:
    service, schematic, session = setup(tmp_path)
    change = service.prepare(session, (operation(),))
    schematic.write_text("external", encoding="utf-8")
    with pytest.raises(CopperbrainError, match="stale"):
        service.apply(change.id, confirmed=True, editor_closed=True)


def test_prepare_rejects_empty_operations(tmp_path: Path) -> None:
    service, _, session = setup(tmp_path)
    with pytest.raises(CopperbrainError, match="At least one"):
        service.prepare(session, ())


def test_prepare_rejects_new_erc_errors(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "demo.kicad_pro").write_text("{}", encoding="utf-8")
    schematic = project / "demo.kicad_sch"
    schematic.write_text("original", encoding="utf-8")
    projects = ProjectService()
    session = projects.open_project(project)

    def erc(path: Path) -> ErcReport:
        violations = ()
        if path.read_text(encoding="utf-8") == "changed":
            violations = (ErcViolation(severity="error", code="new_error", message="regression"),)
        return ErcReport(available=True, violations=violations)

    service = ChangeService(
        projects,
        tmp_path / "data",
        FakeAdapter(),  # type: ignore[arg-type]
        erc,
        export_pdf,
    )
    change = service.prepare(session.id, (operation(),))
    assert change.status is ChangeStatus.PREPARED
    assert not change.validation_report.checks["erc_no_new_errors"]
    with pytest.raises(CopperbrainError, match="not validated"):
        service.apply(change.id, confirmed=True, editor_closed=True)


def test_prepare_rejects_new_multiple_net_name_warning(tmp_path: Path) -> None:
    service, _, session = setup(tmp_path)

    def erc(path: Path) -> ErcReport:
        violations = ()
        if path.read_text(encoding="utf-8") == "changed":
            violations = (
                ErcViolation(
                    severity="warning",
                    code="multiple_net_names",
                    message="two labels share one electrical item",
                ),
            )
        return ErcReport(available=True, violations=violations)

    service.erc_runner = erc
    change = service.prepare(session, (operation(),))

    assert change.status is ChangeStatus.PREPARED
    assert not change.validation_report.checks["erc_no_new_errors"]
    assert any("multiple_net_names" in message for message in change.validation_report.messages)
