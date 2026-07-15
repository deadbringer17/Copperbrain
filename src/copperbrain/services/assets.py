"""Idempotent local asset import through validated project operations."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from copperbrain.adapters.library_tables import ensure_library_entry, validate_nickname
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    AssetImportResult,
    ComponentAssetBundle,
    ErrorCode,
    ValidationReport,
)

_EXTENSIONS = {
    "symbol": {".kicad_sym"},
    "footprint": {".kicad_mod"},
    "model_3d": {".step", ".stp", ".wrl"},
    "datasheet": {".pdf"},
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_source(path: Path, kind: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CopperbrainError(
            ErrorCode.NOT_FOUND, "Component asset was not found", details={"path": str(resolved)}
        )
    if resolved.suffix.casefold() not in _EXTENSIONS[kind]:
        raise CopperbrainError(
            ErrorCode.INVALID_INPUT,
            "Component asset has an unsupported extension",
            details={"kind": kind, "extension": resolved.suffix},
        )
    return resolved


def _asset_numbers(path: Path, kind: str) -> set[str]:
    """Extract symbol pin or footprint pad identifiers for a base correspondence check."""
    content = path.read_text(encoding="utf-8-sig")
    if kind == "symbol":
        if not content.lstrip().startswith("(kicad_symbol_lib"):
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Invalid KiCad symbol library")
        return set(re.findall(r'\(number\s+"([^"\r\n]+)"', content))
    if kind == "footprint":
        if not content.lstrip().startswith("(footprint"):
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Invalid KiCad footprint")
        return set(re.findall(r'\(pad\s+"([^"\r\n]+)"', content))
    return set()


def validate_pin_pad_correspondence(symbol: Path, footprint: Path) -> tuple[str, ...]:
    """Require non-empty, exact logical pin-to-copper-pad number correspondence."""
    pins = _asset_numbers(symbol, "symbol")
    pads = _asset_numbers(footprint, "footprint")
    if not pins or not pads or pins != pads:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "Symbol pins do not correspond to footprint pads",
            details={"pins": sorted(pins), "pads": sorted(pads)},
        )
    return tuple(sorted(pins))


def _copy_atomic(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and _digest(source) == _digest(destination):
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


class AssetService:
    """Import an already-resolved bundle; network/vendor behavior stays in adapters."""

    def import_bundle(self, project_root: Path, bundle: ComponentAssetBundle) -> AssetImportResult:
        root = project_root.expanduser().resolve()
        if not root.is_dir():
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Project root was not found")
        nickname = validate_nickname(bundle.nickname)
        sources = {
            "symbol": _validate_source(bundle.symbol, "symbol"),
            "footprint": _validate_source(bundle.footprint, "footprint"),
        }
        if bundle.model_3d:
            sources["model_3d"] = _validate_source(bundle.model_3d, "model_3d")
        if bundle.datasheet:
            sources["datasheet"] = _validate_source(bundle.datasheet, "datasheet")
        matched_numbers = validate_pin_pad_correspondence(sources["symbol"], sources["footprint"])
        base = root / "copperbrain-libs"
        destinations = {
            "symbol": base / f"{nickname}.kicad_sym",
            "footprint": base / f"{nickname}.pretty" / sources["footprint"].name,
        }
        if "model_3d" in sources:
            destinations["model_3d"] = base / f"{nickname}.3dshapes" / sources["model_3d"].name
        if "datasheet" in sources:
            destinations["datasheet"] = base / "datasheets" / sources["datasheet"].name
        changed = [_copy_atomic(source, destinations[kind]) for kind, source in sources.items()]
        sym_table = root / "sym-lib-table"
        fp_table = root / "fp-lib-table"
        table_changes = [
            ensure_library_entry(
                sym_table,
                table_kind="sym_lib_table",
                nickname=nickname,
                uri=f"${{KIPRJMOD}}/copperbrain-libs/{nickname}.kicad_sym",
            ),
            ensure_library_entry(
                fp_table,
                table_kind="fp_lib_table",
                nickname=nickname,
                uri=f"${{KIPRJMOD}}/copperbrain-libs/{nickname}.pretty",
            ),
        ]
        checks = {kind: destination.is_file() for kind, destination in destinations.items()}
        checks["sym_lib_table"] = sym_table.is_file()
        checks["fp_lib_table"] = fp_table.is_file()
        checks["pin_pad_correspondence"] = bool(matched_numbers)
        valid = all(checks.values())
        return AssetImportResult(
            lcsc=bundle.lcsc,
            imported_files=tuple(destinations.values()),
            library_tables=(sym_table, fp_table),
            idempotent=not any((*changed, *table_changes)),
            validation=ValidationReport(valid=valid, checks=checks),
        )
