import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    ErcReport,
    PcbLayoutPlan,
    PlacementOperation,
    RectangularBoardOutline,
    ValidationReport,
)
from copperbrain.services import pcb_layout
from copperbrain.services.pcb_layout import PcbLayoutService
from copperbrain.services.projects import ProjectService

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


def test_prepare_apply_and_rollback_headless_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "demo.kicad_pro").write_text(json.dumps({"meta": {"version": 1}}), encoding="utf-8")
    (root / "demo.kicad_sch").write_text("schematic", encoding="utf-8")
    board = root / "demo.kicad_pcb"
    board.write_text(
        '(kicad_pcb (version 20240108) (generator pcbnew) (layers (0 "F.Cu" signal)))',
        encoding="utf-8",
    )
    rules = root / "demo.kicad_dru"
    rules.write_text(
        """(version 1)
# BEGIN COPPERBRAIN MANAGED RULES
(rule "Copperbrain_HV"
  (condition "A.hasNetclass('HV')")
  (constraint clearance (min 0.6mm))
)
# END COPPERBRAIN MANAGED RULES
""",
        encoding="utf-8",
    )
    projects = ProjectService()
    session = projects.open_project(root)
    service = PcbLayoutService(projects, tmp_path / "data")

    monkeypatch.setattr(
        pcb_layout,
        "detect_kicad",
        lambda: SimpleNamespace(selected_cli=Path("kicad-cli")),
    )
    monkeypatch.setattr(pcb_layout, "upgrade_pcb", lambda cli, path: None)
    monkeypatch.setattr(pcb_layout, "export_netlist", lambda cli, path: ((), ()))
    monkeypatch.setattr(pcb_layout, "run_erc", lambda cli, path: ErcReport(available=True))
    monkeypatch.setattr(pcb_layout, "run_drc", lambda cli, path: DrcReport(available=True))

    def export_pdf(cli: Path, source: Path, destination: Path) -> Path:
        destination.write_bytes(b"%PDF-1.4\n")
        return destination

    monkeypatch.setattr(pcb_layout, "export_pcb_pdf", export_pdf)
    monkeypatch.setattr(
        service.schematic_adapter,
        "validate",
        lambda path: ValidationReport(valid=True, checks={"schematic": True}),
    )
    monkeypatch.setattr(
        service.layout_adapter,
        "compose",
        lambda path, root, components, nets, plan: shutil.copy2(FIXTURE, path),
    )
    original_board = board.read_bytes()
    original_rules = rules.read_bytes()
    plan = PcbLayoutPlan(
        outline=RectangularBoardOutline(width_mm=40, height_mm=30),
        placements=(PlacementOperation(reference="R1", x_mm=10, y_mm=10),),
    )

    change = service.prepare(session.id, plan)

    assert change.status is ChangeStatus.VALIDATED
    assert change.validation_report.valid
    assert change.preview_pdf is not None and change.preview_pdf.is_file()
    assert board.read_bytes() == original_board
    assert rules.read_bytes() == original_rules
    assert rules in change.affected_files
    assert service.validate(change.id)[0].valid

    applied = service.apply(change.id, confirmed=True, editor_closed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert board.read_bytes() != original_board
    assert b"A.Parent != B.Parent" in rules.read_bytes()

    rolled_back = service.rollback(change.id, confirmed=True, editor_closed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert board.read_bytes() == original_board
    assert rules.read_bytes() == original_rules
