"""Deterministic, connectivity-aware PCB placement heuristics."""

from __future__ import annotations

import math
from collections import defaultdict

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ErrorCode,
    PcbBounds,
    PcbFootprintPlacement,
    PcbPadInspection,
    PcbSummary,
    PlacementOperation,
    PlacementRequest,
)


def _inside(inner: PcbBounds, outer: PcbBounds) -> bool:
    return (
        inner.min_x_mm >= outer.min_x_mm
        and inner.min_y_mm >= outer.min_y_mm
        and inner.max_x_mm <= outer.max_x_mm
        and inner.max_y_mm <= outer.max_y_mm
    )


def _overlap(left: PcbBounds, right: PcbBounds, margin: float) -> bool:
    return not (
        left.max_x_mm + margin <= right.min_x_mm
        or right.max_x_mm + margin <= left.min_x_mm
        or left.max_y_mm + margin <= right.min_y_mm
        or right.max_y_mm + margin <= left.min_y_mm
    )


def _bounds(
    footprint: PcbFootprintPlacement,
    x: float,
    y: float,
    rotation: float,
    layer: str | None = None,
) -> PcbBounds:
    if footprint.local_bounds is not None:
        local = footprint.local_bounds
        corners: tuple[tuple[float, float], ...] = (
            (local.min_x_mm, local.min_y_mm),
            (local.min_x_mm, local.max_y_mm),
            (local.max_x_mm, local.min_y_mm),
            (local.max_x_mm, local.max_y_mm),
        )
        if layer is not None and layer != footprint.layer:
            corners = tuple((local_x, -local_y) for local_x, local_y in corners)
        radians = math.radians(rotation)
        cosine, sine = math.cos(radians), math.sin(radians)
        transformed = tuple(
            (
                x + local_x * cosine + local_y * sine,
                y - local_x * sine + local_y * cosine,
            )
            for local_x, local_y in corners
        )
        return PcbBounds(
            min_x_mm=min(point[0] for point in transformed),
            min_y_mm=min(point[1] for point in transformed),
            max_x_mm=max(point[0] for point in transformed),
            max_y_mm=max(point[1] for point in transformed),
        )
    width = footprint.bounds.max_x_mm - footprint.bounds.min_x_mm
    height = footprint.bounds.max_y_mm - footprint.bounds.min_y_mm
    delta = abs((rotation - footprint.rotation_deg) % 180)
    if 45 < delta < 135:
        width, height = height, width
    return PcbBounds(
        min_x_mm=x - width / 2,
        min_y_mm=y - height / 2,
        max_x_mm=x + width / 2,
        max_y_mm=y + height / 2,
    )


def _snap(value: float, grid: float) -> float:
    return round(round(value / grid) * grid, 6)


def _local_pad(footprint: PcbFootprintPlacement, pad: PcbPadInspection) -> tuple[float, float]:
    radians = math.radians(footprint.rotation_deg)
    cosine, sine = math.cos(radians), math.sin(radians)
    dx, dy = pad.x_mm - footprint.x_mm, pad.y_mm - footprint.y_mm
    return dx * cosine - dy * sine, dx * sine + dy * cosine


def _project_pads(
    footprint: PcbFootprintPlacement,
    pads: tuple[PcbPadInspection, ...],
    operation: PlacementOperation,
) -> tuple[PcbPadInspection, ...]:
    radians = math.radians(operation.rotation_deg)
    cosine, sine = math.cos(radians), math.sin(radians)
    target_layer = operation.layer or footprint.layer
    flipped = target_layer != footprint.layer
    projected: list[PcbPadInspection] = []
    for pad in pads:
        local_x, local_y = _local_pad(footprint, pad)
        if flipped:
            local_y = -local_y
        x = operation.x_mm + local_x * cosine + local_y * sine
        y = operation.y_mm - local_x * sine + local_y * cosine
        layers = pad.layers if len(pad.layers) > 1 else (target_layer,)
        projected.append(
            pad.model_copy(
                update={
                    "x_mm": round(x, 6),
                    "y_mm": round(y, 6),
                    "rotation_deg": operation.rotation_deg,
                    "layers": layers,
                }
            )
        )
    return tuple(projected)


