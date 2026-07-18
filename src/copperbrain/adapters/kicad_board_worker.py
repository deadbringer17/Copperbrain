"""Fixed-action KiCad Python worker for post-route board operations."""

from __future__ import annotations

import sys
from pathlib import Path

import pcbnew  # type: ignore[import-not-found]


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _refill(pcb: Path) -> int:
    if pcb.suffix.casefold() != ".kicad_pcb" or not pcb.is_file():
        return _fail("PCB input for zone refill is missing or invalid")
    board = pcbnew.LoadBoard(str(pcb))
    if board is None:
        return _fail("KiCad failed to load the PCB for zone refill")
    board.BuildConnectivity()
    if not pcbnew.ZONE_FILLER(board).Fill(board.Zones(), False):
        return _fail("KiCad failed to refill PCB zones")
    if not pcbnew.SaveBoard(str(pcb), board):
        return _fail("KiCad failed to save the zone-refilled PCB")
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) == 2 and values[0] == "refill":
        return _refill(Path(values[1]).resolve())
    return _fail("Usage: kicad_board_worker.py refill PCB")


if __name__ == "__main__":
    raise SystemExit(main())
