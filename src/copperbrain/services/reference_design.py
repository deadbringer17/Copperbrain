"""Deterministic semantic templates for approved bounded reference designs."""

from __future__ import annotations

from typing import Literal

from copperbrain.models import (
    ChangeOperation,
    ComponentCandidate,
    ManufacturingProfile,
    MountingHoleSpec,
    NetRuleRequirement,
    PcbLayoutPlan,
    PlacementOperation,
    RectangularBoardOutline,
)


def _component_operation(
    reference: str,
    lib_id: str,
    value: str,
    x: float,
    y: float,
    footprint: str,
) -> ChangeOperation:
    return ChangeOperation(
        kind="add_component",
        target=reference,
        parameters={
            "lib_id": lib_id,
            "value": value,
            "x": x,
            "y": y,
            "footprint": footprint,
        },
    )


def _net_label(net: str, reference: str, pin: str) -> ChangeOperation:
    return ChangeOperation(
        kind="label",
        target=f"{net}:{reference}.{pin}",
        parameters={"text": net, "reference": reference, "pin": pin},
    )


def motor_driver_bench_operations(
    driver: ComponentCandidate,
    transceiver: ComponentCandidate,
    mosfet: ComponentCandidate,
) -> tuple[ChangeOperation, ...]:
    """Build the provisional 12 V / 20 A brushed-DC benchmark reference design."""
    if "DRV8701E" not in driver.mpn.upper():
        raise ValueError("Motor benchmark requires a DRV8701E-compatible gate driver")
    if "THVD1429" not in transceiver.mpn.upper():
        raise ValueError("Motor benchmark requires a THVD1429-compatible RS-485 transceiver")
    if "CSD18540Q5B" not in mosfet.mpn.upper():
        raise ValueError("Motor benchmark requires CSD18540Q5B-compatible 60 V MOSFETs")

    components = (
        (
            "J1",
            "Connector_Generic:Conn_01x02",
            "12V_IN_20A",
            25,
            35,
            "TerminalBlock_Wuerth:Wuerth_691311400102_P7.62mm",
        ),
        ("F1", "Device:Fuse", "25A ATO", 50, 30, "Fuse:Fuse_Blade_ATO_directSolder"),
        ("D1", "Device:D_TVS", "SMBJ24CA", 50, 50, "Diode_SMD:D_SMB"),
        (
            "C1",
            "Device:C_Polarized",
            "470uF 35V LOW ESR",
            70,
            50,
            "Capacitor_THT:CP_Radial_D12.5mm_P5.00mm",
        ),
        (
            "C2",
            "Device:C_Polarized",
            "470uF 35V LOW ESR",
            90,
            50,
            "Capacitor_THT:CP_Radial_D12.5mm_P5.00mm",
        ),
        ("C3", "Device:C", "100nF 50V X7R", 110, 50, "Capacitor_SMD:C_0805_2012Metric"),
        (
            "U1",
            "Copperbrain_DRV8701:DRV8701ERGER",
            "DRV8701ERGER",
            150,
            65,
            "Copperbrain_DRV8701:DRV8701ERGER",
        ),
        (
            "Q1",
            "Copperbrain_CSD18540:CSD18540Q5B",
            "CSD18540Q5B HS-A",
            220,
            40,
            "Copperbrain_CSD18540:CSD18540Q5B",
        ),
        (
            "Q2",
            "Copperbrain_CSD18540:CSD18540Q5B",
            "CSD18540Q5B LS-A",
            220,
            100,
            "Copperbrain_CSD18540:CSD18540Q5B",
        ),
        (
            "Q3",
            "Copperbrain_CSD18540:CSD18540Q5B",
            "CSD18540Q5B HS-B",
            290,
            40,
            "Copperbrain_CSD18540:CSD18540Q5B",
        ),
        (
            "Q4",
            "Copperbrain_CSD18540:CSD18540Q5B",
            "CSD18540Q5B LS-B",
            290,
            100,
            "Copperbrain_CSD18540:CSD18540Q5B",
        ),
        ("RG1", "Device:R", "10R", 195, 40, "Resistor_SMD:R_0805_2012Metric"),
        ("RG2", "Device:R", "10R", 195, 100, "Resistor_SMD:R_0805_2012Metric"),
        ("RG3", "Device:R", "10R", 265, 40, "Resistor_SMD:R_0805_2012Metric"),
        ("RG4", "Device:R", "10R", 265, 100, "Resistor_SMD:R_0805_2012Metric"),
        (
            "RSH1",
            "Device:R_Shunt",
            "1mR 3W KELVIN",
            255,
            135,
            "Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T2.21mm",
        ),
        (
            "J2",
            "Connector_Generic:Conn_01x02",
            "MOTOR_20A",
            350,
            70,
            "TerminalBlock_Wuerth:Wuerth_691311400102_P7.62mm",
        ),
        ("C4", "Device:C", "1uF 50V X7R", 110, 30, "Capacitor_SMD:C_0805_2012Metric"),
        ("C5", "Device:C", "100nF 50V X7R", 125, 30, "Capacitor_SMD:C_0805_2012Metric"),
        ("C6", "Device:C", "100nF", 50, 90, "Capacitor_SMD:C_0603_1608Metric"),
        ("C7", "Device:C", "1uF", 70, 90, "Capacitor_SMD:C_0603_1608Metric"),
        ("C8", "Device:C", "1uF", 90, 90, "Capacitor_SMD:C_0603_1608Metric"),
        ("C9", "Device:C", "1nF", 110, 90, "Capacitor_SMD:C_0603_1608Metric"),
        ("R1", "Device:R", "10k", 50, 115, "Resistor_SMD:R_0603_1608Metric"),
        ("R2", "Device:R", "10k", 70, 115, "Resistor_SMD:R_0603_1608Metric"),
        ("R3", "Device:R", "10k", 90, 115, "Resistor_SMD:R_0603_1608Metric"),
        ("R4", "Device:R", "33k IDRIVE", 110, 115, "Resistor_SMD:R_0603_1608Metric"),
        ("R5", "Device:R", "100k", 130, 115, "Resistor_SMD:R_0603_1608Metric"),
        (
            "U2",
            "MCU_Microchip_ATtiny:ATtiny1616-M",
            "ATtiny1616-MNR",
            120,
            190,
            "Package_DFN_QFN:VQFN-20-1EP_3x3mm_P0.45mm_EP1.55x1.55mm",
        ),
        ("C10", "Device:C", "100nF", 80, 175, "Capacitor_SMD:C_0603_1608Metric"),
        (
            "J3",
            "Connector_Generic:Conn_01x03",
            "UPDI",
            55,
            190,
            "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
        ),
        (
            "U3",
            "Copperbrain_THVD1429:THVD1429DR",
            "THVD1429DR",
            220,
            190,
            "Copperbrain_THVD1429:THVD1429DR",
        ),
        ("C11", "Device:C", "100nF", 190, 215, "Capacitor_SMD:C_0603_1608Metric"),
        ("R6", "Device:R", "120R TERM", 280, 180, "Resistor_SMD:R_0805_2012Metric"),
        (
            "JP1",
            "Jumper:Jumper_2_Open",
            "RS485_TERM_EN",
            260,
            180,
            "Jumper:SolderJumper-2_P1.3mm_Open_Pad1.0x1.5mm",
        ),
        ("D2", "Diode:SM712_SOT23", "SM712", 300, 200, "Package_TO_SOT_SMD:SOT-23"),
        (
            "J4",
            "Connector_Generic:Conn_01x03",
            "RS485_A_B_GND",
            350,
            190,
            "TerminalBlock_4Ucon:TerminalBlock_4Ucon_1x03_P3.50mm_Vertical",
        ),
        ("D3", "Device:LED", "STATUS", 85, 225, "LED_SMD:LED_0603_1608Metric"),
        ("R7", "Device:R", "1k", 65, 225, "Resistor_SMD:R_0603_1608Metric"),
        ("R0", "Device:R", "0R STAR", 150, 155, "Resistor_SMD:R_0805_2012Metric"),
        ("#FLG01", "power:PWR_FLAG", "PWR_FLAG", 40, 20, ""),
        ("#FLG02", "power:PWR_FLAG", "PWR_FLAG", 50, 20, ""),
        ("#FLG03", "power:PWR_FLAG", "PWR_FLAG", 60, 20, ""),
        ("#FLG04", "power:PWR_FLAG", "PWR_FLAG", 70, 20, ""),
    )
    operations = [
        ChangeOperation(
            kind="set_paper_size",
            target="schematic",
            parameters={"paper": "A3"},
        ),
        *(_component_operation(*component) for component in components),
    ]

    sensor_x = (55.0, 145.0, 235.0, 325.0)
    for index, x in enumerate(sensor_x, start=1):
        operations.extend(
            (
                _component_operation(
                    f"J{index + 4}",
                    "Connector_Generic:Conn_01x03",
                    f"SENSOR{index}_12V_SIG_GND",
                    x,
                    240,
                    "TerminalBlock_4Ucon:TerminalBlock_4Ucon_1x03_P3.50mm_Vertical",
                ),
                _component_operation(
                    f"RIN{index}",
                    "Device:R",
                    "2.7k 0.5W",
                    x,
                    250,
                    "Resistor_SMD:R_2010_5025Metric",
                ),
                _component_operation(
                    f"U{index + 3}",
                    "Isolator:LTV-817S",
                    "LTV-817S",
                    x,
                    265,
                    "Package_SO:SOP-4_7.5x4.1mm_P2.54mm",
                ),
                _component_operation(
                    f"DS{index}",
                    "Device:D",
                    "1N4148W REV",
                    x - 8,
                    265,
                    "Diode_SMD:D_SOD-123",
                ),
                _component_operation(
                    f"RP{index}",
                    "Device:R",
                    "10k",
                    x + 8,
                    265,
                    "Resistor_SMD:R_0603_1608Metric",
                ),
                _component_operation(
                    f"CS{index}",
                    "Device:C",
                    "10nF",
                    x + 8,
                    280,
                    "Capacitor_SMD:C_0603_1608Metric",
                ),
            )
        )

    nets: dict[str, tuple[tuple[str, str], ...]] = {
        "VBAT_RAW": (("J1", "1"), ("F1", "1")),
        "VMOTOR": (
            ("F1", "2"),
            ("D1", "2"),
            ("C1", "1"),
            ("C2", "1"),
            ("C3", "1"),
            ("C4", "2"),
            ("U1", "1"),
            ("Q1", "5"),
            ("Q1", "6"),
            ("Q1", "7"),
            ("Q1", "8"),
            ("Q3", "5"),
            ("Q3", "6"),
            ("Q3", "7"),
            ("Q3", "8"),
            ("#FLG01", "1"),
            *((f"J{index + 4}", "1") for index in range(1, 5)),
        ),
        "PGND": (
            ("J1", "2"),
            ("D1", "1"),
            ("C1", "2"),
            ("C2", "2"),
            ("C3", "2"),
            ("U1", "5"),
            ("U1", "16"),
            ("U1", "25"),
            ("RSH1", "4"),
            ("C6", "2"),
            ("C7", "2"),
            ("C8", "2"),
            ("C9", "2"),
            ("R2", "2"),
            ("R4", "2"),
            ("R5", "2"),
            ("R0", "1"),
            ("#FLG02", "1"),
            *((f"J{index + 4}", "3") for index in range(1, 5)),
            *((f"U{index + 3}", "2") for index in range(1, 5)),
            *((f"DS{index}", "2") for index in range(1, 5)),
        ),
        "GND": (
            ("R0", "2"),
            ("#FLG04", "1"),
            ("U2", "3"),
            ("U2", "21"),
            ("C10", "2"),
            ("J3", "3"),
            ("U3", "5"),
            ("C11", "2"),
            ("D2", "3"),
            ("J4", "3"),
            ("D3", "1"),
            *((f"U{index + 3}", "3") for index in range(1, 5)),
            *((f"CS{index}", "2") for index in range(1, 5)),
        ),
        "5V_DVDD": (
            ("U1", "8"),
            ("C8", "1"),
            ("R3", "1"),
            ("U2", "4"),
            ("C10", "1"),
            ("J3", "1"),
            ("U3", "8"),
            ("C11", "1"),
            ("#FLG03", "1"),
            *((f"RP{index}", "1") for index in range(1, 5)),
        ),
        "VCP": (("U1", "2"), ("C4", "1")),
        "CPH": (("U1", "3"), ("C5", "1")),
        "CPL": (("U1", "4"), ("C5", "2")),
        "AVDD": (("U1", "7"), ("C7", "1")),
        "CURRENT_REF": (("U1", "6"), ("R1", "2"), ("R2", "1"), ("C6", "1")),
        "DAC_CURRENT": (("R1", "1"), ("U2", "16")),
        "DRIVER_FAULT": (("U1", "9"), ("R3", "2"), ("U2", "6")),
        "CURRENT_SENSE": (("U1", "10"), ("C9", "1"), ("U2", "7")),
        "SO_ANALOG": (("U1", "11"), ("U2", "8")),
        "IDRIVE_CFG": (("U1", "12"), ("R4", "1")),
        "DRIVER_SLEEP": (("U1", "13"), ("R5", "1"), ("U2", "5")),
        "MOTOR_PWM": (("U1", "14"), ("U2", "1")),
        "MOTOR_DIR": (("U1", "15"), ("U2", "2")),
        "GH1": (("U1", "17"), ("RG1", "1")),
        "GATE_HS_A": (("RG1", "2"), ("Q1", "4")),
        "MOTOR_A": (
            ("U1", "18"),
            ("Q1", "1"),
            ("Q1", "2"),
            ("Q1", "3"),
            ("Q2", "5"),
            ("Q2", "6"),
            ("Q2", "7"),
            ("Q2", "8"),
            ("J2", "1"),
        ),
        "GL1": (("U1", "19"), ("RG2", "1")),
        "GATE_LS_A": (("RG2", "2"), ("Q2", "4")),
        "SHUNT_N": (("U1", "20"), ("RSH1", "3")),
        "SHUNT_P_SENSE": (("U1", "21"), ("RSH1", "2")),
        "GL2": (("U1", "22"), ("RG4", "1")),
        "GATE_LS_B": (("RG4", "2"), ("Q4", "4")),
        "MOTOR_B": (
            ("U1", "23"),
            ("Q3", "1"),
            ("Q3", "2"),
            ("Q3", "3"),
            ("Q4", "5"),
            ("Q4", "6"),
            ("Q4", "7"),
            ("Q4", "8"),
            ("J2", "2"),
        ),
        "GH2": (("U1", "24"), ("RG3", "1")),
        "GATE_HS_B": (("RG3", "2"), ("Q3", "4")),
        "SHUNT_POWER": (
            ("Q2", "1"),
            ("Q2", "2"),
            ("Q2", "3"),
            ("Q4", "1"),
            ("Q4", "2"),
            ("Q4", "3"),
            ("RSH1", "1"),
        ),
        "UPDI": (("U2", "19"), ("J3", "2")),
        "RS485_TX": (("U2", "9"), ("U3", "4")),
        "RS485_RX": (("U2", "10"), ("U3", "1")),
        "RS485_DE_RE": (("U2", "11"), ("U3", "2"), ("U3", "3")),
        "RS485_A": (("U3", "6"), ("D2", "1"), ("J4", "1"), ("JP1", "1")),
        "RS485_A_TERM": (("JP1", "2"), ("R6", "1")),
        "RS485_B": (("U3", "7"), ("D2", "2"), ("J4", "2"), ("R6", "2")),
        "STATUS_LED": (("U2", "17"), ("R7", "1")),
        "STATUS_LED_A": (("R7", "2"), ("D3", "2")),
    }
    for index in range(1, 5):
        sensor_input = f"SENSOR{index}_FIELD"
        sensor_led = f"SENSOR{index}_LED_A"
        sensor_logic = f"SENSOR{index}_N"
        mcu_pin = str(11 + index)
        nets[sensor_input] = ((f"J{index + 4}", "2"), (f"RIN{index}", "1"))
        nets[sensor_led] = (
            (f"RIN{index}", "2"),
            (f"U{index + 3}", "1"),
            (f"DS{index}", "1"),
        )
        nets[sensor_logic] = (
            (f"U{index + 3}", "4"),
            (f"RP{index}", "2"),
            (f"CS{index}", "1"),
            ("U2", mcu_pin),
        )

    for net, pins in nets.items():
        operations.extend(_net_label(net, reference, pin) for reference, pin in pins)

    metadata = {
        "U1": {
            "LCSC": driver.lcsc,
            "MPN": driver.mpn,
            "Manufacturer": driver.manufacturer,
            "Datasheet": "https://www.ti.com/lit/ds/symlink/drv8701.pdf",
            "DesignNote": (
                "PROVISIONAL: 12 V nominal brushed-DC H-bridge, 20 A continuous target; "
                "validate motor stall current, bulk capacitance, switching and thermal behavior"
            ),
        },
        "U2": {
            "LCSC": "C507118",
            "MPN": "ATTINY1616-MNR",
            "Manufacturer": "Microchip Technology",
            "DesignNote": "Firmware must default nSLEEP low and enforce current/fault shutdown",
        },
        "U3": {
            "LCSC": transceiver.lcsc,
            "MPN": transceiver.mpn,
            "Manufacturer": transceiver.manufacturer,
            "Datasheet": "https://www.ti.com/lit/ds/symlink/thvd1429.pdf",
            "DesignNote": "RS-485 half-duplex; JP1 enables 120 ohm termination",
        },
    }
    for reference in ("Q1", "Q2", "Q3", "Q4"):
        metadata[reference] = {
            "LCSC": mosfet.lcsc,
            "MPN": mosfet.mpn,
            "Manufacturer": mosfet.manufacturer,
            "Datasheet": "https://www.ti.com/lit/ds/symlink/csd18540q5b.pdf",
            "DesignNote": "60 V MOSFET; verify junction temperature and SOA at stall current",
        }
    for reference, properties in metadata.items():
        operations.extend(
            ChangeOperation(
                kind="update_property",
                target=reference,
                parameters={"name": name, "value": value, "hidden": True},
            )
            for name, value in properties.items()
        )

    operations.extend(
        ChangeOperation(
            kind="no_connect",
            target=f"U2.{pin}",
            parameters={"reference": "U2", "pin": pin},
        )
        for pin in ("18", "20")
    )
    return tuple(operations)


