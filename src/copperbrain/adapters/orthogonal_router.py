"""Deterministic two-layer orthogonal grid-routing backend.

This backend is intentionally bounded to F.Cu/B.Cu.  It is a fallback for boards where
the external Specctra autorouter cannot make progress; every generated candidate still
passes through the normal Copperbrain connectivity and KiCad DRC gates.
"""

from __future__ import annotations

import heapq
import math
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal

from copperbrain.adapters.freerouting import RoutedBoardCandidate
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ErrorCode,
    PcbBounds,
    PcbFootprintPlacement,
    PcbPadInspection,
    RouteSegment,
    RouteVia,
    RoutingBackendStatus,
    RoutingRequest,
    UnroutedConnection,
)

_LAYERS = ("F.Cu", "B.Cu")
_Node = tuple[int, int, int]


@dataclass(frozen=True)
class _Grid:
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    step: float

    @property
    def width(self) -> int:
        return math.floor((self.max_x - self.min_x) / self.step) + 1

    @property
    def height(self) -> int:
        return math.floor((self.max_y - self.min_y) / self.step) + 1

    def node(self, x: float, y: float, layer: int) -> _Node:
        return (
            min(self.width - 1, max(0, round((x - self.min_x) / self.step))),
            min(self.height - 1, max(0, round((y - self.min_y) / self.step))),
            layer,
        )

    def point(self, node: _Node) -> tuple[float, float]:
        return (
            round(self.min_x + node[0] * self.step, 6),
            round(self.min_y + node[1] * self.step, 6),
        )

    def contains(self, node: _Node) -> bool:
        return 0 <= node[0] < self.width and 0 <= node[1] < self.height


