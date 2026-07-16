"""Fixed-action KiCad Python worker for creating and relayering a PCB."""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import pcbnew  # type: ignore[import-not-found]


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _create_board(destination: Path, copper_layers: int) -> int:
    if destination.suffix.lower() != ".kicad_pcb" or not destination.parent.is_dir():
        return _fail("PCB destination is invalid")
    if destination.exists():
        return _fail("PCB destination already exists")
    if copper_layers not in {2, 4}:
        return _fail("Copper layer count must be 2 or 4")
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(copper_layers)
    if not pcbnew.SaveBoard(str(destination), board) or not destination.is_file():
        return _fail("KiCad failed to save the empty PCB")
    return 0


def _set_copper_layers(source: Path, destination: Path, copper_layers: int) -> int:
    if source.suffix.lower() != ".kicad_pcb" or not source.is_file():
        return _fail("PCB source is missing or invalid")
    if destination.suffix.lower() != ".kicad_pcb" or not destination.parent.is_dir():
        return _fail("PCB destination is invalid")
    if destination.exists():
        return _fail("PCB destination already exists")
    if copper_layers not in {2, 4}:
        return _fail("Copper layer count must be 2 or 4")
    board = pcbnew.LoadBoard(str(source))
    if board is None:
        return _fail("KiCad failed to load the PCB")
    current = board.GetCopperLayerCount()
    if copper_layers < current:
        inner_layers = set(range(1, current - 1))
        if any(track.GetLayer() in inner_layers for track in board.GetTracks()):
            return _fail("PCB contains tracks on copper layers that would be removed")
        if any(zone.GetLayer() in inner_layers for zone in board.Zones()):
            return _fail("PCB contains zones on copper layers that would be removed")
    board.SetCopperLayerCount(copper_layers)
    if not pcbnew.SaveBoard(str(destination), board) or not destination.is_file():
        return _fail("KiCad failed to save the relayered PCB")
    return 0


def _apply_placement(source: Path, destination: Path, manifest: Path) -> int:
    if source.suffix.lower() != ".kicad_pcb" or not source.is_file():
        return _fail("PCB source is missing or invalid")
    if destination.suffix.lower() != ".kicad_pcb" or destination.exists():
        return _fail("PCB destination is invalid")
    if manifest.suffix.lower() != ".json" or not manifest.is_file():
        return _fail("Placement manifest is missing or invalid")
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fail("Placement manifest could not be parsed")
    if not isinstance(raw, list) or not raw:
        return _fail("Placement manifest must contain operations")
    operations: dict[str, tuple[float, float, float, str | None]] = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {
            "reference",
            "x_mm",
            "y_mm",
            "rotation_deg",
            "layer",
        }:
            return _fail("Placement operation has an invalid shape")
        reference, layer = item["reference"], item["layer"]
        if not isinstance(reference, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.-]{0,63}", reference
        ):
            return _fail("Placement reference is invalid")
        if reference in operations or layer not in {None, "F.Cu", "B.Cu"}:
            return _fail("Placement operation is duplicate or has an invalid layer")
        try:
            values = tuple(float(item[name]) for name in ("x_mm", "y_mm", "rotation_deg"))
        except (TypeError, ValueError):
            return _fail("Placement coordinates are invalid")
        if not all(abs(value) < 1_000_000 for value in values):
            return _fail("Placement coordinates are out of range")
        operations[reference] = (*values, layer)
    board = pcbnew.LoadBoard(str(source))
    if board is None:
        return _fail("KiCad failed to load the PCB")
    footprints = {item.GetReference(): item for item in board.GetFootprints()}
    if set(operations) - set(footprints):
        return _fail("Placement references were not found")
    for reference, (x_mm, y_mm, rotation_deg, layer) in operations.items():
        footprint = footprints[reference]
        if footprint.IsLocked():
            return _fail(f"Footprint is locked: {reference}")
        if layer is not None:
            target_front = layer == "F.Cu"
            current_front = footprint.GetLayer() == pcbnew.F_Cu
            if target_front != current_front:
                footprint.Flip(footprint.GetPosition(), False)
        footprint.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
        footprint.SetOrientationDegrees(rotation_deg)
    if not pcbnew.SaveBoard(str(destination), board) or not destination.is_file():
        return _fail("KiCad failed to save the placed PCB")
    return 0


