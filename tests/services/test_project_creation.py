import shutil
from pathlib import Path

import pytest

from copperbrain.errors import CopperbrainError
from copperbrain.models import ChangeStatus, ProjectCreationSpec, ValidationReport
from copperbrain.services.project_creation import ProjectCreationService


class FixtureScaffold:
    def __init__(self, fixture: Path) -> None:
        self.fixture = fixture

    def create(self, destination: Path, spec: ProjectCreationSpec) -> tuple[Path, Path, Path]:
        destination.mkdir(parents=True)
        project = destination / f"{spec.name}.kicad_pro"
        schematic = destination / f"{spec.name}.kicad_sch"
        pcb = destination / f"{spec.name}.kicad_pcb"
        shutil.copy2(self.fixture / "demo.kicad_pro", project)
        shutil.copy2(self.fixture / "demo.kicad_sch", schematic)
        pcb.write_text("(kicad_pcb (version 20240108) (generator pcbnew))\n", encoding="utf-8")
        return project, schematic, pcb


def test_project_creation_requires_confirmation_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path("tests/fixtures/kicad10_minimal")
    service = ProjectCreationService(tmp_path / "data", FixtureScaffold(fixture))  # type: ignore[arg-type]
    monkeypatch.setattr(
        service,
        "_validate",
        lambda workspace, spec: ValidationReport(valid=True, checks={"fixture": True}),
    )
    change = service.prepare(tmp_path, ProjectCreationSpec(name="bench"))

    assert change.status is ChangeStatus.VALIDATED
    assert change.preview_directory.is_dir()
    with pytest.raises(CopperbrainError, match="confirmation"):
        service.apply(change.id, confirmed=False)

    applied = service.apply(change.id, confirmed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert all(path.is_file() for path in applied.affected_files)

    resumed = ProjectCreationService(tmp_path / "data", FixtureScaffold(fixture))  # type: ignore[arg-type]
    monkeypatch.setattr(
        resumed,
        "_validate",
        lambda workspace, spec: ValidationReport(valid=True, checks={"fixture": True}),
    )

    rolled_back = resumed.rollback(change.id, confirmed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert not any(path.exists() for path in rolled_back.affected_files)


def test_project_creation_refuses_nonempty_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "bench"
    target.mkdir()
    (target / "keep.txt").write_text("user", encoding="utf-8")
    fixture = Path("tests/fixtures/kicad10_minimal")
    service = ProjectCreationService(tmp_path / "data", FixtureScaffold(fixture))  # type: ignore[arg-type]
    monkeypatch.setattr(
        service,
        "_validate",
        lambda workspace, spec: ValidationReport(valid=True, checks={"fixture": True}),
    )

    with pytest.raises(CopperbrainError, match="not empty"):
        service.prepare(tmp_path, ProjectCreationSpec(name="bench"))
