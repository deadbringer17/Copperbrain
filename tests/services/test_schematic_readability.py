"""Readability regression tests on a private copy of the BLDC benchmark schematic."""

from pathlib import Path

from copperbrain.adapters.schematic_api import SchematicApiAdapter
from copperbrain.adapters.schematic_readability import analyze_schematic_readability
from copperbrain.models import ComponentCandidate
from copperbrain.services.reference_design import bldc_schematic_readability_operations

BENCHMARK = Path("benchmark_bldc_drv8311/benchmark_bldc_drv8311.kicad_sch")


def _driver() -> ComponentCandidate:
    return ComponentCandidate(
        lcsc="UNSOURCED",
        mpn="DRV8311SRRWR",
        manufacturer="Texas Instruments",
        description="Three-phase BLDC driver with integrated FETs and SPI",
        package="WQFN-24",
        source="Texas Instruments",
    )


def test_bldc_readability_plan_improves_a_private_benchmark_copy(tmp_path: Path) -> None:
    source_bytes = BENCHMARK.read_bytes()
    temporary = tmp_path / BENCHMARK.name
    temporary.write_bytes(source_bytes)

    baseline = analyze_schematic_readability(BENCHMARK)
    operations = bldc_schematic_readability_operations(_driver())
    SchematicApiAdapter().apply(temporary, operations)
    improved = analyze_schematic_readability(temporary)

    assert baseline.labels_directly_on_pins == 99
    assert not baseline.valid
    assert len(operations) == 127
    assert improved.valid
    assert improved.readability_score == 100
    assert improved.labels_directly_on_pins == 0
    assert improved.labels_without_wire_connection == 0
    assert improved.duplicate_label_positions == 0
    assert improved.label_overlap_count == 0
    assert improved.wire_count == 98
    assert improved.occupied_width_mm > baseline.occupied_width_mm
    assert improved.occupied_height_mm > baseline.occupied_height_mm
    assert BENCHMARK.read_bytes() == source_bytes