def _apply_grounding(source: Path, destination: Path, manifest: Path) -> int:
    """Apply validated shaped ground domains, fanouts, vias, and target stackup."""
    if source.suffix.lower() != ".kicad_pcb" or not source.is_file():
        return _fail("PCB source is missing or invalid")
    if destination.suffix.lower() != ".kicad_pcb" or destination.exists():
        return _fail("PCB destination is invalid")
    if manifest.suffix.lower() != ".json" or not manifest.is_file():
        return _fail("Grounding manifest is missing or invalid")
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _fail("Grounding manifest could not be parsed")
    expected = {
        "copper_layers",
        "domains",
        "replace_existing_planes",
        "edge_clearance_mm",
        "clearance_mm",
        "min_thickness_mm",
        "thermal_gap_mm",
        "thermal_spoke_width_mm",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        return _fail("Grounding manifest has an invalid shape")
    raw_domains = raw["domains"]
    copper_layers = raw["copper_layers"]
    replace_existing = raw["replace_existing_planes"]
    if copper_layers not in {2, 4} or not isinstance(replace_existing, bool):
        return _fail("Ground plane replacement policy is invalid")
    if not isinstance(raw_domains, list) or not raw_domains or len(raw_domains) > 32:
        return _fail("Ground domains are invalid")
    try:
        values = {
            name: float(raw[name])
            for name in (
                "edge_clearance_mm",
                "clearance_mm",
                "min_thickness_mm",
                "thermal_gap_mm",
                "thermal_spoke_width_mm",
            )
        }
    except (TypeError, ValueError):
        return _fail("Ground plane dimensions are invalid")
    if (
        not all(math.isfinite(value) for value in values.values())
        or values["edge_clearance_mm"] <= 0
        or values["clearance_mm"] < 0
        or min(
            values["min_thickness_mm"],
            values["thermal_gap_mm"],
            values["thermal_spoke_width_mm"],
        )
        <= 0
    ):
        return _fail("Ground plane dimensions are out of range")

    domains: list[
        tuple[
            str,
            list[str],
            str,
            list[tuple[str, str, tuple[float, float, float, float] | None]],
            list[tuple[float, float, float, float, float, str]],
            list[tuple[float, float, float, float]],
        ]
    ] = []
    seen_nets: set[str] = set()
    requested_layers: set[str] = set()
    board_region_layers: set[str] = set()
    operation_count = 0
    domain_shape = {"net_name", "layers", "regions", "pad_connection", "fanouts", "vias"}
    for raw_domain in raw_domains:
        if not isinstance(raw_domain, dict) or set(raw_domain) != domain_shape:
            return _fail("Ground domain has an invalid shape")
        net_name = raw_domain["net_name"]
        layers = raw_domain["layers"]
        raw_regions = raw_domain["regions"]
        pad_connection = raw_domain["pad_connection"]
        raw_fanouts = raw_domain["fanouts"]
        raw_vias = raw_domain["vias"]
        if not isinstance(net_name, str) or not net_name.strip() or net_name in seen_nets:
            return _fail("Ground domain net name is invalid or duplicate")
        if pad_connection not in {"thermal", "solid"}:
            return _fail("Ground domain pad connection is invalid")
        if (
            not isinstance(layers, list)
            or not layers
            or any(not isinstance(item, str) for item in layers)
            or len(layers) != len(set(layers))
            or any(
                re.fullmatch(r"(?:F|B|In(?:[1-9]|[12][0-9]|30))\.Cu", item) is None
                for item in layers
            )
        ):
            return _fail("Ground domain layers are invalid")
        if (
            not isinstance(raw_regions, list)
            or not raw_regions
            or len(raw_regions) > 512
            or not isinstance(raw_fanouts, list)
            or not isinstance(raw_vias, list)
        ):
            return _fail("Ground domain regions, fanouts, or vias are invalid")
        regions: list[tuple[str, str, tuple[float, float, float, float] | None]] = []
        region_shape = {
            "layer",
            "kind",
            "min_x_mm",
            "min_y_mm",
            "max_x_mm",
            "max_y_mm",
        }
        for item in raw_regions:
            if not isinstance(item, dict) or set(item) != region_shape:
                return _fail("Ground region has an invalid shape")
            layer, kind = item["layer"], item["kind"]
            if layer not in layers or kind not in {"board", "local"}:
                return _fail("Ground region layer or kind is invalid")
            raw_bounds = tuple(
                item[name] for name in ("min_x_mm", "min_y_mm", "max_x_mm", "max_y_mm")
            )
            if kind == "board":
                if any(value is not None for value in raw_bounds) or layer in board_region_layers:
                    return _fail("Board ground regions are invalid or overlap another domain")
                board_region_layers.add(layer)
                regions.append((layer, kind, None))
                continue
            if layer not in {"F.Cu", "B.Cu"}:
                return _fail("Local ground regions require an outer layer")
            try:
                parsed_bounds: tuple[float, float, float, float] = (
                    float(raw_bounds[0]),
                    float(raw_bounds[1]),
                    float(raw_bounds[2]),
                    float(raw_bounds[3]),
                )
            except (TypeError, ValueError):
                return _fail("Local ground region bounds are invalid")
            if (
                not all(math.isfinite(value) for value in parsed_bounds)
                or parsed_bounds[0] >= parsed_bounds[2]
                or parsed_bounds[1] >= parsed_bounds[3]
            ):
                return _fail("Local ground region bounds are out of range")
            regions.append((layer, kind, parsed_bounds))
        operation_count += len(regions) + len(raw_fanouts) + len(raw_vias)
        if operation_count > 1024:
            return _fail("Ground domain operations are excessive")
        fanouts: list[tuple[float, float, float, float, float, str]] = []
        for item in raw_fanouts:
            if not isinstance(item, dict) or set(item) != {
                "start_x_mm",
                "start_y_mm",
                "end_x_mm",
                "end_y_mm",
                "width_mm",
                "layer",
            }:
                return _fail("Ground fanout has an invalid shape")
            layer = item["layer"]
            try:
                numbers = tuple(
                    float(item[name])
                    for name in (
                        "start_x_mm",
                        "start_y_mm",
                        "end_x_mm",
                        "end_y_mm",
                        "width_mm",
                    )
                )
            except (TypeError, ValueError):
                return _fail("Ground fanout dimensions are invalid")
            if (
                layer not in {"F.Cu", "B.Cu"}
                or not all(math.isfinite(value) for value in numbers)
                or numbers[4] <= 0
                or numbers[:2] == numbers[2:4]
            ):
                return _fail("Ground fanout dimensions are out of range")
            fanouts.append((*numbers, layer))
        vias: list[tuple[float, float, float, float]] = []
        for item in raw_vias:
            if not isinstance(item, dict) or set(item) != {
                "x_mm",
                "y_mm",
                "diameter_mm",
                "drill_mm",
            }:
                return _fail("Ground via has an invalid shape")
            try:
                x_mm, y_mm, diameter_mm, drill_mm = (
                    float(item[name]) for name in ("x_mm", "y_mm", "diameter_mm", "drill_mm")
                )
            except (TypeError, ValueError):
                return _fail("Ground via dimensions are invalid")
            via_values = (x_mm, y_mm, diameter_mm, drill_mm)
            if (
                not all(math.isfinite(value) for value in via_values)
                or via_values[2] <= 0
                or via_values[3] <= 0
                or via_values[3] >= via_values[2]
            ):
                return _fail("Ground via dimensions are out of range")
            vias.append(via_values)
        seen_nets.add(net_name)
        requested_layers.update(layers)
        domains.append((net_name, layers, pad_connection, regions, fanouts, vias))

    board = pcbnew.LoadBoard(str(source))
    if board is None:
        return _fail("KiCad failed to load the PCB")
    nets = {net_name: board.FindNet(net_name) for net_name, _, _, _, _, _ in domains}
    if any(net is None for net in nets.values()):
        return _fail("A ground domain net was not found")
    selected_nets = set(nets)
    existing = [zone for zone in board.Zones() if zone.GetNetname() in selected_nets]
    if existing and not replace_existing:
        return _fail("A selected ground domain already contains zones; enable reviewed replacement")
    if replace_existing:
        for zone in existing:
            board.Remove(zone)
    current_layers = board.GetCopperLayerCount()
    if copper_layers < current_layers:
        if any(
            board.GetLayerName(track.GetLayer()).startswith("In") for track in board.GetTracks()
        ):
            return _fail("PCB contains tracks on copper layers that would be removed")
        if any(board.GetLayerName(zone.GetLayer()).startswith("In") for zone in board.Zones()):
            return _fail("PCB contains non-selected zones on copper layers that would be removed")
    board.SetCopperLayerCount(copper_layers)
    layer_ids = {layer: board.GetLayerID(layer) for layer in requested_layers}
    if any(
        layer_id < 0 or not board.GetEnabledLayers().Contains(layer_id)
        for layer_id in layer_ids.values()
    ):
        return _fail("Requested ground region layer is not enabled in the target stackup")

    outline = pcbnew.SHAPE_POLY_SET()
    if not board.GetBoardPolygonOutlines(outline, False) or outline.OutlineCount() == 0:
        return _fail("A valid closed Edge.Cuts outline is required")
    outline.Inflate(
        -pcbnew.FromMM(values["edge_clearance_mm"]),
        pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
        pcbnew.FromMM(0.01),
        True,
    )
    if outline.OutlineCount() == 0:
        return _fail("Ground plane edge clearance consumes the board outline")
    local_shapes: dict[str, list[tuple[str, tuple[float, float, float, float], Any]]] = {}
    for net_name, _, _, regions, _, _ in domains:
        for layer, kind, region_bounds in regions:
            if kind != "local" or region_bounds is None:
                continue
            shape = pcbnew.SHAPE_POLY_SET()
            index = shape.NewOutline()
            min_x, min_y, max_x, max_y = region_bounds
            for x_mm, y_mm in (
                (min_x, min_y),
                (max_x, min_y),
                (max_x, max_y),
                (min_x, max_y),
            ):
                shape.Append(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm), index)
            shape.BooleanIntersection(outline)
            if shape.OutlineCount() == 0:
                return _fail("A local ground region lies outside Edge.Cuts")
            local_shapes.setdefault(layer, []).append((net_name, region_bounds, shape))

    zone_shapes: list[Any] = []
    for net_name, _, pad_connection, regions, fanouts, vias in domains:
        net = nets[net_name]
        for region_index, (layer, kind, region_bounds) in enumerate(regions):
            if kind == "board":
                shape = pcbnew.SHAPE_POLY_SET(outline)
                reserved = pcbnew.SHAPE_POLY_SET()
                for other_net, _, local_shape in local_shapes.get(layer, []):
                    if other_net != net_name:
                        reserved.BooleanAdd(local_shape)
                if reserved.OutlineCount():
                    reserved.Inflate(
                        pcbnew.FromMM(values["clearance_mm"]),
                        pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                        pcbnew.FromMM(0.01),
                        True,
                    )
                    shape.BooleanSubtract(reserved)
            else:
                shape = next(
                    item
                    for other_net, stored_bounds, item in local_shapes[layer]
                    if other_net == net_name and stored_bounds == region_bounds
                )
            if shape.OutlineCount() == 0:
                return _fail("Ground region clipping produced an empty shape")
            zone = pcbnew.ZONE(board)
            zone.SetNet(net)
            zone.SetLayer(layer_ids[layer])
            zone.SetOutline(shape)
            zone_shapes.append(shape)
            if hasattr(zone, "SetZoneName"):
                zone.SetZoneName(f"Copperbrain:{net_name}:{kind}:{region_index}")
            if hasattr(zone, "SetAssignedPriority"):
                zone.SetAssignedPriority(region_index + 1 if kind == "local" else 0)
            zone.SetLocalClearance(pcbnew.FromMM(values["clearance_mm"]))
            zone.SetMinThickness(pcbnew.FromMM(values["min_thickness_mm"]))
            zone.SetPadConnection(
                pcbnew.ZONE_CONNECTION_FULL
                if pad_connection == "solid" or kind == "local"
                else pcbnew.ZONE_CONNECTION_THERMAL
            )
            zone.SetThermalReliefGap(pcbnew.FromMM(values["thermal_gap_mm"]))
            zone.SetThermalReliefSpokeWidth(pcbnew.FromMM(values["thermal_spoke_width_mm"]))
            zone.SetFillMode(pcbnew.ZONE_FILL_MODE_POLYGONS)
            board.Add(zone)
        for start_x, start_y, end_x, end_y, width, layer in fanouts:
            start = pcbnew.VECTOR2I(pcbnew.FromMM(start_x), pcbnew.FromMM(start_y))
            end = pcbnew.VECTOR2I(pcbnew.FromMM(end_x), pcbnew.FromMM(end_y))
            if not outline.Contains(start) or not outline.Contains(end):
                return _fail("Ground fanout lies outside the ground-plane outline")
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(start)
            track.SetEnd(end)
            track.SetWidth(pcbnew.FromMM(width))
            track.SetLayer(board.GetLayerID(layer))
            track.SetNet(net)
            board.Add(track)
        for x_mm, y_mm, diameter_mm, drill_mm in vias:
            position = pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm))
            if not outline.Contains(position):
                return _fail("Ground via lies outside the ground-plane outline")
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(position)
            via.SetWidth(pcbnew.FromMM(diameter_mm))
            via.SetDrill(pcbnew.FromMM(drill_mm))
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            via.SetNet(net)
            board.Add(via)
    board.BuildConnectivity()
    if not pcbnew.ZONE_FILLER(board).Fill(board.Zones(), False):
        return _fail("KiCad failed to fill the ground zones")
    if not pcbnew.SaveBoard(str(destination), board) or not destination.is_file():
        return _fail("KiCad failed to save the grounded PCB")
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) == 3 and values[0] == "create-board":
        try:
            layers = int(values[2])
        except ValueError:
            return _fail("Copper layer count must be an integer")
        return _create_board(Path(values[1]).resolve(), layers)
    if len(values) == 4 and values[0] == "set-copper-layers":
        try:
            layers = int(values[3])
        except ValueError:
            return _fail("Copper layer count must be an integer")
        return _set_copper_layers(
            Path(values[1]).resolve(),
            Path(values[2]).resolve(),
            layers,
        )
    if len(values) == 4 and values[0] == "apply-placement":
        return _apply_placement(
            Path(values[1]).resolve(),
            Path(values[2]).resolve(),
            Path(values[3]).resolve(),
        )
    if len(values) == 4 and values[0] == "apply-grounding":
        return _apply_grounding(
            Path(values[1]).resolve(),
            Path(values[2]).resolve(),
            Path(values[3]).resolve(),
        )
    return _fail(
        "Usage: kicad_project_worker.py create-board OUTPUT LAYERS | "
        "set-copper-layers INPUT OUTPUT LAYERS | apply-placement INPUT OUTPUT MANIFEST | "
        "apply-grounding INPUT OUTPUT MANIFEST"
    )


if __name__ == "__main__":
    raise SystemExit(main())
