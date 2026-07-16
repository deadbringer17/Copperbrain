"""Validated domain-operation adapter around kicad-sch-api."""

from __future__ import annotations

from pathlib import Path

import kicad_sch_api
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


class SchematicApiAdapter:
    """Apply only allowlisted semantic operations; never accepts raw S-expressions."""

    def apply(self, schematic_path: Path, operations: tuple[ChangeOperation, ...]) -> None:
        project_libraries = schematic_path.parent / "copperbrain-libs"
        if project_libraries.is_dir():
            cache = get_symbol_cache()
            for library in sorted(project_libraries.glob("*.kicad_sym")):
                cache.add_library_path(library)
        schematic = kicad_sch_api.load_schematic(str(schematic_path))
        for operation in operations:
            parameters = operation.parameters
            if operation.kind == "add_component":
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
                pin_position = schematic.get_component_pin_position(
                    _string(parameters, "reference"), _string(parameters, "pin")
                )
                if pin_position is None:
                    raise CopperbrainError(
                        ErrorCode.NOT_FOUND,
                        "No-connect target pin was not found",
                        details={
                            "reference": parameters.get("reference"),
                            "pin": parameters.get("pin"),
                        },
                    )
                schematic.no_connects.add(pin_position)
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
