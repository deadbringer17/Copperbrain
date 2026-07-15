"""Deterministic operation template for the bounded 12 V to 5 V / 2 A demo."""

from __future__ import annotations

from copperbrain.models import ChangeOperation, ComponentCandidate


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