def motor_driver_bench_layout_plan() -> PcbLayoutPlan:
    """Place the provisional motor benchmark as a compact 120 x 100 mm unrouted PCB."""
    positions: dict[str, tuple[float, float, float, Literal["F.Cu", "B.Cu"]]] = {
        "C1": (39, 101.5, 90, "F.Cu"),
        "C10": (35.5, 21.5, 0, "B.Cu"),
        "C11": (39.5, 21.5, 0, "B.Cu"),
        "C2": (43, 78, 270, "F.Cu"),
        "C3": (23.5, 35, 270, "B.Cu"),
        "C4": (27.5, 35, 270, "B.Cu"),
        "C5": (24, 31.5, 0, "B.Cu"),
        "C6": (27.5, 31, 90, "B.Cu"),
        "C7": (31, 31, 90, "B.Cu"),
        "C8": (31, 26, 270, "B.Cu"),
        "C9": (34.5, 26, 90, "B.Cu"),
        "CS1": (41.5, 25, 90, "B.Cu"),
        "CS2": (36, 41.5, 90, "B.Cu"),
        "CS3": (36, 50, 90, "B.Cu"),
        "CS4": (37, 57, 0, "B.Cu"),
        "D1": (41, 33.5, 270, "F.Cu"),
        "D2": (36, 32.5, 270, "F.Cu"),
        "D3": (36, 36, 0, "F.Cu"),
        "DS1": (67, 33.5, 0, "F.Cu"),
        "DS2": (57.5, 50, 0, "F.Cu"),
        "DS3": (40.5, 57, 270, "F.Cu"),
        "DS4": (22.5, 57, 270, "F.Cu"),
        "F1": (51, 50, 90, "F.Cu"),
        "J1": (67, 25, 180, "F.Cu"),
        "J2": (76.5, 25, 270, "F.Cu"),
        "J3": (47, 30, 90, "F.Cu"),
        "J4": (89, 25, 0, "F.Cu"),
        "J5": (31.5, 66.5, 180, "F.Cu"),
        "J6": (31.5, 78, 180, "F.Cu"),
        "J7": (25, 104.5, 90, "F.Cu"),
        "J8": (39, 115, 180, "F.Cu"),
        "JP1": (84, 25, 90, "F.Cu"),
        "Q1": (31.5, 41.5, 0, "F.Cu"),
        "Q2": (23.5, 41.5, 180, "F.Cu"),
        "Q3": (23.5, 50, 180, "F.Cu"),
        "Q4": (31.5, 50, 0, "F.Cu"),
        "R0": (36, 32.5, 270, "B.Cu"),
        "R1": (37, 26, 270, "B.Cu"),
        "R2": (27.5, 39, 270, "B.Cu"),
        "R3": (33.5, 31, 90, "B.Cu"),
        "R4": (31, 36, 270, "B.Cu"),
        "R5": (31.5, 21.5, 0, "B.Cu"),
        "R6": (84, 25, 270, "B.Cu"),
        "R7": (36, 36, 0, "B.Cu"),
        "RG1": (31, 40, 270, "B.Cu"),
        "RG2": (23.5, 38.5, 180, "B.Cu"),
        "RG3": (23, 42, 270, "B.Cu"),
        "RG4": (31.5, 48, 270, "B.Cu"),
        "RIN1": (31.5, 57, 90, "B.Cu"),
        "RIN2": (43, 71.5, 0, "F.Cu"),
        "RIN3": (39, 90, 0, "F.Cu"),
        "RIN4": (33, 87, 90, "F.Cu"),
        "RP1": (44, 25, 270, "B.Cu"),
        "RP2": (33.5, 40, 270, "B.Cu"),
        "RP3": (38.5, 50, 270, "B.Cu"),
        "RP4": (37, 54.5, 180, "B.Cu"),
        "RSH1": (31.5, 32.5, 90, "F.Cu"),
        "U1": (25, 32, 0, "F.Cu"),
        "U2": (31.5, 25, 0, "F.Cu"),
        "U3": (37.5, 25, 90, "F.Cu"),
        "U4": (47, 25, 180, "F.Cu"),
        "U5": (41, 41.5, 180, "F.Cu"),
        "U6": (41, 50, 180, "F.Cu"),
        "U7": (31.5, 57, 0, "F.Cu"),
    }
    return PcbLayoutPlan(
        outline=RectangularBoardOutline(
            min_x_mm=20,
            min_y_mm=20,
            width_mm=120,
            height_mm=100,
        ),
        placements=tuple(
            PlacementOperation(
                reference=reference,
                x_mm=x,
                y_mm=y,
                rotation_deg=rotation,
                layer=layer,
            )
            for reference, (x, y, rotation, layer) in sorted(positions.items())
        ),
        mounting_holes=tuple(
            MountingHoleSpec(reference=f"H{index}", x_mm=x, y_mm=y)
            for index, (x, y) in enumerate(((25, 25), (135, 25), (25, 115), (135, 115)), start=1)
        ),
    )


