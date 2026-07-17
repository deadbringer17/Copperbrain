"""Read-only deterministic schematic layout and label-spacing inspection."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import kicad_sch_api

from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode, SchematicReadabilityReport


def _label_bounds(label: object) -> tuple[float, float, float, float]:
    position = label.position  # type: ignore[attr-defined]
    text = str(label.text)  # type: ignore[attr-defined]
    size = float(label.size)  # type: ignore[attr-defined]
    rotation = round(float(label.rotation)) % 360  # type: ignore[attr-defined]
    length = max(size, len(text) * size * 0.68)
    if rotation == 0:
        return position.x, position.y - size / 2, position.x + length, position.y + size / 2
    if rotation == 180:
        return position.x - length, position.y - size / 2, position.x, position.y + size / 2
    if rotation == 90:
        return position.x - size / 2, position.y - length, position.x + size / 2, position.y
    if rotation == 270:
        return position.x - size / 2, position.y, position.x + size / 2, position.y + length
    return (
        position.x - length / 2,
        position.y - length / 2,
        position.x + length / 2,
        position.y + length / 2,
    )


def analyze_schematic_readability(schematic_file: Path) -> SchematicReadabilityReport:
    """Measure label attachment, collision, spacing, and used sheet extent."""
    if not schematic_file.is_file() or schematic_file.suffix.lower() != ".kicad_sch":
        raise CopperbrainError(ErrorCode.NOT_FOUND, "Schematic readability source was not found")
    try:
        schematic = kicad_sch_api.load_schematic(str(schematic_file))
    except Exception as exc:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "Schematic readability source could not be parsed",
            details={"reason": str(exc)},
        ) from exc

    components = tuple(schematic.components.all())
    labels = tuple(schematic.labels)
    pin_positions = []
    for component in components:
        definition = component.get_symbol_definition()
        if definition is None:
            continue
        for pin in definition.list_pins():
            position = schematic.get_component_pin_position(component.reference, str(pin["number"]))
            if position is not None:
                pin_positions.append(position)

    directly_on_pins = sum(
        any(
            abs(label.position.x - pin.x) <= 0.001 and abs(label.position.y - pin.y) <= 0.001
            for pin in pin_positions
        )
        for label in labels
    )
    wire_endpoints = tuple(
        (point.x, point.y) for wire in schematic.wires for point in (wire.start, wire.end)
    )
    without_wire_connection = sum(
        not any(
            abs(label.position.x - x) <= 0.001 and abs(label.position.y - y) <= 0.001
            for x, y in wire_endpoints
        )
        for label in labels
    )
    position_counts = Counter(
        (round(label.position.x, 3), round(label.position.y, 3)) for label in labels
    )
    duplicate_positions = sum(count - 1 for count in position_counts.values() if count > 1)
    bounds = tuple(_label_bounds(label) for label in labels)
    overlaps = 0
    for index, left in enumerate(bounds):
        for right in bounds[index + 1 :]:
            overlap_x = min(left[2], right[2]) - max(left[0], right[0])
            overlap_y = min(left[3], right[3]) - max(left[1], right[1])
            if overlap_x > 0.15 and overlap_y > 0.15:
                overlaps += 1

    component_points = tuple((item.position.x, item.position.y) for item in components)
    minimum_spacing = min(
        (
            math.dist(left, right)
            for index, left in enumerate(component_points)
            for right in component_points[index + 1 :]
        ),
        default=None,
    )
    occupied_points = [*component_points]
    occupied_points.extend((bound[0], bound[1]) for bound in bounds)
    occupied_points.extend((bound[2], bound[3]) for bound in bounds)
    min_x = min((item[0] for item in occupied_points), default=0)
    max_x = max((item[0] for item in occupied_points), default=0)
    min_y = min((item[1] for item in occupied_points), default=0)
    max_y = max((item[1] for item in occupied_points), default=0)
    width = max_x - min_x
    height = max_y - min_y

    score = max(
        0.0,
        min(
            100.0,
            100
            - directly_on_pins * 0.6
            - without_wire_connection * 0.8
            - duplicate_positions * 4
            - overlaps * 1.5
            - (5 if width < 250 else 0)
            - (5 if height < 140 else 0),
        ),
    )
    messages: list[str] = []
    if directly_on_pins:
        messages.append(f"{directly_on_pins} labels are attached directly to symbol pins")
    if without_wire_connection:
        messages.append(f"{without_wire_connection} labels have no wire endpoint connection")
    if duplicate_positions:
        messages.append(f"{duplicate_positions} labels share an identical position")
    if overlaps:
        messages.append(f"{overlaps} estimated label bounding boxes overlap")
    if width < 250 or height < 140:
        messages.append(f"Layout uses only {width:.1f} x {height:.1f} mm of the available A3 sheet")
    valid = (
        directly_on_pins == 0
        and without_wire_connection == 0
        and duplicate_positions == 0
        and overlaps == 0
        and width >= 250
        and height >= 140
    )
    return SchematicReadabilityReport(
        schematic_file=schematic_file,
        component_count=len(components),
        label_count=len(labels),
        wire_count=len(schematic.wires),
        labels_directly_on_pins=directly_on_pins,
        labels_without_wire_connection=without_wire_connection,
        duplicate_label_positions=duplicate_positions,
        label_overlap_count=overlaps,
        minimum_component_spacing_mm=(
            round(minimum_spacing, 6) if minimum_spacing is not None else None
        ),
        occupied_width_mm=round(width, 6),
        occupied_height_mm=round(height, 6),
        readability_score=round(score, 2),
        valid=valid,
        messages=tuple(messages),
    )
