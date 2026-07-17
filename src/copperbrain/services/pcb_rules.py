"""Analyze, propose, preview, validate, and safely apply KiCad PCB design rules."""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from copperbrain.adapters.footprint_geometry import (
    add_generated_courtyard,
    analyze_component_footprint,
    parse_footprint_geometry,
    resolve_footprint,
)
from copperbrain.adapters.kicad_cli import run_drc, validate_footprint
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_rules import PcbRuleAdapter, read_netclasses
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    CourtyardAddition,
    DrcReport,
    ErrorCode,
    FanoutConstraint,
    FootprintConstraintCandidate,
    ManufacturingProfile,
    NetClassAssignment,
    NetClassRule,
    NetConstraintCandidate,
    NetRuleRequirement,
    PcbConstraintAnalysis,
    PcbRuleChangeSet,
    PcbRuleSet,
    ProjectSession,
    ValidationReport,
)
from copperbrain.services.outputs import PROJECT_COPY_IGNORE, publish_preview
from copperbrain.services.projects import ProjectService, aggregate_hash, hash_file

_POWER = re.compile(r"(?:^|[/_])(?:GND|AGND|DGND|VCC|VDD|VBAT|VIN|VOUT|VREF|\+\d+V)", re.I)
_SWITCHING = re.compile(r"(?:^|[/_])(?:SW|LX|PHASE|BOOST)(?:$|[/_])", re.I)
_DIFF_SUFFIXES = (("_P", "_N"), ("+", "-"))
_Role = Literal["signal", "power", "high_current", "high_voltage", "differential", "switching"]


@dataclass
class _PreparedPcbRules:
    change_set: PcbRuleChangeSet
    workspace: Path
    originally_existing: frozenset[str]
    snapshot: Path | None = None


def _editor_lock_exists(root: Path) -> bool:
    return any(root.glob("*.lck")) or any(root.glob(".*.lck"))


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _round_up_hundredth(value: float) -> float:
    return math.ceil(value * 100 - 1e-9) / 100


def _estimated_track_width(current_a: float, profile: ManufacturingProfile) -> float:
    """Return a conservative IPC-2221-style empirical width estimate in millimetres."""
    coefficient = 0.048 if profile.current_layer == "external" else 0.024
    area_mil2 = (current_a / (coefficient * profile.allowed_temperature_rise_c**0.44)) ** (
        1 / 0.725
    )
    copper_thickness_mil = profile.copper_thickness_um / 25.4
    return _round_up_hundredth((area_mil2 / copper_thickness_mil) * 0.0254)


def _classify_nets(session_id: str, projects: ProjectService) -> tuple[NetConstraintCandidate, ...]:
    summary = projects.summary(session_id)
    names = {item.name for item in summary.nets}
    candidates: list[NetConstraintCandidate] = []
    for net in summary.nets:
        role = "signal"
        rationale = "No specialized deterministic naming rule matched"
        pin_functions = tuple(pin.pin_name or "" for pin in net.pins)
        if _POWER.search(net.name) or any(_POWER.search(name) for name in pin_functions):
            role = "power"
            rationale = "Power or ground pattern matched the net or a connected pin function"
        if _SWITCHING.search(net.name) or any(_SWITCHING.search(name) for name in pin_functions):
            role = "switching"
            rationale = "Switch-node pattern matched the net or a connected component pin"
        for positive, negative in _DIFF_SUFFIXES:
            positive_match = (
                net.name.endswith(positive) and f"{net.name[: -len(positive)]}{negative}" in names
            )
            negative_match = (
                net.name.endswith(negative) and f"{net.name[: -len(negative)]}{positive}" in names
            )
            if positive_match or negative_match:
                role = "differential"
                rationale = "Complementary differential net name was found"
        candidates.append(
            NetConstraintCandidate(
                net=net.name,
                suggested_role=role,  # type: ignore[arg-type]
                connected_references=tuple(sorted({pin.reference for pin in net.pins})),
                rationale=rationale,
            )
        )
    return tuple(candidates)


