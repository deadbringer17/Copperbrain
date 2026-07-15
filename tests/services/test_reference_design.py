import pytest

from copperbrain.models import ComponentCandidate
from copperbrain.services.reference_design import (
    five_volt_buck_operations,
    forty_eight_to_twelve_operations,
)


def test_reference_design_contains_components_metadata_connections_and_labels() -> None:
    candidate = ComponentCandidate(
        lcsc="C10002",
        mpn="LM2596SX-5.0/NOPB",
        manufacturer="Texas Instruments",
        description="5V buck",
        package="TO-263-5",
    )
    operations = five_volt_buck_operations(candidate)
    kinds = [operation.kind for operation in operations]
    assert kinds.count("add_component") == 9
    assert kinds.count("update_property") == 3
    assert kinds.count("connect") == 15
    assert kinds.count("label") == 3


def test_reference_design_rejects_incompatible_regulator() -> None:
    candidate = ComponentCandidate(
        lcsc="C1",
        mpn="OTHER",
        manufacturer="Acme",
        description="other",
        package="SOT-23",
    )
    with pytest.raises(ValueError, match="LM2596"):
        five_volt_buck_operations(candidate)


def test_48v_reference_design_is_complete_and_reviewable() -> None:
    candidate = ComponentCandidate(
        lcsc="C182428",
        mpn="LM5576MH/NOPB",
        manufacturer="Texas Instruments",
        description="75V 3A buck",
        package="HTSSOP-20-EP",
    )
    operations = forty_eight_to_twelve_operations(candidate)
    kinds = [operation.kind for operation in operations]
    assert kinds.count("add_component") == 22
    assert kinds.count("update_property") == 5
    assert kinds.count("connect") == 19
    assert kinds.count("label") == 59
    assert kinds.count("no_connect") == 2
    assert any(operation.target == "GND:U1.21" for operation in operations)


def test_48v_reference_design_rejects_incompatible_regulator() -> None:
    candidate = ComponentCandidate(
        lcsc="C1",
        mpn="OTHER",
        manufacturer="Acme",
        description="other",
        package="SOT-23",
    )
    with pytest.raises(ValueError, match="LM5576"):
        forty_eight_to_twelve_operations(candidate)
