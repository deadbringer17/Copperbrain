from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from copperbrain.models import (
    ComponentCandidate,
    ManufacturingProfile,
    NetClassAssignment,
    NetClassRule,
    PcbBounds,
    PcbLayoutPlan,
    PcbRuleSet,
    PlacementOperation,
    PlacementRequest,
    PriceBreak,
    ProjectCreationSpec,
    RectangularBoardOutline,
    utc_now,
)


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is UTC


def test_candidate_accepts_sorted_unique_price_breaks() -> None:
    candidate = ComponentCandidate(
        lcsc="C1",
        mpn="MPN",
        manufacturer="Maker",
        description="Part",
        package="SOT-23",
        price_breaks=(
            PriceBreak(quantity=1, unit_price=0.2),
            PriceBreak(quantity=10, unit_price=0.1),
        ),
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert candidate.price_breaks[1].quantity == 10


@pytest.mark.parametrize(
    "breaks",
    [
        (PriceBreak(quantity=10, unit_price=0.1), PriceBreak(quantity=1, unit_price=0.2)),
        (PriceBreak(quantity=1, unit_price=0.2), PriceBreak(quantity=1, unit_price=0.1)),
    ],
)
def test_candidate_rejects_unsorted_or_duplicate_price_breaks(
    breaks: tuple[PriceBreak, PriceBreak],
) -> None:
    with pytest.raises(ValidationError, match="unique, ascending"):
        ComponentCandidate(
            lcsc="C1",
            mpn="MPN",
            manufacturer="Maker",
            description="Part",
            package="SOT-23",
            price_breaks=breaks,
        )


def test_pcb_rule_set_rejects_ambiguous_assignments() -> None:
    rule = NetClassRule(
        name="SIGNAL",
        clearance_mm=0.2,
        track_width_min_mm=0.2,
        track_width_preferred_mm=0.2,
        via_diameter_mm=0.6,
        via_drill_mm=0.3,
    )
    with pytest.raises(ValidationError, match="assigned only once"):
        PcbRuleSet(
            manufacturing=ManufacturingProfile(),
            classes=(rule,),
            assignments=(
                NetClassAssignment(net="/A", netclass="SIGNAL"),
                NetClassAssignment(net="/A", netclass="SIGNAL"),
            ),
        )


def test_placement_contracts_reject_invalid_regions_and_duplicate_references() -> None:
    with pytest.raises(ValidationError, match="positive area"):
        PcbBounds(min_x_mm=1, min_y_mm=1, max_x_mm=1, max_y_mm=2)
    with pytest.raises(ValidationError, match="unique"):
        PlacementRequest(references=("R1", "R1"))


def test_layout_plan_rejects_duplicate_component_references() -> None:
    with pytest.raises(ValidationError, match="unique references"):
        PcbLayoutPlan(
            outline=RectangularBoardOutline(width_mm=20, height_mm=10),
            placements=(
                PlacementOperation(reference="R1", x_mm=1, y_mm=1),
                PlacementOperation(reference="R1", x_mm=2, y_mm=2),
            ),
        )


def test_project_creation_spec_rejects_paths_and_unsupported_layers() -> None:
    assert ProjectCreationSpec(name="bench").copper_layers == 2
    with pytest.raises(ValidationError):
        ProjectCreationSpec(name="../unsafe")
    with pytest.raises(ValidationError):
        ProjectCreationSpec(name="bench", copper_layers=6)
