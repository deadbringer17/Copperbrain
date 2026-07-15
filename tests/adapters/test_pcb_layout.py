from pathlib import Path

import pytest

from copperbrain.adapters import pcb_layout
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    Component,
    MountingHoleSpec,
    Net,
    NetPin,
    PcbLayoutPlan,
    PlacementOperation,
    RectangularBoardOutline,
)


def _footprint(path: Path) -> None:
    path.write_text(
        """(footprint "Test:Part"
  (version 20240108)
  (generator pcbnew)
  (layer "F.Cu")
  (property "Reference" "REF**" (at 0 -2 0) (layer "F.SilkS"))
  (property "Value" "Part" (at 0 2 0) (layer "F.Fab"))
  (fp_rect (start -1 -1) (end 1 1)
    (stroke (width 0.1) (type default)) (fill none) (layer "F.CrtYd"))
  (pad "1" smd rect (at -0.5 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 0.5 0) (size 0.8 0.8) (layers "F.Cu" "F.Paste" "F.Mask"))
)""",
        encoding="utf-8",
    )


def test_compose_initializes_empty_board_from_typed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "demo.kicad_pcb"
    board.write_text(
        "(kicad_pcb (version 20240108) (generator pcbnew) "
        '(layers (0 "F.Cu" signal) (31 "B.Cu" signal)))',
        encoding="utf-8",
    )
    template = tmp_path / "part.kicad_mod"
    _footprint(template)
    monkeypatch.setattr(pcb_layout, "resolve_footprint", lambda root, library_id: template)
    plan = PcbLayoutPlan(
        outline=RectangularBoardOutline(width_mm=20, height_mm=10),
        placements=(PlacementOperation(reference="R1", x_mm=85, y_mm=85),),
        mounting_holes=(MountingHoleSpec(reference="H1", x_mm=82, y_mm=82),),
    )

    pcb_layout.PcbLayoutAdapter().compose(
        board,
        tmp_path,
        (Component(reference="R1", value="10k", footprint="Test:Part"),),
        (Net(name="/SIG", pins=(NetPin(reference="R1", pin="1"),)),),
        plan,
    )

    summary = PcbFileAdapter().summary(board, "session")
    assert summary.board_bounds is not None
    assert (
        summary.board_bounds.max_x_mm - summary.board_bounds.min_x_mm,
        summary.board_bounds.max_y_mm - summary.board_bounds.min_y_mm,
    ) == (20, 10)
    assert {item.reference for item in summary.footprints} == {"R1", "H1"}
    assert "/SIG" in board.read_text(encoding="utf-8")


def test_compose_refuses_populated_board_and_incomplete_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = tmp_path / "demo.kicad_pcb"
    board.write_text(
        "(kicad_pcb (version 20240108) (generator pcbnew) "
        '(layers (0 "F.Cu" signal)) (segment (start 0 0) (end 1 1) (width 0.2) (layer "F.Cu")))',
        encoding="utf-8",
    )
    plan = PcbLayoutPlan(
        outline=RectangularBoardOutline(width_mm=20, height_mm=10),
        placements=(PlacementOperation(reference="R1", x_mm=85, y_mm=85),),
    )
    adapter = pcb_layout.PcbLayoutAdapter()
    with pytest.raises(CopperbrainError, match="requires an empty board"):
        adapter.compose(board, tmp_path, (), (), plan)

    board.write_text(
        '(kicad_pcb (version 20240108) (generator pcbnew) (layers (0 "F.Cu" signal)))',
        encoding="utf-8",
    )
    with pytest.raises(CopperbrainError, match="every schematic component"):
        adapter.compose(
            board,
            tmp_path,
            (
                Component(reference="R1", value="10k", footprint="Test:Part"),
                Component(reference="C1", value="1u", footprint="Test:Part"),
            ),
            (),
            plan,
        )
