from pathlib import Path
from types import SimpleNamespace

import pytest

from copperbrain import server
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    BomLine,
    ComponentCandidate,
    DrcReport,
    ManufacturingProfile,
    NetClassAssignment,
    NetClassRule,
    PcbRuleSet,
    PriceBreak,
    RequirementSet,
    ValidationReport,
)
from copperbrain.server import _resolve_asset, mcp


class Dump:
    def __init__(self, value: dict[str, object] | None = None) -> None:
        self.value = value or {"ok": True}

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return self.value


def part(lcsc: str = "C1") -> ComponentCandidate:
    return ComponentCandidate(
        lcsc=lcsc,
        mpn="M1",
        manufacturer="Acme",
        description="buck converter",
        package="SOT-23",
        stock=100,
        price_breaks=(PriceBreak(quantity=1, unit_price=0.1),),
    )


def test_server_exposes_complete_mvp_contract() -> None:
    names = set(mcp._tool_manager._tools)
    assert names == {
        "detect_kicad",
        "prepare_project_creation",
        "validate_project_creation",
        "apply_project_creation",
        "rollback_project_creation",
        "open_project",
        "get_project_summary",
        "analyze_schematic",
        "trace_net",
        "run_erc",
        "run_drc",
        "analyze_pcb_constraints",
        "propose_design_rules",
        "prepare_pcb_rule_change",
        "validate_pcb_rule_change",
        "apply_pcb_rule_change",
        "rollback_pcb_rule_change",
        "get_pcb_summary",
        "inspect_pcb_net",
        "get_footprint_placement",
        "analyze_placement",
        "propose_component_placement",
        "prepare_placement_change",
        "validate_placement_change",
        "apply_placement_change",
        "rollback_placement_change",
        "export_pcb_preview",
        "get_routing_backend_status",
        "analyze_unrouted_nets",
        "propose_pcb_routing",
        "prepare_routing_change",
        "validate_routing_change",
        "apply_routing_change",
        "rollback_routing_change",
        "restore_routing_snapshot",
        "get_routing_change_summary",
        "assess_pcb_readiness",
        "prepare_pcb_finalization",
        "validate_pcb_finalization",
        "apply_pcb_finalization",
        "get_pcb_finalization_report",
        "prepare_pcb_layout_change",
        "validate_pcb_layout_change",
        "apply_pcb_layout_change",
        "rollback_pcb_layout_change",
        "search_components",
        "get_component_details",
        "compare_components",
        "find_alternatives",
        "estimate_component_cost",
        "import_component_assets",
        "prepare_schematic_change",
        "validate_change",
        "apply_change",
        "rollback_change",
        "generate_bom",
        "estimate_bom_cost",
        "suggest_bom_substitutions",
    }


def test_resolve_asset_preserves_local_path_and_rejects_url_extension() -> None:
    assert _resolve_asset("relative/part.pdf", "datasheet") == Path("relative/part.pdf")
    with pytest.raises(CopperbrainError, match="unsupported extension"):
        _resolve_asset("https://lcsc.com/part.exe", "datasheet")


def test_project_transport_wrappers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "detect_kicad_service", lambda: Dump({"detected": True}))
    monkeypatch.setattr(server.projects, "open_project", lambda path: Dump({"path": str(path)}))
    monkeypatch.setattr(server.projects, "summary", lambda session: Dump({"session": session}))
    monkeypatch.setattr(server.projects, "analyze", lambda session: {"session": session})
    monkeypatch.setattr(server.projects, "trace_net", lambda session, net: Dump({"net": net}))
    monkeypatch.setattr(server.projects, "run_erc", lambda session: Dump({"erc": True}))
    monkeypatch.setattr(server.projects, "run_drc", lambda session: Dump({"drc": True}))
    assert server.detect_kicad()["detected"]
    assert server.open_project(str(tmp_path))["path"] == str(tmp_path)
    assert server.get_project_summary("s")["session"] == "s"
    assert server.analyze_schematic("s")["session"] == "s"
    assert server.trace_net("s", "VCC")["net"] == "VCC"
    assert server.run_erc("s")["erc"]
    assert server.run_drc("s")["drc"]