def motor_driver_bench_manufacturing_profile() -> ManufacturingProfile:
    """Return the provisional 2 oz external-copper fabrication assumptions."""
    return ManufacturingProfile(
        min_clearance_mm=0.2,
        min_track_width_mm=0.2,
        min_via_diameter_mm=0.6,
        min_via_drill_mm=0.3,
        copper_thickness_um=70,
        allowed_temperature_rise_c=20,
        current_layer="external",
    )


def motor_driver_bench_rule_requirements() -> tuple[NetRuleRequirement, ...]:
    """Return reviewed deterministic net roles for the provisional benchmark."""
    return (
        NetRuleRequirement(
            name="CB_HIGH_CURRENT",
            nets=("/VBAT_RAW", "/VMOTOR", "/MOTOR_A", "/MOTOR_B", "/SHUNT_POWER", "/PGND"),
            role="high_current",
            current_a=20,
            clearance_mm=0.2,
        ),
        NetRuleRequirement(
            name="CB_RS485",
            nets=("/RS485_A", "/RS485_B"),
            role="differential",
            diff_pair_width_mm=0.25,
            diff_pair_gap_mm=0.25,
            max_length_mm=200,
            diff_pair_max_uncoupled_mm=15,
        ),
        NetRuleRequirement(
            name="CB_GATE_DRIVE",
            nets=(
                "/GH1",
                "/GH2",
                "/GL1",
                "/GL2",
                "/GATE_HS_A",
                "/GATE_HS_B",
                "/GATE_LS_A",
                "/GATE_LS_B",
            ),
            role="switching",
            track_width_mm=0.3,
            max_length_mm=30,
        ),
        NetRuleRequirement(
            name="CB_KELVIN_SENSE",
            nets=("/SHUNT_N", "/SHUNT_P_SENSE", "/CURRENT_SENSE", "/SO_ANALOG"),
            role="signal",
            track_width_mm=0.2,
            max_length_mm=30,
        ),
    )


