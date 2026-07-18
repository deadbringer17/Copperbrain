import json
import shutil
from pathlib import Path

import pytest

from copperbrain.adapters.pcb_design import KiCadPcbIpcAdapter, PcbFileAdapter
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    DrcReport,
    GroundDomainRequest,
    GroundingPlan,
    GroundingRequest,
    IntegrationStatus,
)
from copperbrain.services.pcb_design import PcbDesignService
from copperbrain.services.pcb_grounding import PcbGroundingService
from copperbrain.services.projects import ProjectService

FIXTURE = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")


class FakeGroundingAdapter:
    """Offline renderer sufficient to exercise the application safety workflow."""

    def apply(self, pcb: Path, plan: GroundingPlan) -> None:
        text = pcb.read_text(encoding="utf-8-sig")
        closing = text.rfind(")")
        zones = "\n".join(
            f'  (zone (net 1) (net_name "{domain.net_name}") '
            f'(layer "{layer}") '
            "(polygon (pts (xy 0.5 0.5) (xy 39.5 0.5) "
            "(xy 39.5 29.5) (xy 0.5 29.5))))"
            for domain in plan.domains
            for layer in domain.plane_layers
        )
        pcb.write_text(text[:closing].rstrip() + "\n" + zones + "\n)\n", encoding="utf-8")
        segments = tuple(item for domain in plan.domains for item in domain.fanout_segments)
        vias = tuple(item for domain in plan.domains for item in domain.vias)
        if segments or vias:
            PcbFileAdapter().apply_routing(pcb, segments, vias)


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PcbGroundingService, Path, str]:
    monkeypatch.setattr(
        KiCadPcbIpcAdapter,
        "status",
        staticmethod(lambda: IntegrationStatus(name="KiCad PCB IPC", available=False)),
    )
    root = tmp_path / "project"
    root.mkdir()
    (root / "placement.kicad_pro").write_text(
        json.dumps({"meta": {"version": 1}}), encoding="utf-8"
    )
    (root / "placement.kicad_sch").write_text("fixture", encoding="utf-8")
    pcb = root / "placement.kicad_pcb"
    shutil.copy2(FIXTURE, pcb)
    projects = ProjectService()
    session = projects.open_project(root)

    def export_pdf(source: Path, destination: Path) -> Path:
        assert source.is_file()
        destination.write_bytes(b"%PDF-1.4\n% grounding preview\n")
        return destination

    design = PcbDesignService(
        projects,
        tmp_path / "data",
        drc_runner=lambda path: DrcReport(available=True),
        pdf_exporter=export_pdf,
    )
    service = PcbGroundingService(
        projects,
        design,
        tmp_path / "data",
        grounding_adapter=FakeGroundingAdapter(),  # type: ignore[arg-type]
        drc_runner=lambda path: DrcReport(available=True),
        pdf_exporter=export_pdf,
    )
    return service, pcb, session.id


def test_grounding_plan_targets_every_gnd_pad_after_placement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session = setup(tmp_path, monkeypatch)
    plan = service.plan(session, GroundingRequest())
    domain = plan.domains[0]
    assert domain.net_name == "GND"
    assert domain.target_pad_count == 2
    assert domain.target_references == ("C1", "R1")
    assert domain.plane_layers == ("F.Cu", "B.Cu")
    assert domain.planes_connected


def test_single_explicit_ground_domain_keeps_layers_domain_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, session = setup(tmp_path, monkeypatch)
    plan = service.plan(
        session,
        GroundingRequest(domains=(GroundDomainRequest(net_name="GND", layers=("F.Cu",)),)),
    )
    assert plan.request.layers == ()
    assert plan.request.domains[0].layers == ("F.Cu",)
    assert plan.domains[0].primary_layer == "F.Cu"


def test_grounding_auto_policy_uses_first_inner_layer_on_four_layer_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, _session = setup(tmp_path, monkeypatch)
    text = pcb.read_text(encoding="utf-8")
    text = text.replace(
        '    (0 "F.Cu" signal)\n',
        '    (0 "F.Cu" signal)\n    (4 "In1.Cu" power)\n    (6 "In2.Cu" signal)\n',
    )
    pcb.write_text(text, encoding="utf-8")
    refreshed = service.projects.open_project(pcb.parent)

    plan = service.plan(refreshed.id, GroundingRequest(copper_layers=4))

    assert plan.request.layers == ("F.Cu", "In1.Cu", "B.Cu")
    assert plan.domains[0].plane_layers == ("F.Cu", "In1.Cu", "B.Cu")


def test_grounding_prepare_apply_and_byte_exact_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    original = pcb.read_bytes()
    change = service.prepare(session, GroundingRequest())
    assert change.status is ChangeStatus.VALIDATED
    assert change.grounding_analysis.complete
    assert change.grounding_analysis.domains[0].connected_references == ("C1", "R1")
    assert change.preview_pdf is not None and change.preview_pdf.is_file()
    assert pcb.read_bytes() == original
    assert service.validate(change.id)[0].valid
    with pytest.raises(CopperbrainError, match="confirmation"):
        service.apply(change.id, confirmed=False, editor_closed=True)
    project_manager_lock = pcb.parent / "~fixture.kicad_pro.lck"
    project_manager_lock.write_text("project manager open", encoding="utf-8")
    assert (
        service.apply(change.id, confirmed=True, editor_closed=True).status is ChangeStatus.APPLIED
    )
    assert pcb.read_bytes() != original
    pcb_editor_lock = pcb.parent / "~fixture.kicad_pcb.lck"
    pcb_editor_lock.write_text("pcb editor open", encoding="utf-8")
    with pytest.raises(CopperbrainError, match="safely closed"):
        service.rollback(change.id, confirmed=True, editor_closed=True)
    pcb_editor_lock.unlink()
    assert (
        service.rollback(change.id, confirmed=True, editor_closed=True).status
        is ChangeStatus.ROLLED_BACK
    )
    assert pcb.read_bytes() == original


