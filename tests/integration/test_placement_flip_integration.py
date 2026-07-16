import shutil
from pathlib import Path

import pytest

from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.pcb_placement import KiCadPlacementAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import PlacementOperation

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


def test_kicad_worker_flips_complete_footprint_to_bottom(tmp_path: Path) -> None:
    try:
        KiCadPlacementAdapter._kicad_python()
    except CopperbrainError:
        pytest.skip("KiCad bundled Python is unavailable")
    pcb = tmp_path / "placement.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    KiCadPlacementAdapter().apply(
        pcb,
        (
            PlacementOperation(
                reference="R1",
                x_mm=12,
                y_mm=13,
                rotation_deg=90,
                layer="B.Cu",
            ),
        ),
    )
    adapter = PcbFileAdapter()
    footprint = next(
        item for item in adapter.summary(pcb, "test").footprints if item.reference == "R1"
    )
    pads = tuple(item for item in adapter.pads(pcb) if item.reference == "R1")
    assert (footprint.x_mm, footprint.y_mm, footprint.rotation_deg) == (12, 13, 90)
    assert footprint.layer == "B.Cu"
    assert {layer for pad in pads for layer in pad.layers} == {"B.Cu"}
    text = pcb.read_text(encoding="utf-8")
    assert '(layer "B.CrtYd")' in text
    assert "(justify mirror)" in text