def five_volt_buck_operations(candidate: ComponentCandidate) -> tuple[ChangeOperation, ...]:
    """Build a reviewable LM2596-class reference section from validated semantic operations."""
    required = "LM2596"
    if required not in candidate.mpn.upper():
        raise ValueError("Reference design requires an LM2596-compatible selected candidate")
    components = (
        (
            "JIN",
            "Connector_Generic:Conn_01x02",
            "12V_IN",
            55,
            50,
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        ),
        (
            "U1",
            "Regulator_Switching:LM2596S-5",
            "LM2596S-5",
            80,
            50,
            "Package_TO_SOT_SMD:TO-263-5_TabPin3",
        ),
        ("D1", "Device:D_Schottky", "SS34", 90, 65, "Diode_SMD:D_SMA"),
        ("L1", "Device:L", "68uH", 105, 50, "Inductor_SMD:L_12x12mm_H6mm"),
        ("C1", "Device:C", "220uF/25V", 70, 65, "Capacitor_SMD:CP_Elec_8x10"),
        ("C2", "Device:C", "220uF/10V", 115, 65, "Capacitor_SMD:CP_Elec_8x10"),
        (
            "J2",
            "Connector_Generic:Conn_01x02",
            "5V_OUT",
            130,
            50,
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        ),
        ("#FLG01", "power:PWR_FLAG", "PWR_FLAG", 65, 45, ""),
        ("#FLG02", "power:PWR_FLAG", "PWR_FLAG", 65, 75, ""),
    )
    operations: list[ChangeOperation] = [
        ChangeOperation(
            kind="add_component",
            target=reference,
            parameters={
                "lib_id": lib_id,
                "value": value,
                "x": x,
                "y": y,
                "footprint": footprint,
            },
        )
        for reference, lib_id, value, x, y, footprint in components
    ]
    metadata = {
        "LCSC": candidate.lcsc,
        "MPN": candidate.mpn,
        "Manufacturer": candidate.manufacturer,
        "Datasheet": str(candidate.datasheet_url or ""),
    }
    operations.extend(
        ChangeOperation(
            kind="update_property",
            target="U1",
            parameters={"name": name, "value": value},
        )
        for name, value in metadata.items()
        if value
    )
    connections = (
        ("JIN", "1", "U1", "1"),
        ("U1", "1", "C1", "1"),
        ("C1", "1", "#FLG01", "1"),
        ("JIN", "2", "C1", "2"),
        ("C1", "2", "U1", "3"),
        ("U1", "3", "U1", "5"),
        ("U1", "5", "D1", "2"),
        ("D1", "2", "C2", "2"),
        ("C2", "2", "J2", "2"),
        ("J2", "2", "#FLG02", "1"),
        ("U1", "2", "D1", "1"),
        ("D1", "1", "L1", "1"),
        ("L1", "2", "C2", "1"),
        ("C2", "1", "U1", "4"),
        ("U1", "4", "J2", "1"),
    )
    operations.extend(
        ChangeOperation(
            kind="connect",
            target=f"{source}.{source_pin}-{target}.{target_pin}",
            parameters={
                "from_reference": source,
                "from_pin": source_pin,
                "to_reference": target,
                "to_pin": target_pin,
            },
        )
        for source, source_pin, target, target_pin in connections
    )
    operations.extend(
        (
            ChangeOperation(
                kind="label",
                target="VIN12",
                parameters={"text": "VIN12", "reference": "JIN", "pin": "1"},
            ),
            ChangeOperation(
                kind="label",
                target="GND",
                parameters={"text": "GND", "reference": "JIN", "pin": "2"},
            ),
            ChangeOperation(
                kind="label",
                target="+5V",
                parameters={"text": "+5V", "reference": "J2", "pin": "1"},
            ),
        )
    )
    return tuple(operations)


