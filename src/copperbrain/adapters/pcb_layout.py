"""Typed, headless PCB initialization from a validated schematic netlist."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import sexpdata  # type: ignore[import-untyped]

from copperbrain.adapters.footprint_geometry import resolve_footprint
from copperbrain.errors import CopperbrainError
from copperbrain.models import Component, ErrorCode, Net, PcbLayoutPlan, PlacementOperation


def _name(value: object) -> str:
    return value.value() if isinstance(value, sexpdata.Symbol) else str(value)


def _forms(node: list[object], name: str) -> list[list[object]]:
    return [item for item in node[1:] if isinstance(item, list) and item and _name(item[0]) == name]


def _layer(node: list[object]) -> str | None:
    forms = _forms(node, "layer")
    return str(forms[0][1]) if forms and len(forms[0]) > 1 else None


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    try:
        Path(temporary).write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _set_property(footprint: list[object], name: str, value: str) -> None:
    for prop in _forms(footprint, "property"):
        if len(prop) >= 3 and str(prop[1]) == name:
            prop[2] = value
            return
    raise CopperbrainError(
        ErrorCode.VALIDATION_FAILED,
        "Footprint template is missing a required property",
        details={"property": name},
    )


def _instantiate(
    source: Path,
    *,
    library_id: str,
    reference: str,
    value: str,
    placement: PlacementOperation,
    pin_nets: dict[tuple[str, str], str],
) -> list[object]:
    try:
        footprint = sexpdata.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError) as exc:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "Footprint template could not be parsed",
            details={"path": str(source), "reason": str(exc)},
        ) from exc
    if not isinstance(footprint, list) or not footprint or _name(footprint[0]) != "footprint":
        raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Invalid footprint template")
    footprint[1] = library_id
    _set_property(footprint, "Reference", reference)
    _set_property(footprint, "Value", value)
    footprint[:] = [
        child
        for child in footprint
        if not (isinstance(child, list) and child and _name(child[0]) == "at")
    ]
    footprint.append(
        [
            sexpdata.Symbol("at"),
            placement.x_mm,
            placement.y_mm,
            placement.rotation_deg,
        ]
    )
    for pad in _forms(footprint, "pad"):
        pad[:] = [
            child
            for child in pad
            if not (isinstance(child, list) and child and _name(child[0]) == "net")
        ]
        if len(pad) < 2:
            continue
        net = pin_nets.get((reference, str(pad[1])))
        if net:
            pad.append([sexpdata.Symbol("net"), net])
    return footprint


class PcbLayoutAdapter:
    """Build only an empty board using installed allowlisted footprint templates."""

    def compose(
        self,
        pcb: Path,
        project_root: Path,
        components: tuple[Component, ...],
        nets: tuple[Net, ...],
        plan: PcbLayoutPlan,
    ) -> None:
        try:
            tree = sexpdata.loads(pcb.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Initialized PCB could not be parsed",
                details={"reason": str(exc)},
            ) from exc
        if not isinstance(tree, list) or not tree or _name(tree[0]) != "kicad_pcb":
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "File is not a KiCad PCB")
        forbidden = {"footprint", "segment", "arc", "via", "zone"}
        if any(_forms(tree, name) for name in forbidden) or any(
            _layer(item) == "Edge.Cuts"
            for item in tree[1:]
            if isinstance(item, list) and item and _name(item[0]).startswith("gr_")
        ):
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Headless PCB initialization requires an empty board without Edge.Cuts",
            )

        by_reference = {item.reference: item for item in components}
        placements = {item.reference: item for item in plan.placements}
        if set(placements) != set(by_reference):
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "Layout must place every schematic component exactly once",
                details={
                    "missing": sorted(set(by_reference) - set(placements)),
                    "unknown": sorted(set(placements) - set(by_reference)),
                },
            )
        unknown_overrides = sorted(set(plan.footprint_overrides) - set(by_reference))
        if unknown_overrides:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "Footprint overrides contain unknown references",
                details={"references": unknown_overrides},
            )

        outline = plan.outline
        tree.append(
            [
                sexpdata.Symbol("gr_rect"),
                [sexpdata.Symbol("start"), outline.min_x_mm, outline.min_y_mm],
                [
                    sexpdata.Symbol("end"),
                    outline.min_x_mm + outline.width_mm,
                    outline.min_y_mm + outline.height_mm,
                ],
                [
                    sexpdata.Symbol("stroke"),
                    [sexpdata.Symbol("width"), outline.line_width_mm],
                    [sexpdata.Symbol("type"), sexpdata.Symbol("default")],
                ],
                [sexpdata.Symbol("fill"), sexpdata.Symbol("none")],
                [sexpdata.Symbol("layer"), "Edge.Cuts"],
            ]
        )
        pin_nets = {
            (pin.reference, pin.pin): net.name for net in nets for pin in net.pins if net.name
        }
        for reference in sorted(by_reference):
            component = by_reference[reference]
            library_id = plan.footprint_overrides.get(reference, component.footprint or "")
            source = resolve_footprint(project_root, library_id)
            if source is None:
                raise CopperbrainError(
                    ErrorCode.NOT_FOUND,
                    "Component footprint could not be resolved",
                    details={"reference": reference, "footprint": library_id},
                )
            tree.append(
                _instantiate(
                    source,
                    library_id=library_id,
                    reference=reference,
                    value=component.value,
                    placement=placements[reference],
                    pin_nets=pin_nets,
                )
            )

        hole_id = "MountingHole:MountingHole_3.2mm_M3"
        hole_source = resolve_footprint(project_root, hole_id)
        if plan.mounting_holes and hole_source is None:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "M3 mounting-hole footprint is unavailable")
        for hole in plan.mounting_holes:
            assert hole_source is not None
            tree.append(
                _instantiate(
                    hole_source,
                    library_id=hole_id,
                    reference=hole.reference,
                    value="MountingHole_3.2mm_M3",
                    placement=PlacementOperation(
                        reference=hole.reference, x_mm=hole.x_mm, y_mm=hole.y_mm
                    ),
                    pin_nets={},
                )
            )
        _atomic_write(pcb, sexpdata.dumps(tree))
