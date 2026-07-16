"""Tests for the standalone PCB design adapter."""

import shutil
from pathlib import Path

import pytest

from copperbrain.adapters.pcb_design import KiCadPcbIpcAdapter, PcbFileAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import IntegrationStatus, PlacementOperation

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


@pytest.fixture(autouse=True)
def no_live_ipc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        KiCadPcbIpcAdapter,
        "status",
        staticmethod(lambda: IntegrationStatus(name="KiCad PCB IPC", available=False)),
    )


def test_summary_and_net_inspection_extract_board_geometry() -> None:
    adapter = PcbFileAdapter()
    summary = adapter.summary(FIXTURE, "session")
    assert summary.board_bounds is not None
    assert summary.board_bounds.max_x_mm == 40
    assert [item.reference for item in summary.footprints] == ["C1", "R1"]
    assert summary.track_count == 1
    assert summary.via_count == 1
    net = adapter.inspect_net(FIXTURE, "session", "GND")
    assert net.connected_references == ("C1", "R1")
    assert net.pad_count == 2
    assert net.track_count == 1
    assert net.via_count == 1
    assert net.routed_length_mm == 10
    with pytest.raises(CopperbrainError, match="not found"):
        adapter.inspect_net(FIXTURE, "session", "MISSING")


def test_kicad_10_name_only_nets_are_resolved(tmp_path: Path) -> None:
    pcb = tmp_path / "name-only-nets.kicad_pcb"
    text = FIXTURE.read_text(encoding="utf-8-sig")
    text = text.replace('  (net 0 "")\n', "").replace('  (net 1 "GND")\n', "")
    text = text.replace('(net 1 "GND")', '(net "GND")').replace("(net 1)", '(net "GND")')
    pcb.write_text(text, encoding="utf-8")

    adapter = PcbFileAdapter()
    summary = adapter.summary(pcb, "session")
    net = adapter.inspect_net(pcb, "session", "GND")

    assert summary.net_count == 1
    assert net.connected_references == ("C1", "R1")
    assert net.track_count == 1
    assert net.via_count == 1


def test_circle_courtyard_contributes_full_radius_to_local_bounds(tmp_path: Path) -> None:
    pcb = tmp_path / "circle-courtyard.kicad_pcb"
    pcb.write_text(
        """(kicad_pcb (version 20240108) (generator pcbnew)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (footprint "Test:Radial" (layer "F.Cu") (at 10 10)
    (property "Reference" "C1" (at 0 0) (layer "F.SilkS"))
    (fp_circle (center 2.5 0) (end 9 0)
      (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))
    (pad "1" thru_hole circle (at 0 0) (size 2 2) (drill 1) (layers "*.Cu" "*.Mask")))
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) (type default))
    (fill none) (layer "Edge.Cuts")))""",
        encoding="utf-8",
    )
    footprint = PcbFileAdapter().summary(pcb, "session").footprints[0]
    assert footprint.local_bounds is not None
    assert footprint.local_bounds.min_x_mm == -4
    assert footprint.local_bounds.max_x_mm == 9
    assert footprint.local_bounds.min_y_mm == -6.5
    assert footprint.local_bounds.max_y_mm == 6.5


def test_ipc_board_path_uses_project_directory(tmp_path: Path) -> None:
    class Project:
        path = str(tmp_path)

    class Board:
        name = "preview.kicad_pcb"

        @staticmethod
        def get_project() -> Project:
            return Project()

    assert (
        KiCadPcbIpcAdapter._open_board_path(Board()) == (tmp_path / "preview.kicad_pcb").resolve()
    )


def test_typed_placement_changes_only_selected_footprint(tmp_path: Path) -> None:
    pcb = tmp_path / "placement.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    adapter = PcbFileAdapter()
    before = adapter.summary(pcb, "session")
    adapter.apply_placement(
        pcb,
        (PlacementOperation(reference="R1", x_mm=12.5, y_mm=14, rotation_deg=90),),
    )
    after = adapter.summary(pcb, "session")
    placements = {item.reference: item for item in after.footprints}
    assert (placements["R1"].x_mm, placements["R1"].y_mm) == (12.5, 14)
    assert placements["R1"].rotation_deg == 90
    original_c1 = next(item for item in before.footprints if item.reference == "C1")
    assert placements["C1"] == original_c1
    assert adapter.validate(pcb).valid
    with pytest.raises(CopperbrainError, match="not found"):
        adapter.apply_placement(
            pcb,
            (PlacementOperation(reference="U99", x_mm=1, y_mm=1),),
        )
    with pytest.raises(CopperbrainError, match="outside the placement extension"):
        adapter.apply_placement(
            pcb,
            (PlacementOperation(reference="R1", x_mm=1, y_mm=1, layer="B.Cu"),),
        )
