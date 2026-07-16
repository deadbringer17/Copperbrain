import shutil
import subprocess
from pathlib import Path

import pytest

from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.pcb_grounding import KiCadGroundingAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    GroundBridge,
    GroundDomainPlan,
    GroundDomainRequest,
    GroundingPlan,
    GroundingRequest,
    GroundZoneRegion,
    RouteSegment,
    RouteVia,
)

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


def test_kicad_worker_fills_ground_planes_and_connects_layers(tmp_path: Path) -> None:
    try:
        KiCadGroundingAdapter._kicad_python()
    except CopperbrainError:
        pytest.skip("KiCad bundled Python is unavailable")
    pcb = tmp_path / "placement.kicad_pcb"
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
    four_layer = tmp_path / "four-layer.kicad_pcb"
    worker = Path("src/copperbrain/adapters/kicad_project_worker.py").resolve()
    subprocess.run(
        [
            str(KiCadGroundingAdapter._kicad_python()),
            str(worker),
            "set-copper-layers",
            str(pcb),
            str(four_layer),
            "4",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    four_layer.replace(pcb)
    plan = GroundingPlan(
        session_id="test",
        request=GroundingRequest(copper_layers=4, layers=("F.Cu", "In1.Cu", "B.Cu")),
        domains=(
            GroundDomainPlan(
                net_name="GND",
                primary_layer="F.Cu",
                plane_layers=("F.Cu", "In1.Cu", "B.Cu"),
                regions=(
                    GroundZoneRegion(layer="F.Cu", kind="board"),
                    GroundZoneRegion(layer="In1.Cu", kind="board"),
                    GroundZoneRegion(layer="B.Cu", kind="board"),
                ),
                vias=(
                    RouteVia(net="GND", x_mm=5, y_mm=5),
                    RouteVia(net="GND", x_mm=35, y_mm=5),
                ),
                target_pad_count=2,
                target_references=("C1", "R1"),
                planes_connected=True,
            ),
        ),
    )

    KiCadGroundingAdapter().apply(pcb, plan)

    adapter = PcbFileAdapter()
    assert adapter.summary(pcb, "test").zone_count == 3
    assert adapter.copper_layers(pcb) == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    assert adapter.ground_plane_layers(pcb, "GND") == ("B.Cu", "F.Cu", "In1.Cu")
    assert adapter.inspect_net(pcb, "test", "GND").via_count == 2
    assert adapter.analyze_routing(pcb, "test", ("GND",)).complete
    assert "filled_polygon" in pcb.read_text(encoding="utf-8")


def test_kicad_worker_separates_bridge_connected_ground_domains(tmp_path: Path) -> None:
    try:
        KiCadGroundingAdapter._kicad_python()
    except CopperbrainError:
        pytest.skip("KiCad bundled Python is unavailable")
    pcb = tmp_path / "multi-ground.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    text = pcb.read_text(encoding="utf-8")
    text = text.replace('(net 1 "GND")', '(net 1 "GND")\n  (net 2 "PGND")', 1)
    text = text.replace(
        '(pad "2" smd roundrect (at 0.8 0) (size 0.8 0.9) '
        '(layers "F.Cu" "F.Paste" "F.Mask")\n      (roundrect_rratio 0.25))',
        '(pad "2" smd roundrect (at 0.8 0) (size 0.8 0.9) '
        '(layers "F.Cu" "F.Paste" "F.Mask")\n      '
        '(roundrect_rratio 0.25) (net 2 "PGND"))',
        1,
    )
    text = text.replace(
        '  (segment (start 9.2 10) (end 19.2 10) (width 0.25) (layer "F.Cu") (net 1))\n',
        "",
    )
    text = text.replace(
        '  (via (at 14 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n',
        "",
    )
    pcb.write_text(text, encoding="utf-8")
    worker = Path("src/copperbrain/adapters/kicad_project_worker.py").resolve()
    four_layer = tmp_path / "multi-ground-four-layer.kicad_pcb"
    subprocess.run(
        [
            str(KiCadGroundingAdapter._kicad_python()),
            str(worker),
            "set-copper-layers",
            str(pcb),
            str(four_layer),
            "4",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    four_layer.replace(pcb)
    gnd_fanouts = (
        RouteSegment(
            net="GND",
            start_x_mm=9.2,
            start_y_mm=10,
            end_x_mm=9.2,
            end_y_mm=8.5,
            width_mm=0.2,
        ),
        RouteSegment(
            net="GND",
            start_x_mm=19.2,
            start_y_mm=10,
            end_x_mm=19.2,
            end_y_mm=8.5,
            width_mm=0.2,
        ),
    )
    gnd_vias = (
        RouteVia(net="GND", x_mm=9.2, y_mm=8.5),
        RouteVia(net="GND", x_mm=19.2, y_mm=8.5),
    )
    plan = GroundingPlan(
        session_id="test",
        request=GroundingRequest(
            domains=(
                GroundDomainRequest(net_name="GND", layers=("B.Cu",)),
                GroundDomainRequest(net_name="PGND", layers=("F.Cu",), pad_connection="solid"),
            ),
            bridge_references=("R1",),
        ),
        domains=(
            GroundDomainPlan(
                net_name="GND",
                primary_layer="B.Cu",
                plane_layers=("F.Cu", "B.Cu"),
                regions=(
                    GroundZoneRegion(layer="B.Cu", kind="board"),
                    GroundZoneRegion(
                        layer="F.Cu",
                        kind="local",
                        min_x_mm=8.5,
                        min_y_mm=8,
                        max_x_mm=10,
                        max_y_mm=10.6,
                    ),
                    GroundZoneRegion(
                        layer="F.Cu",
                        kind="local",
                        min_x_mm=18.5,
                        min_y_mm=8,
                        max_x_mm=20,
                        max_y_mm=10.6,
                    ),
                ),
                fanout_segments=gnd_fanouts,
                vias=gnd_vias,
                target_pad_count=2,
                target_references=("C1", "R1"),
                planes_connected=True,
            ),
            GroundDomainPlan(
                net_name="PGND",
                primary_layer="F.Cu",
                plane_layers=("F.Cu",),
                regions=(GroundZoneRegion(layer="F.Cu", kind="board"),),
                pad_connection="solid",
                target_pad_count=1,
                target_references=("R1",),
                planes_connected=True,
            ),
        ),
        bridges=(
            GroundBridge(
                reference="R1",
                net_a="GND",
                pad_a="1",
                net_b="PGND",
                pad_b="2",
            ),
        ),
    )

    KiCadGroundingAdapter().apply(pcb, plan)

    adapter = PcbFileAdapter()
    assert adapter.copper_layers(pcb) == ("F.Cu", "B.Cu")
    assert adapter.summary(pcb, "test").zone_count == 4
    assert adapter.ground_plane_layers(pcb, "GND") == ("B.Cu", "F.Cu")
    assert adapter.ground_plane_layers(pcb, "PGND") == ("F.Cu",)
    assert adapter.analyze_routing(pcb, "test", ("GND",)).complete
    assert adapter.analyze_routing(pcb, "test", ("PGND",)).complete
    drc_report = tmp_path / "multi-ground-drc.rpt"
    subprocess.run(
        [
            str(KiCadGroundingAdapter._kicad_python().with_name("kicad-cli.exe")),
            "pcb",
            "drc",
            "--output",
            str(drc_report),
            str(pcb),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report_text = drc_report.read_text(encoding="utf-8")
    assert "zones_intersect" not in report_text
    assert report_text.count("[lib_footprint_issues]") == 2
