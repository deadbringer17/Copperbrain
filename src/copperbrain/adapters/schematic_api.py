"""Validated domain-operation adapter around kicad-sch-api."""

from __future__ import annotations

from pathlib import Path

import kicad_sch_api
from kicad_sch_api.core.pin_utils import get_component_pin_info
from kicad_sch_api.library import get_symbol_cache

from copperbrain.errors import CopperbrainError
from copperbrain.models import ChangeOperation, ErrorCode, ValidationReport


def _number(parameters: dict[str, str | int | float | bool], name: str) -> float:
    value = parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CopperbrainError(ErrorCode.INVALID_INPUT, f"Operation requires numeric {name}")
    return float(value)


def _string(parameters: dict[str, str | int | float | bool], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value:
        raise CopperbrainError(ErrorCode.INVALID_INPUT, f"Operation requires string {name}")
    return value


def _remove_private_library_properties(lib_id: str) -> None:
    """Drop documentation-only properties that kicad-sch-api 0.5.6 serializes incorrectly."""
    symbol = get_symbol_cache().get_symbol(lib_id)
    if symbol is None:
        return
    raw = symbol.raw_kicad_data
    if not isinstance(raw, list):
        return

    def atom(value: object) -> str:
        resolver = getattr(value, "value", None)
        return str(resolver()) if callable(resolver) else str(value)

    symbol.raw_kicad_data = [
        item
        for item in raw
        if not (
            isinstance(item, list)
            and len(item) >= 3
            and atom(item[0]) == "property"
            and atom(item[1]) == "private"
        )
    ]
    symbol.property_positions.pop("private", None)


class SchematicApiAdapter:
    """Apply only allowlisted semantic operations; never accepts raw S-expressions."""

    def apply(self, schematic_path: Path, operations: tuple[ChangeOperation, ...]) -> None:
        project_libraries = schematic_path.parent / "copperbrain-libs"
        if project_libraries.is_dir():
            cache = get_symbol_cache()
            for library in sorted(project_libraries.glob("*.kicad_sym")):
                cache.add_library_path(library)
        schematic = kicad_sch_api.load_schematic(str(schematic_path))
        for loaded_component in schematic.components.all():
            _remove_private_library_properties(loaded_component.lib_id)
        for operation in operations:
            parameters = operation.parameters
            if operation.kind == "add_component":
                _remove_private_library_properties(_string(parameters, "lib_id"))
                schematic.components.add(
                    _string(parameters, "lib_id"),
                    reference=operation.target,
                    value=str(parameters.get("value", "")),
                    position=(_number(parameters, "x"), _number(parameters, "y")),
                    footprint=str(parameters.get("footprint", "")) or None,
                )
            elif operation.kind == "replace_component":
                existing = schematic.components.get(operation.target)
                if existing is None:
                    raise CopperbrainError(
                        ErrorCode.NOT_FOUND,
                        "Replacement target component was not found",
                        details={"reference": operation.target},
                    )
                position = existing.position
                properties = dict(existing.properties)
                schematic.components.remove(operation.target)
                _remove_private_library_properties(_string(parameters, "lib_id"))
                replacement = schematic.components.add(
                    _string(parameters, "lib_id"),
                    reference=operation.target,
                    value=str(parameters.get("value", existing.value)),
                    position=position,
                    footprint=str(parameters.get("footprint", existing.footprint or "")) or None,
                )
                for name, value in properties.items():
                    if name not in {"Reference", "Value", "Footprint"}:
                        replacement.set_property(name, str(value))
            elif operation.kind == "move_component":
                moving_component = schematic.components.get(operation.target)
                if moving_component is None:
                    raise CopperbrainError(
                        ErrorCode.NOT_FOUND,
                        "Schematic layout target component was not found",
                        details={"reference": operation.target},
                    )
                if len(schematic.wires):
                    raise CopperbrainError(
                        ErrorCode.CONFLICT,
                        "Component movement requires a label-only schematic section",
                        actionable_hint=(
                            "Use a dedicated typed wire-aware layout operation for already wired "
                            "schematic sections."
                        ),
                        details={"wire_count": len(schematic.wires)},
                    )
                definition = moving_component.get_symbol_definition()
                if definition is None:
                    raise CopperbrainError(
                        ErrorCode.VALIDATION_FAILED,
                        "Schematic layout symbol definition is unavailable",
                        details={"reference": operation.target},
                    )
                old_pins = tuple(
                    pin_info
                    for pin in definition.list_pins()
                    if (
                        pin_info := get_component_pin_info(
                            moving_component,  # type: ignore[arg-type]
                            str(pin["number"]),
                        )
                    )
                    is not None
                )
                attached_labels = tuple(
                    label
                    for label in schematic.labels
                    if any(
                        abs(label.position.x - pin_position.x) <= 0.001
                        and abs(label.position.y - pin_position.y) <= 0.001
                        and round(label.rotation) % 360 == round(pin_rotation + 180) % 360
                        for pin_position, pin_rotation in old_pins
                    )
                )
                old_x, old_y = moving_component.position.x, moving_component.position.y
                new_x, new_y = _number(parameters, "x"), _number(parameters, "y")
                moving_component.move(new_x, new_y)
                delta_x, delta_y = new_x - old_x, new_y - old_y
                for label in attached_labels:
                    label.move(label.position.x + delta_x, label.position.y + delta_y)
            elif operation.kind == "relayout_pin_label":
                label_component = schematic.components.get(_string(parameters, "reference"))
                pin_number = _string(parameters, "pin")
                text = _string(parameters, "text")
                if label_component is None:
                    raise CopperbrainError(
                        ErrorCode.NOT_FOUND,
                        "Schematic label layout component was not found",
                        details={"reference": parameters.get("reference")},
                    )
                layout_pin_info = get_component_pin_info(
                    label_component,  # type: ignore[arg-type]
                    pin_number,
                )
                if layout_pin_info is None:
                    raise CopperbrainError(
                        ErrorCode.NOT_FOUND,
                        "Schematic label layout pin was not found",
                        details={
                            "reference": parameters.get("reference"),
                            "pin": pin_number,
                        },
                    )
                pin_position, pin_rotation = layout_pin_info
                matches = tuple(
                    label
                    for label in schematic.labels
                    if label.text == text
                    and abs(label.position.x - pin_position.x) <= 0.001
                    and abs(label.position.y - pin_position.y) <= 0.001
                )
                if not matches:
                    raise CopperbrainError(
                        ErrorCode.NOT_FOUND,
                        "Existing pin label was not found at the expected pin position",
                        details={
                            "reference": parameters.get("reference"),
                            "pin": pin_number,
                            "text": text,
                        },
                    )
                length = _number(parameters, "stub_length_mm")
                if length < 2.54 or length > 25.4:
                    raise CopperbrainError(
                        ErrorCode.INVALID_INPUT,
                        "Schematic label stub length must be between 2.54 and 25.4 mm",
                    )
                outward_rotation = (pin_rotation + 180) % 360
                direction = {
                    0.0: (1.0, 0.0),
                    90.0: (0.0, -1.0),
                    180.0: (-1.0, 0.0),
                    270.0: (0.0, 1.0),
                }.get(outward_rotation)
                if direction is None:
                    raise CopperbrainError(
                        ErrorCode.VALIDATION_FAILED,
                        "Only orthogonal schematic pins support automatic label relayout",
                    )
                end = (
                    pin_position.x + direction[0] * length,
                    pin_position.y + direction[1] * length,
                )
                schematic.labels.remove(matches[0].uuid)
                duplicate = any(
                    label.text == text
                    and abs(label.position.x - end[0]) <= 0.001
                    and abs(label.position.y - end[1]) <= 0.001
                    for label in schematic.labels
                )
                wire_exists = any(
                    (
                        abs(wire.start.x - pin_position.x) <= 0.001
                        and abs(wire.start.y - pin_position.y) <= 0.001
                        and abs(wire.end.x - end[0]) <= 0.001
                        and abs(wire.end.y - end[1]) <= 0.001
                    )
                    or (
                        abs(wire.end.x - pin_position.x) <= 0.001
                        and abs(wire.end.y - pin_position.y) <= 0.001
                        and abs(wire.start.x - end[0]) <= 0.001
                        and abs(wire.start.y - end[1]) <= 0.001
                    )
                    for wire in schematic.wires
                )
                if not wire_exists:
                    schematic.add_wire((pin_position.x, pin_position.y), end)
                if not duplicate:
                    schematic.add_label(text, position=end, rotation=outward_rotation)
            elif operation.kind == "update_property":
                component = schematic.components.get(operation.target)
                if component is None:
                    raise CopperbrainError(
                        ErrorCode.NOT_FOUND,
                        "Property target component was not found",
                        details={"reference": operation.target},
                    )
                name = _string(parameters, "name")
                value = _string(parameters, "value")
                hidden = parameters.get("hidden") is True
                # kicad-sch-api stores these standard fields separately from
                # custom properties. Using add_property() for them updates only
                # the property map and the preserved S-expression wins on save.
                if name == "Footprint":
                    component.footprint = value
                    component.set_property_effects(name, {"visible": not hidden})
                elif name == "Value":
                    component.value = value
                    component.set_property_effects(name, {"visible": not hidden})
                else:
                    component.add_property(name, value, hidden=hidden)
            elif operation.kind == "connect":
                if "from_reference" in parameters and "to_reference" in parameters:
                    schematic.add_wire_between_pins(
                        _string(parameters, "from_reference"),
                        _string(parameters, "from_pin"),
                        _string(parameters, "to_reference"),
                        _string(parameters, "to_pin"),
                    )
                elif "reference" in parameters:
                    result = schematic.add_wire_to_pin(
                        (_number(parameters, "x"), _number(parameters, "y")),
                        _string(parameters, "reference"),
                        _string(parameters, "pin"),
                    )
                    if result is None:
                        raise CopperbrainError(
                            ErrorCode.NOT_FOUND,
                            "Wire target pin was not found",
                            details={
                                "reference": parameters.get("reference"),
                                "pin": parameters.get("pin"),
                            },
                        )
                else:
                    schematic.add_wire(
                        (_number(parameters, "x1"), _number(parameters, "y1")),
                        (_number(parameters, "x2"), _number(parameters, "y2")),
                    )
            elif operation.kind == "label":
                if "reference" in parameters:
                    schematic.add_label(
                        _string(parameters, "text"),
                        pin=(_string(parameters, "reference"), _string(parameters, "pin")),
                    )
                else:
                    schematic.add_label(
                        _string(parameters, "text"),
                        position=(_number(parameters, "x"), _number(parameters, "y")),
                    )
            elif operation.kind == "no_connect":
                no_connect_position = schematic.get_component_pin_position(
                    _string(parameters, "reference"), _string(parameters, "pin")
                )
                if no_connect_position is None:
                    raise CopperbrainError(
                        ErrorCode.NOT_FOUND,
                        "No-connect target pin was not found",
                        details={
                            "reference": parameters.get("reference"),
                            "pin": parameters.get("pin"),
                        },
                    )
                schematic.no_connects.add(no_connect_position)
            elif operation.kind == "set_paper_size":
                paper = _string(parameters, "paper")
                if paper not in {"A4", "A3", "A2", "A1", "A0"}:
                    raise CopperbrainError(
                        ErrorCode.INVALID_INPUT,
                        "Unsupported schematic paper size",
                        details={"paper": paper},
                    )
                schematic.set_paper_size(paper)
            else:  # pragma: no cover - Pydantic rejects this before the adapter
                raise CopperbrainError(ErrorCode.INVALID_INPUT, "Unsupported schematic operation")
        schematic.save(schematic_path, preserve_format=True)

    def validate(self, schematic_path: Path) -> ValidationReport:
        """Parse the generated schematic and normalize library validation issues."""
        try:
            header = schematic_path.read_text(encoding="utf-8-sig")[:128].lstrip()
            if not header.startswith("(kicad_sch"):
                raise ValueError("missing kicad_sch root expression")
            schematic = kicad_sch_api.load_schematic(str(schematic_path))
            issues = schematic.validate()
        except Exception as exc:
            return ValidationReport(
                valid=False,
                checks={"parse": False},
                messages=(f"Parser rejected generated schematic: {exc}",),
            )
        errors = [
            issue
            for issue in issues
            if getattr(getattr(issue, "level", None), "value", None) == "error"
        ]
        return ValidationReport(
            valid=not errors,
            checks={"parse": True, "library_validation": not errors},
            messages=tuple(str(issue) for issue in issues),
        )
