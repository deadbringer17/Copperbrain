from datetime import UTC, datetime
from pathlib import Path

import pytest

from copperbrain.models import Component, ComponentCandidate, PriceBreak, ProjectSummary
from copperbrain.services.bom import (
    enrich_bom,
    estimate_bom_cost,
    export_bom,
    generate_bom,
    render_bom,
)


def summary() -> ProjectSummary:
    components = (
        Component(reference="R1", value="10k", footprint="R_0603", properties={"LCSC": "C1"}),
        Component(reference="R2", value="10k", footprint="R_0603", properties={"LCSC": "C1"}),
        Component(reference="#PWR01", value="VCC"),
    )
    return ProjectSummary(
        session_id="s", sheets=("x",), components=components, nets=(), power_symbols=("VCC",)
    )


def test_generate_enrich_and_estimate_bom() -> None:
    lines = generate_bom(summary())
    assert lines[0].references == ("R1", "R2")
    candidate = ComponentCandidate(
        lcsc="C1",
        mpn="M1",
        manufacturer="Acme",
        description="resistor",
        package="0603",
        stock=15,
        price_breaks=(PriceBreak(quantity=1, unit_price=0.1),),
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    enriched = enrich_bom(lines, {"C1": candidate}, (10, 100))
    estimate = estimate_bom_cost(enriched, 10)
    assert estimate.component_cost == 2
    assert estimate.insufficient_stock == ("C1",)
    missing = estimate_bom_cost(lines, 10)
    assert missing.missing_prices == ("C1",)


def test_generate_bom_orders_groups_by_lowest_reference() -> None:
    components = (
        Component(reference="R2", value="10k", footprint="R_0603", properties={}),
        Component(reference="R10", value="1k", footprint="R_0603", properties={}),
        Component(reference="R1", value="10k", footprint="R_0603", properties={}),
    )
    lines = generate_bom(
        ProjectSummary(
            session_id="s", sheets=("x",), components=components, nets=(), power_symbols=()
        )
    )
    assert [line.references for line in lines] == [("R1", "R2"), ("R10",)]


@pytest.mark.parametrize("output_format", ["json", "csv", "markdown"])
def test_render_and_export_bom(tmp_path: Path, output_format: str) -> None:
    lines = generate_bom(summary())
    rendered = render_bom(lines, output_format)
    assert "R1" in rendered
    suffix = {"json": ".json", "csv": ".csv", "markdown": ".md"}[output_format]
    destination = tmp_path / f"bom{suffix}"
    assert export_bom(lines, destination, output_format) == destination
    assert destination.read_text(encoding="utf-8").replace("\r\n", "\n") == rendered


def test_render_and_export_reject_invalid_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="output_format"):
        render_bom((), "xml")
    with pytest.raises(ValueError, match="suffix"):
        export_bom((), tmp_path / "bom.txt", "json")