def test_private_grounding_prepare_skips_preview_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _pcb, session = setup(tmp_path, monkeypatch)
    service.publish_artifacts = False
    service.pdf_exporter = lambda *_args: pytest.fail("private prepare exported a PDF")

    change = service.prepare(session, GroundingRequest())

    assert change.preview_pdf is None
    assert change.preview_directory.is_relative_to(tmp_path / "data" / "workspaces")
    assert not (tmp_path / "project" / "copperbrain-output").exists()


def test_grounding_can_apply_and_rollback_after_service_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, session = setup(tmp_path, monkeypatch)
    original = pcb.read_bytes()
    change = service.prepare(session, GroundingRequest())

    restarted = restart_service(tmp_path, pcb.parent)
    applied = restarted.apply(change.id, confirmed=True, editor_closed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert pcb.read_bytes() != original

    restarted_again = restart_service(tmp_path, pcb.parent)
    rolled_back = restarted_again.rollback(change.id, confirmed=True, editor_closed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert pcb.read_bytes() == original


def restart_service(tmp_path: Path, root: Path) -> PcbGroundingService:
    projects = ProjectService()
    projects.open_project(root)
    design = PcbDesignService(
        projects,
        tmp_path / "data",
        drc_runner=lambda path: DrcReport(available=True),
    )
    return PcbGroundingService(
        projects,
        design,
        tmp_path / "data",
        grounding_adapter=FakeGroundingAdapter(),  # type: ignore[arg-type]
        drc_runner=lambda path: DrcReport(available=True),
    )


def test_grounding_refuses_multiple_domains_without_two_terminal_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, _session = setup(tmp_path, monkeypatch)
    text = pcb.read_text(encoding="utf-8")
    text = text.replace('(net 1 "GND")', '(net 1 "GND")\n  (net 2 "AGND")', 1)
    text = text.replace(
        "(roundrect_rratio 0.25))",
        '(roundrect_rratio 0.25) (net 2 "AGND"))',
        1,
    )
    text = text.replace('(net 1 "GND"))', ")", 1)
    pcb.write_text(text, encoding="utf-8")
    refreshed = service.projects.open_project(pcb.parent)
    request = GroundingRequest(
        domains=(
            GroundDomainRequest(net_name="GND", layers=("F.Cu",)),
            GroundDomainRequest(net_name="AGND", layers=("B.Cu",)),
        )
    )
    with pytest.raises(CopperbrainError, match="bridge references"):
        service.plan(refreshed.id, request)


def test_grounding_separates_bridge_connected_domains_by_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pcb, _session = setup(tmp_path, monkeypatch)
    text = pcb.read_text(encoding="utf-8")
    text = text.replace('(net 1 "GND")', '(net 1 "GND")\n  (net 2 "PGND")', 1)
    text = text.replace(
        '(pad "2" smd roundrect (at 0.8 0) (size 0.8 0.9) '
        '(layers "F.Cu" "F.Paste" "F.Mask")\n      (roundrect_rratio 0.25))',
        '(pad "2" smd roundrect (at 0.8 0) (size 0.8 0.9) '
        '(layers "F.Cu" "F.Paste" "F.Mask")\n      '
        '(roundrect_rratio 0.25) (net 2 "PGND"))',
        1,
    )
    text = text.replace(
        '  (segment (start 9.2 10) (end 19.2 10) (width 0.25) (layer "F.Cu") (net 1))\n',
        "",
    )
    text = text.replace(
        '  (via (at 14 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n',
        "",
    )
    pcb.write_text(text, encoding="utf-8")
    refreshed = service.projects.open_project(pcb.parent)

    with pytest.raises(CopperbrainError, match="explicit reviewed bridge"):
        service.plan(refreshed.id, GroundingRequest())
    request = GroundingRequest(bridge_references=("R1",))
    plan = service.plan(refreshed.id, request)

    by_net = {item.net_name: item for item in plan.domains}
    assert by_net["GND"].primary_layer == "B.Cu"
    assert by_net["GND"].plane_layers == ("F.Cu", "B.Cu")
    assert {item.kind for item in by_net["GND"].regions} == {"board", "local"}
    assert by_net["PGND"].primary_layer == "F.Cu"
    assert by_net["PGND"].plane_layers == ("F.Cu",)
    assert len(by_net["GND"].fanout_segments) == 2
    assert len(by_net["GND"].vias) == 2
    assert plan.bridges[0].reference == "R1"
    change = service.prepare(refreshed.id, request)
    assert change.status is ChangeStatus.VALIDATED
    assert change.grounding_analysis.complete
    assert change.grounding_analysis.bridges_connected
    assert all(item.complete for item in change.grounding_analysis.domains)
