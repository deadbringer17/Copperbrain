"""Fixed-action KiCad Python worker for headless Specctra DSN/SES exchange."""

from __future__ import annotations

import sys
from pathlib import Path

import pcbnew  # type: ignore[import-not-found]


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _export(pcb: Path, dsn: Path) -> int:
    if pcb.suffix.lower() != ".kicad_pcb" or not pcb.is_file():
        return _fail("Input PCB is missing or has an invalid extension")
    if dsn.suffix.lower() != ".dsn" or not dsn.parent.is_dir():
        return _fail("DSN destination is invalid")
    board = pcbnew.LoadBoard(str(pcb))
    if board is None or not pcbnew.ExportSpecctraDSN(board, str(dsn)) or not dsn.is_file():
        return _fail("KiCad failed to export Specctra DSN")
    return 0


def _import(pcb: Path, ses: Path, destination: Path) -> int:
    if pcb.suffix.lower() != ".kicad_pcb" or not pcb.is_file():
        return _fail("Input PCB is missing or has an invalid extension")
    if ses.suffix.lower() != ".ses" or not ses.is_file():
        return _fail("Specctra session is missing or invalid")
    if destination.suffix.lower() != ".kicad_pcb" or not destination.parent.is_dir():
        return _fail("Routed PCB destination is invalid")
    board = pcbnew.LoadBoard(str(pcb))
    if board is None or not pcbnew.ImportSpecctraSES(board, str(ses)):
        return _fail("KiCad failed to import Specctra SES")
    if not pcbnew.SaveBoard(str(destination), board) or not destination.is_file():
        return _fail("KiCad failed to save the routed PCB")
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) == 3 and values[0] == "export":
        return _export(Path(values[1]).resolve(), Path(values[2]).resolve())
    if len(values) == 4 and values[0] == "import":
        return _import(
            Path(values[1]).resolve(),
            Path(values[2]).resolve(),
            Path(values[3]).resolve(),
        )
    return _fail("Usage: kicad_specctra_worker.py export PCB DSN | import PCB SES OUTPUT")


if __name__ == "__main__":
    raise SystemExit(main())
