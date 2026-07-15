"""PCB finalization readiness tests."""

from copperbrain.models import (
    DrcReport,
    ErcReport,
    IntegrationStatus,
    PcbSummary,
    PlacementAnalysis,
    RoutingAnalysis,
)
from copperbrain.services.pcb_finalization import PcbFinalizationService


class FakeProjects:
    def run_drc(self, session_id: str) -> DrcReport:
        return DrcReport(available=True)

    def run_erc(self, session_id: str) -> ErcReport:
        return ErcReport(available=True)


class FakeDesign:
    def summary(self, session_id: str) -> PcbSummary:
        return PcbSummary(
            session_id=session_id,
            pcb_file="board.kicad_pcb",
            zone_count=0,
            ipc=IntegrationStatus(name="KiCad PCB IPC", available=False),
        )

    def analyze_placement(self, session_id: str) -> PlacementAnalysis:
        return PlacementAnalysis(session_id=session_id, score=100)


class FakeRouting:
    def analyze(self, session_id: str) -> RoutingAnalysis:
        return RoutingAnalysis(
            session_id=session_id,
            complete=True,
            net_count=2,
            routed_net_count=2,
            unrouted_net_count=0,
            unrouted_connection_count=0,
        )


def test_readiness_does_not_equate_clean_electrical_checks_with_production_ready() -> None:
    service = PcbFinalizationService(
        FakeProjects(),  # type: ignore[arg-type]
        FakeDesign(),  # type: ignore[arg-type]
        FakeRouting(),  # type: ignore[arg-type]
    )

    readiness = service.assess("session")

    assert readiness.status == "routing_validated"
    assert readiness.electrically_validated
    assert not readiness.production_ready
    assert any(item.status == "not_assessed" for item in readiness.checks)
    zones = next(item for item in readiness.checks if item.name == "copper_zones")
    assert zones.status == "warning"
