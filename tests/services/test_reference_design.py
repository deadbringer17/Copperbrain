import pytest

from copperbrain.models import ComponentCandidate
from copperbrain.services.reference_design import (
    bldc_driver_bench_layout_plan,
    bldc_driver_bench_manufacturing_profile,
    bldc_driver_bench_operations,
    bldc_driver_bench_rule_requirements,
    bldc_schematic_readability_operations,
    five_volt_buck_operations,
    forty_eight_to_twelve_operations,
    motor_driver_bench_layout_plan,
    motor_driver_bench_manufacturing_profile,
    motor_driver_bench_operations,
    motor_driver_bench_rule_requirements,
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


def test_motor_driver_benchmark_has_power_control_rs485_and_four_sensor_inputs() -> None:
    driver = ComponentCandidate(
        lcsc="C90964",
        mpn="DRV8701ERGER",
        manufacturer="Texas Instruments",
        description="H bridge gate driver",
        package="VQFN-24",
        stock=100,
    )
    transceiver = ComponentCandidate(
        lcsc="C1850236",
        mpn="THVD1429DR",
        manufacturer="Texas Instruments",
        description="RS-485",
        package="SOIC-8",
        stock=100,
    )
    mosfet = ComponentCandidate(
        lcsc="C86513",
        mpn="CSD18540Q5B",
        manufacturer="Texas Instruments",
        description="60 V MOSFET",
        package="VSON-8",
        stock=100,
    )

    operations = motor_driver_bench_operations(driver, transceiver, mosfet)
    added = {item.target for item in operations if item.kind == "add_component"}
    labels = {str(item.parameters.get("text")) for item in operations if item.kind == "label"}

    assert {"U1", "U2", "U3", "Q1", "Q2", "Q3", "Q4", "RSH1", "R0"} <= added
    assert {"J5", "J6", "J7", "J8", "U4", "U5", "U6", "U7"} <= added
    assert {"MOTOR_A", "MOTOR_B", "SHUNT_POWER", "RS485_A", "RS485_B"} <= labels
    assert {f"SENSOR{index}_N" for index in range(1, 5)} <= labels
    assert "5V_DVDD" in labels
    assert "PGND" in labels
    assert "3V3" not in labels
    assert any(
        item.kind == "set_paper_size" and item.parameters.get("paper") == "A3"
        for item in operations
    )
    sensor_diode_nets = {
        item.target: item.parameters.get("text")
        for item in operations
        if item.kind == "label" and item.target.startswith("SENSOR1_LED_A:DS1")
    }
    assert sensor_diode_nets == {"SENSOR1_LED_A:DS1.1": "SENSOR1_LED_A"}
    assert any(item.target == "PGND:DS1.2" for item in operations)
    assert any(item.target == "PGND:R0.1" for item in operations)
    assert any(item.target == "GND:R0.2" for item in operations)
    assert any(item.target == "GND:D3.1" for item in operations)
    assert any(item.target == "STATUS_LED_A:D3.2" for item in operations)
    u2 = next(item for item in operations if item.kind == "add_component" and item.target == "U2")
    assert "ThermalVias" not in str(u2.parameters["footprint"])

    layout = motor_driver_bench_layout_plan()
    assert (layout.outline.width_mm, layout.outline.height_mm) == (120, 100)
    assert len(layout.placements) == 64
    assert len(layout.mounting_holes) == 4
    assert {item.reference for item in layout.placements} == {
        reference for reference in added if not reference.startswith("#")
    }
    assert sum(item.layer == "B.Cu" for item in layout.placements) == 30
    assert sum(item.rotation_deg % 360 != 0 for item in layout.placements) >= 40

    profile = motor_driver_bench_manufacturing_profile()
    requirements = motor_driver_bench_rule_requirements()
    assert profile.copper_thickness_um == 70
    assert profile.allowed_temperature_rise_c == 20
    high_current = next(item for item in requirements if item.name == "CB_HIGH_CURRENT")
    assert high_current.current_a == 20
    assert "/PGND" in high_current.nets
    assert "/GND" not in high_current.nets
    assert any(
        item.kind == "update_property"
        and item.target == "U1"
        and item.parameters.get("name") == "DesignNote"
        and "PROVISIONAL" in str(item.parameters.get("value"))
        for item in operations
    )


def test_motor_driver_benchmark_rejects_incompatible_power_parts() -> None:
    invalid = ComponentCandidate(
        lcsc="C1",
        mpn="WRONG",
        manufacturer="Acme",
        description="wrong",
        package="SOP",
        stock=1,
    )
    with pytest.raises(ValueError, match="DRV8701E"):
        motor_driver_bench_operations(invalid, invalid, invalid)


def test_bldc_driver_benchmark_is_compact_three_phase_and_reviewable() -> None:
    driver = ComponentCandidate(
        lcsc="UNSOURCED",
        mpn="DRV8311SRRWR",
        manufacturer="Texas Instruments",
        description="3-phase BLDC driver with integrated FETs and SPI",
        package="WQFN-24",
        stock=0,
        source="Texas Instruments",
    )

    operations = bldc_driver_bench_operations(driver)
    added = {item.target for item in operations if item.kind == "add_component"}
    labels = {str(item.parameters.get("text")) for item in operations if item.kind == "label"}
    readability = bldc_schematic_readability_operations(driver)

    assert {"U1", "J1", "J2", "J3", "J4", "F1", "D1"} <= added
    assert {"PHASE_A", "PHASE_B", "PHASE_C", "VM", "GND", "AVDD"} <= labels
    assert {"PWM_AH", "PWM_AL", "PWM_BH", "PWM_BL", "PWM_CH", "PWM_CL"} <= labels
    assert {"SPI_SCLK", "SPI_MOSI", "SPI_MISO", "SPI_CS_N", "FAULT_N"} <= labels
    assert {"ISENSE_A", "ISENSE_B", "ISENSE_C", "HALL_A", "HALL_B", "HALL_C"} <= labels
    assert sum(item.kind == "relayout_pin_label" for item in operations) == 99
    assert sum(item.kind == "move_component" for item in readability) == 28
    assert sum(item.kind == "relayout_pin_label" for item in readability) == 99
    assert any(
        item.kind == "update_property"
        and item.target == "U1"
        and item.parameters.get("name") == "DesignNote"
        and "PROVISIONAL" in str(item.parameters.get("value"))
        for item in operations
    )

    layout = bldc_driver_bench_layout_plan()
    assert (layout.outline.width_mm, layout.outline.height_mm) == (85, 50)
    assert layout.outline.width_mm <= 100
    assert layout.outline.height_mm <= 60
    assert len(layout.mounting_holes) == 4
    assert {item.reference for item in layout.placements} == {
        reference for reference in added if not reference.startswith("#")
    }
    assert sum(item.layer == "B.Cu" for item in layout.placements) >= 10

    profile = bldc_driver_bench_manufacturing_profile()
    requirements = bldc_driver_bench_rule_requirements()
    assert profile.copper_thickness_um == 35
    high_current = next(item for item in requirements if item.name == "CB_BLDC_POWER")
    assert high_current.current_a == 3
    assert {"/PHASE_A", "/PHASE_B", "/PHASE_C"} <= set(high_current.nets)
    assert any(item.name == "CB_BLDC_CSA" for item in requirements)


def test_bldc_driver_benchmark_rejects_incompatible_driver() -> None:
    invalid = ComponentCandidate(
        lcsc="C1",
        mpn="WRONG",
        manufacturer="Acme",
        description="wrong",
        package="SOP",
    )
    with pytest.raises(ValueError, match="DRV8311S"):
        bldc_driver_bench_operations(invalid)