def _pad_clearance_violation(
    candidate_pads: tuple[PcbPadInspection, ...],
    other_pads: tuple[PcbPadInspection, ...],
    clearance_mm: float = 0.25,
) -> bool:
    for left in candidate_pads:
        if not left.net:
            continue
        left_width, left_height = left.width_mm, left.height_mm
        if 45 < left.rotation_deg % 180 < 135:
            left_width, left_height = left_height, left_width
        for right in other_pads:
            if not right.net or left.net == right.net or not set(left.layers) & set(right.layers):
                continue
            right_width, right_height = right.width_mm, right.height_mm
            if 45 < right.rotation_deg % 180 < 135:
                right_width, right_height = right_height, right_width
            x_gap = abs(left.x_mm - right.x_mm) - (left_width + right_width) / 2
            y_gap = abs(left.y_mm - right.y_mm) - (left_height + right_height) / 2
            if x_gap < clearance_mm and y_gap < clearance_mm:
                return True
    return False


def project_placement(
    summary: PcbSummary,
    pads: tuple[PcbPadInspection, ...],
    operations: tuple[PlacementOperation, ...],
) -> tuple[tuple[PcbFootprintPlacement, ...], tuple[PcbPadInspection, ...]]:
    """Project typed placement operations without mutating a PCB."""
    by_operation = {item.reference: item for item in operations}
    by_footprint = {item.reference: item for item in summary.footprints}
    pads_by_reference: dict[str, list[PcbPadInspection]] = defaultdict(list)
    for pad in pads:
        pads_by_reference[pad.reference].append(pad)
    footprints = tuple(
        item.model_copy(
            update={
                "x_mm": operation.x_mm,
                "y_mm": operation.y_mm,
                "rotation_deg": operation.rotation_deg,
                "layer": operation.layer or item.layer,
                "bounds": _bounds(
                    item,
                    operation.x_mm,
                    operation.y_mm,
                    operation.rotation_deg,
                    operation.layer or item.layer,
                ),
            }
        )
        if (operation := by_operation.get(item.reference))
        else item
        for item in summary.footprints
    )
    projected: list[PcbPadInspection] = []
    for reference, footprint in by_footprint.items():
        operation = by_operation.get(reference)
        original = tuple(pads_by_reference.get(reference, ()))
        projected.extend(_project_pads(footprint, original, operation) if operation else original)
    return footprints, tuple(projected)


def _layer_candidates(
    footprint: PcbFootprintPlacement, request: PlacementRequest
) -> tuple[str, ...]:
    if footprint.mount_type != "smd":
        return (footprint.layer,)
    if request.layer_policy == "preserve":
        return (footprint.layer,)
    if request.layer_policy == "front":
        return ("F.Cu",)
    if request.layer_policy == "back":
        return ("B.Cu",)
    area = (footprint.bounds.max_x_mm - footprint.bounds.min_x_mm) * (
        footprint.bounds.max_y_mm - footprint.bounds.min_y_mm
    )
    small_passive = footprint.reference.upper().startswith(("R", "C", "L", "FB")) and area <= 25
    if not small_passive:
        return (footprint.layer,)
    other = "B.Cu" if footprint.layer == "F.Cu" else "F.Cu"
    return footprint.layer, other


def _rotation_candidates(request: PlacementRequest) -> tuple[float, ...]:
    preferred = request.rotation_deg % 360
    if request.rotation_policy == "fixed":
        return (preferred,)
    values = (preferred, (preferred + 90) % 360, (preferred + 180) % 360, (preferred + 270) % 360)
    return tuple(dict.fromkeys(values))


