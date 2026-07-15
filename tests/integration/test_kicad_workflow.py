import shutil
from pathlib import Path

import pytest

from copperbrain.adapters.jlc_catalog import (
    JlcpcbToolsDatabaseAdapter,
    discover_jlcpcb_tools_database,
)
from copperbrain.adapters.kicad_cli import export_pcb_pdf, run_drc
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.pcb_design import PcbFileAdapter
from copperbrain.adapters.pcb_rules import PcbRuleAdapter
from copperbrain.adapters.schematic_api import SchematicApiAdapter
from copperbrain.models import (
    ChangeOperation,
    ChangeStatus,
    ManufacturingProfile,
    NetClassAssignment,
    NetClassRule,
    PcbRuleSet,
    PlacementOperation,
    RouteSegment,
)
from copperbrain.services.changes import ChangeService
from copperbrain.services.projects import ProjectService

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kicad10_minimal"


def test_installed_jlcpcb_tools_database_is_searchable() -> None:
    database = discover_jlcpcb_tools_database()
    if database is None:
        pytest.skip("JLCPCB Tools database is not installed")
    results = JlcpcbToolsDatabaseAdapter(database).search("buck")
    assert results
    assert results[0].source.startswith("JLCPCB Tools:")
    assert results[0].retrieved_at.tzinfo is not None


def test_real_kicad_summary_erc_change_and_rollback(tmp_path: Path) -> None:
    detection = detect_kicad()
    if detection.selected_cli is None:
        pytest.skip("KiCad CLI is not installed")
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    projects = ProjectService()
    session = projects.open_project(project)
    summary = projects.summary(session.id)
    assert summary.components[0].reference == "J1"
    erc = projects.run_erc(session.id)
    assert erc.available

    schematic = project / "demo.kicad_sch"
    original = schematic.read_bytes()
    changes = ChangeService(projects, tmp_path / "data", SchematicApiAdapter())
    change = changes.prepare(
        session.id,
        (
            ChangeOperation(
                kind="update_property",
                target="J1",
                parameters={"name": "LCSC", "value": "C25804"},
            ),
        ),
    )
    assert change.status is ChangeStatus.VALIDATED
    changes.apply(change.id, confirmed=True, editor_closed=True)
    assert schematic.read_bytes() != original
    changes.rollback(change.id, confirmed=True, editor_closed=True)
    assert schematic.read_bytes() == original


def test_real_adapter_supports_every_allowlisted_operation(tmp_path: Path) -> None:
    schematic = tmp_path / "demo.kicad_sch"
    shutil.copy2(FIXTURE / "demo.kicad_sch", schematic)
    adapter = SchematicApiAdapter()
    adapter.apply(
        schematic,
        (
            ChangeOperation(
                kind="add_component",
                target="R1",
                parameters={
                    "lib_id": "Device:R",
                    "value": "10k",
                    "x": 50,
                    "y": 50,
                    "footprint": "Resistor_SMD:R_0603_1608Metric",
                },
            ),
            ChangeOperation(
                kind="replace_component",
                target="J1",
                parameters={"lib_id": "Connector_Generic:Conn_01x02", "value": "Power"},
            ),
            ChangeOperation(
                kind="update_property",
                target="J1",
                parameters={"name": "LCSC", "value": "C25804"},
            ),
            ChangeOperation(
                kind="connect",
                target="wire-1",
                parameters={"x1": 50, "y1": 50, "x2": 60, "y2": 50},
            ),
            ChangeOperation(
                kind="label",
                target="VCC",
                parameters={"text": "VCC", "x": 60, "y": 50},
            ),
            ChangeOperation(
                kind="no_connect",
                target="R1.1",
                parameters={"reference": "R1", "pin": "1"},
            ),
        ),
    )
    assert adapter.validate(schematic).valid