def test_pcb_rule_transport_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = PcbRuleSet(
        manufacturing=ManufacturingProfile(),
        classes=(
            NetClassRule(
                name="SIGNAL",
                clearance_mm=0.2,
                track_width_min_mm=0.2,
                track_width_preferred_mm=0.2,
                via_diameter_mm=0.6,
                via_drill_mm=0.3,
            ),
        ),
        assignments=(NetClassAssignment(net="/SIG", netclass="SIGNAL"),),
    )
    monkeypatch.setattr(server.pcb_rules, "analyze", lambda session: Dump({"analysis": True}))
    monkeypatch.setattr(server.pcb_rules, "propose", lambda *args: rules)
    monkeypatch.setattr(server.pcb_rules, "prepare", lambda *args: Dump())
    monkeypatch.setattr(
        server.pcb_rules,
        "validate",
        lambda change: (ValidationReport(valid=True), DrcReport(available=True)),
    )
    monkeypatch.setattr(server.pcb_rules, "apply", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.pcb_rules, "rollback", lambda *args, **kwargs: Dump())
    assert server.analyze_pcb_constraints("s")["analysis"]
    serialized = server.propose_design_rules("s", {}, [])
    assert serialized["classes"][0]["name"] == "SIGNAL"
    assert server.prepare_pcb_rule_change("s", rules.model_dump(mode="json"))["ok"]
    assert server.validate_pcb_rule_change("c")["validation"]["valid"]
    assert server.apply_pcb_rule_change("c", True, True)["ok"]
    assert server.rollback_pcb_rule_change("c", True, True)["ok"]


def test_pcb_design_transport_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.pcb_design, "summary", lambda session: Dump({"summary": True}))
    monkeypatch.setattr(server.pcb_design, "inspect_net", lambda session, net: Dump({"net": net}))
    monkeypatch.setattr(
        server.pcb_design,
        "footprint",
        lambda session, reference: Dump({"reference": reference}),
    )
    monkeypatch.setattr(
        server.pcb_design, "analyze_placement", lambda session: Dump({"score": 100})
    )
    monkeypatch.setattr(server.pcb_design, "propose", lambda *args: Dump())
    monkeypatch.setattr(server.pcb_design, "prepare", lambda *args: Dump())
    monkeypatch.setattr(
        server.pcb_design,
        "validate",
        lambda change: (ValidationReport(valid=True), DrcReport(available=True)),
    )
    monkeypatch.setattr(server.pcb_design, "apply", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.pcb_design, "rollback", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.pcb_design, "export_preview", lambda session: Path("preview.pdf"))
    assert server.get_pcb_summary("s")["summary"]
    assert server.inspect_pcb_net("s", "GND")["net"] == "GND"
    assert server.get_footprint_placement("s", "R1")["reference"] == "R1"
    assert server.analyze_placement("s")["score"] == 100
    request = {
        "references": ["R1"],
        "region": {"min_x_mm": 0, "min_y_mm": 0, "max_x_mm": 10, "max_y_mm": 10},
    }
    assert server.propose_component_placement("s", request)["ok"]
    operation = {"reference": "R1", "x_mm": 5, "y_mm": 5}
    assert server.prepare_placement_change("s", [operation])["ok"]
    assert server.validate_placement_change("c")["validation"]["valid"]
    assert server.apply_placement_change("c", True, True)["ok"]
    assert server.rollback_placement_change("c", True, True)["ok"]
    assert server.export_pcb_preview("s")["preview_pdf"] == "preview.pdf"


def test_pcb_layout_transport_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.pcb_layout, "prepare", lambda *args: Dump())
    monkeypatch.setattr(
        server.pcb_layout,
        "validate",
        lambda change: (
            ValidationReport(valid=True),
            Dump({"available": True}),
            DrcReport(available=True),
            Dump({"score": 100}),
        ),
    )
    monkeypatch.setattr(server.pcb_layout, "apply", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.pcb_layout, "rollback", lambda *args, **kwargs: Dump())
    plan = {
        "outline": {"width_mm": 20, "height_mm": 10},
        "placements": [{"reference": "R1", "x_mm": 85, "y_mm": 85}],
    }
    assert server.prepare_pcb_layout_change("s", plan)["ok"]
    assert server.validate_pcb_layout_change("c")["validation"]["valid"]
    assert server.apply_pcb_layout_change("c", True, True)["ok"]
    assert server.rollback_pcb_layout_change("c", True, True)["ok"]


