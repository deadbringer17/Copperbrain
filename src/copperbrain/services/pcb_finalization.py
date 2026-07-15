"""Application-level PCB finalization orchestration and readiness audit."""

from __future__ import annotations

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    ErcReport,
    ErrorCode,
    PcbFinalizationResult,
    PcbProductionReadiness,
    PcbReadinessCheck,
    PcbRoutingChangeSet,
    PlacementAnalysis,
    RoutingAnalysis,
    RoutingRequest,
)
from copperbrain.services.pcb_design import PcbDesignService
from copperbrain.services.pcb_routing import PcbRoutingService
from copperbrain.services.projects import ProjectService

_LIMITATIONS = (
    "Signal integrity, power integrity, EMC, thermal behavior, and controlled impedance "
    "are not assessed automatically",
    "Stackup, fabrication tolerances, assembly constraints, and manufacturer DFM review "
    "still require explicit engineering review",
)


class PcbFinalizationService:
    """Compose routing lifecycle operations with an honest production-readiness report."""

    def __init__(
        self,
        projects: ProjectService,
        design: PcbDesignService,
        routing: PcbRoutingService,
    ) -> None:
        self.projects = projects
        self.design = design
        self.routing = routing

    @staticmethod
    def _report_count(report: DrcReport | ErcReport, severity: str) -> int:
        return sum(item.severity == severity for item in report.violations)

    def _build_readiness(
        self,
        session_id: str,
        routing: RoutingAnalysis,
        drc: DrcReport,
        erc: ErcReport,
        placement: PlacementAnalysis,
        zone_count: int,
    ) -> PcbProductionReadiness:
        drc_errors = self._report_count(drc, "error")
        drc_open_items = len(drc.unconnected_items)
        drc_warnings = self._report_count(drc, "warning")
        erc_errors = self._report_count(erc, "error")
        erc_warnings = self._report_count(erc, "warning")
        placement_errors = sum(item.severity == "error" for item in placement.issues)
        checks = (
            PcbReadinessCheck(
                name="routing_connectivity",
                status="pass" if routing.complete else "fail",
                message=(
                    "All selected electrical connections are routed"
                    if routing.complete
                    else f"{routing.unrouted_connection_count} connection(s) remain open"
                ),
            ),
            PcbReadinessCheck(
                name="pcb_drc",
                status=(
                    "pass"
                    if drc.available
                    and drc.error is None
                    and drc_errors == 0
                    and drc_open_items == 0
                    else "fail"
                ),
                message=(
                    f"{drc_errors} error(s), {drc_warnings} warning(s), "
                    f"{drc_open_items} unconnected item(s)"
                    if drc.available and drc.error is None
                    else "KiCad DRC was unavailable or failed"
                ),
            ),
            PcbReadinessCheck(
                name="schematic_erc",
                status=(
                    "pass" if erc.available and erc.error is None and erc_errors == 0 else "fail"
                ),
                message=(
                    f"{erc_errors} error(s), {erc_warnings} warning(s)"
                    if erc.available and erc.error is None
                    else "KiCad ERC was unavailable or failed"
                ),
            ),
            PcbReadinessCheck(
                name="placement_geometry",
                status="pass" if placement_errors == 0 else "fail",
                message=f"Placement score {placement.score}/100; {placement_errors} error(s)",
            ),
            PcbReadinessCheck(
                name="copper_zones",
                status="pass" if zone_count > 0 else "warning",
                message=(
                    f"{zone_count} copper zone(s) present"
                    if zone_count > 0
                    else "No copper zones are present; confirm return paths and thermal spreading"
                ),
            ),
            PcbReadinessCheck(
                name="engineering_signoff",
                status="not_assessed",
                message=_LIMITATIONS[0],
            ),
            PcbReadinessCheck(
                name="manufacturer_dfm",
                status="not_assessed",
                message=_LIMITATIONS[1],
            ),
        )
        blocking = tuple(check.message for check in checks if check.status == "fail")
        electrically_validated = not blocking
        return PcbProductionReadiness(
            session_id=session_id,
            status="routing_validated" if electrically_validated else "blocked",
            electrically_validated=electrically_validated,
            production_ready=False,
            checks=checks,
            blocking_reasons=blocking,
            limitations=_LIMITATIONS,
        )

    def assess(self, session_id: str) -> PcbProductionReadiness:
        """Audit the live source project without changing it."""
        summary = self.design.summary(session_id)
        return self._build_readiness(
            session_id,
            self.routing.analyze(session_id),
            self.projects.run_drc(session_id),
            self.projects.run_erc(session_id),
            self.design.analyze_placement(session_id),
            summary.zone_count,
        )

    def _assess_change(self, change: PcbRoutingChangeSet) -> PcbProductionReadiness:
        summary = self.design.summary(change.session_id)
        return self._build_readiness(
            change.session_id,
            change.routing_analysis,
            change.drc,
            self.projects.run_erc(change.session_id),
            self.design.analyze_placement(change.session_id),
            summary.zone_count,
        )

    def _result(self, change: PcbRoutingChangeSet) -> PcbFinalizationResult:
        return PcbFinalizationResult(
            stage=change.status.value,
            routing=self.routing.review(change.id),
            readiness=self._assess_change(change),
            confirmation_required=change.status
            not in {
                ChangeStatus.APPLIED,
                ChangeStatus.ROLLED_BACK,
            },
        )

    def prepare(self, session_id: str, request: RoutingRequest) -> PcbFinalizationResult:
        """Propose, preview, validate, and persist a routing finalization change."""
        if request.nets:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "PCB finalization must target all routable nets",
                actionable_hint="Leave routing_request.nets empty for a whole-board finalization.",
            )
        plan = self.routing.propose(session_id, request)
        return self._result(self.routing.prepare(session_id, plan))

    def validate(self, change_set_id: str) -> PcbFinalizationResult:
        """Revalidate a persisted finalization change and return concise evidence."""
        self.routing.validate(change_set_id)
        return self._result(self.routing.change_set(change_set_id))

    def apply(
        self, change_set_id: str, *, confirmed: bool, editor_closed: bool
    ) -> PcbFinalizationResult:
        """Apply a validated finalization change, retaining normal confirmation gates."""
        change = self.routing.apply(change_set_id, confirmed=confirmed, editor_closed=editor_closed)
        return self._result(change)

    def report(self, change_set_id: str) -> PcbFinalizationResult:
        """Resume a finalization change from storage and return its current report."""
        return self._result(self.routing.change_set(change_set_id))
