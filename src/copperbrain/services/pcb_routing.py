"""External-backend PCB routing proposals and safe copper mutation workflow."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from copperbrain.adapters.freerouting import FreeRoutingAdapter, RoutingBackend
from copperbrain.adapters.kicad_cli import export_pcb_pdf, run_drc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.pcb_rules import (
    read_managed_widths,
    read_netclasses,
    stage_router_project,
)
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    ErrorCode,
    PcbPadInspection,
    PcbRoutingChangeSet,
    ProjectSession,
    RouteSegment,
    RouteVia,
    RoutingAnalysis,
    RoutingBackendStatus,
    RoutingCandidateEvaluation,
    RoutingChangeRecord,
    RoutingPlan,
    RoutingRequest,
    RoutingReviewSummary,
    RoutingSnapshotRestoreResult,
    ValidationReport,
)
from copperbrain.services.outputs import PROJECT_COPY_IGNORE, publish_preview
from copperbrain.services.projects import ProjectService, aggregate_hash, hash_file


@dataclass
class _PreparedRouting:
    change_set: PcbRoutingChangeSet
    workspace: Path
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


class PcbRoutingService:
    """Orchestrate external routing, deterministic evaluation, preview, and safe apply."""

    def __init__(
        self,
        projects: ProjectService,
        data_dir: Path,
        adapter: PcbFileAdapter | None = None,
        drc_runner: Callable[[Path | None], DrcReport] | None = None,
        pdf_exporter: Callable[[Path, Path], Path] | None = None,
        routing_backend: RoutingBackend | None = None,
    ) -> None:
        self.projects = projects
        self.data_dir = data_dir
        self.adapter = adapter or PcbFileAdapter()
        self.drc_runner = drc_runner or (lambda pcb: run_drc(detect_kicad().selected_cli, pcb))
        self.pdf_exporter = pdf_exporter or (
            lambda pcb, destination: export_pcb_pdf(detect_kicad().selected_cli, pcb, destination)
        )
        self.routing_backend = routing_backend or FreeRoutingAdapter.discover(data_dir)
        self._changes: dict[str, _PreparedRouting] = {}

    @property
    def _records_dir(self) -> Path:
        return self.data_dir / "routing-changes"

    def _record_path(self, change_set_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", change_set_id) is None:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Routing change identifier is invalid")
        return self._records_dir / f"{change_set_id}.json"

    def _persist(self, prepared: _PreparedRouting, project_root: Path) -> None:
        """Atomically persist enough typed state to resume after an MCP restart."""
        change = prepared.change_set
        record = RoutingChangeRecord(
            project_root=project_root.resolve(),
            workspace=prepared.workspace.resolve(),
            affected_relative_files=tuple(
                path.resolve().relative_to(project_root.resolve()) for path in change.affected_files
            ),
            change_set=change,
            snapshot=prepared.snapshot.resolve() if prepared.snapshot is not None else None,
        )
        path = self._record_path(change.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(record.model_dump(mode="json"), stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _require_child(path: Path, parent: Path, label: str) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(parent.resolve())
        except ValueError as exc:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                f"Persisted {label} path is outside Copperbrain private storage",
            ) from exc
        return resolved

    def _load(self, change_set_id: str) -> _PreparedRouting:
        path = self._record_path(change_set_id)
        try:
            record = RoutingChangeRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CopperbrainError(ErrorCode.NOT_FOUND, "Routing change set was not found") from exc
        except (OSError, ValueError) as exc:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Persisted routing change set is invalid",
                actionable_hint="Prepare the routing change again from the source project.",
                details={"reason": str(exc)},
            ) from exc

        workspace = self._require_child(
            record.workspace, self.data_dir / "workspaces", "routing workspace"
        )
        if not workspace.is_dir():
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Persisted routing workspace was not found",
                actionable_hint="Prepare the routing change again from the source project.",
            )
        snapshot = None
        if record.snapshot is not None:
            snapshot = self._require_child(
                record.snapshot, self.data_dir / "snapshots", "routing snapshot"
            )
            if not snapshot.is_dir():
                raise CopperbrainError(
                    ErrorCode.NOT_FOUND, "Persisted routing snapshot was not found"
                )

        session = self.projects.open_project(record.project_root)
        affected = tuple(session.root / item for item in record.affected_relative_files)
        if any(not item.is_file() for item in affected):
            raise CopperbrainError(
                ErrorCode.NOT_FOUND, "A source file referenced by the routing change was not found"
            )
        old = record.change_set
        plan = old.plan.model_copy(
            update={
                "session_id": session.id,
                "analysis_before": old.plan.analysis_before.model_copy(
                    update={"session_id": session.id}
                ),
            }
        )
        change = old.model_copy(
            update={
                "session_id": session.id,
                "plan": plan,
                "affected_files": affected,
                "routing_analysis": old.routing_analysis.model_copy(
                    update={"session_id": session.id}
                ),
            }
        )
        prepared = _PreparedRouting(change, workspace, snapshot)
        self._changes[change_set_id] = prepared
        return prepared

    def _session_pcb(self, session_id: str) -> tuple[ProjectSession, Path]:
        session = self.projects.get_session(session_id)
        if session.pcb_file is None or not session.pcb_file.is_file():
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Project contains no PCB file",
                actionable_hint="Prepare and apply a PCB layout before routing.",
            )
        return session, session.pcb_file

    def backend_status(self) -> RoutingBackendStatus:
        return self.routing_backend.status()

    def analyze(self, session_id: str, net_names: tuple[str, ...] = ()) -> RoutingAnalysis:
        _, pcb = self._session_pcb(session_id)
        return self.adapter.analyze_routing(pcb, session_id, net_names)

    def _routing_delta(
        self,
        source: Path,
        routed: Path,
        target_nets: tuple[str, ...],
    ) -> tuple[tuple[RouteSegment, ...], tuple[RouteVia, ...]]:
        before_segments, before_vias = self.adapter.routing_items(source)
        after_segments, after_vias = self.adapter.routing_items(routed)
        removed_segments = Counter(before_segments) - Counter(after_segments)
        removed_vias = Counter(before_vias) - Counter(after_vias)
        if removed_segments or removed_vias:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "The autorouter changed or removed existing copper",
                actionable_hint=(
                    "Route from a clean board or preserve existing tracks in FreeRouting."
                ),
                details={
                    "removed_segments": sum(removed_segments.values()),
                    "removed_vias": sum(removed_vias.values()),
                },
            )
        target = set(target_nets)
        segments = tuple(
            sorted(
                (
                    item
                    for item in (Counter(after_segments) - Counter(before_segments)).elements()
                    if item.net in target
                ),
                key=lambda item: item.model_dump_json(),
            )
        )
        vias = tuple(
            sorted(
                (
                    item
                    for item in (Counter(after_vias) - Counter(before_vias)).elements()
                    if item.net in target
                ),
                key=lambda item: item.model_dump_json(),
            )
        )
        if not segments and not vias:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "The autorouter produced no copper for the requested nets",
            )
        return segments, vias

    def _stage_router_input(
        self,
        pcb: Path,
        workspace: Path,
        target_nets: tuple[str, ...],
        *,
        context_pcb: Path | None = None,
        add_escape_stubs: bool = False,
    ) -> Path:
        """Stage preferred netclasses and deterministic fine-pitch escape stubs."""
        workspace.mkdir(parents=True, exist_ok=False)
        context = context_pcb or pcb
        staged_pcb = workspace / context.name
        shutil.copy2(pcb, staged_pcb)
        source_rules = context.with_suffix(".kicad_dru")
        preferred_classes, fanout_limits = read_managed_widths(source_rules)
        if source_rules.is_file():
            shutil.copy2(source_rules, workspace / source_rules.name)
        source_project = context.with_suffix(".kicad_pro")
        if source_project.is_file():
            stage_router_project(
                source_project,
                workspace / source_project.name,
                preferred_classes,
            )
        _, assignments = read_netclasses(source_project) if source_project.is_file() else ((), ())
        net_classes = {item.net: item.netclass for item in assignments}
        net_widths = {
            net: preferred_classes[netclass]
            for net, netclass in net_classes.items()
            if netclass in preferred_classes
        }
        if not fanout_limits or not net_widths:
            return staged_pcb

        parsed = self.adapter.parse(staged_pcb)
        bounds_by_reference = {item.reference: item.bounds for item in parsed.summary.footprints}
        stubs: list[RouteSegment] = []
        fine_pads: list[tuple[PcbPadInspection, float, tuple[float, float]]] = []
        for pad in parsed.pads:
            preferred = net_widths.get(pad.net)
            configured_limit = fanout_limits.get(pad.reference)
            bounds = bounds_by_reference.get(pad.reference)
            if (
                pad.net not in target_nets
                or preferred is None
                or configured_limit is None
                or bounds is None
                or configured_limit > 0.5
            ):
                continue
            pad_limit = round(
                min(
                    preferred,
                    configured_limit,
                    min(pad.width_mm, pad.height_mm) * 0.8,
                ),
                6,
            )
            if pad_limit >= preferred:
                continue
            center_x = (bounds.min_x_mm + bounds.max_x_mm) / 2
            center_y = (bounds.min_y_mm + bounds.max_y_mm) / 2
            horizontal = abs(pad.x_mm - center_x) / max(
                (bounds.max_x_mm - bounds.min_x_mm) / 2, 1e-9
            )
            vertical = abs(pad.y_mm - center_y) / max((bounds.max_y_mm - bounds.min_y_mm) / 2, 1e-9)
            margin = pad_limit / 2 + 0.01
            if horizontal >= vertical:
                end_x = (
                    bounds.max_x_mm + margin if pad.x_mm >= center_x else bounds.min_x_mm - margin
                )
                end_y = pad.y_mm
            else:
                end_x = pad.x_mm
                end_y = (
                    bounds.max_y_mm + margin if pad.y_mm >= center_y else bounds.min_y_mm - margin
                )
            if (pad.x_mm, pad.y_mm) == (end_x, end_y):
                continue
            escape = (round(end_x, 6), round(end_y, 6))
            fine_pads.append((pad, pad_limit, escape))
            if add_escape_stubs:
                stubs.append(
                    RouteSegment(
                        net=pad.net,
                        start_x_mm=pad.x_mm,
                        start_y_mm=pad.y_mm,
                        end_x_mm=escape[0],
                        end_y_mm=escape[1],
                        width_mm=pad_limit,
                        layer=pad.layers[0],
                    )
                )
        for index, (left_pad, left_width, _) in enumerate(fine_pads):
            for right_pad, right_width, _ in fine_pads[index + 1 :]:
                if (
                    left_pad.reference != right_pad.reference
                    or left_pad.net != right_pad.net
                    or left_pad.layers[0] != right_pad.layers[0]
                    or math.dist(
                        (left_pad.x_mm, left_pad.y_mm),
                        (right_pad.x_mm, right_pad.y_mm),
                    )
                    > 1.0
                ):
                    continue
                stubs.append(
                    RouteSegment(
                        net=left_pad.net,
                        start_x_mm=left_pad.x_mm,
                        start_y_mm=left_pad.y_mm,
                        end_x_mm=right_pad.x_mm,
                        end_y_mm=right_pad.y_mm,
                        width_mm=min(left_width, right_width),
                        layer=left_pad.layers[0],
                    )
                )
        groups: dict[
            tuple[str, str, str],
            list[tuple[PcbPadInspection, float, tuple[float, float]]],
        ] = {}
        for item in fine_pads:
            pad = item[0]
            groups.setdefault((pad.reference, pad.net, pad.layers[0]), []).append(item)
        for (reference, net, layer), group in groups.items():
            if net_widths.get(net, 0) < 1.0:
                continue
            nearby: list[
                tuple[
                    float,
                    PcbPadInspection,
                    float,
                    tuple[float, float],
                    PcbPadInspection,
                ]
            ] = []
            for pad, width, escape in group:
                for target in parsed.pads:
                    if (
                        target.reference == reference
                        or target.net != net
                        or layer not in target.layers
                    ):
                        continue
                    distance = math.dist(
                        (pad.x_mm, pad.y_mm),
                        (target.x_mm, target.y_mm),
                    )
                    if distance <= 5.0:
                        nearby.append((distance, pad, width, escape, target))
            if not nearby:
                continue
            _, pad, width, escape, target = min(
                nearby,
                key=lambda item: (
                    item[0],
                    item[1].number,
                    item[4].reference,
                    item[4].number,
                ),
            )
            stubs.extend(
                (
                    RouteSegment(
                        net=net,
                        start_x_mm=pad.x_mm,
                        start_y_mm=pad.y_mm,
                        end_x_mm=escape[0],
                        end_y_mm=escape[1],
                        width_mm=width,
                        layer=layer,  # type: ignore[arg-type]
                    ),
                    RouteSegment(
                        net=net,
                        start_x_mm=escape[0],
                        start_y_mm=escape[1],
                        end_x_mm=target.x_mm,
                        end_y_mm=target.y_mm,
                        width_mm=round(
                            min(
                                net_widths[net],
                                min(target.width_mm, target.height_mm) * 0.8,
                            ),
                            6,
                        ),
                        layer=layer,  # type: ignore[arg-type]
                    ),
                )
            )
        if stubs:
            self.adapter.apply_routing(staged_pcb, tuple(stubs), ())
        return staged_pcb

    @staticmethod
    def _stage_candidate_rules(source_pcb: Path, candidate_pcb: Path) -> None:
        """Give standalone candidate DRC the same same-stem custom rules as the project."""
        source_rules = source_pcb.with_suffix(".kicad_dru")
        if source_rules.is_file():
            shutil.copy2(source_rules, candidate_pcb.with_suffix(".kicad_dru"))

    @staticmethod
    def _new_drc_errors(before: DrcReport, after: DrcReport) -> int | None:
        if not before.available or not after.available or before.error or after.error:
            return None
        baseline = Counter(
            (item.code, item.message) for item in before.violations if item.severity == "error"
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if item.severity == "error"
        )
        return sum((generated - baseline).values())

    def propose(self, session_id: str, request: RoutingRequest) -> RoutingPlan:
        _, pcb = self._session_pcb(session_id)
        analysis = self.adapter.analyze_routing(pcb, session_id, request.nets)
        if analysis.complete:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Selected PCB nets are already fully routed",
                details={"nets": list(request.nets)},
            )
        existing_segments, existing_vias = self.adapter.routing_items(pcb)
        if request.existing_copper_policy == "reject" and (existing_segments or existing_vias):
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Incremental FreeRouting is disabled for a board that already contains copper",
                actionable_hint=(
                    "Route from the clean placed board, or explicitly use "
                    "existing_copper_policy='preserve' with watchdog protection."
                ),
                details={
                    "existing_segment_count": len(existing_segments),
                    "existing_via_count": len(existing_vias),
                },
            )
        status = self.routing_backend.status()
        if not status.available:
            raise CopperbrainError(
                ErrorCode.INTEGRATION_UNAVAILABLE,
                "The local FreeRouting backend is unavailable",
                actionable_hint=(
                    "Install Java 25+ and a FreeRouting JAR, optionally set "
                    "COPPERBRAIN_FREEROUTING_JAVA and COPPERBRAIN_FREEROUTING_JAR, "
                    "then restart Copperbrain."
                ),
                details={"reason": status.reason or "unknown"},
            )
        target_nets = tuple(sorted({item.net for item in analysis.unrouted_connections}))
        baseline_drc = self.drc_runner(pcb)
        strategies = ("prioritized", "sequential")[: request.candidate_count]
        evaluated: list[
            tuple[
                tuple[int, int, int, int, float, int],
                tuple[RouteSegment, ...],
                tuple[RouteVia, ...],
                RoutingCandidateEvaluation,
            ]
        ] = []
        failures: list[str] = []
        seen_fingerprints: dict[str, str] = {}
        proposal_root = self.data_dir / "routing-proposals"
        proposal_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="freerouting-", dir=proposal_root) as directory:
            root = Path(directory)
            router_input = self._stage_router_input(pcb, root / "input", target_nets)
            for strategy_index, strategy in enumerate(strategies):
                try:
                    candidate = self.routing_backend.route(
                        router_input,
                        root / strategy,
                        request,
                        strategy,  # type: ignore[arg-type]
                    )
                    segments, vias = self._routing_delta(pcb, candidate.pcb, target_nets)
                    if not request.allow_vias and vias:
                        raise CopperbrainError(
                            ErrorCode.VALIDATION_FAILED,
                            "FreeRouting used vias although this request forbids them",
                            details={"via_count": len(vias)},
                        )
                    routed_analysis = self.adapter.analyze_routing(
                        candidate.pcb, session_id, target_nets
                    )
                    self._stage_candidate_rules(pcb, candidate.pcb)
                    candidate_drc = self.drc_runner(candidate.pcb)
                    new_errors = self._new_drc_errors(baseline_drc, candidate_drc)
                    track_length = round(
                        sum(
                            math.dist(
                                (item.start_x_mm, item.start_y_mm),
                                (item.end_x_mm, item.end_y_mm),
                            )
                            for item in segments
                        ),
                        6,
                    )
                    fingerprint_payload = "\n".join(
                        sorted(
                            [item.model_dump_json() for item in segments]
                            + [item.model_dump_json() for item in vias]
                        )
                    )
                    fingerprint = hashlib.sha256(fingerprint_payload.encode()).hexdigest()
                    duplicate_of = seen_fingerprints.get(fingerprint)
                    seen_fingerprints.setdefault(fingerprint, strategy)
                    evaluation = RoutingCandidateEvaluation(
                        strategy=strategy,  # type: ignore[arg-type]
                        complete=routed_analysis.complete,
                        unrouted_connection_count=routed_analysis.unrouted_connection_count,
                        drc_available=candidate_drc.available and candidate_drc.error is None,
                        new_drc_error_count=new_errors,
                        segment_count=len(segments),
                        via_count=len(vias),
                        track_length_mm=track_length,
                        fingerprint=fingerprint,
                        duplicate_of=duplicate_of,  # type: ignore[arg-type]
                    )
                    rank = (
                        0 if evaluation.complete else 1,
                        new_errors if new_errors is not None else 1_000_000,
                        evaluation.unrouted_connection_count,
                        evaluation.via_count,
                        evaluation.track_length_mm,
                        strategy_index,
                    )
                    evaluated.append((rank, segments, vias, evaluation))
                except CopperbrainError as exc:
                    failures.append(f"{strategy}: {exc}")
        if not evaluated:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "FreeRouting produced no usable routing candidate",
                actionable_hint="Review placement and design rules, then retry.",
                details={"failures": failures},
            )
        evaluated.sort(key=lambda item: item[0])
        _, segments, vias, selected = evaluated[0]
        evaluations = tuple(
            item[3].model_copy(update={"selected": item[3].strategy == selected.strategy})
            for item in evaluated
        )
        return RoutingPlan(
            session_id=session_id,
            request=request,
            segments=segments,
            vias=vias,
            target_nets=target_nets,
            analysis_before=analysis,
            predicted_complete=selected.complete,
            backend="freerouting",
            backend_version=status.version,
            candidate_evaluations=evaluations,
            evidence=(
                "Copper geometry was generated by a local FreeRouting process through "
                "KiCad's official Specctra DSN/SES bridge",
                f"Evaluated {len(evaluations)} deterministic candidate configuration(s)",
                f"Observed {len(seen_fingerprints)} unique copper candidate(s)",
                f"Selected {selected.strategy}: {selected.segment_count} segments, "
                f"{selected.via_count} vias, {selected.track_length_mm:g} mm routed length",
            ),
            assumptions=(
                "FreeRouting consumes the netclasses and board rules serialized by KiCad",
                "The selected candidate is still subject to the authoritative prepared-workspace "
                "connectivity and comparative KiCad DRC gates",
                "Routing does not certify SI, PI, EMC, thermal, or impedance behavior",
            ),
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
        temporary_pcb: Path,
        target_nets: tuple[str, ...],
        require_complete: bool,
    ) -> tuple[ValidationReport, DrcReport, RoutingAnalysis]:
        structural = self.adapter.validate(temporary_pcb)
        routing = self.adapter.analyze_routing(temporary_pcb, session.id, target_nets)
        before = self.drc_runner(session.pcb_file)
        after = self.drc_runner(temporary_pcb)
        baseline = Counter(
            (item.code, item.message) for item in before.violations if item.severity == "error"
        )
        generated = Counter(
            (item.code, item.message) for item in after.violations if item.severity == "error"
        )
        new_errors = generated - baseline
        complete_ok = routing.complete or not require_complete
        drc_ok = after.available and after.error is None and not new_errors
        messages = list(structural.messages)
        if not complete_ok:
            messages.append(
                f"Prepared routing leaves {routing.unrouted_connection_count} connection(s) open"
            )
        if after.error is not None:
            messages.append(after.error.message)
        messages.extend(
            f"New DRC error: {code or 'unknown'}: {message} (x{count})"
            for (code, message), count in new_errors.items()
        )
        validation = ValidationReport(
            valid=structural.valid and complete_ok and drc_ok,
            checks={
                **structural.checks,
                "routing_complete": routing.complete,
                "routing_completion_required": require_complete,
                "drc_available": after.available,
                "drc_command_ok": after.error is None,
                "drc_no_new_errors": not new_errors,
            },
            messages=tuple(messages),
        )
        return validation, after, routing

    def prepare(self, session_id: str, plan: RoutingPlan) -> PcbRoutingChangeSet:
        if plan.session_id != session_id:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT, "Routing plan belongs to another session"
            )
        session, pcb = self._session_pcb(session_id)
        current = self._current_hashes(session)
        if current != session.hashes:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Project changed after the session was opened",
                actionable_hint="Open the project again before preparing routing changes.",
            )
        identifier = uuid.uuid4().hex
        workspace = self.data_dir / "workspaces" / identifier
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(session.root, workspace, ignore=PROJECT_COPY_IGNORE)
        temporary_pcb = workspace / pcb.relative_to(session.root)
        self.adapter.apply_routing(temporary_pcb, plan.segments, plan.vias)
        validation, drc, routing = self._validate_workspace(
            session, temporary_pcb, plan.target_nets, plan.request.require_complete
        )
        pdf = self.pdf_exporter(temporary_pcb, workspace / "Copperbrain-PCB-routing-preview.pdf")
        preview_directory = publish_preview(workspace, session.root, identifier)
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        change_set = PcbRoutingChangeSet(
            id=identifier,
            session_id=session_id,
            project_hash=aggregate_hash(current),
            plan=plan,
            affected_files=(pcb,),
            source_hashes=current,
            semantic_diff=(
                *(
                    f"route {item.net} on {item.layer}: "
                    f"({item.start_x_mm:g}, {item.start_y_mm:g}) to "
                    f"({item.end_x_mm:g}, {item.end_y_mm:g}), "
                    f"width {item.width_mm:g} mm"
                    for item in plan.segments
                ),
                *(
                    f"via {item.net} at ({item.x_mm:g}, {item.y_mm:g}), "
                    f"{item.diameter_mm:g}/{item.drill_mm:g} mm"
                    for item in plan.vias
                ),
            ),
            risks=(
                "A clean DRC does not certify signal integrity, power integrity, EMC, "
                "thermal, or impedance behavior",
                "KiCad may overwrite external changes if an editor has unsaved state",
            ),
            validation_report=validation,
            drc=drc,
            routing_analysis=routing,
            preview_directory=preview_directory,
            preview_pdf=preview_directory / pdf.relative_to(workspace),
            status=status,
        )
        self._changes[identifier] = _PreparedRouting(change_set, workspace)
        self._persist(self._changes[identifier], session.root)
        return change_set

    def _get(self, change_set_id: str) -> _PreparedRouting:
        try:
            return self._changes[change_set_id]
        except KeyError:
            return self._load(change_set_id)

    def change_set(self, change_set_id: str) -> PcbRoutingChangeSet:
        """Return a routing change, resuming it from private storage when necessary."""
        return self._get(change_set_id).change_set

    def review(self, change_set_id: str) -> RoutingReviewSummary:
        """Return concise decision evidence without serializing every route operation."""
        change = self.change_set(change_set_id)
        errors = sum(item.severity == "error" for item in change.drc.violations)
        warnings = sum(item.severity == "warning" for item in change.drc.violations)
        return RoutingReviewSummary(
            change_set_id=change.id,
            status=change.status,
            target_nets=change.plan.target_nets,
            validation_valid=change.validation_report.valid,
            routing_complete=change.routing_analysis.complete,
            unrouted_connection_count=change.routing_analysis.unrouted_connection_count,
            segment_count=len(change.plan.segments),
            via_count=len(change.plan.vias),
            drc_error_count=errors,
            drc_warning_count=warnings,
            preview_directory=change.preview_directory,
            preview_pdf=change.preview_pdf,
            risks=change.risks,
        )

    def validate(self, change_set_id: str) -> tuple[ValidationReport, DrcReport, RoutingAnalysis]:
        prepared = self._get(change_set_id)
        change = prepared.change_set
        session, pcb = self._session_pcb(change.session_id)
        validation, drc, routing = self._validate_workspace(
            session,
            prepared.workspace / pcb.relative_to(session.root),
            change.plan.target_nets,
            change.plan.request.require_complete,
        )
        status = ChangeStatus.VALIDATED if validation.valid else ChangeStatus.PREPARED
        prepared.change_set = change.model_copy(
            update={
                "validation_report": validation,
                "drc": drc,
                "routing_analysis": routing,
                "status": status,
            }
        )
        self._persist(prepared, session.root)
        return validation, drc, routing

    def apply(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbRoutingChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        prepared = self._get(change_set_id)
        change = prepared.change_set
        if change.status is not ChangeStatus.VALIDATED:
            raise CopperbrainError(ErrorCode.VALIDATION_FAILED, "Routing is not validated")
        session, pcb = self._session_pcb(change.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE,
                "KiCad editor state is not safely closed",
                actionable_hint="Save and close PCB Editor, then retry.",
            )
        if self._current_hashes(session) != change.source_hashes:
            prepared.change_set = change.model_copy(update={"status": ChangeStatus.STALE})
            self._persist(prepared, session.root)
            raise CopperbrainError(ErrorCode.CONFLICT, "Routing change set is stale")
        validation, drc, routing = self._validate_workspace(
            session,
            prepared.workspace / pcb.relative_to(session.root),
            change.plan.target_nets,
            change.plan.request.require_complete,
        )
        if not validation.valid:
            prepared.change_set = change.model_copy(
                update={
                    "validation_report": validation,
                    "drc": drc,
                    "routing_analysis": routing,
                    "status": ChangeStatus.PREPARED,
                }
            )
            self._persist(prepared, session.root)
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Routing failed validation immediately before apply",
                actionable_hint="Review the persisted preview and prepare the routing again.",
            )
        change = change.model_copy(
            update={
                "validation_report": validation,
                "drc": drc,
                "routing_analysis": routing,
            }
        )
        snapshot_id = uuid.uuid4().hex
        snapshot = self.data_dir / "snapshots" / snapshot_id
        snapshot.mkdir(parents=True, exist_ok=False)
        for affected in change.affected_files:
            relative = affected.relative_to(session.root)
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(affected, destination)
        try:
            for affected in change.affected_files:
                relative = affected.relative_to(session.root)
                _atomic_copy(prepared.workspace / relative, affected)
        except Exception:
            for affected in change.affected_files:
                relative = affected.relative_to(session.root)
                _atomic_copy(snapshot / relative, affected)
            raise
        prepared.snapshot = snapshot
        prepared.change_set = change.model_copy(
            update={"status": ChangeStatus.APPLIED, "snapshot_id": snapshot_id}
        )
        self._persist(prepared, session.root)
        return prepared.change_set

    def rollback(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbRoutingChangeSet:
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        prepared = self._get(change_set_id)
        change = prepared.change_set
        if change.status is not ChangeStatus.APPLIED or prepared.snapshot is None:
            raise CopperbrainError(
                ErrorCode.CONFLICT, "Only an applied routing change can be rolled back"
            )
        session, _ = self._session_pcb(change.session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE, "KiCad editor state is not safely closed"
            )
        for affected in change.affected_files:
            relative = affected.relative_to(session.root)
            _atomic_copy(prepared.snapshot / relative, affected)
        prepared.change_set = change.model_copy(update={"status": ChangeStatus.ROLLED_BACK})
        self._persist(prepared, session.root)
        return prepared.change_set

    def restore_snapshot(
        self,
        session_id: str,
        snapshot_id: str,
        *,
        confirmed: bool,
        editor_closed: bool,
    ) -> RoutingSnapshotRestoreResult:
        """Restore a private routing snapshot after binding it to the current board."""
        if not confirmed:
            raise CopperbrainError(
                ErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required"
            )
        if re.fullmatch(r"[0-9a-f]{32}", snapshot_id) is None:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Snapshot identifier is invalid")
        session, pcb = self._session_pcb(session_id)
        if not editor_closed or _editor_lock_exists(session.root):
            raise CopperbrainError(
                ErrorCode.UNSAFE_EDITOR_STATE, "KiCad editor state is not safely closed"
            )
        snapshot_root = (self.data_dir / "snapshots" / snapshot_id).resolve()
        snapshots_root = (self.data_dir / "snapshots").resolve()
        if snapshot_root.parent != snapshots_root:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Snapshot path is invalid")
        relative_pcb = pcb.relative_to(session.root)
        snapshot_pcb = snapshot_root / relative_pcb
        if not snapshot_pcb.is_file():
            raise CopperbrainError(
                ErrorCode.NOT_FOUND,
                "Routing snapshot was not found for this PCB",
                details={"snapshot_id": snapshot_id, "pcb": str(relative_pcb)},
            )

        current_board = self.adapter.parse(pcb)
        snapshot_board = self.adapter.parse(snapshot_pcb)
        excluded_summary = {
            "session_id",
            "pcb_file",
            "track_count",
            "via_count",
            "ipc",
            "warnings",
        }
        current_identity = (
            current_board.summary.model_dump(mode="json", exclude=excluded_summary),
            tuple(sorted(item.model_dump_json() for item in current_board.pads)),
        )
        snapshot_identity = (
            snapshot_board.summary.model_dump(mode="json", exclude=excluded_summary),
            tuple(sorted(item.model_dump_json() for item in snapshot_board.pads)),
        )
        if current_identity != snapshot_identity:
            raise CopperbrainError(
                ErrorCode.CONFLICT,
                "Routing snapshot does not belong to the current PCB geometry",
                actionable_hint="Select a snapshot created from this exact placed board.",
            )

        validation = self.adapter.validate(snapshot_pcb)
        drc = self.drc_runner(snapshot_pcb)
        if not validation.valid or not drc.available or drc.error is not None:
            raise CopperbrainError(
                ErrorCode.VALIDATION_FAILED,
                "Routing snapshot could not be validated before restoration",
                details={
                    "validation_messages": list(validation.messages),
                    "drc_error": drc.error.model_dump(mode="json") if drc.error else None,
                },
            )

        recovery_snapshot_id = uuid.uuid4().hex
        recovery_root = self.data_dir / "snapshots" / recovery_snapshot_id
        recovery_pcb = recovery_root / relative_pcb
        recovery_pcb.parent.mkdir(parents=True, exist_ok=False)
        shutil.copy2(pcb, recovery_pcb)
        try:
            _atomic_copy(snapshot_pcb, pcb)
        except Exception:
            _atomic_copy(recovery_pcb, pcb)
            raise
        return RoutingSnapshotRestoreResult(
            restored_snapshot_id=snapshot_id,
            recovery_snapshot_id=recovery_snapshot_id,
            affected_file=pcb,
            validation_report=validation,
            drc=drc,
        )