def test_pcb_routing_transport_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.pcb_routing, "backend_status", lambda: Dump({"available": True}))
    monkeypatch.setattr(server.pcb_routing, "analyze", lambda *args: Dump({"complete": False}))
    monkeypatch.setattr(server.pcb_routing, "propose", lambda *args: Dump())
    monkeypatch.setattr(server.pcb_routing, "prepare", lambda *args: Dump())
    monkeypatch.setattr(
        server.pcb_routing,
        "validate",
        lambda change: (
            ValidationReport(valid=True),
            DrcReport(available=True),
            Dump({"complete": True}),
        ),
    )
    monkeypatch.setattr(server.pcb_routing, "apply", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.pcb_routing, "rollback", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.pcb_routing, "restore_snapshot", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.pcb_routing, "review", lambda *args: Dump())
    monkeypatch.setattr(server.pcb_finalization, "assess", lambda *args: Dump())
    monkeypatch.setattr(server.pcb_finalization, "prepare", lambda *args: Dump())
    monkeypatch.setattr(server.pcb_finalization, "validate", lambda *args: Dump())
    monkeypatch.setattr(server.pcb_finalization, "apply", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.pcb_finalization, "report", lambda *args: Dump())
    assert server.get_routing_backend_status()["available"]
    assert not server.analyze_unrouted_nets("s")["complete"]
    request = {"nets": ["GND"], "default_track_width_mm": 0.25}
    assert server.propose_pcb_routing("s", request)["ok"]
    plan = {
        "session_id": "s",
        "request": request,
        "segments": [
            {
                "net": "GND",
                "start_x_mm": 1,
                "start_y_mm": 1,
                "end_x_mm": 2,
                "end_y_mm": 1,
                "width_mm": 0.25,
            }
        ],
        "target_nets": ["GND"],
        "analysis_before": {
            "session_id": "s",
            "complete": False,
            "net_count": 1,
            "routed_net_count": 0,
            "unrouted_net_count": 1,
            "unrouted_connection_count": 1,
        },
        "predicted_complete": True,
    }
    assert server.prepare_routing_change("s", plan)["ok"]
    assert server.validate_routing_change("c")["validation"]["valid"]
    assert server.apply_routing_change("c", True, True)["ok"]
    assert server.rollback_routing_change("c", True, True)["ok"]
    assert server.restore_routing_snapshot("s", "a" * 32, True, True)["ok"]
    assert server.get_routing_change_summary("c")["ok"]
    assert server.assess_pcb_readiness("s")["ok"]
    assert server.prepare_pcb_finalization("s", request)["ok"]
    assert server.validate_pcb_finalization("c")["ok"]
    assert server.apply_pcb_finalization("c", True, True)["ok"]
    assert server.get_pcb_finalization_report("c")["ok"]


def test_sourcing_transport_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = part()
    monkeypatch.setattr(server.sourcing, "search", lambda *args, **kwargs: (candidate,))
    monkeypatch.setattr(server.sourcing, "details", lambda lcsc: candidate)
    monkeypatch.setattr(
        server.sourcing,
        "compare",
        lambda *args, **kwargs: ({"lcsc": "C1", "score": 1},),
    )
    monkeypatch.setattr(server.sourcing, "alternatives", lambda *args, **kwargs: (part("C2"),))
    serialized = candidate.model_dump(mode="json")
    assert server.search_components("buck", {}, 10)[0]["lcsc"] == "C1"
    assert server.get_component_details("C1")["mpn"] == "M1"
    assert server.compare_components([serialized], {}, 10)[0]["score"] == 1
    assert server.find_alternatives("C1", {}, 10)[0]["lcsc"] == "C2"
    assert server.estimate_component_cost("C1", 10)["component_cost"] == 1


def test_asset_and_change_transport_wrappers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(server.projects, "get_session", lambda session_id: session)
    monkeypatch.setattr(
        server.assets, "import_bundle", lambda root, bundle: Dump({"lcsc": bundle.lcsc})
    )
    assert (
        server.import_component_assets("s", "C1", "CB", "a.kicad_sym", "a.kicad_mod")["lcsc"]
        == "C1"
    )
    monkeypatch.setattr(server.changes, "prepare", lambda session_id, operations: Dump())
    monkeypatch.setattr(server.changes, "validate", lambda change_id: Dump())
    monkeypatch.setattr(server.changes, "apply", lambda *args, **kwargs: Dump())
    monkeypatch.setattr(server.changes, "rollback", lambda *args, **kwargs: Dump())
    operation = {
        "kind": "label",
        "target": "VCC",
        "parameters": {"text": "VCC", "x": 1, "y": 2},
    }
    assert server.prepare_schematic_change("s", [operation])["ok"]
    assert server.validate_change("c")["ok"]
    assert server.apply_change("c", True, True)["ok"]
    assert server.rollback_change("c", True, True)["ok"]


def test_bom_transport_wrappers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    line = BomLine(
        references=("R1",),
        quantity_per_board=1,
        value="10k",
        lcsc="C1",
        unit_prices={10: 0.1},
        stock=100,
    )
    monkeypatch.setattr(server, "_bom_with_catalog", lambda session, quantities: (line,))
    monkeypatch.setattr(
        server.projects,
        "get_session",
        lambda session: type("Session", (), {"root": tmp_path})(),
    )
    monkeypatch.setattr(
        server,
        "export_bom",
        lambda lines, destination, output_format: destination,
    )
    result = server.generate_bom("s", "json", "bom.json")
    assert result["exported"] == [str(tmp_path / "copperbrain-output" / "bom" / "bom.json")]
    costs = server.estimate_bom_cost("s", [10, 10])
    assert costs[0]["component_cost"] == 1

    monkeypatch.setattr(server.projects, "summary", lambda session: Dump())
    monkeypatch.setattr(server, "generate_bom_lines", lambda summary: (line,))
    monkeypatch.setattr(server.sourcing, "alternatives", lambda *args, **kwargs: (part("C2"),))
    substitutions = server.suggest_bom_substitutions("s", RequirementSet().model_dump(), 10)
    assert substitutions[0]["alternatives"][0]["lcsc"] == "C2"