def test_generated_pcb_rules_are_accepted_by_real_kicad_drc(tmp_path: Path) -> None:
    detection = detect_kicad()
    cli = detection.selected_cli
    if cli is None:
        pytest.skip("KiCad CLI is not installed")
    demo = cli.parent.parent / "share" / "kicad" / "demos" / "ecc83"
    board_source = demo / "ecc83-pp.kicad_pcb"
    project_source = demo / "ecc83-pp.kicad_pro"
    if not board_source.is_file() or not project_source.is_file():
        pytest.skip("Installed KiCad distribution does not include the ecc83 demo")
    project = tmp_path / "ecc83-pp.kicad_pro"
    board = tmp_path / "ecc83-pp.kicad_pcb"
    rules = tmp_path / "ecc83-pp.kicad_dru"
    shutil.copy2(project_source, project)
    shutil.copy2(board_source, board)
    rule_set = PcbRuleSet(
        manufacturing=ManufacturingProfile(),
        classes=(
            NetClassRule(
                name="CB_GND",
                clearance_mm=0.25,
                track_width_min_mm=0.3,
                track_width_preferred_mm=0.5,
                via_diameter_mm=0.6,
                via_drill_mm=0.3,
                creepage_mm=0.3,
                max_length_mm=500,
                diff_pair_width_mm=0.25,
                diff_pair_gap_mm=0.25,
                diff_pair_max_uncoupled_mm=10,
            ),
        ),
        assignments=(NetClassAssignment(net="GND", netclass="CB_GND"),),
    )
    adapter = PcbRuleAdapter()
    adapter.apply(project, rules, rule_set)
    assert adapter.validate(project, rules).valid
    report = run_drc(cli, board)
    assert report.available
    assert report.error is None


def test_real_kicad_accepts_typed_placement_and_exports_preview(tmp_path: Path) -> None:
    detection = detect_kicad()
    cli = detection.selected_cli
    if cli is None:
        pytest.skip("KiCad CLI is not installed")
    source = cli.parent.parent / "share" / "kicad" / "demos" / "ecc83" / "ecc83-pp.kicad_pcb"
    if not source.is_file():
        pytest.skip("Installed KiCad distribution does not include the ecc83 demo")
    board = tmp_path / "ecc83-pp.kicad_pcb"
    shutil.copy2(source, board)
    adapter = PcbFileAdapter()
    summary = adapter.summary(board, "integration")
    reference_counts = {
        item.reference: sum(other.reference == item.reference for other in summary.footprints)
        for item in summary.footprints
    }
    movable = next(
        item
        for item in summary.footprints
        if not item.locked and reference_counts[item.reference] == 1
    )
    adapter.apply_placement(
        board,
        (
            PlacementOperation(
                reference=movable.reference,
                x_mm=movable.x_mm + 0.5,
                y_mm=movable.y_mm,
                rotation_deg=movable.rotation_deg,
            ),
        ),
    )
    assert adapter.validate(board).valid
    report = run_drc(cli, board)
    assert report.available
    assert report.error is None
    preview = export_pcb_pdf(cli, board, tmp_path / "preview.pdf")
    assert preview.is_file() and preview.stat().st_size > 0


def test_real_kicad_accepts_typed_routing_and_reports_connected_net(tmp_path: Path) -> None:
    detection = detect_kicad()
    cli = detection.selected_cli
    if cli is None:
        pytest.skip("KiCad CLI is not installed")
    source = Path("tests/fixtures/kicad10_placement/placement.kicad_pcb")
    board = tmp_path / "routing.kicad_pcb"
    shutil.copy2(source, board)
    text = board.read_text(encoding="utf-8")
    text = text.replace(
        '  (segment (start 9.2 10) (end 19.2 10) (width 0.25) (layer "F.Cu") (net 1))\n',
        "",
    )
    text = text.replace(
        '  (via (at 14 10) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n',
        "",
    )
    board.write_text(text, encoding="utf-8")
    adapter = PcbFileAdapter()
    assert not adapter.analyze_routing(board, "integration").complete
    adapter.apply_routing(
        board,
        (
            RouteSegment(
                net="GND",
                start_x_mm=9.2,
                start_y_mm=10,
                end_x_mm=19.2,
                end_y_mm=10,
                width_mm=0.25,
            ),
        ),
        (),
    )
    assert adapter.analyze_routing(board, "integration").complete
    report = run_drc(cli, board)
    assert report.available
    assert report.error is None
