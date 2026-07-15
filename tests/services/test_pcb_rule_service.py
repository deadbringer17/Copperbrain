import json
from pathlib import Path

import pytest

from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    ChangeStatus,
    Component,
    DrcReport,
    ManufacturingProfile,
    Net,
    NetPin,
    NetRuleRequirement,
    ProjectSummary,
    ValidationReport,
)
from copperbrain.services.pcb_rules import PcbRuleService
from copperbrain.services.projects import ProjectService


def setup(tmp_path: Path) -> tuple[PcbRuleService, Path, str]:
    root = tmp_path / "project"
    root.mkdir()
    project = root / "demo.kicad_pro"
    project.write_text(
        json.dumps(
            {
                "meta": {"version": 1},
                "net_settings": {"classes": [], "netclass_patterns": []},
            }
        ),
        encoding="utf-8",
    )
    (root / "demo.kicad_sch").write_text("fixture", encoding="utf-8")
    projects = ProjectService()
    session = projects.open_project(root)
    summary = ProjectSummary(
        session_id=session.id,
        sheets=("demo.kicad_sch",),
        components=(Component(reference="U1", value="MCU"),),
        nets=(
            Net(name="/+5V", pins=(NetPin(reference="U1", pin="1"),)),
            Net(name="/SIG", pins=(NetPin(reference="U1", pin="2"),)),
            Net(name="/USB_P", pins=(NetPin(reference="U1", pin="3"),)),
            Net(name="/USB_N", pins=(NetPin(reference="U1", pin="4"),)),
            Net(name="/NODE", pins=(NetPin(reference="U1", pin="5", pin_name="SW"),)),
        ),
        power_symbols=("+5V",),
    )
    projects.summary = lambda session_id: summary  # type: ignore[method-assign]
    service = PcbRuleService(
        projects,
        tmp_path / "data",
        drc_runner=lambda pcb: DrcReport(available=False),
        footprint_validator=lambda path: ValidationReport(valid=True),
    )
    return service, project, session.id


def requirement() -> NetRuleRequirement:
    return NetRuleRequirement(
        name="PWR_2A",
        nets=("/+5V",),
        role="high_current",
        current_a=2,
        clearance_mm=0.3,
    )


def test_analysis_classifies_power_differential_and_switching_nets(tmp_path: Path) -> None:
    service, _, session = setup(tmp_path)
    roles = {item.net: item.suggested_role for item in service.analyze(session).candidates}
    assert roles["/+5V"] == "power"
    assert roles["/USB_P"] == roles["/USB_N"] == "differential"
    assert roles["/NODE"] == "switching"


def test_proposal_sizes_high_current_track_and_rejects_unsafe_intent(tmp_path: Path) -> None:
    service, _, session = setup(tmp_path)
    proposal = service.propose(session, ManufacturingProfile(), (requirement(),))
    assert proposal.classes[0].track_width_min_mm > 0.2
    assert proposal.assignments[0].net == "/+5V"
    with pytest.raises(CopperbrainError, match="explicit reviewed clearance"):
        service.propose(
            session,
            ManufacturingProfile(),
            (NetRuleRequirement(name="HV", nets=("/SIG",), role="high_voltage", voltage_v=400),),
        )
    with pytest.raises(CopperbrainError, match="unknown nets"):
        service.propose(
            session,
            ManufacturingProfile(),
            (NetRuleRequirement(name="BAD", nets=("/MISSING",), role="signal"),),
        )


def test_prepare_apply_and_rollback_preserve_live_project_until_confirmation(
    tmp_path: Path,
) -> None:
    service, project, session = setup(tmp_path)
    original = project.read_bytes()
    proposal = service.propose(session, ManufacturingProfile(), (requirement(),))
    change = service.prepare(session, proposal)
    assert change.status is ChangeStatus.VALIDATED
    assert project.read_bytes() == original
    assert not (project.parent / "demo.kicad_dru").exists()
    assert (change.preview_directory / "demo.kicad_dru").is_file()
    with pytest.raises(CopperbrainError, match="confirmation"):
        service.apply(change.id, confirmed=False, editor_closed=True)
    applied = service.apply(change.id, confirmed=True, editor_closed=True)
    assert applied.status is ChangeStatus.APPLIED
    assert (project.parent / "demo.kicad_dru").is_file()
    rolled_back = service.rollback(change.id, confirmed=True, editor_closed=True)
    assert rolled_back.status is ChangeStatus.ROLLED_BACK
    assert project.read_bytes() == original
    assert not (project.parent / "demo.kicad_dru").exists()