def optimize_placement(
    summary: PcbSummary,
    pads: tuple[PcbPadInspection, ...],
    request: PlacementRequest,
) -> tuple[PlacementOperation, ...]:
    """Pack selected footprints using pad connectivity, orthogonal rotations, and safe sides."""
    footprints = {item.reference: item for item in summary.footprints}
    selected = set(request.references)
    region = request.region or summary.board_bounds
    if region is None:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "Placement requires explicit bounds when Edge.Cuts cannot be detected",
        )
    pads_by_reference: dict[str, tuple[PcbPadInspection, ...]] = {}
    for reference in footprints:
        pads_by_reference[reference] = tuple(item for item in pads if item.reference == reference)
    net_references: dict[str, set[str]] = defaultdict(set)
    for pad in pads:
        if pad.net:
            net_references[pad.net].add(pad.reference)
    adjacency: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    priority_pairs: set[frozenset[str]] = set()
    for net, references in net_references.items():
        if len(references) < 2:
            continue
        weight = 1.0 / max(1, len(references) - 1)
        priority_net = any(
            token in net.upper() for token in ("MOTOR", "VBAT", "VMOTOR", "SHUNT", "PGND")
        )
        if priority_net:
            weight *= 2
            ordered_priority = sorted(references)
            priority_pairs.update(
                frozenset((left, right))
                for index, left in enumerate(ordered_priority)
                for right in ordered_priority[index + 1 :]
            )
        ordered = sorted(references)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                adjacency[left][right] += weight
                adjacency[right][left] += weight

    fixed = {reference: item for reference, item in footprints.items() if reference not in selected}
    placed: dict[str, PlacementOperation] = {}

    def current_footprints() -> tuple[PcbFootprintPlacement, ...]:
        operations = tuple(placed.values())
        return project_placement(summary, pads, operations)[0]

    order = sorted(
        selected,
        key=lambda reference: (
            -sum(adjacency[reference].values()),
            -len(pads_by_reference[reference]),
            reference,
        ),
    )
    for reference in order:
        footprint = footprints[reference]
        other_footprints = {
            item.reference: item
            for item in current_footprints()
            if item.reference != reference
            and (item.reference not in selected or item.reference in placed)
        }
        anchor_weights = adjacency[reference]
        anchors = [
            (other_footprints[name], weight)
            for name, weight in sorted(anchor_weights.items())
            if name in other_footprints and (name in fixed or name in placed)
        ]
        if anchors:
            total = sum(weight for _, weight in anchors)
            target_x = sum(item.x_mm * weight for item, weight in anchors) / total
            target_y = sum(item.y_mm * weight for item, weight in anchors) / total
        else:
            target_x = (region.min_x_mm + region.max_x_mm) / 2
            target_y = (region.min_y_mm + region.max_y_mm) / 2

        best: tuple[float, float, float, float, str] | None = None
        for rotation in _rotation_candidates(request):
            candidate_bounds = _bounds(footprint, 0, 0, rotation)
            width = candidate_bounds.max_x_mm - candidate_bounds.min_x_mm
            height = candidate_bounds.max_y_mm - candidate_bounds.min_y_mm
            points = {
                (target_x, target_y),
                ((region.min_x_mm + region.max_x_mm) / 2, (region.min_y_mm + region.max_y_mm) / 2),
            }
            for other in other_footprints.values():
                corridor = max(request.spacing_mm, request.routing_corridor_mm)
                if frozenset((reference, other.reference)) in priority_pairs:
                    corridor = max(corridor, request.power_corridor_mm)
                points.update(
                    {
                        (other.x_mm, other.y_mm),
                        (other.bounds.min_x_mm - corridor - width / 2, other.y_mm),
                        (other.bounds.max_x_mm + corridor + width / 2, other.y_mm),
                        (other.x_mm, other.bounds.min_y_mm - corridor - height / 2),
                        (other.x_mm, other.bounds.max_y_mm + corridor + height / 2),
                    }
                )
            if reference.upper().startswith(("J", "P")):
                points.update(
                    {
                        (region.min_x_mm + width / 2, target_y),
                        (region.max_x_mm - width / 2, target_y),
                        (target_x, region.min_y_mm + height / 2),
                        (target_x, region.max_y_mm - height / 2),
                    }
                )
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    points.add((target_x + dx * request.grid_mm, target_y + dy * request.grid_mm))
            for layer in _layer_candidates(footprint, request):
                layer_points = set(points)
                origin_bounds = _bounds(footprint, 0, 0, rotation, layer)
                for other in other_footprints.values():
                    corridor = max(request.spacing_mm, request.routing_corridor_mm)
                    if frozenset((reference, other.reference)) in priority_pairs:
                        corridor = max(corridor, request.power_corridor_mm)
                    layer_points.update(
                        {
                            (
                                other.bounds.min_x_mm - corridor - origin_bounds.max_x_mm,
                                other.y_mm,
                            ),
                            (
                                other.bounds.max_x_mm + corridor - origin_bounds.min_x_mm,
                                other.y_mm,
                            ),
                            (
                                other.x_mm,
                                other.bounds.min_y_mm - corridor - origin_bounds.max_y_mm,
                            ),
                            (
                                other.x_mm,
                                other.bounds.max_y_mm + corridor - origin_bounds.min_y_mm,
                            ),
                        }
                    )
                if reference.upper().startswith(("J", "P")):
                    layer_points.update(
                        {
                            (region.min_x_mm - origin_bounds.min_x_mm, target_y),
                            (region.max_x_mm - origin_bounds.max_x_mm, target_y),
                            (target_x, region.min_y_mm - origin_bounds.min_y_mm),
                            (target_x, region.max_y_mm - origin_bounds.max_y_mm),
                        }
                    )
                for raw_x, raw_y in sorted(layer_points):
                    x, y = _snap(raw_x, request.grid_mm), _snap(raw_y, request.grid_mm)
                    bounds = _bounds(footprint, x, y, rotation, layer)
                    if not _inside(bounds, region):
                        continue
                    collision = False
                    for other in other_footprints.values():
                        corridor = max(request.spacing_mm, request.routing_corridor_mm)
                        if frozenset((reference, other.reference)) in priority_pairs:
                            corridor = max(corridor, request.power_corridor_mm)
                        shares_physical_side = (
                            layer == other.layer
                            or footprint.mount_type != "smd"
                            or other.mount_type != "smd"
                        )
                        if shares_physical_side and _overlap(bounds, other.bounds, corridor):
                            collision = True
                            break
                    if collision:
                        continue
                    operation = PlacementOperation(
                        reference=reference,
                        x_mm=x,
                        y_mm=y,
                        rotation_deg=rotation,
                        layer=layer,  # type: ignore[arg-type]
                    )
                    candidate_pads = _project_pads(
                        footprint, pads_by_reference[reference], operation
                    )
                    projected_others = tuple(
                        item
                        for item in project_placement(summary, pads, tuple(placed.values()))[1]
                        if item.reference not in selected or item.reference in placed
                    )
                    if _pad_clearance_violation(candidate_pads, projected_others):
                        continue
                    net_cost = 0.0
                    layer_cost = 0.0
                    for candidate_pad in candidate_pads:
                        connected = [
                            item
                            for item in projected_others
                            if item.net == candidate_pad.net and item.reference != reference
                        ]
                        if not connected or not candidate_pad.net:
                            continue
                        weight = 1.0 / max(1, len(net_references[candidate_pad.net]) - 1)
                        net_cost += weight * min(
                            math.dist(
                                (candidate_pad.x_mm, candidate_pad.y_mm), (item.x_mm, item.y_mm)
                            )
                            for item in connected
                        )
                        if not any(
                            set(candidate_pad.layers) & set(item.layers) for item in connected
                        ):
                            layer_cost += weight * 1.5
                    all_bounds = [
                        item.bounds
                        for item in other_footprints.values()
                        if item.reference in fixed or item.reference in placed
                    ] + [bounds]
                    min_x = min(item.min_x_mm for item in all_bounds)
                    min_y = min(item.min_y_mm for item in all_bounds)
                    max_x = max(item.max_x_mm for item in all_bounds)
                    max_y = max(item.max_y_mm for item in all_bounds)
                    envelope = (max_x - min_x) * (max_y - min_y)
                    compact_cost = envelope * (0.03 if request.strategy == "compact" else 0.005)
                    edge_cost = 0.0
                    if reference.upper().startswith(("J", "P")):
                        edge_cost = 2 * min(
                            bounds.min_x_mm - region.min_x_mm,
                            region.max_x_mm - bounds.max_x_mm,
                            bounds.min_y_mm - region.min_y_mm,
                            region.max_y_mm - bounds.max_y_mm,
                        )
                    back_penalty = 0.2 if layer == "B.Cu" and footprint.layer != "B.Cu" else 0
                    score = net_cost + layer_cost + compact_cost + edge_cost + back_penalty
                    key = (round(score, 9), x, y, rotation, layer)
                    if best is None or key < best:
                        best = key
        if best is None:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Requested footprints do not fit in the placement region",
                details={"reference": reference},
            )
        _, x, y, rotation, layer = best
        placed[reference] = PlacementOperation(
            reference=reference,
            x_mm=x,
            y_mm=y,
            rotation_deg=rotation,
            layer=layer,  # type: ignore[arg-type]
        )
    return tuple(placed[reference] for reference in request.references)