class OrthogonalRoutingAdapter:
    """Route typed pad connections with a deterministic two-layer A* search."""

    def __init__(self, adapter: PcbFileAdapter | None = None) -> None:
        self.adapter = adapter or PcbFileAdapter()

    def status(self) -> RoutingBackendStatus:
        return RoutingBackendStatus(
            name="Copperbrain two-layer orthogonal router",
            available=True,
            version="1",
            reason=None,
        )

    @staticmethod
    def _pad_layers(pad: PcbPadInspection, preferred: str) -> tuple[int, ...]:
        values = tuple(index for index, layer in enumerate(_LAYERS) if layer in pad.layers)
        preferred_index = _LAYERS.index(preferred)
        return tuple(sorted(values, key=lambda item: item != preferred_index))

    @staticmethod
    def _rotated_size(pad: PcbPadInspection) -> tuple[float, float]:
        angle = pad.rotation_deg % 180
        if 45 <= angle < 135:
            return pad.height_mm, pad.width_mm
        return pad.width_mm, pad.height_mm

    @staticmethod
    def _terminal_point(
        pad: PcbPadInspection,
        bounds: PcbBounds,
        request: RoutingRequest,
    ) -> tuple[float, float]:
        """Move an SMD terminal outside its footprint before permitting a via."""
        if len(pad.layers) > 1:
            return pad.x_mm, pad.y_mm
        center_x = (bounds.min_x_mm + bounds.max_x_mm) / 2
        center_y = (bounds.min_y_mm + bounds.max_y_mm) / 2
        horizontal = abs(pad.x_mm - center_x) / max((bounds.max_x_mm - bounds.min_x_mm) / 2, 1e-9)
        vertical = abs(pad.y_mm - center_y) / max((bounds.max_y_mm - bounds.min_y_mm) / 2, 1e-9)
        margin = request.via_diameter_mm / 2 + request.default_clearance_mm + 0.3
        if horizontal >= vertical:
            return (
                bounds.max_x_mm + margin if pad.x_mm >= center_x else bounds.min_x_mm - margin,
                pad.y_mm,
            )
        return (
            pad.x_mm,
            bounds.max_y_mm + margin if pad.y_mm >= center_y else bounds.min_y_mm - margin,
        )

    def _pad_occupancy(
        self,
        pads: tuple[PcbPadInspection, ...],
        footprints: tuple[PcbFootprintPlacement, ...],
        grid: _Grid,
        margin: float,
    ) -> dict[_Node, set[str]]:
        occupied: dict[_Node, set[str]] = defaultdict(set)
        for pad in pads:
            width, height = self._rotated_size(pad)
            min_node = grid.node(pad.x_mm - width / 2 - margin, pad.y_mm - height / 2 - margin, 0)
            max_node = grid.node(pad.x_mm + width / 2 + margin, pad.y_mm + height / 2 + margin, 0)
            for layer in self._pad_layers(pad, "F.Cu"):
                for x in range(min_node[0], max_node[0] + 1):
                    for y in range(min_node[1], max_node[1] + 1):
                        occupied[(x, y, layer)].add(pad.net)
        pads_by_reference: dict[str, list[PcbPadInspection]] = defaultdict(list)
        for pad in pads:
            pads_by_reference[pad.reference].append(pad)
        for footprint in footprints:
            footprint_pads = pads_by_reference.get(footprint.reference, [])
            if not footprint_pads or any(len(pad.layers) > 1 for pad in footprint_pads):
                continue
            layer = _LAYERS.index(footprint.layer)
            min_node = grid.node(
                footprint.bounds.min_x_mm - margin,
                footprint.bounds.min_y_mm - margin,
                layer,
            )
            max_node = grid.node(
                footprint.bounds.max_x_mm + margin,
                footprint.bounds.max_y_mm + margin,
                layer,
            )
            for x in range(min_node[0], max_node[0] + 1):
                for y in range(min_node[1], max_node[1] + 1):
                    occupied[(x, y, layer)].add("__footprint__")
        return occupied

    @staticmethod
    def _heuristic(node: _Node, goals: set[_Node], via_cost: float) -> float:
        return min(
            abs(node[0] - goal[0])
            + abs(node[1] - goal[1])
            + (via_cost if node[2] != goal[2] else 0)
            for goal in goals
        )

    @staticmethod
    def _neighbors(
        node: _Node,
        allow_vias: bool,
        preferred_layer: int,
    ) -> tuple[tuple[_Node, float], ...]:
        x, y, layer = node
        horizontal_cost = 1.0 if layer == 0 else 1.35
        vertical_cost = 1.0 if layer == 1 else 1.35
        if preferred_layer == 1 and layer != preferred_layer:
            horizontal_cost *= 8
            vertical_cost *= 8
        result = [
            ((x - 1, y, layer), horizontal_cost),
            ((x + 1, y, layer), horizontal_cost),
            ((x, y - 1, layer), vertical_cost),
            ((x, y + 1, layer), vertical_cost),
        ]
        if allow_vias:
            result.append(((x, y, 1 - layer), 5.0 if preferred_layer == 1 else 12.0))
        return tuple(result)

    def _search(
        self,
        grid: _Grid,
        starts: tuple[_Node, ...],
        goals: tuple[_Node, ...],
        net: str,
        pad_occupancy: dict[_Node, set[str]],
        copper_occupancy: dict[_Node, set[str]],
        via_occupancy: dict[_Node, set[str]],
        allow_vias: bool,
        preferred_layer: int,
    ) -> list[_Node]:
        goal_set = set(goals)
        terminal_set = set(starts) | goal_set
        via_cost = 5.0 if preferred_layer == 1 else 12.0

        def blocked(node: _Node) -> bool:
            if node in terminal_set:
                return any(owner != net for owner in copper_occupancy.get(node, ()))
            return any(owner != net for owner in pad_occupancy.get(node, ())) or any(
                owner != net for owner in copper_occupancy.get(node, ())
            )

        def via_blocked(node: _Node) -> bool:
            return any(
                owner != net
                for layer in (0, 1)
                for owner in via_occupancy.get((node[0], node[1], layer), ())
            )

        queue: list[tuple[float, float, _Node]] = []
        distance: dict[_Node, float] = {}
        previous: dict[_Node, _Node] = {}
        for start in starts:
            distance[start] = 0.0
            heapq.heappush(queue, (self._heuristic(start, goal_set, via_cost), 0.0, start))
        visited: set[_Node] = set()
        found: _Node | None = None
        while queue:
            _, cost, node = heapq.heappop(queue)
            if node in visited:
                continue
            visited.add(node)
            if node in goal_set:
                found = node
                break
            for neighbor, move_cost in self._neighbors(node, allow_vias, preferred_layer):
                if (
                    not grid.contains(neighbor)
                    or blocked(neighbor)
                    or (neighbor[2] != node[2] and via_blocked(neighbor))
                ):
                    continue
                candidate = cost + move_cost
                if candidate >= distance.get(neighbor, math.inf):
                    continue
                distance[neighbor] = candidate
                previous[neighbor] = node
                score = candidate + self._heuristic(neighbor, goal_set, via_cost)
                heapq.heappush(queue, (score, candidate, neighbor))
        if found is None:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Two-layer graph routing could not connect a pad pair",
                details={"net": net, "visited_nodes": len(visited)},
            )
        path = [found]
        while path[-1] not in starts:
            path.append(previous[path[-1]])
        path.reverse()
        return path

    @staticmethod
    def _connection_key(item: UnroutedConnection) -> tuple[str, str, str, str, str]:
        return (item.net, item.start_reference, item.start_pad, item.end_reference, item.end_pad)

    @staticmethod
    def _terminal_segment(
        net: str,
        pad: PcbPadInspection,
        point: tuple[float, float],
        width: float,
        layer: str,
    ) -> RouteSegment | None:
        if (pad.x_mm, pad.y_mm) == point:
            return None
        return RouteSegment(
            net=net,
            start_x_mm=pad.x_mm,
            start_y_mm=pad.y_mm,
            end_x_mm=point[0],
            end_y_mm=point[1],
            width_mm=width,
            layer=layer,  # type: ignore[arg-type]
        )

    @staticmethod
    def _path_items(
        grid: _Grid,
        path: list[_Node],
        net: str,
        width: float,
        request: RoutingRequest,
    ) -> tuple[list[RouteSegment], list[RouteVia]]:
        segments: list[RouteSegment] = []
        vias: list[RouteVia] = []
        run_start = path[0]
        previous = path[0]
        direction: tuple[int, int] | None = None

        def flush(end: _Node) -> None:
            nonlocal run_start
            if run_start[:2] == end[:2]:
                run_start = end
                return
            start_point, end_point = grid.point(run_start), grid.point(end)
            segments.append(
                RouteSegment(
                    net=net,
                    start_x_mm=start_point[0],
                    start_y_mm=start_point[1],
                    end_x_mm=end_point[0],
                    end_y_mm=end_point[1],
                    width_mm=width,
                    layer=_LAYERS[run_start[2]],  # type: ignore[arg-type]
                )
            )
            run_start = end

        for node in path[1:]:
            if node[2] != previous[2]:
                flush(previous)
                point = grid.point(node)
                vias.append(
                    RouteVia(
                        net=net,
                        x_mm=point[0],
                        y_mm=point[1],
                        diameter_mm=request.via_diameter_mm,
                        drill_mm=request.via_drill_mm,
                    )
                )
                run_start = node
                direction = None
            else:
                next_direction = (node[0] - previous[0], node[1] - previous[1])
                if direction is not None and next_direction != direction:
                    flush(previous)
                direction = next_direction
            previous = node
        flush(previous)
        return segments, vias

    @staticmethod
    def _mark_path(
        path: list[_Node],
        net: str,
        radius: int,
        grid: _Grid,
        occupancy: dict[_Node, set[str]],
    ) -> None:
        for node in path:
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    candidate = (node[0] + dx, node[1] + dy, node[2])
                    if grid.contains(candidate) and dx * dx + dy * dy <= radius * radius:
                        occupancy[candidate].add(net)
        for left, right in pairwise(path):
            if left[2] != right[2]:
                for layer in (0, 1):
                    occupancy[(left[0], left[1], layer)].add(net)

    @staticmethod
    def _mark_disc(
        point: tuple[float, float],
        layers: tuple[int, ...],
        radius_mm: float,
        net: str,
        grid: _Grid,
        occupancy: dict[_Node, set[str]],
    ) -> None:
        center = grid.node(point[0], point[1], layers[0])
        radius = max(1, math.ceil((radius_mm - 1e-9) / grid.step))
        for layer in layers:
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    node = (center[0] + dx, center[1] + dy, layer)
                    if grid.contains(node) and dx * dx + dy * dy <= radius * radius:
                        occupancy[node].add(net)

    @classmethod
    def _mark_segment(
        cls,
        segment: RouteSegment,
        clearance_mm: float,
        grid: _Grid,
        occupancy: dict[_Node, set[str]],
    ) -> None:
        distance = math.hypot(
            segment.end_x_mm - segment.start_x_mm,
            segment.end_y_mm - segment.start_y_mm,
        )
        samples = max(1, math.ceil(distance / (grid.step / 2)))
        layer = _LAYERS.index(segment.layer)
        radius_mm = segment.width_mm / 2 + clearance_mm
        for index in range(samples + 1):
            fraction = index / samples
            cls._mark_disc(
                (
                    segment.start_x_mm + (segment.end_x_mm - segment.start_x_mm) * fraction,
                    segment.start_y_mm + (segment.end_y_mm - segment.start_y_mm) * fraction,
                ),
                (layer,),
                radius_mm,
                segment.net,
                grid,
                occupancy,
            )

    @classmethod
    def _mark_routing_items(
        cls,
        segments: tuple[RouteSegment, ...] | list[RouteSegment],
        vias: tuple[RouteVia, ...] | list[RouteVia],
        clearance_mm: float,
        grid: _Grid,
        occupancy: dict[_Node, set[str]],
    ) -> None:
        for segment in segments:
            cls._mark_segment(segment, clearance_mm, grid, occupancy)
        for via in vias:
            cls._mark_disc(
                (via.x_mm, via.y_mm),
                (0, 1),
                via.diameter_mm / 2 + clearance_mm,
                via.net,
                grid,
                occupancy,
            )

    def route(
        self,
        pcb: Path,
        workspace: Path,
        request: RoutingRequest,
        strategy: Literal["prioritized", "sequential"],
    ) -> RoutedBoardCandidate:
        if strategy not in {"prioritized", "sequential"}:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Unsupported graph-routing strategy")
        if not pcb.is_file() or pcb.suffix.lower() != ".kicad_pcb":
            raise CopperbrainError(ErrorCode.NOT_FOUND, "PCB input for graph routing was not found")
        started = time.monotonic()
        workspace.mkdir(parents=True, exist_ok=False)
        routed = workspace / "orthogonal-routed.kicad_pcb"
        shutil.copy2(pcb, routed)
        parsed = self.adapter.parse(routed)
        bounds = parsed.summary.board_bounds
        if bounds is None:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "PCB outline was not detected")
        step = max(0.05, request.grid_mm)
        grid = _Grid(
            bounds.min_x_mm + step,
            bounds.min_y_mm + step,
            bounds.max_x_mm - step,
            bounds.max_y_mm - step,
            step,
        )
        analysis = self.adapter.analyze_routing(routed, "graph", request.nets)
        target_nets = {item.net for item in analysis.unrouted_connections}
        pads = tuple(pad for pad in parsed.pads if pad.net in target_nets)
        pad_lookup = {(pad.net, pad.reference, pad.number): pad for pad in pads}
        bounds_by_reference = {
            footprint.reference: footprint.bounds for footprint in parsed.summary.footprints
        }
        pad_occupancy = self._pad_occupancy(
            parsed.pads,
            parsed.summary.footprints,
            grid,
            request.default_clearance_mm + request.default_track_width_mm / 2,
        )
        via_occupancy = self._pad_occupancy(
            parsed.pads,
            parsed.summary.footprints,
            grid,
            request.default_clearance_mm + request.via_diameter_mm / 2,
        )
        for pad in parsed.pads:
            if not pad.net or len(pad.layers) != 1:
                continue
            terminal = self._terminal_point(pad, bounds_by_reference[pad.reference], request)
            escape = self._terminal_segment(
                pad.net,
                pad,
                terminal,
                request.default_track_width_mm,
                pad.layers[0],
            )
            if escape is None:
                continue
            self._mark_segment(
                escape,
                request.default_clearance_mm + request.default_track_width_mm / 2,
                grid,
                pad_occupancy,
            )
            self._mark_segment(
                escape,
                request.default_clearance_mm + request.via_diameter_mm / 2,
                grid,
                via_occupancy,
            )
        copper_occupancy: dict[_Node, set[str]] = defaultdict(set)
        existing_segments, existing_vias = self.adapter.routing_items(routed)
        self._mark_routing_items(
            existing_segments,
            existing_vias,
            request.default_clearance_mm + request.default_track_width_mm / 2,
            grid,
            copper_occupancy,
        )
        self._mark_routing_items(
            existing_segments,
            existing_vias,
            request.default_clearance_mm + request.via_diameter_mm / 2,
            grid,
            via_occupancy,
        )
        connections = list(analysis.unrouted_connections)
        if strategy == "prioritized":
            counts: dict[str, int] = defaultdict(int)
            for item in connections:
                counts[item.net] += 1
            connections.sort(
                key=lambda item: (
                    -counts[item.net],
                    -item.distance_mm,
                    self._connection_key(item),
                )
            )
        else:
            connections.sort(key=self._connection_key)

        segments: list[RouteSegment] = []
        vias: list[RouteVia] = []
        for connection in connections:
            start = pad_lookup[(connection.net, connection.start_reference, connection.start_pad)]
            end = pad_lookup[(connection.net, connection.end_reference, connection.end_pad)]
            width = request.default_track_width_mm
            start_point = self._terminal_point(
                start,
                bounds_by_reference[start.reference],
                request,
            )
            end_point = self._terminal_point(
                end,
                bounds_by_reference[end.reference],
                request,
            )
            starts = tuple(
                grid.node(start_point[0], start_point[1], layer)
                for layer in self._pad_layers(start, request.preferred_layer)
            )
            goals = tuple(
                grid.node(end_point[0], end_point[1], layer)
                for layer in self._pad_layers(end, request.preferred_layer)
            )
            path = self._search(
                grid,
                starts,
                goals,
                connection.net,
                pad_occupancy,
                copper_occupancy,
                via_occupancy,
                request.allow_vias,
                _LAYERS.index(request.preferred_layer),
            )
            path_segments, path_vias = self._path_items(grid, path, connection.net, width, request)
            start_link = self._terminal_segment(
                connection.net, start, grid.point(path[0]), width, _LAYERS[path[0][2]]
            )
            end_link = self._terminal_segment(
                connection.net, end, grid.point(path[-1]), width, _LAYERS[path[-1][2]]
            )
            if start_link is not None:
                path_segments.insert(0, start_link)
            if end_link is not None:
                path_segments.append(end_link)
            segments.extend(path_segments)
            vias.extend(path_vias)
            radius = max(
                1,
                math.ceil((width / 2 + request.default_clearance_mm - 1e-9) / grid.step),
            )
            self._mark_path(path, connection.net, radius, grid, copper_occupancy)
            self._mark_routing_items(
                path_segments,
                path_vias,
                request.default_clearance_mm + request.default_track_width_mm / 2,
                grid,
                copper_occupancy,
            )
            self._mark_routing_items(
                path_segments,
                path_vias,
                request.default_clearance_mm + request.via_diameter_mm / 2,
                grid,
                via_occupancy,
            )
        self.adapter.apply_routing(routed, tuple(segments), tuple(vias))
        return RoutedBoardCandidate(
            strategy=strategy,
            pcb=routed,
            elapsed_seconds=time.monotonic() - started,
            stdout_tail=(
                f"two-layer orthogonal route: {len(segments)} segment(s), "
                f"{len(vias)} via(s), grid {grid.step:g} mm"
            ),
        )