def forty_eight_to_twelve_operations(
    candidate: ComponentCandidate,
) -> tuple[ChangeOperation, ...]:
    """Build the reviewable LM5576 48 V nominal to 12 V / 2 A reference section."""
    if "LM5576" not in candidate.mpn.upper():
        raise ValueError("48 V reference design requires an LM5576-compatible candidate")

    components = (
        (
            "J1",
            "Connector_Generic:Conn_01x02",
            "48V_IN",
            30,
            90,
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        ),
        ("F1", "Device:Fuse", "2A T / >=80VDC", 55, 85, "Fuse:Fuse_1206_3216Metric"),
        (
            "U1",
            "Copperbrain:LM5576MH_NOPB",
            "LM5576MH/NOPB",
            145,
            95,
            "Copperbrain:LM5576MH_NOPB",
        ),
        ("D1", "Device:D_Schottky", "CSHD6-100C 100V/6A", 180, 125, "Package_TO_SOT_SMD:TO-252-2"),
        ("L1", "Device:L", "68uH Isat>=5.1A", 195, 85, "Inductor_SMD:L_Coilcraft_MSS1260-XXX"),
        ("C1", "Device:C", "2.2uF 100V X7R", 75, 115, "Capacitor_SMD:C_1210_3225Metric"),
        ("C2", "Device:C", "2.2uF 100V X7R", 92, 115, "Capacitor_SMD:C_1210_3225Metric"),
        ("C3", "Device:C_Polarized", "47uF 100V", 58, 115, "Capacitor_SMD:CP_Elec_10x10.5"),
        ("C4", "Device:C", "470nF 16V X7R", 115, 55, "Capacitor_SMD:C_0805_2012Metric"),
        ("C5", "Device:C", "22nF 100V X7R", 180, 55, "Capacitor_SMD:C_0805_2012Metric"),
        ("C6", "Device:C", "10nF", 115, 145, "Capacitor_SMD:C_0805_2012Metric"),
        ("C7", "Device:C", "680pF C0G", 135, 145, "Capacitor_SMD:C_0805_2012Metric"),
        ("R1", "Device:R", "21k 1% (300kHz)", 95, 145, "Resistor_SMD:R_0805_2012Metric"),
        ("R2", "Device:R", "14.5k 1%", 215, 105, "Resistor_SMD:R_0805_2012Metric"),
        ("R3", "Device:R", "1.65k 1%", 215, 135, "Resistor_SMD:R_0805_2012Metric"),
        ("R4", "Device:R", "49.9k 1% TUNE", 160, 145, "Resistor_SMD:R_0805_2012Metric"),
        ("C8", "Device:C", "10nF TUNE", 180, 145, "Capacitor_SMD:C_0805_2012Metric"),
        ("C9", "Device:C", "22uF 25V X7R", 225, 85, "Capacitor_SMD:C_1210_3225Metric"),
        ("C10", "Device:C_Polarized", "150uF 25V polymer", 245, 85, "Capacitor_SMD:CP_Elec_10x10"),
        (
            "J2",
            "Connector_Generic:Conn_01x02",
            "12V_OUT_2A",
            270,
            90,
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        ),
        ("#FLG01", "power:PWR_FLAG", "PWR_FLAG", 75, 55, ""),
        ("#FLG02", "power:PWR_FLAG", "PWR_FLAG", 75, 145, ""),
    )
    operations: list[ChangeOperation] = [
        ChangeOperation(
            kind="add_component",
            target=reference,
            parameters={
                "lib_id": lib_id,
                "value": value,
                "x": x,
                "y": y,
                "footprint": footprint,
            },
        )
        for reference, lib_id, value, x, y, footprint in components
    ]

    metadata = {
        "LCSC": candidate.lcsc,
        "MPN": candidate.mpn,
        "Manufacturer": candidate.manufacturer,
        "Datasheet": "https://www.ti.com/lit/ds/symlink/lm5576.pdf",
        "DesignNote": (
            "48V nominal, 12V/2A continuous, non-isolated; verify VIN transients, "
            "thermal layout and compensation before production"
        ),
    }
    operations.extend(
        ChangeOperation(
            kind="update_property",
            target="U1",
            parameters={"name": name, "value": value, "hidden": True},
        )
        for name, value in metadata.items()
        if value
    )

    nets = {
        "VIN48_RAW": (("J1", "1"), ("F1", "1")),
        "VIN48_PROTECTED": (
            ("F1", "2"),
            ("U1", "3"),
            ("U1", "4"),
            ("C1", "1"),
            ("C2", "1"),
            ("C3", "1"),
            ("#FLG01", "1"),
        ),
        "GND": (
            ("J1", "2"),
            ("C1", "2"),
            ("C2", "2"),
            ("C3", "2"),
            ("U1", "10"),
            ("U1", "13"),
            ("U1", "14"),
            ("U1", "21"),
            ("C4", "2"),
            ("C6", "2"),
            ("C7", "2"),
            ("R1", "2"),
            ("R3", "2"),
            ("C9", "2"),
            ("C10", "2"),
            ("J2", "2"),
            ("#FLG02", "1"),
        ),
        "VCC_7V": (("U1", "1"), ("C4", "1")),
        "SW_300KHZ": (
            ("U1", "17"),
            ("U1", "18"),
            ("U1", "19"),
            ("C5", "2"),
            ("D1", "1"),
            ("L1", "1"),
        ),
        "BST": (("U1", "20"), ("C5", "1")),
        "IS_SENSE": (("D1", "2"), ("U1", "15"), ("U1", "16")),
        "SOFT_START": (("U1", "11"), ("C6", "1")),
        "RAMP": (("U1", "9"), ("C7", "1")),
        "RT_300KHZ": (("U1", "8"), ("R1", "1")),
        "VOUT_12V": (
            ("L1", "2"),
            ("U1", "12"),
            ("C9", "1"),
            ("C10", "1"),
            ("R2", "1"),
            ("J2", "1"),
        ),
        "FB_1V225": (("R2", "2"), ("U1", "7"), ("R3", "1"), ("C8", "2")),
        "COMP": (("U1", "6"), ("R4", "1")),
        "COMP_RC": (("R4", "2"), ("C8", "1")),
    }
    u1_pin_positions = {
        "1": (99.06, 85.09),
        "3": (99.06, 90.17),
        "4": (99.06, 92.71),
        "6": (99.06, 97.79),
        "7": (99.06, 100.33),
        "8": (99.06, 102.87),
        "9": (99.06, 105.41),
        "10": (99.06, 107.95),
        "11": (175.26, 107.95),
        "12": (175.26, 105.41),
        "13": (175.26, 102.87),
        "14": (175.26, 100.33),
        "15": (175.26, 97.79),
        "16": (175.26, 95.25),
        "17": (175.26, 92.71),
        "18": (175.26, 90.17),
        "19": (175.26, 87.63),
        "20": (175.26, 85.09),
        "21": (175.26, 82.55),
    }
    for net, pins in nets.items():
        for reference, pin in pins:
            label_parameters: dict[str, str | int | float | bool]
            if reference == "U1":
                x, y = u1_pin_positions[pin]
                operations.append(
                    ChangeOperation(
                        kind="connect",
                        target=f"stub:U1.{pin}",
                        parameters={"reference": "U1", "pin": pin, "x": x, "y": y},
                    )
                )
                label_parameters = {"text": net, "x": x, "y": y}
            else:
                label_parameters = {"text": net, "reference": reference, "pin": pin}
            operations.append(
                ChangeOperation(
                    kind="label",
                    target=f"{net}:{reference}.{pin}",
                    parameters=label_parameters,
                )
            )
    operations.extend(
        (
            ChangeOperation(
                kind="no_connect",
                target="U1.2",
                parameters={"reference": "U1", "pin": "2"},
            ),
            ChangeOperation(
                kind="no_connect",
                target="U1.5",
                parameters={"reference": "U1", "pin": "5"},
            ),
        )
    )
    return tuple(operations)
