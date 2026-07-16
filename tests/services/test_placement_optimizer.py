from pathlib import Path

from copperbrain.models import (
    IntegrationStatus,
    PcbBounds,
    PcbFootprintPlacement,
    PcbPadInspection,
    PcbSummary,
    PlacementRequest,
)
from copperbrain.services.placement_optimizer import optimize_placement


def _footprint(
    reference: str,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    mount_type: str = "smd",
) -> PcbFootprintPlacement:
    return PcbFootprintPlacement(
        reference=reference,
        footprint=f"Test:{reference}",
        x_mm=x,
        y_mm=y,
        layer="F.Cu",
        mount_type=mount_type,  # type: ignore[arg-type]
        bounds=PcbBounds(
            min_x_mm=x - width / 2,
            min_y_mm=y - height / 2,
            max_x_mm=x + width / 2,
            max_y_mm=y + height / 2,
        ),
    )


def test_auto_side_uses_bottom_for_small_passive_when_front_is_congested() -> None:
    board = PcbBounds(min_x_mm=0, min_y_mm=0, max_x_mm=6, max_y_mm=6)
    summary = PcbSummary(
        session_id="test",
        pcb_file=Path("board.kicad_pcb"),
        board_bounds=board,
        footprints=(
            _footprint("U1", 3, 3, 6, 6),
            _footprint("C1", 5, 5, 2, 2),
        ),
        ipc=IntegrationStatus(name="KiCad PCB IPC", available=False),
    )
    pads = (
        PcbPadInspection(
            reference="U1",
            number="1",
            net="VDD",
            x_mm=3,
            y_mm=3,
            width_mm=1,
            height_mm=1,
            layers=("F.Cu",),
        ),
        PcbPadInspection(
            reference="C1",
            number="1",
            net="VDD",
            x_mm=5,
            y_mm=5,
            width_mm=1,
            height_mm=1,
            layers=("F.Cu",),
        ),
    )
    operation = optimize_placement(
        summary,
        pads,
        PlacementRequest(
            references=("C1",),
            region=board,
            spacing_mm=0.2,
            grid_mm=0.5,
        ),
    )[0]
    assert operation.layer == "B.Cu"
    assert (operation.x_mm, operation.y_mm) == (3, 3)


def test_through_hole_side_is_never_changed_automatically() -> None:
    board = PcbBounds(min_x_mm=0, min_y_mm=0, max_x_mm=20, max_y_mm=20)
    connector = _footprint("J1", 10, 10, 4, 4, mount_type="through_hole")
    summary = PcbSummary(
        session_id="test",
        pcb_file=Path("board.kicad_pcb"),
        board_bounds=board,
        footprints=(connector,),
        ipc=IntegrationStatus(name="KiCad PCB IPC", available=False),
    )
    operation = optimize_placement(
        summary,
        (),
        PlacementRequest(references=("J1",), region=board, layer_policy="back"),
    )[0]
    assert operation.layer == "F.Cu"
