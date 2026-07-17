from pathlib import Path

from copperbrain.models import (
    IntegrationStatus,
    PcbBounds,
    PcbFootprintPlacement,
    PcbPadInspection,
    PcbSummary,
    PlacementRequest,
    RouteSegment,
    RouteVia,
)
from copperbrain.services.placement_optimizer import (
    _is_power_net,
    copper_anchored_references,
    copper_conflicting_references,
    optimize_placement,
)


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


def test_routing_coherent_strategy_keeps_high_fanout_power_cluster_relevant() -> None:
    board = PcbBounds(min_x_mm=0, min_y_mm=0, max_x_mm=50, max_y_mm=20)
    power = tuple(_footprint(f"Q{index}", 2 + index * 3, 10, 2, 2) for index in range(4))
    summary = PcbSummary(
        session_id="test",
        pcb_file=Path("board.kicad_pcb"),
        board_bounds=board,
        footprints=(*power, _footprint("R1", 46, 10, 2, 2), _footprint("U1", 25, 10, 2, 2)),
        ipc=IntegrationStatus(name="KiCad PCB IPC", available=False),
    )
    pads = (
        *(
            PcbPadInspection(
                reference=f"Q{index}",
                number="1",
                net="/VMOTOR",
                x_mm=2 + index * 3,
                y_mm=10,
                width_mm=0.5,
                height_mm=0.5,
                layers=("F.Cu",),
            )
            for index in range(4)
        ),
        PcbPadInspection(
            reference="U1",
            number="1",
            net="/VMOTOR",
            x_mm=25,
            y_mm=10,
            width_mm=0.5,
            height_mm=0.5,
            layers=("F.Cu",),
        ),
        PcbPadInspection(
            reference="U1",
            number="2",
            net="/SIG",
            x_mm=25,
            y_mm=10,
            width_mm=0.5,
            height_mm=0.5,
            layers=("F.Cu",),
        ),
        PcbPadInspection(
            reference="R1",
            number="1",
            net="/SIG",
            x_mm=46,
            y_mm=10,
            width_mm=0.5,
            height_mm=0.5,
            layers=("F.Cu",),
        ),
    )
    common = {
        "references": ("U1",),
        "region": board,
        "spacing_mm": 0.5,
        "routing_corridor_mm": 0.5,
        "power_corridor_mm": 2,
        "grid_mm": 0.5,
        "rotation_policy": "fixed",
        "layer_policy": "preserve",
    }
    compact = optimize_placement(summary, pads, PlacementRequest(strategy="compact", **common))[0]
    coherent = optimize_placement(
        summary, pads, PlacementRequest(strategy="routing_coherent", **common)
    )[0]

    power_center_x = sum(item.x_mm for item in power) / len(power)
    assert abs(coherent.x_mm - power_center_x) < abs(compact.x_mm - power_center_x)


def test_routing_coherent_strategy_seeds_first_hub_at_board_center() -> None:
    board = PcbBounds(min_x_mm=0, min_y_mm=0, max_x_mm=40, max_y_mm=20)
    summary = PcbSummary(
        session_id="test",
        pcb_file=Path("board.kicad_pcb"),
        board_bounds=board,
        footprints=(
            _footprint("H1", 2, 2, 2, 2, mount_type="through_hole"),
            _footprint("H2", 38, 18, 2, 2, mount_type="through_hole"),
            _footprint("U1", 5, 5, 4, 4),
        ),
        ipc=IntegrationStatus(name="KiCad PCB IPC", available=False),
    )

    operation = optimize_placement(
        summary,
        (),
        PlacementRequest(
            references=("U1",),
            strategy="routing_coherent",
            region=board,
            spacing_mm=0.5,
            grid_mm=0.5,
            rotation_policy="fixed",
            layer_policy="preserve",
        ),
    )[0]

    assert (operation.x_mm, operation.y_mm) == (20, 10)


def test_routing_coherent_strategy_reserves_global_power_corridors() -> None:
    board = PcbBounds(min_x_mm=0, min_y_mm=0, max_x_mm=40, max_y_mm=30)
    summary = PcbSummary(
        session_id="test",
        pcb_file=Path("board.kicad_pcb"),
        board_bounds=board,
        footprints=(
            _footprint("Q1", 10, 15, 2, 2),
            _footprint("RSH1", 30, 15, 2, 2),
            _footprint("U1", 20, 15, 4, 4),
        ),
        ipc=IntegrationStatus(name="KiCad PCB IPC", available=False),
    )
    pads = (
        PcbPadInspection(
            reference="Q1",
            number="1",
            net="/SHUNT_POWER",
            x_mm=10,
            y_mm=15,
            width_mm=1,
            height_mm=1,
            layers=("F.Cu",),
        ),
        PcbPadInspection(
            reference="RSH1",
            number="1",
            net="/SHUNT_POWER",
            x_mm=30,
            y_mm=15,
            width_mm=1,
            height_mm=1,
            layers=("F.Cu",),
        ),
    )

    operation = optimize_placement(
        summary,
        pads,
        PlacementRequest(
            references=("U1",),
            strategy="routing_coherent",
            region=board,
            spacing_mm=0.5,
            routing_corridor_mm=0.5,
            power_corridor_mm=6,
            grid_mm=0.5,
            rotation_policy="fixed",
            layer_policy="preserve",
        ),
    )[0]

    assert abs(operation.y_mm - 15) >= 5


def test_power_net_classification_excludes_motor_control_signals() -> None:
    assert _is_power_net("/MOTOR_A")
    assert _is_power_net("/SHUNT_POWER")
    assert not _is_power_net("/MOTOR_PWM")
    assert not _is_power_net("/MOTOR_DIR")
    assert not _is_power_net("/SHUNT_P_SENSE")


def test_existing_copper_anchor_detection_is_net_and_layer_aware() -> None:
    pads = (
        PcbPadInspection(
            reference="U1",
            number="1",
            net="/A",
            x_mm=5,
            y_mm=5,
            width_mm=1,
            height_mm=1,
            layers=("F.Cu",),
        ),
        PcbPadInspection(
            reference="U2",
            number="1",
            net="/B",
            x_mm=6.4,
            y_mm=5,
            width_mm=1,
            height_mm=1,
            layers=("F.Cu",),
        ),
    )
    segments = (
        RouteSegment(
            net="/A",
            start_x_mm=5,
            start_y_mm=5,
            end_x_mm=6,
            end_y_mm=5,
            width_mm=0.2,
            layer="F.Cu",
        ),
    )
    vias = (RouteVia(net="/B", x_mm=20, y_mm=20, diameter_mm=0.6, drill_mm=0.3),)

    assert copper_anchored_references(pads, segments, vias) == ("U1",)
    assert copper_anchored_references(pads, segments, vias, ignored_nets=frozenset({"/A"})) == ()
    assert copper_conflicting_references(pads, segments, vias) == ("U2",)
