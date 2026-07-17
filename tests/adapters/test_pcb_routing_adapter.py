"""Routing connectivity and typed copper writer tests."""

import shutil
from pathlib import Path

import pytest

from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import RouteSegment

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


def _unrouted_board(tmp_path: Path) -> Path:
    pcb = tmp_path / "unrouted.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    text = pcb.read_text(encoding="utf-8")
    text = text.replace(
        '  (segment (start 9.2 10) (end 19.2 10) (width 0.25) (layer "F.Cu") (net 1))\n',
        "",
    )
    text = text.replace(
        '  (via (at 14 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n',
        "",
    )
    pcb.write_text(text, encoding="utf-8")
    return pcb


def test_analyze_and_apply_typed_routing(tmp_path: Path) -> None:
    pcb = _unrouted_board(tmp_path)
    adapter = PcbFileAdapter()
    before = adapter.analyze_routing(pcb, "session")
    assert not before.complete
    assert before.unrouted_connection_count == 1
    connection = before.unrouted_connections[0]
    assert (connection.start_reference, connection.end_reference) == ("C1", "R1")

    adapter.apply_routing(
        pcb,
        (
            RouteSegment(
                net="GND",
                start_x_mm=9.2,
                start_y_mm=10,
                end_x_mm=19.2,
                end_y_mm=10,
                width_mm=0.25,
            ),
        ),
        (),
    )
    after = adapter.analyze_routing(pcb, "session")
    assert after.complete
    assert adapter.summary(pcb, "session").track_count == 1
    assert adapter.validate(pcb).valid


def test_existing_fixture_is_already_connected() -> None:
    analysis = PcbFileAdapter().analyze_routing(FIXTURE, "session")
    assert analysis.complete
    assert analysis.routed_net_count == 1


def test_tracks_ending_on_pad_copper_are_connected(tmp_path: Path) -> None:
    pcb = tmp_path / "pad-edge.kicad_pcb"
    content = FIXTURE.read_text(encoding="utf-8")
    content = content.replace("(start 9.2 10) (end 19.2 10)", "(start 9.6 10) (end 18.8 10)")
    pcb.write_text(content, encoding="utf-8")

    analysis = PcbFileAdapter().analyze_routing(pcb, "session")

    assert analysis.complete
    assert analysis.unrouted_connection_count == 0


def test_existing_copper_is_exposed_as_typed_routing_items() -> None:
    segments, vias = PcbFileAdapter().routing_items(FIXTURE)
    assert len(segments) == 1
    assert segments[0].net == "GND"
    assert segments[0].width_mm == 0.25
    assert len(vias) == 1
    assert vias[0].net == "GND"


def test_routing_items_accepts_kicad_10_name_valued_net_fields(tmp_path: Path) -> None:
    pcb = tmp_path / "named-net.kicad_pcb"
    content = FIXTURE.read_text(encoding="utf-8")
    content = content.replace('(layer "F.Cu") (net 1))', '(layer "F.Cu") (net "GND"))')
    content = content.replace(
        '(layers "F.Cu" "B.Cu") (net 1))',
        '(layers "F.Cu" "B.Cu") (net "GND"))',
    )
    pcb.write_text(content, encoding="utf-8")
    segments, vias = PcbFileAdapter().routing_items(pcb)
    assert segments[0].net == "GND"
    assert vias[0].net == "GND"


def test_inner_copper_segments_are_typed_and_disabled_layers_are_rejected(tmp_path: Path) -> None:
    pcb = tmp_path / "four-layer.kicad_pcb"
    content = FIXTURE.read_text(encoding="utf-8")
    content = content.replace(
        '    (0 "F.Cu" signal)\n    (31 "B.Cu" signal)',
        '    (0 "F.Cu" signal)\n'
        '    (2 "In1.Cu" power)\n'
        '    (4 "In2.Cu" signal)\n'
        '    (31 "B.Cu" signal)',
    )
    content = content.replace('(layer "F.Cu") (net 1))', '(layer "In2.Cu") (net 1))')
    pcb.write_text(content, encoding="utf-8")

    segments, _ = PcbFileAdapter().routing_items(pcb)
    assert segments[0].layer == "In2.Cu"

    with pytest.raises(CopperbrainError, match="not enabled"):
        PcbFileAdapter().apply_routing(
            pcb,
            (
                RouteSegment(
                    net="GND",
                    start_x_mm=5,
                    start_y_mm=5,
                    end_x_mm=6,
                    end_y_mm=5,
                    width_mm=0.2,
                    layer="In3.Cu",
                ),
            ),
            (),
        )
