"""Run Copperbrain's offline, non-destructive reference demo."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from copperbrain.adapters.jlc_catalog import JsonCatalogAdapter
from copperbrain.adapters.schematic_api import SchematicApiAdapter
from copperbrain.models import ChangeOperation, RequirementSet
from copperbrain.services.bom import enrich_bom, estimate_bom_cost, generate_bom
from copperbrain.services.changes import ChangeService
from copperbrain.services.projects import ProjectService
from copperbrain.services.reference_design import five_volt_buck_operations
from copperbrain.services.sourcing import CatalogCache, SourcingService


def main() -> None:
    repository = Path(__file__).parents[1]
    fixtures = repository / "tests" / "fixtures"
    with tempfile.TemporaryDirectory(prefix="copperbrain-demo-") as directory:
        workspace = Path(directory)
        project = workspace / "project"
        shutil.copytree(fixtures / "kicad10_minimal", project)
        SchematicApiAdapter().apply(
            project / "demo.kicad_sch",
            (
                ChangeOperation(
                    kind="update_property",
                    target="J1",
                    parameters={"name": "InputVoltage", "value": "12V"},
                ),
            ),
        )
        projects = ProjectService()
        session = projects.open_project(project)
        summary = projects.summary(session.id)
        before_erc = projects.run_erc(session.id)
        sourcing = SourcingService(
            JsonCatalogAdapter(fixtures / "jlc_catalog.json"),
            CatalogCache(workspace / "catalog.sqlite"),
        )
        candidate = sourcing.search(
            "fixed 5V 3A buck regulator",
            RequirementSet(
                mechanical={"package": "TO-263-5"},
                sourcing={"prefer_basic": True, "min_stock": 100},
            ),
            quantity=100,
        )[0]
        changes = ChangeService(projects, workspace / "data")
        change = changes.prepare(session.id, five_volt_buck_operations(candidate))
        applied = changes.apply(change.id, confirmed=True, editor_closed=True)
        refreshed_projects = ProjectService()
        refreshed = refreshed_projects.open_project(project)
        erc = refreshed_projects.run_erc(refreshed.id)
        lines = enrich_bom(
            generate_bom(refreshed_projects.summary(refreshed.id)),
            {candidate.lcsc: candidate},
            (10, 100),
        )
        cost = estimate_bom_cost(lines, 100)
        changes.rollback(change.id, confirmed=True, editor_closed=True)
        print(
            json.dumps(
                {
                    "components": len(summary.components),
                    "selected": candidate.lcsc,
                    "change_status": applied.status,
                    "priced_component_cost_100": cost.component_cost,
                    "missing_price_lines": len(cost.missing_prices),
                    "erc_before": len(before_erc.violations),
                    "erc_violations": len(erc.violations),
                    "erc_details": [
                        {"severity": item.severity, "code": item.code, "message": item.message}
                        for item in erc.violations
                    ],
                    "rollback": "verified",
                },
                indent=2,
                default=str,
            )
        )


if __name__ == "__main__":
    main()
