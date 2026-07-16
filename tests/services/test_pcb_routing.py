"""Safe deterministic PCB routing service tests."""

import json
import shutil
from pathlib import Path

import pytest

from copperbrain.adapters.freerouting import FreeRoutingAdapter, RoutedBoardCandidate
from copperbrain.adapters.pcb_design import KiCadPcbIpcAdapter, PcbFileAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    IntegrationStatus,
    RouteSegment,
    RoutingBackendStatus,
    RoutingRequest,
)
from copperbrain.services.pcb_routing import PcbRoutingService
from copperbrain.services.projects import ProjectService

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


class FakeRoutingBackend:
    def status(self) -> RoutingBackendStatus:
        return RoutingBackendStatus(available=True, version="test")

    def route(
        self, pcb: Path, workspace: Path, request: RoutingRequest, strategy: str
    ) -> RoutedBoardCandidate:
        assert request.nets == ("GND",)
        workspace.mkdir(parents=True)
        routed = workspace / "routed.kicad_pcb"
        shutil.copy2(pcb, routed)
        PcbFileAdapter().apply_routing(
            routed,
            (
                RouteSegment(
                    net="GND",
                    start_x_mm=9.2,
                    start_y_mm=10,
                    end_x_mm=19.23,
                    end_y_mm=10.02,
                    width_mm=0.25,
                ),
            ),
            (),
        )
        return RoutedBoardCandidate(
            strategy="prioritized",
            pcb=routed,
            elapsed_seconds=0.01,
        )


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PcbRoutingService, Path, str]:
    monkeypatch.setattr(
        KiCadPcbIpcAdapter,
        "status",
        staticmethod(lambda: IntegrationStatus(name="KiCad PCB IPC", available=False)),
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "routing.kicad_pro").write_text(json.dumps({"meta": {"version": 1}}), encoding="utf-8")
    (root / "routing.kicad_sch").write_text("fixture", encoding="utf-8")
    (root / "routing.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
    pcb = root / "routing.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    text = pcb.read_text(encoding="utf-8")
    text = text.replace(
        '  (segment (start 9.2 10) (end 19.2 10) (width 0.25) (layer "F.Cu") (net 1))\n',
        "",
    )
    text = text.replace(
        '  (via (at 14 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n',
        "",
    )
    text = text.replace("(at 20 10 0)", "(at 20.03 10.02 0)")
    pcb.write_text(text, encoding="utf-8")
    projects = ProjectService()
    session = projects.open_project(root)

    def export_pdf(source: Path, destination: Path) -> Path:
        assert source.is_file()
        destination.write_bytes(b"%PDF-1.4\n% routing preview\n")
        return destination

    service = PcbRoutingService(
        projects,
        tmp_path / "data",
        drc_runner=lambda path: DrcReport(available=True),
        pdf_exporter=export_pdf,
        routing_backend=FakeRoutingBackend(),
    )
    return service, pcb, session.id


def test_propose_prepare_apply_and_byte_exact_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    original = pcb.read_bytes()
    analysis = service.analyze(session)
    assert analysis.unrouted_connection_count == 1
    plan = service.propose(session, RoutingRequest())
    assert plan.target_nets == ("GND",)
    assert plan.request.nets == ("GND",)
    assert plan.segments

    change = service.prepare(session, plan)
    assert change.status is ChangeStatus.VALIDATED
    assert change.routing_analysis.complete
    assert pcb.read_bytes() == original
    assert change.preview_pdf is not None and change.preview_pdf.is_file()
    assert service.validate(change.id)[0].valid
    with pytest.raises(CopperbrainError, match="confirmation"):
        service.apply(change.id, confirmed=False, editor_closed=True)
    project_manager_lock = pcb.parent / "~routing.kicad_pro.lck"
    project_manager_lock.write_text("project manager open", encoding="utf-8")
    applied = service.apply(change.id, confirmed=True, editor_closed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert service.analyze(session).complete
    pcb_editor_lock = pcb.parent / "~routing.kicad_pcb.lck"
    pcb_editor_lock.write_text("pcb editor open", encoding="utf-8")
    with pytest.raises(CopperbrainError, match="safely closed"):
        service.rollback(change.id, confirmed=True, editor_closed=True)
    pcb_editor_lock.unlink()
    rolled_back = service.rollback(change.id, confirmed=True, editor_closed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert pcb.read_bytes() == original


def test_stale_routing_change_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    change = service.prepare(session, service.propose(session, RoutingRequest()))
    pcb.write_text(pcb.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(CopperbrainError, match="stale"):
        service.apply(change.id, confirmed=True, editor_closed=True)


def test_routing_change_resumes_across_service_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    original = pcb.read_bytes()
    change = service.prepare(session, service.propose(session, RoutingRequest()))
    record = tmp_path / "data" / "routing-changes" / f"{change.id}.json"
    assert record.is_file()

    resumed = PcbRoutingService(
        ProjectService(),
        tmp_path / "data",
        drc_runner=lambda path: DrcReport(available=True),
        routing_backend=FakeRoutingBackend(),
    )
    assert resumed.validate(change.id)[0].valid
    applied = resumed.apply(change.id, confirmed=True, editor_closed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert pcb.read_bytes() != original

    restarted_again = PcbRoutingService(
        ProjectService(),
        tmp_path / "data",
        drc_runner=lambda path: DrcReport(available=True),
        routing_backend=FakeRoutingBackend(),
    )
    rolled_back = restarted_again.rollback(change.id, confirmed=True, editor_closed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert pcb.read_bytes() == original


def test_apply_revalidates_persisted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    original = pcb.read_bytes()
    change = service.prepare(session, service.propose(session, RoutingRequest()))
    workspace_pcb = tmp_path / "data" / "workspaces" / change.id / pcb.name
    shutil.copy2(pcb, workspace_pcb)

    with pytest.raises(CopperbrainError, match="immediately before apply"):
        service.apply(change.id, confirmed=True, editor_closed=True)

    assert pcb.read_bytes() == original
    assert service.change_set(change.id).status is ChangeStatus.PREPARED


def test_applied_routing_snapshot_can_be_restored_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    original = pcb.read_bytes()
    change = service.prepare(session, service.propose(session, RoutingRequest()))
    applied = service.apply(change.id, confirmed=True, editor_closed=True)
    routed = pcb.read_bytes()
    assert applied.snapshot_id is not None
    assert routed != original

    with pytest.raises(CopperbrainError, match="confirmation"):
        service.restore_snapshot(
            session,
            applied.snapshot_id,
            confirmed=False,
            editor_closed=True,
        )
    restored = service.restore_snapshot(
        session,
        applied.snapshot_id,
        confirmed=True,
        editor_closed=True,
    )

    assert restored.status == "restored"
    assert pcb.read_bytes() == original
    recovery = tmp_path / "data" / "snapshots" / restored.recovery_snapshot_id / pcb.name
    assert recovery.read_bytes() == routed


def test_candidate_drc_receives_same_stem_project_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session = setup(tmp_path, monkeypatch)

    def inspect_rules(pcb: Path | None) -> DrcReport:
        assert pcb is not None
        if pcb.name == "routed.kicad_pcb":
            assert pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8") == "(version 1)\n"
        return DrcReport(available=True)

    service.drc_runner = inspect_rules
    assert service.propose(session, RoutingRequest()).segments


def test_duplicate_candidates_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, session = setup(tmp_path, monkeypatch)
    plan = service.propose(session, RoutingRequest(candidate_count=2))

    assert len(plan.candidate_evaluations) == 2
    assert plan.candidate_evaluations[0].duplicate_of is None
    assert plan.candidate_evaluations[1].duplicate_of == "prioritized"
    assert plan.candidate_evaluations[0].fingerprint == plan.candidate_evaluations[1].fingerprint


def test_unavailable_backend_is_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, session = setup(tmp_path, monkeypatch)
    service.routing_backend = FreeRoutingAdapter(
        jar_path=None,
        java_path=None,
        kicad_python_path=None,
    )
    with pytest.raises(CopperbrainError, match="unavailable") as caught:
        service.propose(session, RoutingRequest())
    assert caught.value.error.actionable_hint is not None
    assert "COPPERBRAIN_FREEROUTING_JAR" in caught.value.error.actionable_hint


def test_incremental_routing_requires_explicit_preserve_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    service.adapter.apply_routing(
        pcb,
        (
            RouteSegment(
                net="GND",
                start_x_mm=1,
                start_y_mm=1,
                end_x_mm=2,
                end_y_mm=1,
                width_mm=0.25,
            ),
        ),
        (),
    )
    with pytest.raises(CopperbrainError, match="already contains copper") as caught:
        service.propose(session, RoutingRequest())
    assert caught.value.error.actionable_hint is not None
    assert "existing_copper_policy='preserve'" in caught.value.error.actionable_hint
