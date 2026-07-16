"""Fixed-action KiCad Python worker for creating and relayering a PCB."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

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
    return _fail(
        "Usage: kicad_project_worker.py create-board OUTPUT LAYERS | "
        "set-copper-layers INPUT OUTPUT LAYERS | apply-placement INPUT OUTPUT MANIFEST"
    )


if __name__ == "__main__":
    raise SystemExit(main())