class PcbRuleService:
    """Application service for deterministic PCB constraints and safe mutations."""

    def __init__(
        self,
        projects: ProjectService,
        data_dir: Path,
        adapter: PcbRuleAdapter | None = None,
        drc_runner: Callable[[Path | None], DrcReport] | None = None,
        footprint_validator: Callable[[Path], ValidationReport] | None = None,
    ) -> None:
        self.projects = projects
        self.data_dir = data_dir
        self.adapter = adapter or PcbRuleAdapter()
        self.drc_runner = drc_runner or (lambda pcb: run_drc(detect_kicad().selected_cli, pcb))
        self.footprint_validator = footprint_validator or (
            lambda footprint: validate_footprint(detect_kicad().selected_cli, footprint)
        )
        self._changes: dict[str, _PreparedPcbRules] = {}

    @staticmethod
    def _rule_path(session: ProjectSession) -> Path:
        return session.root / f"{session.project_file.stem}.kicad_dru"

    def analyze(self, session_id: str) -> PcbConstraintAnalysis:
        session = self.projects.get_session(session_id)
        summary = self.projects.summary(session_id)
        classes, assignments = read_netclasses(session.project_file)
        rule_file = self._rule_path(session)
        warnings: list[str] = []
        if session.pcb_file is None:
            warnings.append("No PCB exists yet; DRC will be deferred until a board is available")
        warnings.append(
            "Suggested roles are naming-based evidence, not inferred electrical ratings"
        )
        footprints: list[FootprintConstraintCandidate] = []
        pin_nets = {(pin.reference, pin.pin): net.name for net in summary.nets for pin in net.pins}
        for component in summary.components:
            if component.reference.startswith("#") or not component.footprint:
                continue
            _, candidate = analyze_component_footprint(
                session.root,
                reference=component.reference,
                library_id=component.footprint,
                width_ratio=ManufacturingProfile().fanout_width_ratio,
                pin_nets={
                    pin: net
                    for (reference, pin), net in pin_nets.items()
                    if reference == component.reference
                },
            )
            footprints.append(candidate)
        return PcbConstraintAnalysis(
            session_id=session_id,
            pcb_available=session.pcb_file is not None,
            existing_classes=classes,
            assignments=assignments,
            candidates=_classify_nets(session_id, self.projects),
            footprints=tuple(footprints),
            custom_rule_file=rule_file if rule_file.is_file() else None,
            warnings=tuple(warnings),
        )

    def propose(
        self,
        session_id: str,
        profile: ManufacturingProfile,
        requirements: tuple[NetRuleRequirement, ...],
    ) -> PcbRuleSet:
        candidates = self.analyze(session_id).candidates
        existing_nets = {item.net for item in candidates}
        assumptions: list[str] = []
        if not requirements:
            grouped: dict[str, list[str]] = {}
            for candidate in candidates:
                grouped.setdefault(candidate.suggested_role, []).append(candidate.net)
            requirements = tuple(
                NetRuleRequirement(
                    name=f"CB_{role.upper()}",
                    nets=tuple(nets),
                    role=cast(_Role, role),
                )
                for role, nets in sorted(grouped.items())
                if nets
            )
            assumptions.append(
                "Automatic roles use net names and connectivity only; current and voltage "
                "are not inferred"
            )
        seen_nets: set[str] = set()
        classes: list[NetClassRule] = []
        assignments: list[NetClassAssignment] = []
        for requirement in requirements:
            missing = sorted(set(requirement.nets) - existing_nets)
            if missing:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Rule requirement references unknown nets",
                    details={"nets": missing},
                )
            duplicates = sorted(set(requirement.nets) & seen_nets)
            if duplicates:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "A net cannot belong to more than one proposed netclass",
                    details={"nets": duplicates},
                )
            seen_nets.update(requirement.nets)
            if requirement.role == "high_voltage" and requirement.clearance_mm is None:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "High-voltage rules require an explicit reviewed clearance_mm",
                    actionable_hint=(
                        "Derive clearance and creepage from the applicable safety standard "
                        "and environment."
                    ),
                )
            if (
                requirement.role == "high_current"
                and requirement.current_a is None
                and requirement.track_width_mm is None
            ):
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "High-current rules require current_a or an explicit track_width_mm",
                )
            clearance = max(
                profile.min_clearance_mm,
                requirement.clearance_mm or profile.min_clearance_mm,
            )
            estimated = (
                _estimated_track_width(requirement.current_a, profile)
                if requirement.current_a is not None
                else profile.min_track_width_mm
            )
            preferred = max(
                profile.min_track_width_mm,
                requirement.track_width_mm or estimated,
            )
            # The class minimum must remain fabricable at fine-pitch neck-downs. The reviewed
            # current-carrying width is retained as the preferred width and is emitted as the
            # router's netclass width; local courtyard rules cap only the short pad fanout.
            minimum = profile.min_track_width_mm
            rationale = [
                f"Clearance is not below fabrication minimum {profile.min_clearance_mm:g} mm",
                f"Track width is not below fabrication minimum {profile.min_track_width_mm:g} mm",
            ]
            if requirement.current_a is not None:
                rationale.append(
                    "Preferred track width uses a conservative IPC-2221-style estimate "
                    "from current, "
                    "copper thickness, layer type, and allowed temperature rise"
                )
            if requirement.role == "power" and requirement.current_a is None:
                assumptions.append(
                    f"{requirement.name}: power current was not provided; fabrication minimum used"
                )
            if requirement.role == "differential" and (
                requirement.diff_pair_width_mm is None or requirement.diff_pair_gap_mm is None
            ):
                assumptions.append(
                    f"{requirement.name}: differential geometry is not impedance-controlled "
                    "until width and gap are supplied from a verified stackup calculation"
                )
            classes.append(
                NetClassRule(
                    name=requirement.name,
                    clearance_mm=clearance,
                    track_width_min_mm=minimum,
                    track_width_preferred_mm=preferred,
                    via_diameter_mm=profile.min_via_diameter_mm,
                    via_drill_mm=profile.min_via_drill_mm,
                    diff_pair_width_mm=requirement.diff_pair_width_mm,
                    diff_pair_gap_mm=requirement.diff_pair_gap_mm,
                    creepage_mm=requirement.creepage_mm,
                    max_length_mm=requirement.max_length_mm,
                    diff_pair_max_uncoupled_mm=requirement.diff_pair_max_uncoupled_mm,
                    rationale=tuple(rationale),
                )
            )
            assignments.extend(
                NetClassAssignment(net=net, netclass=requirement.name) for net in requirement.nets
            )
        class_widths = {item.name: item.track_width_preferred_mm for item in classes}
        class_clearances = {item.name: item.clearance_mm for item in classes}
        net_classes = {item.net: item.netclass for item in assignments}
        summary = self.projects.summary(session_id)
        pin_nets = {(pin.reference, pin.pin): net.name for net in summary.nets for pin in net.pins}
        required_by_reference: dict[str, float] = {}
        clearance_by_reference: dict[str, float] = {}
        for net in summary.nets:
            netclass = net_classes.get(net.name)
            if netclass is None:
                continue
            preferred = class_widths[netclass]
            clearance = class_clearances[netclass]
            for pin in net.pins:
                required_by_reference[pin.reference] = max(
                    preferred, required_by_reference.get(pin.reference, 0)
                )
                clearance_by_reference[pin.reference] = max(
                    clearance, clearance_by_reference.get(pin.reference, 0)
                )
        fanouts: list[FanoutConstraint] = []
        courtyards: list[CourtyardAddition] = []
        pcb_has_footprints = False
        session = self.projects.get_session(session_id)
        if session.pcb_file is not None and session.pcb_file.is_file():
            pcb_has_footprints = "(footprint " in session.pcb_file.read_text(
                encoding="utf-8-sig", errors="ignore"
            )
        for component in summary.components:
            required_width = required_by_reference.get(component.reference)
            required_clearance = clearance_by_reference.get(component.reference)
            if required_width is None or required_clearance is None or not component.footprint:
                continue
            geometry, footprint_candidate = analyze_component_footprint(
                session.root,
                reference=component.reference,
                library_id=component.footprint,
                width_ratio=profile.fanout_width_ratio,
                pin_nets={
                    pin: net
                    for (reference, pin), net in pin_nets.items()
                    if reference == component.reference
                },
            )
            if (
                geometry is None
                or footprint_candidate.safe_fanout_width_mm is None
                or footprint_candidate.safe_clearance_mm is None
            ):
                if (
                    required_width > profile.min_track_width_mm
                    or required_clearance > profile.min_clearance_mm
                ):
                    raise CopperbrainError(
                        ErrorCode.VALIDATION_FAILED,
                        "Wide-net fanout cannot be validated because a footprint is unresolved",
                        actionable_hint="Install or repair the referenced footprint library.",
                        details={
                            "reference": component.reference,
                            "footprint": component.footprint,
                            "required_width_mm": required_width,
                            "required_clearance_mm": required_clearance,
                        },
                    )
                continue
            safe_width = footprint_candidate.safe_fanout_width_mm
            safe_clearance = footprint_candidate.safe_clearance_mm
            if required_width <= safe_width and required_clearance <= safe_clearance:
                continue
            if safe_width < profile.min_track_width_mm:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Footprint fanout is narrower than the fabrication minimum",
                    actionable_hint="Use a finer fabrication profile or a larger package.",
                    details={
                        "reference": component.reference,
                        "pad_dimension_mm": footprint_candidate.pad_min_dimension_mm,
                        "safe_fanout_width_mm": safe_width,
                        "fabrication_minimum_mm": profile.min_track_width_mm,
                    },
                )
            if safe_clearance < profile.min_clearance_mm:
                raise CopperbrainError(
                    ErrorCode.VALIDATION_FAILED,
                    "Footprint pad clearance is below the fabrication minimum",
                    actionable_hint="Use a finer fabrication profile or a larger package.",
                    details={
                        "reference": component.reference,
                        "pad_clearance_mm": safe_clearance,
                        "fabrication_minimum_mm": profile.min_clearance_mm,
                    },
                )
            if not geometry.has_courtyard:
                try:
                    geometry.source.relative_to(session.root / "copperbrain-libs")
                except ValueError as exc:
                    raise CopperbrainError(
                        ErrorCode.VALIDATION_FAILED,
                        "Fine-pitch footprint has no courtyard for a local neck-down rule",
                        actionable_hint="Repair the footprint courtyard before generating rules.",
                        details={"reference": component.reference},
                    ) from exc
                if pcb_has_footprints:
                    raise CopperbrainError(
                        ErrorCode.VALIDATION_FAILED,
                        "Board footprint has no validated courtyard for local neck-down",
                        actionable_hint=(
                            "Update the footprint courtyard and refresh footprints from "
                            "the library."
                        ),
                        details={"reference": component.reference},
                    )
                margin = profile.courtyard_margin_mm
                if all(item.footprint != component.footprint for item in courtyards):
                    courtyards.append(
                        CourtyardAddition(
                            footprint=component.footprint,
                            min_x_mm=geometry.min_x_mm - margin,
                            min_y_mm=geometry.min_y_mm - margin,
                            max_x_mm=geometry.max_x_mm + margin,
                            max_y_mm=geometry.max_y_mm + margin,
                        )
                    )
                assumptions.append(
                    f"{component.reference}: generated a {margin:g} mm project-library courtyard "
                    "so the local fanout rule has a validated scope"
                )
            fanouts.append(
                FanoutConstraint(
                    reference=component.reference,
                    footprint=component.footprint,
                    min_track_width_mm=profile.min_track_width_mm,
                    max_track_width_mm=min(required_width, safe_width),
                    # Fine-pitch fanout is the explicit local exception to a wider class
                    # clearance. It may use the reviewed fabrication minimum while the
                    # package courtyard bounds the exception.
                    clearance_mm=profile.min_clearance_mm,
                    pad_min_dimension_mm=geometry.pad_min_dimension_mm,
                    pad_min_clearance_mm=safe_clearance,
                    min_pitch_mm=geometry.min_pitch_mm,
                    rationale=(
                        f"Preferred net width {required_width:g} mm exceeds safe pad fanout",
                        f"Fanout capped at {profile.fanout_width_ratio:g} of the smallest "
                        "pad dimension",
                        f"Local clearance uses fabrication minimum "
                        f"{profile.min_clearance_mm:g} mm within the courtyard",
                    ),
                )
            )
        return PcbRuleSet(
            manufacturing=profile,
            classes=tuple(classes),
            assignments=tuple(assignments),
            class_roles={item.name: item.role for item in requirements},
            fanout_constraints=tuple(fanouts),
            courtyard_additions=tuple(courtyards),
            evidence=tuple(
                f"{item.net}: {item.rationale}; connected to {', '.join(item.connected_references)}"
                for item in candidates
                if item.net in seen_nets
            ),
            assumptions=tuple(dict.fromkeys(assumptions)),
        )

    def _current_hashes(self, session: ProjectSession) -> dict[str, str]:
        return {
            relative: hash_file(session.root / relative)
            for relative in session.hashes
            if (session.root / relative).is_file()
        }

    def _validate_workspace(
        self,
        session: ProjectSession,
        project_file: Path,
        rule_file: Path,
        pcb_file: Path | None,
        rule_set: PcbRuleSet | None = None,
    ) -> tuple[ValidationReport, DrcReport]:
        structural = self.adapter.validate(project_file, rule_file)
        drc = self.drc_runner(pcb_file)
        pcb_required = session.pcb_file is not None
        drc_ok = (not pcb_required) or (drc.available and drc.error is None)
        checks = {**structural.checks, "drc_executed": drc.available, "drc_command_ok": drc_ok}
        messages = list(structural.messages)
        courtyard_ok = True
        footprint_parse_ok = True
        if rule_set is not None:
            for addition in rule_set.courtyard_additions:
                footprint = resolve_footprint(project_file.parent, addition.footprint)
                if footprint is None:
                    courtyard_ok = False
                    messages.append(
                        f"Generated courtyard footprint is unresolved: {addition.footprint}"
                    )
                    continue
                geometry = parse_footprint_geometry(
                    footprint, reference="validation", library_id=addition.footprint
                )
                courtyard_ok = courtyard_ok and geometry.has_courtyard
                cli_report = self.footprint_validator(footprint)
                footprint_parse_ok = footprint_parse_ok and cli_report.valid
                messages.extend(cli_report.messages)
            checks["footprint_courtyards"] = courtyard_ok
            if rule_set.courtyard_additions:
                checks["footprint_kicad_parse"] = footprint_parse_ok
        if not pcb_required:
            messages.append("DRC deferred because the project has no PCB")
        elif drc.error is not None:
            messages.append(drc.error.message)
        elif drc.violations:
            messages.append(
                f"Proposed constraints produce {len(drc.violations)} DRC violation(s) for review"
            )
        return (
            ValidationReport(
                valid=structural.valid and drc_ok and courtyard_ok and footprint_parse_ok,
                checks=checks,
                messages=tuple(messages),
            ),
            drc,
        )

    def prepare(self, session_id: str, rule_set: PcbRuleSet) -> PcbRuleChangeSet:
        session = self.projects.get_session(session_id)
        current = self._current_hashes(session)
        if current != session.hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project changed after the session was opened",
                actionable_hint="Open the project again before preparing PCB rules.",
            )
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(session.root, workspace, ignore=PROJECT_COPY_IGNORE)
        relative_project = session.project_file.relative_to(session.root)
        live_rule = self._rule_path(session)
        relative_rule = live_rule.relative_to(session.root)
        temporary_project = workspace / relative_project
        temporary_rule = workspace / relative_rule
        live_footprints: list[Path] = []
        for addition in rule_set.courtyard_additions:
            live_footprint = resolve_footprint(session.root, addition.footprint)
            if live_footprint is None:
                raise CopperbrainError(
                    ErrorCode.NOT_FOUND,
                    "Project footprint for generated courtyard was not found",
                    details={"footprint": addition.footprint},
                )
            try:
                relative_footprint = live_footprint.relative_to(session.root)
            except ValueError as exc:
                raise CopperbrainError(
                    ErrorCode.INVALID_INPUT,
                    "Generated courtyards are restricted to project-local footprints",
                ) from exc
            add_generated_courtyard(workspace / relative_footprint, addition)
            live_footprints.append(live_footprint)
        source_hashes = {
            **current,
            **{str(path.relative_to(session.root)): hash_file(path) for path in live_footprints},
        }
        self.adapter.apply(temporary_project, temporary_rule, rule_set)
        temporary_pcb = (
            workspace / session.pcb_file.relative_to(session.root)
            if session.pcb_file is not None
            else None
        )
        validation, drc = self._validate_workspace(
            session, temporary_project, temporary_rule, temporary_pcb, rule_set
        )
        preview = publish_preview(workspace, session.root, identifier)
        affected = (session.project_file, live_rule, *live_footprints)
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        change_set = PcbRuleChangeSet(
            id=identifier,
            session_id=session.id,
            project_hash=aggregate_hash(source_hashes),
            rule_set=rule_set,
            affected_files=affected,
            source_hashes=source_hashes,
            semantic_diff=tuple(
                f"netclass {item.name}: clearance {item.clearance_mm:g} mm, "
                f"track {item.track_width_min_mm:g}/{item.track_width_preferred_mm:g} mm"
                for item in rule_set.classes
            )
            + tuple(f"assign {item.net} -> {item.netclass}" for item in rule_set.assignments)
            + tuple(
                f"fanout {item.reference}: max track {item.max_track_width_mm:g} mm "
                f"inside courtyard (pad {item.pad_min_dimension_mm:g} mm)"
                for item in rule_set.fanout_constraints
            )
            + tuple(
                f"add generated courtyard to {item.footprint}"
                for item in rule_set.courtyard_additions
            ),
            risks=(
                "New constraints may intentionally reveal DRC violations in an existing layout",
                "Electrical ratings and applicable safety standards require engineering review",
                "KiCad must be saved and closed before applying external project changes",
            ),
            validation_report=validation,
            drc=drc,
            preview_directory=preview,
            status=status,
        )
        originally_existing = frozenset(
            str(path.relative_to(session.root)) for path in affected if path.is_file()
        )
        self._changes[identifier] = _PreparedPcbRules(
            change_set=change_set,
            workspace=workspace,
            originally_existing=originally_existing,
        )
        return change_set

    def _get(self, change_set_id: str) -> _PreparedPcbRules:
        try:
            return self._changes[change_set_id]
        except KeyError as exc:
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "PCB rule change set was not found"
            ) from exc

    def validate(self, change_set_id: str) -> tuple[ValidationReport, DrcReport]:
        prepared = self._get(change_set_id)
        session = self.projects.get_session(prepared.change_set.session_id)
        project = prepared.workspace / session.project_file.relative_to(session.root)
        rule = prepared.workspace / self._rule_path(session).relative_to(session.root)
        pcb = (
            prepared.workspace / session.pcb_file.relative_to(session.root)
            if session.pcb_file is not None
            else None
        )
        return self._validate_workspace(session, project, rule, pcb, prepared.change_set.rule_set)

    def apply(
        self,
        change_set_id: str,
        *,
        confirmed: bool,
        editor_closed: bool,
    ) -> PcbRuleChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        prepared = self._get(change_set_id)
        change_set = prepared.change_set
        if change_set.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "PCB rule change is not validated")
        session = self.projects.get_session(change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE,
                "KiCad editor state is not safely closed",
                actionable_hint="Save and close KiCad, then retry.",
            )
        current = {
            relative: hash_file(session.root / relative)
            for relative in change_set.source_hashes
            if (session.root / relative).is_file()
        }
        if current != change_set.source_hashes:
            prepared.change_set = change_set.model_copy(update={"status": ChangeStatus.STALE})
            raise CopperbrainError(ErrorCode.CONFLICT, "PCB rule change is stale")
        for affected in change_set.affected_files:
            relative_name = str(affected.relative_to(session.root))
            if affected.is_file() != (relative_name in prepared.originally_existing):
                prepared.change_set = change_set.model_copy(update={"status": ChangeStatus.STALE})
                raise CopperbrainError(
                    ErrorCode.CONFLICT, "An affected PCB rule file appeared or disappeared"
                )
        snapshot_id = uuid.uuid4().hex
        snapshot = self.data_dir / "snapshots" / snapshot_id
        snapshot.mkdir(parents=True, exist_ok=False)
        for affected in change_set.affected_files:
            relative_path = affected.relative_to(session.root)
            if affected.is_file():
                destination = snapshot / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(affected, destination)
        applied: list[Path] = []
        try:
            for affected in change_set.affected_files:
                relative_path = affected.relative_to(session.root)
                _atomic_copy(prepared.workspace / relative_path, affected)
                applied.append(affected)
        except Exception:
            for affected in applied:
                relative_path = affected.relative_to(session.root)
                backup = snapshot / relative_path
                if backup.is_file():
                    _atomic_copy(backup, affected)
                elif affected.is_file():
                    affected.unlink()
            raise
        prepared.snapshot = snapshot
        prepared.change_set = change_set.model_copy(
            update={"status": ChangeStatus.APPLIED, "snapshot_id": snapshot_id}
        )
        return prepared.change_set

    def rollback(
        self,
        change_set_id: str,
        *,
        confirmed: bool,
        editor_closed: bool,
    ) -> PcbRuleChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Rollback confirmation is required"
            )
        prepared = self._get(change_set_id)
        if prepared.change_set.status is not ChangeStatus.APPLIED or prepared.snapshot is None:
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Only an applied PCB rule change can be rolled back"
            )
        session = self.projects.get_session(prepared.change_set.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(ErrorCode.UNSAFE_EDITOR_STATE, "KiCad is not safely closed")
        for affected in prepared.change_set.affected_files:
            relative = affected.relative_to(session.root)
            backup = prepared.snapshot / relative
            if backup.is_file():
                _atomic_copy(backup, affected)
            elif affected.is_file():
                affected.unlink()
        prepared.change_set = prepared.change_set.model_copy(
            update={"status": ChangeStatus.ROLLED_BACK}
        )
        return prepared.change_set
