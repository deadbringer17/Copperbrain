"""Read-only footprint geometry analysis and controlled courtyard generation."""

from __future__ import annotations

import math
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.errors import CopperbrainError
from copperbrain.models import CourtyardAddition, ErrorCode, FootprintConstraintCandidate

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_PAD_HEADER = re.compile(r'^\(pad\s+"?([^"\s)]+)"?\s+([^\s)]+)(?:\s+([^\s)]+))?')
_AT = re.compile(rf"\(at\s+({_NUMBER})\s+({_NUMBER})(?:\s+({_NUMBER}))?")
_SIZE = re.compile(rf"\(size\s+({_NUMBER})\s+({_NUMBER})")
_POINT = re.compile(rf"\((?:start|end|center)\s+({_NUMBER})\s+({_NUMBER})")
_XY = re.compile(rf"\(xy\s+({_NUMBER})\s+({_NUMBER})")
_WIDTH = re.compile(rf"\(width\s+({_NUMBER})\s*\)")
_LIBRARY = re.compile(r'\(lib\s+\(name\s+"([^"]+)"\).*?\(uri\s+"([^"]+)"\)', re.DOTALL)


@dataclass(frozen=True)
class PadGeometry:
    number: str
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    custom: bool = False


@dataclass(frozen=True)
class FootprintGeometry:
    reference: str
    library_id: str
    source: Path
    pads: tuple[PadGeometry, ...]
    has_courtyard: bool
    min_x_mm: float
    min_y_mm: float
    max_x_mm: float
    max_y_mm: float

    @property
    def pad_min_dimension_mm(self) -> float:
        return min(min(pad.width_mm, pad.height_mm) for pad in self.pads)

    @property
    def min_pitch_mm(self) -> float | None:
        distances = [
            math.hypot(left.x_mm - right.x_mm, left.y_mm - right.y_mm)
            for index, left in enumerate(self.pads)
            for right in self.pads[index + 1 :]
            if left.number != right.number and (left.x_mm, left.y_mm) != (right.x_mm, right.y_mm)
        ]
        return min(distances) if distances else None

    @property
    def pad_min_clearance_mm(self) -> float | None:
        """Minimum edge-to-edge gap between axis-aligned electrical pad bounds."""
        return self.pad_min_clearance_for_pin_nets()

    def pad_min_clearance_for_pin_nets(
        self, pin_nets: dict[str, str] | None = None
    ) -> float | None:
        """Measure clearance while allowing overlapping pads on the same electrical net."""
        clearances = []
        for index, left in enumerate(self.pads):
            for right in self.pads[index + 1 :]:
                if left.number == right.number or (
                    left.x_mm,
                    left.y_mm,
                ) == (right.x_mm, right.y_mm):
                    continue
                if (
                    pin_nets is not None
                    and pin_nets.get(left.number)
                    and pin_nets.get(left.number) == pin_nets.get(right.number)
                ):
                    continue
                x_gap = max(
                    abs(left.x_mm - right.x_mm) - (left.width_mm + right.width_mm) / 2,
                    0.0,
                )
                y_gap = max(
                    abs(left.y_mm - right.y_mm) - (left.height_mm + right.height_mm) / 2,
                    0.0,
                )
                if (
                    x_gap == 0
                    and y_gap == 0
                    and left.x_mm != right.x_mm
                    and left.y_mm != right.y_mm
                    and (left.custom or right.custom)
                ):
                    # IPC-style corner pads use chamfered custom polygons. Their rectangular
                    # bounds overlap diagonally even though the copper polygons do not.
                    continue
                clearances.append(math.hypot(x_gap, y_gap))
        return min(clearances) if clearances else None


def _extract_forms(content: str, keyword: str) -> tuple[str, ...]:
    forms: list[str] = []
    marker = f"({keyword}"
    offset = 0
    while True:
        start = content.find(marker, offset)
        if start < 0:
            break
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(content)):
            character = content[index]
            if escaped:
                escaped = False
            elif quoted and character == "\\":
                escaped = True
            elif character == '"':
                quoted = not quoted
            elif not quoted and character == "(":
                depth += 1
            elif not quoted and character == ")":
                depth -= 1
                if depth == 0:
                    forms.append(content[start : index + 1])
                    offset = index + 1
                    break
        else:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                f"Unbalanced KiCad {keyword} expression",
            )
    return tuple(forms)


def parse_footprint_geometry(
    path: Path,
    *,
    reference: str,
    library_id: str,
) -> FootprintGeometry:
    """Parse the pad and body bounds needed for deterministic fanout limits."""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise CopperbrainError(
            ErrorCode.NOT_FOUND,
            "Footprint file could not be read",
            details={"path": str(path)},
        ) from exc
    if not content.lstrip().startswith("(footprint"):
        raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Invalid KiCad footprint file")
    pads: list[PadGeometry] = []
    for form in _extract_forms(content, "pad"):
        header = _PAD_HEADER.search(form)
        position = _AT.search(form)
        size = _SIZE.search(form)
        if header is None or position is None or size is None:
            continue
        if header.group(2).casefold() == "np_thru_hole":
            continue
        width = float(size.group(1))
        height = float(size.group(2))
        custom = (header.group(3) or "").casefold() == "custom"
        if custom:
            points = [(float(x), float(y)) for x, y in _XY.findall(form)]
            if points:
                stroke = max((float(value) for value in _WIDTH.findall(form)), default=0.0)
                width = max(width, max(x for x, _ in points) - min(x for x, _ in points) + stroke)
                height = max(
                    height,
                    max(y for _, y in points) - min(y for _, y in points) + stroke,
                )
        angle = math.radians(float(position.group(3) or 0))
        rotated_width = abs(width * math.cos(angle)) + abs(height * math.sin(angle))
        rotated_height = abs(width * math.sin(angle)) + abs(height * math.cos(angle))
        pads.append(
            PadGeometry(
                number=header.group(1),
                x_mm=float(position.group(1)),
                y_mm=float(position.group(2)),
                width_mm=rotated_width,
                height_mm=rotated_height,
                custom=custom,
            )
        )
    if not pads:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "Footprint contains no measurable electrical pads",
            details={"footprint": library_id, "path": str(path)},
        )
    min_x = min(pad.x_mm - pad.width_mm / 2 for pad in pads)
    min_y = min(pad.y_mm - pad.height_mm / 2 for pad in pads)
    max_x = max(pad.x_mm + pad.width_mm / 2 for pad in pads)
    max_y = max(pad.y_mm + pad.height_mm / 2 for pad in pads)
    for x_text, y_text in _POINT.findall(content):
        x = float(x_text)
        y = float(y_text)
        min_x, min_y = min(min_x, x), min(min_y, y)
        max_x, max_y = max(max_x, x), max(max_y, y)
    return FootprintGeometry(
        reference=reference,
        library_id=library_id,
        source=path,
        pads=tuple(pads),
        has_courtyard='(layer "F.CrtYd")' in content or '(layer "B.CrtYd")' in content,
        min_x_mm=min_x,
        min_y_mm=min_y,
        max_x_mm=max_x,
        max_y_mm=max_y,
    )


def _project_libraries(project_root: Path) -> dict[str, Path]:
    table = project_root / "fp-lib-table"
    if not table.is_file():
        return {}
    content = table.read_text(encoding="utf-8-sig")
    libraries: dict[str, Path] = {}
    root = project_root.resolve()
    for nickname, uri in _LIBRARY.findall(content):
        if not uri.startswith("${KIPRJMOD}/"):
            continue
        candidate = (root / uri.removeprefix("${KIPRJMOD}/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        libraries[nickname] = candidate
    return libraries


def resolve_footprint(project_root: Path, library_id: str) -> Path | None:
    """Resolve project-local or installed standard footprint libraries dynamically."""
    if ":" not in library_id:
        return None
    nickname, name = library_id.split(":", 1)
    if not nickname or not name or Path(name).name != name:
        return None
    project_library = _project_libraries(project_root).get(nickname)
    if project_library is not None:
        candidate = project_library / f"{name}.kicad_mod"
        return candidate if candidate.is_file() else None
    cli = detect_kicad().selected_cli
    if cli is None:
        return None
    candidate = cli.parent.parent / "share" / "kicad" / "footprints" / f"{nickname}.pretty"
    candidate /= f"{name}.kicad_mod"
    return candidate if candidate.is_file() else None


def analyze_component_footprint(
    project_root: Path,
    *,
    reference: str,
    library_id: str,
    width_ratio: float,
    pin_nets: dict[str, str] | None = None,
) -> tuple[FootprintGeometry | None, FootprintConstraintCandidate]:
    path = resolve_footprint(project_root, library_id)
    if path is None:
        return None, FootprintConstraintCandidate(
            reference=reference,
            footprint=library_id,
            warnings=("Footprint file could not be resolved",),
        )
    try:
        geometry = parse_footprint_geometry(path, reference=reference, library_id=library_id)
    except CopperbrainError as exc:
        return None, FootprintConstraintCandidate(
            reference=reference,
            footprint=library_id,
            source=path,
            warnings=(exc.error.message,),
        )
    safe_width = math.floor((geometry.pad_min_dimension_mm * width_ratio + 0.0005) * 100) / 100
    pad_clearance = geometry.pad_min_clearance_for_pin_nets(pin_nets)
    safe_clearance = (
        math.floor((pad_clearance + 0.0005) * 100) / 100 if pad_clearance is not None else None
    )
    warnings = () if geometry.has_courtyard else ("Footprint has no courtyard",)
    return geometry, FootprintConstraintCandidate(
        reference=reference,
        footprint=library_id,
        source=path,
        pad_count=len(geometry.pads),
        pad_min_dimension_mm=geometry.pad_min_dimension_mm,
        min_pitch_mm=geometry.min_pitch_mm,
        safe_fanout_width_mm=safe_width,
        safe_clearance_mm=safe_clearance,
        has_courtyard=geometry.has_courtyard,
        warnings=warnings,
    )


def add_generated_courtyard(path: Path, addition: CourtyardAddition) -> None:
    """Atomically add one allowlisted rectangular courtyard to a project footprint."""
    content = path.read_text(encoding="utf-8-sig")
    if '(layer "F.CrtYd")' in content or '(layer "B.CrtYd")' in content:
        return
    closing = content.rfind(")")
    if closing < 0 or not content.lstrip().startswith("(footprint"):
        raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Invalid footprint for courtyard")
    identifier = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"copperbrain:{addition.footprint}:{addition.model_dump_json()}",
    )
    rectangle = (
        f"  (fp_rect (start {addition.min_x_mm:g} {addition.min_y_mm:g}) "
        f"(end {addition.max_x_mm:g} {addition.max_y_mm:g})\n"
        f"    (stroke (width {addition.line_width_mm:g}) (type default))\n"
        f'    (fill none) (layer "F.CrtYd") (uuid "{identifier}"))\n'
    )
    updated = f"{content[:closing].rstrip()}\n{rectangle}{content[closing:]}"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(updated)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
