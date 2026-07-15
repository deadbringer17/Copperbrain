import json
import shutil
from pathlib import Path

import pytest

from copperbrain.adapters.pcb_design import KiCadPcbIpcAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    IntegrationStatus,
    PcbBounds,
    PlacementOperation,
    PlacementRequest,
)
from copperbrain.services.pcb_design import PcbDesignService
from copperbrain.services.projects import ProjectService

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PcbDesignService, Path, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        KiCadPcbIpcAdapter,
        "status",
        staticmethod(lambda: IntegrationStatus(name="KiCad PCB IPC", available=False)),
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "placement.kicad_pro").write_text(
        json.dumps({"meta": {"version": 1}}), encoding="utf-8"
    )
    (root / "placement.kicad_sch").write_text("fixture", encoding="utf-8")
    pcb = root / "placement.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    projects = ProjectService()
    session = projects.open_project(root)

    def export_pdf(source: Path, destination: Path) -> Path:
        assert source.is_file()
        destination.write_bytes(b"%PDF-1.4\n% placement preview\n")
        return destination

    service = PcbDesignService(
        projects,
        tmp_path / "data",
        drc_runner=lambda path: DrcReport(available=True),
        pdf_exporter=export_pdf,
    )
    return service, pcb, session.id


def test_queries_analysis_and_deterministic_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session = setup(tmp_path, monkeypatch)
    assert service.summary(session).footprints[0].reference == "C1"
    assert service.inspect_net(session, "GND").routed_length_mm == 10
    assert service.footprint(session, "R1").x_mm == 10
    assert service.analyze_placement(session).score == 100
    proposal = service.propose(
        session,
        PlacementRequest(
            references=("R1", "C1"),
            region=PcbBounds(min_x_mm=1, min_y_mm=1, max_x_mm=12, max_y_mm=12),
            spacing_mm=1,
            grid_mm=0.5,
        ),
    )
    assert [item.reference for item in proposal.operations] == ["R1", "C1"]
    assert proposal.analysis_after.score == 100
    assert proposal.operations == service.propose(session, proposal.request).operations


def test_empty_board_is_not_reported_as_perfect_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    pcb.write_text(
        "(kicad_pcb (version 20240108) (generator pcbnew) "
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal)))',
        encoding="utf-8",
    )
    analysis = service.analyze_placement(session)
    assert analysis.score == 0
    assert {item.kind for item in analysis.issues} == {"empty_board", "missing_outline"}


def test_prepare_apply_and_byte_exact_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    original = pcb.read_bytes()
    operation = PlacementOperation(reference="R1", x_mm=15, y_mm=15, rotation_deg=90)
    change = service.prepare(session, (operation,))
    assert change.status is ChangeStatus.VALIDATED
    assert pcb.read_bytes() == original
    assert change.preview_pdf is not None and change.preview_pdf.is_file()
    preview = service.adapter.summary(change.preview_directory / pcb.name, session)
    moved = next(item for item in preview.footprints if item.reference == "R1")
    assert (moved.x_mm, moved.y_mm, moved.rotation_deg) == (15, 15, 90)
    assert service.validate(change.id)[0].valid
    with pytest.raises(CopperbrainError, match="confirmation"):
        service.apply(change.id, confirmed=False, editor_closed=True)
    applied = service.apply(change.id, confirmed=True, editor_closed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert pcb.read_bytes() != original
    rolled_back = service.rollback(change.id, confirmed=True, editor_closed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert pcb.read_bytes() == original


def test_stale_change_and_read_only_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    change = service.prepare(session, (PlacementOperation(reference="C1", x_mm=25, y_mm=20),))
    pcb.write_text(pcb.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(CopperbrainError, match="stale"):
        service.apply(change.id, confirmed=True, editor_closed=True)

    service, _, session = setup(tmp_path / "second", monkeypatch)
    preview = service.export_preview(session)
    assert preview.is_file()
    assert "copperbrain-output" in preview.parts
