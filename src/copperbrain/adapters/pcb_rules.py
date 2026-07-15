"""Validated KiCad project/netclass and managed custom-rule adapter."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ErrorCode,
    ExistingNetClass,
    NetClassAssignment,
    PcbRuleSet,
    ValidationReport,
)

MANAGED_BEGIN = "# BEGIN COPPERBRAIN MANAGED RULES"
MANAGED_END = "# END COPPERBRAIN MANAGED RULES"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_project(project_file: Path) -> dict[str, Any]:
    try:
        payload = json.loads(project_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "KiCad project JSON is invalid",
            details={"path": str(project_file), "reason": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "KiCad project root must be an object")
    return payload


def _net_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = payload.get("net_settings")
    if not isinstance(settings, dict):
        settings = {
            "classes": [],
            "meta": {"version": 4},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        }
        payload["net_settings"] = settings
    return settings


def read_netclasses(
    project_file: Path,
) -> tuple[tuple[ExistingNetClass, ...], tuple[NetClassAssignment, ...]]:
    """Read the stable KiCad 10 netclass subset without modifying the project."""
    settings = _net_settings(_load_project(project_file))
    classes: list[ExistingNetClass] = []
    raw_classes = settings.get("classes", [])
    if isinstance(raw_classes, list):
        for item in raw_classes:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            try:
                classes.append(
                    ExistingNetClass(
                        name=item["name"],
                        clearance_mm=float(item.get("clearance", 0)),
                        track_width_mm=float(item.get("track_width", 0)),
                        via_diameter_mm=float(item.get("via_diameter", 0)),
                        via_drill_mm=float(item.get("via_drill", 0)),
                    )
                )
            except (TypeError, ValueError):
                continue
    assignments: list[NetClassAssignment] = []
    patterns = settings.get("netclass_patterns", [])
    if isinstance(patterns, list):
        for item in patterns:
            if not isinstance(item, dict):
                continue
            netclass = item.get("netclass")
            pattern = item.get("pattern")
            if isinstance(netclass, str) and isinstance(pattern, str):
                assignments.append(NetClassAssignment(net=pattern, netclass=netclass))
    return tuple(classes), tuple(assignments)


def _class_payload(rule: Any, priority: int) -> dict[str, Any]:
    diff_width = rule.diff_pair_width_mm or rule.track_width_preferred_mm
    diff_gap = rule.diff_pair_gap_mm or rule.clearance_mm
    return {
        "bus_width": 12,
        "clearance": rule.clearance_mm,
        "diff_pair_gap": diff_gap,
        "diff_pair_via_gap": diff_gap,
        "diff_pair_width": diff_width,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": rule.name,
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "priority": priority,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        # KiCad treats the netclass track width as a hard DRC minimum. Keep it at the
        # fabrication minimum; the managed rule retains the preferred router width.
        "track_width": rule.track_width_min_mm,
        "via_diameter": rule.via_diameter_mm,
        "via_drill": rule.via_drill_mm,
        "wire_width": 6,
    }


def render_managed_rules(rule_set: PcbRuleSet) -> str:
    """Render only allowlisted constraints from typed values."""
    lines = [MANAGED_BEGIN, "# Generated from typed Copperbrain constraints; do not edit here."]
    for rule in rule_set.classes:
        lines.extend(
            [
                f'(rule "Copperbrain_{rule.name}"',
                f"  (condition \"A.hasNetclass('{rule.name}')\")",
                "  (constraint track_width "
                f"(min {rule.track_width_min_mm:g}mm) "
                f"(opt {rule.track_width_preferred_mm:g}mm))",
                f"  (constraint via_diameter (min {rule.via_diameter_mm:g}mm))",
                f"  (constraint hole_size (min {rule.via_drill_mm:g}mm))",
            ]
        )
        if rule.diff_pair_width_mm is not None:
            lines.append(f"  (constraint diff_pair_width (opt {rule.diff_pair_width_mm:g}mm))")
        if rule.diff_pair_gap_mm is not None:
            lines.append(f"  (constraint diff_pair_gap (opt {rule.diff_pair_gap_mm:g}mm))")
        if rule.max_length_mm is not None:
            lines.append(f"  (constraint length (max {rule.max_length_mm:g}mm))")
        if rule.diff_pair_max_uncoupled_mm is not None:
            lines.append(
                f"  (constraint diff_pair_uncoupled (max {rule.diff_pair_max_uncoupled_mm:g}mm))"
            )
        lines.append(")")
        lines.extend(
            [
                f'(rule "Copperbrain_{rule.name}_clearance"',
                f"  (condition \"A.hasNetclass('{rule.name}') && A.Parent != B.Parent\")",
                f"  (constraint clearance (min {rule.clearance_mm:g}mm))",
                ")",
            ]
        )
        if rule.creepage_mm is not None:
            lines.extend(
                [
                    f'(rule "Copperbrain_{rule.name}_creepage"',
                    f"  (condition \"A.hasNetclass('{rule.name}') && A.Parent != B.Parent\")",
                    f"  (constraint creepage (min {rule.creepage_mm:g}mm))",
                    ")",
                ]
            )
    for fanout in rule_set.fanout_constraints:
        lines.extend(
            [
                f'(rule "Copperbrain_fanout_{fanout.reference}"',
                f"  (condition \"A.intersectsCourtyard('{fanout.reference}')\")",
                "  (constraint track_width "
                f"(min {fanout.min_track_width_mm:g}mm) "
                f"(opt {fanout.max_track_width_mm:g}mm))",
                ")",
                f'(rule "Copperbrain_fanout_clearance_{fanout.reference}"',
                "  (condition \"A.intersectsCourtyard('"
                f"{fanout.reference}') || B.intersectsCourtyard('{fanout.reference}')\")",
                f"  (constraint clearance (min {fanout.clearance_mm:g}mm))",
                ")",
            ]
        )
    lines.append(MANAGED_END)
    return "\n".join(lines)


def read_managed_widths(rule_file: Path) -> tuple[dict[str, float], dict[str, float]]:
    """Read generated class preferences and fanout widths from the managed rule block."""
    if not rule_file.is_file():
        return {}, {}
    content = rule_file.read_text(encoding="utf-8")
    if MANAGED_BEGIN not in content or MANAGED_END not in content:
        return {}, {}
    managed = content.split(MANAGED_BEGIN, 1)[1].split(MANAGED_END, 1)[0]
    classes: dict[str, float] = {}
    fanouts: dict[str, float] = {}
    current: str | None = None
    for line in managed.splitlines():
        rule_match = re.match(r'^\s*\(rule "Copperbrain_([A-Za-z][A-Za-z0-9_-]{0,63})"', line)
        if rule_match:
            current = rule_match.group(1)
            continue
        if current is None or "(constraint track_width " not in line:
            continue
        width_match = re.search(r"\(opt ([0-9]+(?:\.[0-9]+)?)mm\)", line)
        if width_match is None:
            continue
        width = float(width_match.group(1))
        if current.startswith("fanout_"):
            fanouts[current.removeprefix("fanout_")] = width
        elif not current.endswith(("_clearance", "_creepage")):
            classes[current] = width
    return classes, fanouts


def stage_router_project(
    source: Path, destination: Path, preferred_widths: dict[str, float]
) -> None:
    """Create a private project copy whose netclasses carry router-preferred widths."""
    payload = _load_project(source)
    settings = _net_settings(payload)
    classes = settings.get("classes", [])
    if isinstance(classes, list):
        for item in classes:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name in preferred_widths:
                item["track_width"] = preferred_widths[name]
    _atomic_write(destination, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _merge_managed_rules(existing: str, managed: str) -> str:
    if not existing.strip():
        return f"(version 1)\n\n{managed}\n"
    if MANAGED_BEGIN in existing or MANAGED_END in existing:
        if existing.count(MANAGED_BEGIN) != 1 or existing.count(MANAGED_END) != 1:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Custom rule file has malformed Copperbrain managed markers",
            )
        before, remainder = existing.split(MANAGED_BEGIN, 1)
        _, after = remainder.split(MANAGED_END, 1)
        return f"{before.rstrip()}\n\n{managed}{after.rstrip()}\n"
    if "(version 1)" not in existing:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "Existing custom rule file does not declare KiCad rule version 1",
        )
    return f"{existing.rstrip()}\n\n{managed}\n"


def _balanced_rule_syntax(content: str) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for raw_line in content.splitlines():
        line = raw_line.lstrip()
        if line.startswith("#"):
            continue
        for character in raw_line:
            if escaped:
                escaped = False
                continue
            if character == "\\" and quoted:
                escaped = True
            elif character == '"':
                quoted = not quoted
            elif not quoted and character == "(":
                depth += 1
            elif not quoted and character == ")":
                depth -= 1
                if depth < 0:
                    return False
    return depth == 0 and not quoted


def migrate_managed_pair_rules(rule_file: Path) -> bool:
    """Split legacy pair constraints into same-parent-safe Copperbrain rules."""
    if not rule_file.is_file():
        return False
    content = rule_file.read_text(encoding="utf-8")
    if MANAGED_BEGIN not in content and MANAGED_END not in content:
        return False
    if content.count(MANAGED_BEGIN) != 1 or content.count(MANAGED_END) != 1:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "Custom rule file has malformed Copperbrain managed markers",
        )

    before, remainder = content.split(MANAGED_BEGIN, 1)
    managed, after = remainder.split(MANAGED_END, 1)
    lines = managed.splitlines()
    migrated: list[str] = []
    changed = False
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped.startswith('(rule "Copperbrain_'):
            migrated.append(line)
            index += 1
            continue

        block: list[str] = []
        depth = 0
        while index < len(lines):
            current = lines[index]
            block.append(current)
            depth += current.count("(") - current.count(")")
            index += 1
            if depth == 0:
                break
        rule_name = stripped.split('"', 2)[1]
        condition = next((item for item in block if "(condition " in item), "")
        pair_constraints = {
            kind: next((item for item in block if f"(constraint {kind} " in item), None)
            for kind in ("clearance", "creepage")
        }
        if (
            not any(pair_constraints.values())
            or rule_name.endswith(("_clearance", "_creepage"))
            or "A.hasNetclass('" not in condition
        ):
            migrated.extend(block)
            continue

        migrated.extend(item for item in block if item not in pair_constraints.values())
        for kind, constraint in pair_constraints.items():
            if constraint is not None:
                migrated.extend(
                    [
                        f'(rule "{rule_name}_{kind}"',
                        condition.replace('")', ' && A.Parent != B.Parent")'),
                        constraint,
                        ")",
                    ]
                )
        changed = True

    if not changed:
        return False
    migrated_block = "\n".join(migrated).strip("\n")
    updated = f"{before}{MANAGED_BEGIN}\n{migrated_block}\n{MANAGED_END}{after}"
    if not _balanced_rule_syntax(updated):
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "Migrated custom rules are structurally invalid",
        )
    _atomic_write(rule_file, updated)
    return True


class PcbRuleAdapter:
    """Apply typed rule sets to temporary KiCad project copies."""

    def apply(self, project_file: Path, rule_file: Path, rule_set: PcbRuleSet) -> None:
        class_names = [item.name for item in rule_set.classes]
        if len(class_names) != len(set(class_names)):
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Netclass names must be unique")
        known_classes = set(class_names)
        assigned_nets = [item.net for item in rule_set.assignments]
        if len(assigned_nets) != len(set(assigned_nets)):
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Each net may be assigned only once")
        if any(item.netclass not in known_classes for item in rule_set.assignments):
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT, "Every assignment must reference a proposed netclass"
            )

        payload = _load_project(project_file)
        settings = _net_settings(payload)
        existing_classes = settings.get("classes", [])
        if not isinstance(existing_classes, list):
            existing_classes = []
        retained = [
            item
            for item in existing_classes
            if not isinstance(item, dict) or item.get("name") not in known_classes
        ]
        next_priority = (
            max(
                (
                    int(item.get("priority", -1))
                    for item in retained
                    if isinstance(item, dict) and isinstance(item.get("priority", -1), int)
                ),
                default=-1,
            )
            + 1
        )
        settings["classes"] = retained + [
            _class_payload(rule, next_priority + index)
            for index, rule in enumerate(rule_set.classes)
        ]

        patterns = settings.get("netclass_patterns", [])
        if not isinstance(patterns, list):
            patterns = []
        assigned = set(assigned_nets)
        settings["netclass_patterns"] = [
            item
            for item in patterns
            if not isinstance(item, dict) or item.get("pattern") not in assigned
        ] + [{"netclass": item.netclass, "pattern": item.net} for item in rule_set.assignments]
        project_content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        existing_rules = rule_file.read_text(encoding="utf-8") if rule_file.is_file() else ""
        rule_content = _merge_managed_rules(existing_rules, render_managed_rules(rule_set))
        if not _balanced_rule_syntax(rule_content):
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED, "Generated custom rules are invalid"
            )
        _atomic_write(project_file, project_content)
        _atomic_write(rule_file, rule_content)

    def validate(self, project_file: Path, rule_file: Path) -> ValidationReport:
        checks = {"project_json": False, "managed_rules_present": False, "rule_syntax": False}
        messages: list[str] = []
        try:
            _load_project(project_file)
            checks["project_json"] = True
        except CopperbrainError as exc:
            messages.append(exc.error.message)
        try:
            content = rule_file.read_text(encoding="utf-8")
        except OSError as exc:
            messages.append(str(exc))
        else:
            checks["managed_rules_present"] = (
                content.count(MANAGED_BEGIN) == 1 and content.count(MANAGED_END) == 1
            )
            checks["rule_syntax"] = _balanced_rule_syntax(content) and "(version 1)" in content
            if not checks["managed_rules_present"]:
                messages.append("Copperbrain managed rules are missing or ambiguous")
            if not checks["rule_syntax"]:
                messages.append("KiCad custom rule syntax failed structural validation")
        return ValidationReport(valid=all(checks.values()), checks=checks, messages=tuple(messages))
