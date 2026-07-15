"""Safe, fixed-command adapter for read-only KiCad CLI operations."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    Component,
    DrcReport,
    DrcViolation,
    ErcReport,
    ErcViolation,
    ErrorCode,
    Net,
    NetPin,
    StructuredError,
    ValidationReport,
)


def _run(command: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run one allowlisted KiCad command without a shell."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def upgrade_pcb(cli: Path | None, pcb: Path) -> None:
    """Initialize or normalize one temporary PCB through a fixed KiCad command."""
    if cli is None or not cli.is_file():
        raise CopperbrainError(
            ErrorCode.INTEGRATION_UNAVAILABLE,
            "KiCad CLI is required to initialize the PCB",
        )
    try:
        result = _run([str(cli), "pcb", "upgrade", "--force", str(pcb)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CopperbrainError(
            ErrorCode.INTEGRATION_UNAVAILABLE,
            "KiCad could not initialize the PCB",
            details={"reason": str(exc)},
        ) from exc
    if result.returncode != 0:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "KiCad rejected the generated PCB",
            details={"reason": result.stderr.strip() or result.stdout.strip()},
        )


def _text(parent: ET.Element, path: str, default: str = "") -> str:
    node = parent.find(path)
    return node.text.strip() if node is not None and node.text else default


def parse_kicad_xml_netlist(content: str) -> tuple[tuple[Component, ...], tuple[Net, ...]]:
    """Normalize KiCad XML netlist components and electrical nets."""
    root = ET.fromstring(content)
    components: list[Component] = []
    for node in root.findall("./components/comp"):
        libsource = node.find("libsource")
        lib_id = None
        if libsource is not None:
            library = libsource.attrib.get("lib", "")
            part = libsource.attrib.get("part", "")
            lib_id = f"{library}:{part}" if library or part else None
        properties = {
            field.attrib.get("name", ""): (field.text or "")
            for field in node.findall("./fields/field")
            if field.attrib.get("name")
        }
        components.append(
            Component(
                reference=node.attrib.get("ref", ""),
                value=_text(node, "value"),
                lib_id=lib_id,
                footprint=_text(node, "footprint") or None,
                properties=properties,
            )
        )
    nets: list[Net] = []
    for node in root.findall("./nets/net"):
        pins = tuple(
            NetPin(
                reference=pin.attrib.get("ref", ""),
                pin=pin.attrib.get("pin", ""),
                pin_name=pin.attrib.get("pinfunction") or None,
            )
            for pin in node.findall("node")
        )
        nets.append(Net(name=node.attrib.get("name", ""), pins=pins))
    return (
        tuple(sorted(components, key=lambda item: item.reference)),
        tuple(sorted(nets, key=lambda item: item.name)),
    )


def export_netlist(cli: Path, schematic: Path) -> tuple[tuple[Component, ...], tuple[Net, ...]]:
    """Export and parse a temporary XML netlist using fixed CLI arguments."""
    with tempfile.TemporaryDirectory(prefix="copperbrain-netlist-") as directory:
        output = Path(directory) / "netlist.xml"
        result = _run(
            [
                str(cli),
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                str(output),
                str(schematic),
            ]
        )
        if result.returncode != 0 or not output.is_file():
            raise RuntimeError(
                result.stderr.strip() or result.stdout.strip() or "netlist export failed"
            )
        return parse_kicad_xml_netlist(output.read_text(encoding="utf-8"))


def parse_erc_json(payload: dict[str, object]) -> tuple[ErcViolation, ...]:
    """Normalize the stable subset of KiCad ERC JSON."""
    violations: list[ErcViolation] = []
    sheets = payload.get("sheets", [])
    if not isinstance(sheets, list):
        return ()
    for sheet in sheets:
        if not isinstance(sheet, dict):
            continue
        entries = sheet.get("violations", [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            severity = str(entry.get("severity", "unknown")).lower()
            normalized = severity if severity in {"error", "warning", "info"} else "unknown"
            item_values = entry.get("items", [])
            items = (
                tuple(
                    str(item.get("description", item.get("uuid", "")))
                    for item in item_values
                    if isinstance(item, dict)
                )
                if isinstance(item_values, list)
                else ()
            )
            violations.append(
                ErcViolation(
                    severity=normalized,  # type: ignore[arg-type]
                    code=str(entry.get("type", entry.get("code", ""))) or None,
                    message=str(entry.get("description", entry.get("message", "ERC violation"))),
                    items=items,
                )
            )
    return tuple(violations)


def run_erc(cli: Path | None, schematic: Path) -> ErcReport:
    """Run KiCad ERC in a temporary directory and return actionable failures."""
    if cli is None or not cli.is_file():
        return ErcReport(
            available=False,
            error=StructuredError(
                code=ErrorCode.INTEGRATION_UNAVAILABLE,
                message="KiCad CLI is unavailable",
                actionable_hint="Install KiCad 10 or configure it on PATH.",
            ),
        )
    with tempfile.TemporaryDirectory(prefix="copperbrain-erc-") as directory:
        output = Path(directory) / "erc.json"
        try:
            result = _run(
                [
                    str(cli),
                    "sch",
                    "erc",
                    "--format",
                    "json",
                    "--output",
                    str(output),
                    str(schematic),
                ]
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ErcReport(
                available=False,
                error=StructuredError(code=ErrorCode.INTEGRATION_UNAVAILABLE, message=str(exc)),
            )
        if result.returncode != 0 or not output.is_file():
            return ErcReport(
                available=True,
                error=StructuredError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message=result.stderr.strip() or result.stdout.strip() or "ERC failed",
                ),
            )
        payload = json.loads(output.read_text(encoding="utf-8-sig"))
        return ErcReport(available=True, violations=parse_erc_json(payload))


def parse_drc_json(payload: dict[str, object]) -> tuple[DrcViolation, ...]:
    """Normalize the stable subset of KiCad 10 PCB DRC JSON."""
    raw_violations = payload.get("violations", [])
    if not isinstance(raw_violations, list):
        return ()
    violations: list[DrcViolation] = []
    for entry in raw_violations:
        if not isinstance(entry, dict):
            continue
        severity = str(entry.get("severity", "unknown")).lower()
        normalized = severity if severity in {"error", "warning", "info"} else "unknown"
        raw_items = entry.get("items", [])
        items = (
            tuple(
                str(item.get("description", item.get("uuid", "")))
                for item in raw_items
                if isinstance(item, dict)
            )
            if isinstance(raw_items, list)
            else ()
        )
        violations.append(
            DrcViolation(
                severity=normalized,  # type: ignore[arg-type]
                code=str(entry.get("type", entry.get("code", ""))) or None,
                message=str(entry.get("description", entry.get("message", "DRC violation"))),
                items=items,
            )
        )
    return tuple(violations)


def run_drc(cli: Path | None, pcb: Path | None) -> DrcReport:
    """Run PCB DRC using fixed arguments and a temporary JSON report."""
    if pcb is None or not pcb.is_file():
        return DrcReport(
            available=False,
            error=StructuredError(
                code=ErrorCode.NOT_FOUND,
                message="Project contains no PCB file",
            ),
        )
    if cli is None or not cli.is_file():
        return DrcReport(
            available=False,
            error=StructuredError(
                code=ErrorCode.INTEGRATION_UNAVAILABLE,
                message="KiCad CLI is unavailable",
                actionable_hint="Install KiCad 10 or configure it on PATH.",
            ),
        )
    with tempfile.TemporaryDirectory(prefix="copperbrain-drc-") as directory:
        output = Path(directory) / "drc.json"
        try:
            result = _run(
                [
                    str(cli),
                    "pcb",
                    "drc",
                    "--format",
                    "json",
                    "--output",
                    str(output),
                    str(pcb),
                ]
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return DrcReport(
                available=False,
                error=StructuredError(code=ErrorCode.INTEGRATION_UNAVAILABLE, message=str(exc)),
            )
        if result.returncode != 0 or not output.is_file():
            return DrcReport(
                available=True,
                error=StructuredError(
                    code=ErrorCode.VALIDATION_FAILED,
                    message=result.stderr.strip() or result.stdout.strip() or "DRC failed",
                ),
            )
        payload = json.loads(output.read_text(encoding="utf-8-sig"))
        raw_unconnected = payload.get("unconnected_items", [])
        unconnected = (
            tuple(
                str(item.get("description", item.get("uuid", "")))
                for item in raw_unconnected
                if isinstance(item, dict)
            )
            if isinstance(raw_unconnected, list)
            else ()
        )
        return DrcReport(
            available=True,
            violations=parse_drc_json(payload),
            unconnected_items=unconnected,
        )


def validate_footprint(cli: Path | None, footprint: Path) -> ValidationReport:
    """Ask KiCad to parse and resave a footprint only into a temporary output directory."""
    if cli is None or not cli.is_file():
        return ValidationReport(
            valid=False,
            checks={"kicad_footprint_parse": False},
            messages=("KiCad CLI is unavailable for footprint validation",),
        )
    with tempfile.TemporaryDirectory(prefix="copperbrain-footprint-") as directory:
        output = Path(directory) / "Validated.pretty"
        try:
            result = _run(
                [
                    str(cli),
                    "fp",
                    "upgrade",
                    "--force",
                    "--output",
                    str(output),
                    str(footprint.parent),
                ]
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ValidationReport(
                valid=False,
                checks={"kicad_footprint_parse": False},
                messages=(str(exc),),
            )
        valid = result.returncode == 0 and (output / footprint.name).is_file()
        message = result.stderr.strip() or result.stdout.strip()
        return ValidationReport(
            valid=valid,
            checks={"kicad_footprint_parse": valid},
            messages=() if valid else (message or "KiCad footprint validation failed",),
        )


def export_schematic_pdf(cli: Path | None, schematic: Path, destination: Path) -> Path:
    """Export a schematic PDF atomically with a fixed KiCad CLI command."""
    if cli is None or not cli.is_file():
        raise CopperbrainError(
            ErrorCode.INTEGRATION_UNAVAILABLE,
            "KiCad CLI is unavailable for PDF export",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".pdf", dir=destination.parent
    )
    os.close(descriptor)
    os.unlink(temporary_name)
    temporary = Path(temporary_name)
    try:
        result = _run(
            [
                str(cli),
                "sch",
                "export",
                "pdf",
                "--output",
                str(temporary),
                str(schematic),
            ]
        )
        if result.returncode != 0 or not temporary.is_file():
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad could not export the schematic preview PDF",
                details={"reason": result.stderr.strip() or result.stdout.strip()},
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def export_pcb_pdf(cli: Path | None, pcb: Path, destination: Path) -> Path:
    """Export a PCB PDF atomically with a fixed KiCad CLI command."""
    if cli is None or not cli.is_file():
        raise CopperbrainError(
            ErrorCode.INTEGRATION_UNAVAILABLE,
            "KiCad CLI is unavailable for PCB PDF export",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".pdf", dir=destination.parent
    )
    os.close(descriptor)
    os.unlink(temporary_name)
    temporary = Path(temporary_name)
    try:
        result = _run(
            [
                str(cli),
                "pcb",
                "export",
                "pdf",
                "--layers",
                "F.Cu,B.Cu,F.Silkscreen,B.Silkscreen,Edge.Cuts",
                "--output",
                str(temporary),
                str(pcb),
            ]
        )
        if result.returncode != 0 or not temporary.is_file():
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "KiCad could not export the PCB preview PDF",
                details={"reason": result.stderr.strip() or result.stdout.strip()},
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination
