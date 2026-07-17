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
    ErrorCode,
    FreeRoutingPassMetric,
    IntegrationStatus,
    PcbPadInspection,
    RouteSegment,
    RoutingAnalysis,
    RoutingBackendStatus,
    RoutingRequest,
    UnroutedConnection,
)
from copperbrain.services.pcb_routing import PcbRoutingService
from copperbrain.services.projects import ProjectService

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


class FakeRoutingBackend:
    def status(self) -> RoutingBackendStatus:
        return RoutingBackendStatus(available=True, version="test")

    def refill_zones(self, pcb: Path) -> None:
        assert pcb.is_file()

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
            pass_metrics=(
                FreeRoutingPassMetric(
                    pass_number=1,
                    board_incomplete_count=1,
                    queued_item_count=1,
                    board_unrouted_count=0,
                    duration_seconds=0.01,
                ),
            ),
        )


class FailingRoutingBackend:
    def status(self) -> RoutingBackendStatus:
        return RoutingBackendStatus(available=True, version="test")

    def refill_zones(self, pcb: Path) -> None:
        assert pcb.is_file()

    def route(
        self, pcb: Path, workspace: Path, request: RoutingRequest, strategy: str
    ) -> RoutedBoardCandidate:
        raise CopperbrainError(
            ErrorCode.VALIDATION_FAILED,
            "fixture routing failure",
            details={
                "watchdog": "stalled",
                "freerouting_pass_metrics": [
                    FreeRoutingPassMetric(
                        pass_number=1,
                        board_incomplete_count=1,
                        queued_item_count=1,
                    ).model_dump(mode="json")
                ],
                "freerouting_normalization_count": 2,
            },
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


def test_routing_proposal_emits_reusable_connectivity_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session = setup(tmp_path, monkeypatch)

    plan = service.propose(session, RoutingRequest())

    records = sorted((tmp_path / "data" / "metrics" / "connectivity").rglob("*.json"))
    assert [path.name for path in records] == ["baseline.json", "candidate-prioritized.json"]
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in records]
    assert {payload["phase"] for payload in payloads} == {"baseline", "candidate"}
    assert all(payload["schema_version"] == 4 for payload in payloads)
    assert all(len(payload["project_fingerprint"]) == 64 for payload in payloads)
    assert payloads[1]["outcome"] == "success"
    assert payloads[1]["freerouting_pass_metrics"][0]["queued_item_count"] == 1
    assert payloads[1]["requested_net_role_counts"] == {"ground": 1}
    assert payloads[1]["board_width_mm"] == 40
    assert payloads[1]["copper_layer_count"] == 2
    assert payloads[1]["footprint_count"] == 2
    assert payloads[1]["pad_count"] == 4
    assert payloads[1]["open_connection_delta"] == 1
    assert payloads[1]["copper_produced_per_second"] > 0
    assert payloads[1]["connections_resolved_per_pass"] == 1
    assert payloads[1]["best_pass_number"] == 1
    assert plan.metrics_run_id == payloads[0]["run_id"] == payloads[1]["run_id"]

    summary = service.metrics_for_run(plan.metrics_run_id)
    assert summary.record_count == 2
    assert summary.best_strategy == "prioritized"
    assert summary.best_open_connection_delta == 1
    assert summary.comparable_candidate_count == 1
    assert summary.failed_candidate_count == 0
    assert summary.recommended_max_passes == 2
    assert summary.same_baseline_batches[0].run_id == plan.metrics_run_id


def test_net_role_metrics_require_both_differential_members() -> None:
    assert PcbRoutingService._requested_net_role_counts(("FAULT_N", "SPI_CS_N")) == {"signal": 2}
    assert PcbRoutingService._requested_net_role_counts(("USB_D_P", "USB_D_N")) == {
        "differential_candidate": 2
    }
    assert PcbRoutingService._requested_net_role_counts(
        ("MYSTERY",), {"MYSTERY": "high_current"}
    ) == {"high_current": 1}


def test_local_routing_hotspots_trigger_placement_rework_evidence() -> None:
    pads = tuple(
        PcbPadInspection(
            reference=reference,
            number="1",
            net="N",
            x_mm=x,
            y_mm=y,
            width_mm=1,
            height_mm=1,
            layers=("F.Cu",),
        )
        for reference, x, y in (("U1", 10, 10), ("R1", 12, 10), ("R2", 13, 11))
    )
    analysis = RoutingAnalysis(
        session_id="fixture",
        complete=False,
        net_count=1,
        routed_net_count=0,
        unrouted_net_count=1,
        unrouted_connection_count=2,
        unrouted_connections=(
            UnroutedConnection(
                net="N",
                start_reference="U1",
                start_pad="1",
                end_reference="R1",
                end_pad="1",
                distance_mm=2,
            ),
            UnroutedConnection(
                net="N",
                start_reference="U1",
                start_pad="1",
                end_reference="R2",
                end_pad="1",
                distance_mm=3.162,
            ),
        ),
    )

    hotspots = PcbRoutingService._routing_hotspots(pads, analysis)

    assert hotspots[0].connection_count == 2
    assert hotspots[0].references == ("R1", "R2", "U1")


def test_routing_lifecycle_metrics_are_correlated_to_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session = setup(tmp_path, monkeypatch)
    plan = service.propose(session, RoutingRequest())
    change = service.prepare(session, plan)
    service.validate(change.id)
    service.apply(change.id, confirmed=True, editor_closed=True)
    service.rollback(change.id, confirmed=True, editor_closed=True)

    summary = service.metrics_for_run(plan.metrics_run_id)
    assert {item.phase for item in summary.records} == {
        "baseline",
        "candidate",
        "prepare",
        "validate",
        "apply",
        "rollback",
    }
    lifecycle = [item for item in summary.records if item.operation == "routing_change"]
    assert all(item.parent_run_id == plan.metrics_run_id for item in lifecycle)


def test_metrics_reader_accepts_persisted_schema_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session = setup(tmp_path, monkeypatch)
    plan = service.propose(session, RoutingRequest())
    baseline = next((tmp_path / "data" / "metrics" / "connectivity").rglob("baseline.json"))
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    assert service.metrics_for_run(plan.metrics_run_id).records[0].schema_version == 2


def test_failed_candidate_flushes_structured_metrics_before_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session = setup(tmp_path, monkeypatch)
    service.routing_backend = FailingRoutingBackend()

    with pytest.raises(CopperbrainError, match="no usable routing candidate") as caught:
        service.propose(session, RoutingRequest())

    diagnostics = caught.value.error.details["partial_candidate_diagnostics"]
    assert diagnostics[0]["diagnostic_only"] is True
    assert diagnostics[0]["applicable"] is False

    candidate = next(
        (tmp_path / "data" / "metrics" / "connectivity").rglob("candidate-prioritized.json")
    )
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    assert payload["outcome"] == "failure"
    assert payload["error_code"] == "validation_failed"
    assert payload["watchdog_reason"] == "stalled"
    assert payload["freerouting_pass_metrics"][0]["board_incomplete_count"] == 1
    assert payload["freerouting_normalization_count"] == 2
    summary = service.metrics_for_run(candidate.parent.name)
    assert summary.failed_candidate_count == 1
    assert summary.best_observed_pass_number == 1
    assert summary.watchdog_reasons == ("stalled",)


def test_routing_delta_rejects_new_copper_outside_requested_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, _ = setup(tmp_path, monkeypatch)
    source = tmp_path / "source.kicad_pcb"
    routed = tmp_path / "routed.kicad_pcb"
    content = pcb.read_text(encoding="utf-8").replace(
        '  (net 1 "GND")', '  (net 1 "GND")\n  (net 2 "OTHER")'
    )
    source.write_text(content, encoding="utf-8")
    routed.write_text(content, encoding="utf-8")
    service.adapter.apply_routing(
        routed,
        (
            RouteSegment(
                net="OTHER",
                start_x_mm=5,
                start_y_mm=5,
                end_x_mm=6,
                end_y_mm=5,
                width_mm=0.25,
            ),
        ),
        (),
    )

    with pytest.raises(CopperbrainError, match="outside the requested routing scope"):
        service._routing_delta(source, routed, ("GND",))


def test_routing_delta_tolerates_specctra_coordinate_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = setup(tmp_path, monkeypatch)
    source = tmp_path / "source.kicad_pcb"
    routed = tmp_path / "routed.kicad_pcb"
    shutil.copy2(FIXTURE, source)
    content = FIXTURE.read_text(encoding="utf-8")
    content = content.replace("(start 9.2 10)", "(start 9.2004 10)")
    content = content.replace("(at 14 10)", "(at 14.0004 10)")
    routed.write_text(content, encoding="utf-8")
    service.adapter.apply_routing(
        routed,
        (
            RouteSegment(
                net="GND",
                start_x_mm=9.2,
                start_y_mm=10,
                end_x_mm=14,
                end_y_mm=12,
                width_mm=0.25,
            ),
        ),
        (),
    )

    segments, vias = service._routing_delta(source, routed, ("GND",))

    assert len(segments) == 1
    assert not vias


def test_routing_delta_tolerates_specctra_segment_subdivision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = setup(tmp_path, monkeypatch)
    source = tmp_path / "source.kicad_pcb"
    routed = tmp_path / "routed.kicad_pcb"
    shutil.copy2(FIXTURE, source)
    content = FIXTURE.read_text(encoding="utf-8").replace(
        '  (segment (start 9.2 10) (end 19.2 10) (width 0.25) (layer "F.Cu") (net 1))\n',
        '  (segment (start 9.2 10) (end 14.2 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        '  (segment (start 14.2 10) (end 19.2 10) (width 0.25) (layer "F.Cu") (net 1))\n',
    )
    routed.write_text(content, encoding="utf-8")
    service.adapter.apply_routing(
        routed,
        (
            RouteSegment(
                net="GND",
                start_x_mm=5,
                start_y_mm=5,
                end_x_mm=6,
                end_y_mm=5,
                width_mm=0.25,
            ),
        ),
        (),
    )

    segments, vias = service._routing_delta(source, routed, ("GND",))

    assert len(segments) == 1
    assert not vias


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