def test_apply_refuses_rule_file_that_appeared_after_prepare(tmp_path: Path) -> None:
    service, project, session = setup(tmp_path)
    proposal = service.propose(session, ManufacturingProfile(), (requirement(),))
    change = service.prepare(session, proposal)
    (project.parent / "demo.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
    with pytest.raises(CopperbrainError, match="appeared or disappeared"):
        service.apply(change.id, confirmed=True, editor_closed=True)


def test_fine_pitch_component_gets_local_neckdown_and_courtyard(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "demo.kicad_pro").write_text(
        json.dumps({"net_settings": {"classes": [], "netclass_patterns": []}}),
        encoding="utf-8",
    )
    (root / "demo.kicad_sch").write_text("fixture", encoding="utf-8")
    library = root / "copperbrain-libs" / "CB.pretty"
    library.mkdir(parents=True)
    footprint = library / "FinePitch.kicad_mod"
    footprint.write_text(
        """(footprint "FinePitch" (version 20241229) (layer "F.Cu")
        (fp_line (start -1 -1) (end 1 -1) (stroke (width 0.1) (type solid))
          (layer "F.SilkS"))
        (pad "1" smd rect (at -0.325 0) (size 0.343 1.2) (layers "F.Cu" "F.Mask"))
        (pad "2" smd rect (at 0.325 0) (size 0.343 1.2) (layers "F.Cu" "F.Mask")))""",
        encoding="utf-8",
    )
    (root / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "CB")(type "KiCad")'
        '(uri "${KIPRJMOD}/copperbrain-libs/CB.pretty")))',
        encoding="utf-8",
    )
    projects = ProjectService()
    session = projects.open_project(root)
    summary = ProjectSummary(
        session_id=session.id,
        sheets=("demo.kicad_sch",),
        components=(Component(reference="U1", value="Power IC", footprint="CB:FinePitch"),),
        nets=(
            Net(
                name="/SW",
                pins=(NetPin(reference="U1", pin="1", pin_name="SW"),),
            ),
        ),
        power_symbols=(),
    )
    projects.summary = lambda session_id: summary  # type: ignore[method-assign]
    service = PcbRuleService(
        projects,
        tmp_path / "data",
        drc_runner=lambda pcb: DrcReport(available=False),
        footprint_validator=lambda path: ValidationReport(valid=True),
    )
    requirement = NetRuleRequirement(
        name="SWITCHING",
        nets=("/SW",),
        role="switching",
        clearance_mm=0.4,
        track_width_mm=1.2,
    )
    proposal = service.propose(session.id, ManufacturingProfile(), (requirement,))
    assert proposal.fanout_constraints[0].reference == "U1"
    assert proposal.fanout_constraints[0].max_track_width_mm == 0.27
    assert proposal.fanout_constraints[0].clearance_mm == 0.3
    assert proposal.courtyard_additions[0].footprint == "CB:FinePitch"
    change = service.prepare(session.id, proposal)
    assert change.status is ChangeStatus.VALIDATED
    preview_footprint = change.preview_directory / "copperbrain-libs" / "CB.pretty" / footprint.name
    assert '(layer "F.CrtYd")' in preview_footprint.read_text(encoding="utf-8")
    rules = (change.preview_directory / "demo.kicad_dru").read_text(encoding="utf-8")
    assert "A.intersectsCourtyard('U1')" in rules
    assert "(max 0.27mm)" in rules
    assert "Copperbrain_fanout_clearance_U1" in rules
    assert "(constraint clearance (min 0.3mm))" in rules
    assert '(layer "F.CrtYd")' not in footprint.read_text(encoding="utf-8")
    service.apply(change.id, confirmed=True, editor_closed=True)
    assert '(layer "F.CrtYd")' in footprint.read_text(encoding="utf-8")
    service.rollback(change.id, confirmed=True, editor_closed=True)
    assert '(layer "F.CrtYd")' not in footprint.read_text(encoding="utf-8")
