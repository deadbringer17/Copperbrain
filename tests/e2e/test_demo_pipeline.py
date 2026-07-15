import shutil
from pathlib import Path

import pytest

from copperbrain.adapters.jlc_catalog import JsonCatalogAdapter
from copperbrain.adapters.kicad_detection import detect_kicad
from copperbrain.adapters.schematic_api import SchematicApiAdapter
from copperbrain.models import ChangeOperation, RequirementSet
from copperbrain.services.bom import enrich_bom, estimate_bom_cost, generate_bom
from copperbrain.services.changes import ChangeService
from copperbrain.services.projects import ProjectService
from copperbrain.services.reference_design import five_volt_buck_operations
from copperbrain.services.sourcing import CatalogCache, SourcingService

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_offline_demo_analysis_sourcing_apply_bom_rollback(tmp_path: Path) -> None:
    if detect_kicad().selected_cli is None:
        pytest.skip("KiCad CLI is not installed")
    root = tmp_path / "demo"
    shutil.copytree(FIXTURES / "kicad10_minimal", root)
    SchematicApiAdapter().apply(
        root / "demo.kicad_sch",
        (
            ChangeOperation(
                kind="update_property",
                target="J1",
                parameters={"name": "InputVoltage", "value": "12V"},
            ),
        ),
    )
    projects = ProjectService()
    session = projects.open_project(root)
    assert projects.summary(session.id).components[0].reference == "J1"
    before_erc = projects.run_erc(session.id)

    sourcing = SourcingService(
        JsonCatalogAdapter(FIXTURES / "jlc_catalog.json"),
        CatalogCache(tmp_path / "catalog.sqlite"),
    )
    requirements = RequirementSet(
        mechanical={"package": "TO-263-5"},
        sourcing={"prefer_basic": True, "min_stock": 100},
    )
    candidate = sourcing.search("fixed 5V 3A buck regulator", requirements, quantity=100)[0]
    assert candidate.lcsc == "C10002"

    changes = ChangeService(projects, tmp_path / "data")
    change = changes.prepare(session.id, five_volt_buck_operations(candidate))
    assert change.validation_report.erc is not None
    assert not any(
        violation.severity == "error" for violation in change.validation_report.erc.violations
    )
    changes.apply(change.id, confirmed=True, editor_closed=True)

    refreshed_projects = ProjectService()
    refreshed = refreshed_projects.open_project(root)
    refreshed_summary = refreshed_projects.summary(refreshed.id)
    post_erc = refreshed_projects.run_erc(refreshed.id)
    assert post_erc.available
    assert sum(item.severity == "error" for item in post_erc.violations) <= sum(
        item.severity == "error" for item in before_erc.violations
    )
    assert {"JIN", "U1", "D1", "L1", "C1", "C2", "J2"}.issubset(
        {component.reference for component in refreshed_summary.components}
    )
    lines = generate_bom(refreshed_summary)
    enriched = enrich_bom(lines, {candidate.lcsc: candidate}, (10, 100))
    estimate = estimate_bom_cost(enriched, 100)
    assert estimate.component_cost > 0
    assert candidate.lcsc not in estimate.missing_prices

    changes.rollback(change.id, confirmed=True, editor_closed=True)
