"""Typed KiCad PCB inspection, placement editing, and optional live IPC access."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sexpdata  # type: ignore[import-untyped]

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ErrorCode,
    IntegrationStatus,
    PcbBounds,
    PcbFootprintPlacement,
    PcbNetInspection,
    PcbPadInspection,
    PcbSummary,
    PlacementOperation,
    RouteSegment,
    RouteVia,
    RoutingAnalysis,
    UnroutedConnection,
    ValidationReport,
)


def _name(value: object) -> str:
    return value.value() if isinstance(value, sexpdata.Symbol) else str(value)


def _forms(node: list[object], name: str) -> list[list[object]]:
    return [item for item in node[1:] if isinstance(item, list) and item and _name(item[0]) == name]


def _first(node: list[object], name: str) -> list[object] | None:
    matches = _forms(node, name)
    return matches[0] if matches else None


def _number(value: object, default: float = 0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _resolve_net_name(value: object, code_to_name: dict[int, str]) -> str | None:
    """Accept both legacy numeric net IDs and KiCad 10 name-valued copper fields."""
    raw = str(value)
    if re.fullmatch(r"\d+", raw):
        return code_to_name.get(int(raw))
    return raw or None


def _xy(form: list[object] | None) -> tuple[float, float] | None:
    if form is None or len(form) < 3:
        return None
    return _number(form[1]), _number(form[2])


@dataclass(frozen=True)
class _ParsedBoard:
    tree: list[object]
    summary: PcbSummary
    nets: dict[str, PcbNetInspection]
    pads: tuple[PcbPadInspection, ...]
    segments: tuple[list[object], ...]
    vias: tuple[list[object], ...]


def _footprint_reference(node: list[object]) -> str | None:
    for prop in _forms(node, "property"):
        if len(prop) >= 3 and str(prop[1]) == "Reference":
            return str(prop[2])
    for text in _forms(node, "fp_text"):
        if len(text) >= 3 and _name(text[1]) == "reference":
            return str(text[2])
    return None


def _footprint_value(node: list[object]) -> str | None:
    for prop in _forms(node, "property"):
        if len(prop) >= 3 and str(prop[1]) == "Value":
            return str(prop[2])
    for text in _forms(node, "fp_text"):
        if len(text) >= 3 and _name(text[1]) == "value":
            return str(text[2])
    return None


def _layer(node: list[object]) -> str:
    form = _first(node, "layer")
    return str(form[1]) if form and len(form) > 1 else "F.Cu"


def _graphic_points(node: list[object]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for key in ("start", "end", "center", "mid", "xy"):
        for form in _forms(node, key):
            point = _xy(form)
            if point is not None:
                points.append(point)
    pts = _first(node, "pts")
    if pts:
        for form in _forms(pts, "xy"):
            point = _xy(form)
            if point is not None:
                points.append(point)
    return points


def _local_footprint_bounds(node: list[object]) -> tuple[float, float, float, float]:
    courtyard: list[tuple[float, float]] = []
    general: list[tuple[float, float]] = []
    for child in node[1:]:
        if not isinstance(child, list) or not child:
            continue
        child_name = _name(child[0])
        if child_name.startswith("fp_"):
            if child_name == "fp_circle":
                center = _xy(_first(child, "center"))
                end = _xy(_first(child, "end"))
                if center is not None and end is not None:
                    radius = math.dist(center, end)
                    points = [
                        (center[0] - radius, center[1] - radius),
                        (center[0] + radius, center[1] + radius),
                    ]
                else:
                    points = _graphic_points(child)
            else:
                points = _graphic_points(child)
            general.extend(points)
            if _layer(child) in {"F.CrtYd", "B.CrtYd"}:
                courtyard.extend(points)
        elif child_name == "pad":
            position = _xy(_first(child, "at")) or (0, 0)
            size = _xy(_first(child, "size")) or (1, 1)
            general.extend(
                (
                    (position[0] - size[0] / 2, position[1] - size[1] / 2),
                    (position[0] + size[0] / 2, position[1] + size[1] / 2),
                )
            )
    points = courtyard or general or [(-0.5, -0.5), (0.5, 0.5)]
    min_x, min_y = min(point[0] for point in points), min(point[1] for point in points)
    max_x, max_y = max(point[0] for point in points), max(point[1] for point in points)
    if min_x >= max_x:
        min_x, max_x = min_x - 0.05, max_x + 0.05
    if min_y >= max_y:
        min_y, max_y = min_y - 0.05, max_y + 0.05
    return min_x, min_y, max_x, max_y


def _absolute_bounds(
    local: tuple[float, float, float, float], x: float, y: float, angle: float
) -> PcbBounds:
    radians = math.radians(angle)
    cosine, sine = math.cos(radians), math.sin(radians)
    corners = (
        (local[0], local[1]),
        (local[0], local[3]),
        (local[2], local[1]),
        (local[2], local[3]),
    )
    transformed = tuple(
        (x + px * cosine + py * sine, y - px * sine + py * cosine) for px, py in corners
    )
    return PcbBounds(
        min_x_mm=min(point[0] for point in transformed),
        min_y_mm=min(point[1] for point in transformed),
        max_x_mm=max(point[0] for point in transformed),
        max_y_mm=max(point[1] for point in transformed),
    )


def _absolute_point(
    local_x: float, local_y: float, x: float, y: float, angle: float
) -> tuple[float, float]:
    radians = math.radians(angle)
    cosine, sine = math.cos(radians), math.sin(radians)
    return x + local_x * cosine + local_y * sine, y - local_x * sine + local_y * cosine


def _copper_layers(form: list[object] | None) -> tuple[str, ...]:
    raw = tuple(str(item) for item in form[1:]) if form else ()
    if "*.Cu" in raw:
        return ("F.Cu", "B.Cu")
    return tuple(item for item in raw if item.endswith(".Cu"))


def _board_bounds(tree: list[object]) -> PcbBounds | None:
    points: list[tuple[float, float]] = []
    for child in tree[1:]:
        if not isinstance(child, list) or not child or not _name(child[0]).startswith("gr_"):
            continue
        if _layer(child) == "Edge.Cuts":
            points.extend(_graphic_points(child))
    if len(points) < 2:
        return None
    min_x, min_y = min(x for x, _ in points), min(y for _, y in points)
    max_x, max_y = max(x for x, _ in points), max(y for _, y in points)
    if min_x >= max_x or min_y >= max_y:
        return None
    return PcbBounds(min_x_mm=min_x, min_y_mm=min_y, max_x_mm=max_x, max_y_mm=max_y)


class PcbFileAdapter:
    """Inspect and apply only allowlisted placement operations to KiCad PCB files."""

    def parse(self, pcb: Path, session_id: str = "") -> _ParsedBoard:
        try:
            text = pcb.read_text(encoding="utf-8-sig")
            tree = sexpdata.loads(text)
        except (OSError, ValueError, TypeError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad PCB could not be parsed",
                details={"reason": str(exc), "path": str(pcb)},
            ) from exc
        if not isinstance(tree, list) or not tree or _name(tree[0]) != "kicad_pcb":
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "File is not a KiCad PCB")

        net_names: dict[int, str] = {}
        net_codes_by_name: dict[str, int] = {}
        footprints: list[PcbFootprintPlacement] = []
        pads: list[PcbPadInspection] = []
        pads_by_net: dict[int, list[str]] = {}
        layers_by_net: dict[int, set[str]] = {}
        for net in _forms(tree, "net"):
            if len(net) >= 3:
                code, name = int(_number(net[1])), str(net[2])
                net_names[code] = name
                net_codes_by_name[name] = code

        def net_code(net_form: list[object] | None) -> int | None:
            """Resolve both KiCad 9 coded nets and KiCad 10 name-only nets."""
            if net_form is None or len(net_form) < 2:
                return None
            if len(net_form) >= 3:
                code, name = int(_number(net_form[1])), str(net_form[2])
                net_names.setdefault(code, name)
                net_codes_by_name.setdefault(name, code)
                return code
            raw = net_form[1]
            if isinstance(raw, (int, float)) or str(raw).lstrip("-").isdigit():
                return int(_number(raw))
            name = str(raw)
            if name not in net_codes_by_name:
                code = max(net_names, default=0) + 1
                net_codes_by_name[name] = code
                net_names[code] = name
            return net_codes_by_name[name]

        for node in _forms(tree, "footprint"):
            reference = _footprint_reference(node)
            if not reference:
                continue
            at = _first(node, "at") or ["at", 0, 0, 0]
            x, y = _number(at[1]), _number(at[2])
            rotation = _number(at[3]) if len(at) > 3 else 0
            local = _local_footprint_bounds(node)
            layer = _layer(node)
            pad_kinds = {_name(pad[2]) for pad in _forms(node, "pad") if len(pad) > 2}
            has_smd = "smd" in pad_kinds
            has_through_hole = bool({"thru_hole", "np_thru_hole"} & pad_kinds)
            mount_type = (
                "mixed"
                if has_smd and has_through_hole
                else "through_hole"
                if has_through_hole
                else "smd"
                if has_smd
                else "unknown"
            )
            footprints.append(
                PcbFootprintPlacement(
                    reference=reference,
                    footprint=str(node[1]) if len(node) > 1 else "",
                    value=_footprint_value(node),
                    x_mm=x,
                    y_mm=y,
                    rotation_deg=rotation,
                    layer=layer if layer in {"F.Cu", "B.Cu"} else "F.Cu",  # type: ignore[arg-type]
                    mount_type=mount_type,  # type: ignore[arg-type]
                    locked=any(
                        _name(item) == "locked" for item in node if not isinstance(item, list)
                    ),
                    bounds=_absolute_bounds(local, x, y, rotation),
                    local_bounds=PcbBounds(
                        min_x_mm=local[0],
                        min_y_mm=local[1],
                        max_x_mm=local[2],
                        max_y_mm=local[3],
                    ),
                )
            )
            for pad in _forms(node, "pad"):
                resolved_code = net_code(_first(pad, "net"))
                layers = _first(pad, "layers")
                copper_layers = _copper_layers(layers)
                if not copper_layers:
                    continue
                if resolved_code is not None:
                    pads_by_net.setdefault(resolved_code, []).append(reference)
                    if layers:
                        layers_by_net.setdefault(resolved_code, set()).update(
                            str(item) for item in layers[1:]
                        )
                pad_at = _first(pad, "at")
                local_at = _xy(pad_at) or (0, 0)
                pad_rotation = _number(pad_at[3]) if pad_at is not None and len(pad_at) > 3 else 0
                absolute = _absolute_point(local_at[0], local_at[1], x, y, rotation)
                size = _xy(_first(pad, "size")) or (1, 1)
                pads.append(
                    PcbPadInspection(
                        reference=reference,
                        number=str(pad[1]) if len(pad) > 1 else "",
                        net=net_names[resolved_code] if resolved_code is not None else "",
                        x_mm=round(absolute[0], 6),
                        y_mm=round(absolute[1], 6),
                        width_mm=size[0],
                        height_mm=size[1],
                        rotation_deg=(rotation + pad_rotation) % 360,
                        layers=copper_layers,  # type: ignore[arg-type]
                    )
                )

        tracks_by_net: dict[int, list[list[object]]] = {}
        vias_by_net: dict[int, list[list[object]]] = {}
        for segment in _forms(tree, "segment"):
            resolved_code = net_code(_first(segment, "net"))
            if resolved_code is not None:
                tracks_by_net.setdefault(resolved_code, []).append(segment)
                layers_by_net.setdefault(resolved_code, set()).add(_layer(segment))
        for via in _forms(tree, "via"):
            resolved_code = net_code(_first(via, "net"))
            if resolved_code is not None:
                vias_by_net.setdefault(resolved_code, []).append(via)

        nets: dict[str, PcbNetInspection] = {}
        for code, name in net_names.items():
            if not name:
                continue
            length = 0.0
            for segment in tracks_by_net.get(code, []):
                start, end = _xy(_first(segment, "start")), _xy(_first(segment, "end"))
                if start and end:
                    length += math.dist(start, end)
            references = pads_by_net.get(code, [])
            nets[name] = PcbNetInspection(
                session_id=session_id,
                net=name,
                code=code,
                connected_references=tuple(sorted(set(references))),
                pad_count=len(references),
                track_count=len(tracks_by_net.get(code, [])),
                via_count=len(vias_by_net.get(code, [])),
                routed_length_mm=round(length, 6),
                layers=tuple(sorted(layers_by_net.get(code, set()))),
            )
        ipc = KiCadPcbIpcAdapter.status()
        counts = Counter(item.reference for item in footprints)
        duplicates = tuple(sorted(reference for reference, count in counts.items() if count > 1))
        warnings: list[str] = []
        if _board_bounds(tree) is None:
            warnings.append("Closed Edge.Cuts bounds were not detected")
        if duplicates:
            warnings.append(f"Duplicate footprint references detected: {', '.join(duplicates)}")
        summary = PcbSummary(
            session_id=session_id,
            pcb_file=pcb,
            board_bounds=_board_bounds(tree),
            footprints=tuple(sorted(footprints, key=lambda item: item.reference)),
            net_count=len(nets),
            track_count=len(_forms(tree, "segment")),
            via_count=len(_forms(tree, "via")),
            zone_count=len(_forms(tree, "zone")),
            ipc=ipc,
            warnings=tuple(warnings),
        )
        return _ParsedBoard(
            tree=tree,
            summary=summary,
            nets=nets,
            pads=tuple(sorted(pads, key=lambda item: (item.net, item.reference, item.number))),
            segments=tuple(segment for items in tracks_by_net.values() for segment in items),
            vias=tuple(via for items in vias_by_net.values() for via in items),
        )

    def summary(self, pcb: Path, session_id: str) -> PcbSummary:
        return self.parse(pcb, session_id).summary

    def inspect_net(self, pcb: Path, session_id: str, net_name: str) -> PcbNetInspection:
        parsed = self.parse(pcb, session_id)
        try:
            return parsed.nets[net_name]
        except KeyError as exc:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "PCB net was not found", details={"net": net_name}
            ) from exc

    def pads(self, pcb: Path, net_names: tuple[str, ...] = ()) -> tuple[PcbPadInspection, ...]:
        parsed = self.parse(pcb)
        selected = set(net_names)
        return tuple(item for item in parsed.pads if not selected or item.net in selected)

    def routing_items(
        self, pcb: Path, net_names: tuple[str, ...] = ()
    ) -> tuple[tuple[RouteSegment, ...], tuple[RouteVia, ...]]:
        """Return typed existing copper primitives for safe autorouter delta extraction."""
        parsed = self.parse(pcb)
        selected = set(net_names)
        known = set(parsed.nets)
        missing = sorted(selected - known)
        if missing:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "PCB routing nets were not found",
                details={"nets": missing},
            )
        code_to_name = {item.code: item.net for item in parsed.nets.values()}
        segments: list[RouteSegment] = []
        for item in parsed.segments:
            resolved = _first(item, "net")
            net = (
                _resolve_net_name(resolved[1], code_to_name)
                if resolved and len(resolved) > 1
                else None
            )
            start, end = _xy(_first(item, "start")), _xy(_first(item, "end"))
            width = _first(item, "width")
            layer = _layer(item)
            if net is None or start is None or end is None or (selected and net not in selected):
                continue
            if layer not in {"F.Cu", "B.Cu"}:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Controlled routing supports only F.Cu and B.Cu",
                    details={"layer": layer},
                )
            segments.append(
                RouteSegment(
                    net=net,
                    start_x_mm=round(start[0], 6),
                    start_y_mm=round(start[1], 6),
                    end_x_mm=round(end[0], 6),
                    end_y_mm=round(end[1], 6),
                    width_mm=_number(width[1]) if width and len(width) > 1 else 0.25,
                    layer=layer,  # type: ignore[arg-type]
                )
            )
        vias: list[RouteVia] = []
        for item in parsed.vias:
            resolved = _first(item, "net")
            net = (
                _resolve_net_name(resolved[1], code_to_name)
                if resolved and len(resolved) > 1
                else None
            )
            position = _xy(_first(item, "at"))
            size, drill = _first(item, "size"), _first(item, "drill")
            layers = _copper_layers(_first(item, "layers"))
            if net is None or position is None or (selected and net not in selected):
                continue
            if set(layers) != {"F.Cu", "B.Cu"}:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Controlled routing supports only through vias between F.Cu and B.Cu",
                    details={"layers": list(layers)},
                )
            vias.append(
                RouteVia(
                    net=net,
                    x_mm=round(position[0], 6),
                    y_mm=round(position[1], 6),
                    diameter_mm=_number(size[1]) if size and len(size) > 1 else 0.6,
                    drill_mm=_number(drill[1]) if drill and len(drill) > 1 else 0.3,
                    layers=("F.Cu", "B.Cu"),
                )
            )
        return (
            tuple(sorted(segments, key=lambda item: item.model_dump_json())),
            tuple(sorted(vias, key=lambda item: item.model_dump_json())),
        )

    def analyze_routing(
        self, pcb: Path, session_id: str, net_names: tuple[str, ...] = ()
    ) -> RoutingAnalysis:
        parsed = self.parse(pcb, session_id)
        known = set(parsed.nets)
        requested = set(net_names)
        missing = sorted(requested - known)
        if missing:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "PCB routing nets were not found",
                details={"nets": missing},
            )
        selected = requested or known
        pads_by_net: dict[str, list[PcbPadInspection]] = {}
        for pad in parsed.pads:
            if pad.net in selected:
                pads_by_net.setdefault(pad.net, []).append(pad)

        def key(x: float, y: float, layer: str) -> tuple[int, int, str]:
            return round(x * 1_000_000), round(y * 1_000_000), layer

        parent: dict[tuple[int, int, str], tuple[int, int, str]] = {}

        def find(item: tuple[int, int, str]) -> tuple[int, int, str]:
            parent.setdefault(item, item)
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left: tuple[int, int, str], right: tuple[int, int, str]) -> None:
            a, b = find(left), find(right)
            if a != b:
                parent[max(a, b)] = min(a, b)

        code_to_name = {item.code: item.net for item in parsed.nets.values()}
        segment_items: list[
            tuple[
                str,
                str,
                tuple[float, float],
                tuple[float, float],
                float,
                tuple[int, int, str],
            ]
        ] = []
        for segment in parsed.segments:
            net_form = _first(segment, "net")
            if net_form is None or len(net_form) < 2:
                continue
            raw = net_form[1]
            name = _resolve_net_name(raw, code_to_name)
            if name not in selected:
                continue
            start, end = _xy(_first(segment, "start")), _xy(_first(segment, "end"))
            if start and end:
                layer = _layer(segment)
                union(key(*start, layer), key(*end, layer))
                width = _number((_first(segment, "width") or ["width", 0])[1])
                segment_items.append((name, layer, start, end, width, key(*start, layer)))

        def point_segment_distance(
            point: tuple[float, float],
            start: tuple[float, float],
            end: tuple[float, float],
        ) -> float:
            dx, dy = end[0] - start[0], end[1] - start[1]
            length_squared = dx * dx + dy * dy
            if length_squared == 0:
                return math.dist(point, start)
            ratio = max(
                0.0,
                min(
                    1.0,
                    ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared,
                ),
            )
            projection = (start[0] + ratio * dx, start[1] + ratio * dy)
            return math.dist(point, projection)

        def segment_intersects_pad(
            start: tuple[float, float],
            end: tuple[float, float],
            track_width: float,
            pad: PcbPadInspection,
        ) -> bool:
            angle = math.radians(-pad.rotation_deg)
            cosine, sine = math.cos(angle), math.sin(angle)

            def local(point: tuple[float, float]) -> tuple[float, float]:
                dx, dy = point[0] - pad.x_mm, point[1] - pad.y_mm
                return dx * cosine - dy * sine, dx * sine + dy * cosine

            local_start, local_end = local(start), local(end)
            half_x = pad.width_mm / 2 + track_width / 2 + 1e-6
            half_y = pad.height_mm / 2 + track_width / 2 + 1e-6
            direction = (
                local_end[0] - local_start[0],
                local_end[1] - local_start[1],
            )
            minimum, maximum = 0.0, 1.0
            for origin, delta, extent in (
                (local_start[0], direction[0], half_x),
                (local_start[1], direction[1], half_y),
            ):
                if abs(delta) < 1e-12:
                    if origin < -extent or origin > extent:
                        return False
                    continue
                low, high = (-extent - origin) / delta, (extent - origin) / delta
                if low > high:
                    low, high = high, low
                minimum, maximum = max(minimum, low), min(maximum, high)
                if minimum > maximum:
                    return False
            return True

        # Copper segments of the same net and layer connect at endpoints, crossings, and
        # T-junctions. FreeRouting commonly emits a junction on the interior of another segment.
        for index, segment_left in enumerate(segment_items):
            for segment_right in segment_items[index + 1 :]:
                if segment_left[0] != segment_right[0] or segment_left[1] != segment_right[1]:
                    continue
                tolerance = (segment_left[4] + segment_right[4]) / 2 + 1e-6
                if any(
                    point_segment_distance(point, segment_right[2], segment_right[3]) <= tolerance
                    for point in (segment_left[2], segment_left[3])
                ) or any(
                    point_segment_distance(point, segment_left[2], segment_left[3]) <= tolerance
                    for point in (segment_right[2], segment_right[3])
                ):
                    union(segment_left[5], segment_right[5])

        via_items: list[tuple[str, tuple[float, float], float, tuple[str, ...]]] = []
        for via in parsed.vias:
            at = _xy(_first(via, "at"))
            net_form = _first(via, "net")
            name = (
                _resolve_net_name(net_form[1], code_to_name)
                if net_form is not None and len(net_form) > 1
                else None
            )
            if at and name in selected:
                layers = _copper_layers(_first(via, "layers"))
                for layer in layers[1:]:
                    union(key(*at, layers[0]), key(*at, layer))
                size = _number((_first(via, "size") or ["size", 0])[1])
                via_items.append((name, at, size, layers))
                for segment_name, layer, start, end, width, segment_node in segment_items:
                    if (
                        segment_name == name
                        and layer in layers
                        and point_segment_distance(at, start, end) <= (size + width) / 2 + 1e-6
                    ):
                        union(key(*at, layer), segment_node)
        for pad in parsed.pads:
            pad_nodes = [key(pad.x_mm, pad.y_mm, layer) for layer in pad.layers]
            for node in pad_nodes[1:]:
                union(pad_nodes[0], node)
            if pad.net not in selected:
                continue
            for name, layer, start, end, width, segment_node in segment_items:
                if (
                    name == pad.net
                    and layer in pad.layers
                    and segment_intersects_pad(start, end, width, pad)
                ):
                    union(pad_nodes[0], segment_node)
            for name, at, size, layers in via_items:
                if name != pad.net or not set(layers).intersection(pad.layers):
                    continue
                synthetic = PcbPadInspection(
                    reference=pad.reference,
                    number=pad.number,
                    net=pad.net,
                    x_mm=pad.x_mm,
                    y_mm=pad.y_mm,
                    width_mm=pad.width_mm + size,
                    height_mm=pad.height_mm + size,
                    rotation_deg=pad.rotation_deg,
                    layers=pad.layers,
                )
                if segment_intersects_pad(at, at, 0, synthetic):
                    union(pad_nodes[0], key(*at, next(iter(set(layers) & set(pad.layers)))))

        connections: list[UnroutedConnection] = []
        routed_nets = 0
        ignored: list[str] = []
        for net in sorted(selected):
            pads = pads_by_net.get(net, [])
            if len(pads) < 2:
                ignored.append(net)
                continue
            groups: dict[tuple[int, int, str], list[PcbPadInspection]] = {}
            for pad in pads:
                root = find(key(pad.x_mm, pad.y_mm, pad.layers[0]))
                groups.setdefault(root, []).append(pad)
            components = [
                sorted(items, key=lambda p: (p.reference, p.number)) for items in groups.values()
            ]
            components.sort(key=lambda items: (items[0].reference, items[0].number))
            while len(components) > 1:
                best: tuple[float, int, int, PcbPadInspection, PcbPadInspection] | None = None
                for left_index, left in enumerate(components):
                    for right_index in range(left_index + 1, len(components)):
                        for pad_start in left:
                            for pad_end in components[right_index]:
                                candidate = (
                                    math.dist(
                                        (pad_start.x_mm, pad_start.y_mm),
                                        (pad_end.x_mm, pad_end.y_mm),
                                    ),
                                    left_index,
                                    right_index,
                                    pad_start,
                                    pad_end,
                                )
                                if best is None or candidate[:3] < best[:3]:
                                    best = candidate
                assert best is not None
                distance, left_index, right_index, pad_start, pad_end = best
                connections.append(
                    UnroutedConnection(
                        net=net,
                        start_reference=pad_start.reference,
                        start_pad=pad_start.number,
                        end_reference=pad_end.reference,
                        end_pad=pad_end.number,
                        distance_mm=round(distance, 6),
                    )
                )
                components[left_index].extend(components.pop(right_index))
            if not any(item.net == net for item in connections):
                routed_nets += 1
        active_nets = len(selected - set(ignored))
        unrouted_nets = len({item.net for item in connections})
        return RoutingAnalysis(
            session_id=session_id,
            complete=not connections,
            net_count=active_nets,
            routed_net_count=routed_nets,
            unrouted_net_count=unrouted_nets,
            unrouted_connection_count=len(connections),
            unrouted_connections=tuple(connections),
            ignored_single_pad_nets=tuple(sorted(ignored)),
            assumptions=(
                "Connectivity is established from copper contact among rotated pads, tracks, "
                "junctions, and vias",
                "Single-pad nets do not require copper routing",
            ),
        )

    def validate(self, pcb: Path) -> ValidationReport:
        try:
            parsed = self.parse(pcb)
        except CopperbrainError as exc:
            return ValidationReport(
                valid=False,
                checks={"pcb_parse": False, "unique_references": False},
                messages=(str(exc),),
            )
        references = [item.reference for item in parsed.summary.footprints]
        unique = len(references) == len(set(references))
        return ValidationReport(
            valid=unique,
            checks={"pcb_parse": True, "unique_references": unique},
            messages=() if unique else ("PCB contains duplicate footprint references",),
        )

    def apply_placement(self, pcb: Path, operations: tuple[PlacementOperation, ...]) -> None:
        parsed = self.parse(pcb)
        counts = Counter(item.reference for item in parsed.summary.footprints)
        known = {item.reference: item for item in parsed.summary.footprints}
        missing = sorted({item.reference for item in operations} - set(known))
        ambiguous = sorted(item.reference for item in operations if counts[item.reference] > 1)
        locked = sorted(
            item.reference
            for item in operations
            if known.get(item.reference) and known[item.reference].locked
        )
        if missing:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Placement references were not found",
                details={"references": missing},
            )
        if ambiguous:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Duplicate footprint references make placement ambiguous",
                details={"references": ambiguous},
            )
        if locked:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Locked footprints cannot be moved",
                details={"references": locked},
            )
        side_changes = sorted(
            item.reference
            for item in operations
            if item.layer is not None and item.layer != known[item.reference].layer
        )
        if side_changes:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "Changing a footprint side is outside the placement extension",
                actionable_hint="Flip the footprint in KiCad and reopen the project session.",
                details={"references": side_changes},
            )
        text = pcb.read_text(encoding="utf-8-sig")
        blocks = _footprint_blocks(text)
        replacements: list[tuple[int, int, str]] = []
        by_reference = {item.reference: item for item in operations}
        for start, end in blocks:
            block = text[start:end]
            try:
                node = sexpdata.loads(block)
            except (ValueError, TypeError):
                continue
            if not isinstance(node, list):
                continue
            reference = _footprint_reference(node)
            operation = by_reference.get(reference or "")
            if operation is None:
                continue
            at_span = _direct_form_span(block, "at")
            if at_span is None:
                raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Footprint has no placement")
            replacement = (
                f"(at {_format_number(operation.x_mm)} {_format_number(operation.y_mm)} "
                f"{_format_number(operation.rotation_deg)})"
            )
            changed = block[: at_span[0]] + replacement + block[at_span[1] :]
            replacements.append((start, end, changed))
        if len(replacements) != len(operations):
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED, "Not all placement operations applied"
            )
        for start, end, replacement in reversed(replacements):
            text = text[:start] + replacement + text[end:]
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{pcb.name}.", dir=pcb.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            os.replace(temporary, pcb)
        finally:
            if temporary.exists():
                temporary.unlink()

    def apply_routing(
        self,
        pcb: Path,
        segments: tuple[RouteSegment, ...],
        vias: tuple[RouteVia, ...],
    ) -> None:
        """Append only typed copper primitives; callers cannot provide KiCad expressions."""
        if not segments and not vias:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT, "At least one routing operation is required"
            )
        parsed = self.parse(pcb)
        known = set(parsed.nets)
        requested = {item.net for item in segments} | {item.net for item in vias}
        missing = sorted(requested - known)
        if missing:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Routing operations reference unknown nets",
                details={"nets": missing},
            )
        if parsed.summary.board_bounds is not None:
            bounds = parsed.summary.board_bounds
            points = [
                (point[0], point[1])
                for item in segments
                for point in (
                    (item.start_x_mm, item.start_y_mm),
                    (item.end_x_mm, item.end_y_mm),
                )
            ] + [(item.x_mm, item.y_mm) for item in vias]
            outside = [
                point
                for point in points
                if not (
                    bounds.min_x_mm <= point[0] <= bounds.max_x_mm
                    and bounds.min_y_mm <= point[1] <= bounds.max_y_mm
                )
            ]
            if outside:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Routing operations must remain inside Edge.Cuts bounds",
                    details={"points": outside},
                )

        root_codes = {
            str(item[2]): int(_number(item[1]))
            for item in _forms(parsed.tree, "net")
            if len(item) >= 3
        }

        def net_clause(name: str) -> str:
            code = root_codes.get(name)
            return f"(net {code})" if code is not None else f"(net {json.dumps(name)})"

        lines: list[str] = []
        for segment in segments:
            lines.append(
                "  (segment "
                f"(start {_format_number(segment.start_x_mm)} "
                f"{_format_number(segment.start_y_mm)}) "
                f"(end {_format_number(segment.end_x_mm)} "
                f"{_format_number(segment.end_y_mm)}) "
                f"(width {_format_number(segment.width_mm)}) "
                f"(layer {json.dumps(segment.layer)}) {net_clause(segment.net)})"
            )
        for via in vias:
            lines.append(
                "  (via "
                f"(at {_format_number(via.x_mm)} {_format_number(via.y_mm)}) "
                f"(size {_format_number(via.diameter_mm)}) "
                f"(drill {_format_number(via.drill_mm)}) "
                f"(layers {json.dumps(via.layers[0])} {json.dumps(via.layers[1])}) "
                f"{net_clause(via.net)})"
            )
        text = pcb.read_text(encoding="utf-8-sig")
        closing = text.rfind(")")
        if closing < 0:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "KiCad PCB has no closing form")
        addition = "\n" + "\n".join(lines) + "\n"
        changed = text[:closing].rstrip() + addition + text[closing:]
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{pcb.name}.", dir=pcb.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(changed, encoding="utf-8", newline="\n")
            self.parse(temporary)
            os.replace(temporary, pcb)
        finally:
            if temporary.exists():
                temporary.unlink()


def _format_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _footprint_blocks(text: str) -> list[tuple[int, int]]:
    starts = [match.start() for match in re.finditer(r"(?m)^\s*\(footprint(?:\s|\")", text)]
    blocks: list[tuple[int, int]] = []
    for raw_start in starts:
        start = text.find("(", raw_start)
        end = _balanced_end(text, start)
        if end is not None:
            blocks.append((start, end))
    return blocks


def _balanced_end(text: str, start: int) -> int | None:
    depth, quoted, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _direct_form_span(block: str, name: str) -> tuple[int, int] | None:
    depth, quoted, escaped, index = 0, False, False, 0
    while index < len(block):
        char = block[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            if depth == 1:
                match = re.match(rf"\({re.escape(name)}(?:\s|\))", block[index:])
                if match:
                    end = _balanced_end(block, index)
                    return (index, end) if end is not None else None
            depth += 1
        elif char == ")":
            depth -= 1
        index += 1
    return None


class KiCadPcbIpcAdapter:
    """Optional official KiCad IPC backend for a board already open in PCB Editor."""

    @staticmethod
    def _open_board_path(board: Any) -> Path:
        """Reconstruct the full path because KiCad 10 exposes only the board basename."""
        project = board.get_project()
        return Path(project.path, board.name).resolve()

    @staticmethod
    def status() -> IntegrationStatus:
        try:
            from kipy import KiCad

            client = KiCad(client_name="Copperbrain", timeout_ms=250)
            version = client.get_version()
            return IntegrationStatus(
                name="KiCad PCB IPC",
                available=True,
                version=f"{version.major}.{version.minor}.{version.patch}",
            )
        except Exception as exc:
            return IntegrationStatus(
                name="KiCad PCB IPC",
                available=False,
                details={"reason": str(exc)},
            )

    def apply_to_open_board(
        self, expected_board: Path, operations: tuple[PlacementOperation, ...]
    ) -> None:
        try:
            from kipy import KiCad
            from kipy.board_types import BoardLayer  # type: ignore[attr-defined]
            from kipy.geometry import Angle, Vector2

            client = KiCad(client_name="Copperbrain")
            board = client.get_board()
            open_board = self._open_board_path(board)
            if open_board != expected_board.resolve():
                raise CopperbrainError(
                    ErrorCode.CONFLICT,
                    "The PCB open in KiCad is not the expected temporary board",
                    details={"open": str(open_board), "expected": str(expected_board)},
                )
            footprints = {item.reference_field.text.value: item for item in board.get_footprints()}
            missing = sorted({item.reference for item in operations} - set(footprints))
            if missing:
                raise CopperbrainError(
                    ErrorCode.NOT_FOUND,
                    "Placement references were not found in the open PCB",
                    details={"references": missing},
                )
            commit = board.begin_commit()
            try:
                changed: list[Any] = []
                for operation in operations:
                    footprint = footprints[operation.reference]
                    if footprint.locked:
                        raise CopperbrainError(
                            ErrorCode.CONFLICT,
                            "Locked footprints cannot be moved",
                            details={"reference": operation.reference},
                        )
                    footprint.position = Vector2.from_xy_mm(operation.x_mm, operation.y_mm)
                    footprint.orientation = Angle.from_degrees(operation.rotation_deg)
                    if operation.layer is not None:
                        footprint.layer = (
                            BoardLayer.BL_F_Cu if operation.layer == "F.Cu" else BoardLayer.BL_B_Cu
                        )
                    changed.append(footprint)
                board.update_items(changed)
                board.push_commit(commit, "Copperbrain placement preview")
                board.save()  # type: ignore[no-untyped-call]
            except Exception:
                board.drop_commit(commit)
                raise
        except CopperbrainError:
            raise
        except Exception as exc:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "KiCad PCB IPC operation failed",
                actionable_hint="Open the expected temporary board in KiCad PCB Editor and retry.",
                details={"reason": str(exc)},
            ) from exc
