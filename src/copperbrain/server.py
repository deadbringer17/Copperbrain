"""Thin FastMCP transport layer."""

from __future__ import annotations

import hashlib
import urllib.parse
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import TypeAdapter

from copperbrain.adapters.downloads import DownloadAdapter
from copperbrain.adapters.freerouting import FreeRoutingAdapter
from copperbrain.adapters.jlc_catalog import configured_catalog
from copperbrain.adapters.kicad_detection import detect_kicad as detect_kicad_service
from copperbrain.config import Settings
from copperbrain.errors import CopperbrainError
from copperbrain.models import (
    BomLine,
    ChangeOperation,
    ComponentAssetBundle,
    ComponentCandidate,
    ErrorCode,
    ManufacturingProfile,
    NetRuleRequirement,
    PcbLayoutPlan,
    PcbRuleSet,
    PlacementOperation,
    PlacementRequest,
    RequirementSet,
    RoutingPlan,
    RoutingRequest,
)
from copperbrain.services.assets import AssetService
from copperbrain.services.bom import enrich_bom, export_bom
from copperbrain.services.bom import estimate_bom_cost as estimate_bom
from copperbrain.services.bom import generate_bom as generate_bom_lines
from copperbrain.services.changes import ChangeService
from copperbrain.services.outputs import output_path
from copperbrain.services.pcb_design import PcbDesignService
from copperbrain.services.pcb_finalization import PcbFinalizationService
from copperbrain.services.pcb_layout import PcbLayoutService
from copperbrain.services.pcb_routing import PcbRoutingService
from copperbrain.services.pcb_rules import PcbRuleService
from copperbrain.services.projects import ProjectService
from copperbrain.services.sourcing import (
    CatalogCache,
    SourcingService,
)
from copperbrain.services.sourcing import (
    estimate_component_cost as estimate_one_component,
)

mcp = FastMCP("Copperbrain")
settings = Settings.from_environment()
projects = ProjectService()
assets = AssetService()
sourcing = SourcingService(
    configured_catalog(), CatalogCache(settings.cache_dir / "catalog.sqlite")
)
changes = ChangeService(projects, settings.data_dir)
pcb_rules = PcbRuleService(projects, settings.data_dir)
pcb_design = PcbDesignService(projects, settings.data_dir)
pcb_layout = PcbLayoutService(projects, settings.data_dir)
pcb_routing = PcbRoutingService(
    projects,
    settings.data_dir,
    routing_backend=FreeRoutingAdapter.discover(
        settings.data_dir,
        explicit_jar=settings.freerouting_jar,
        explicit_java=settings.freerouting_java,
        timeout_seconds=settings.freerouting_timeout_seconds,
        stall_seconds=settings.freerouting_stall_seconds,
        normalization_limit=settings.freerouting_normalization_limit,
    ),
)
pcb_finalization = PcbFinalizationService(projects, pcb_design, pcb_routing)
downloads = DownloadAdapter(
    settings.allowed_download_hosts,
    timeout=settings.connect_timeout_seconds + settings.read_timeout_seconds,
    max_bytes=settings.max_download_bytes,
)

_ASSET_DOWNLOADS = {
    "symbol": ({".kicad_sym"}, ("application/octet-stream", "text/plain")),
    "footprint": ({".kicad_mod"}, ("application/octet-stream", "text/plain")),
    "model_3d": ({".step", ".stp", ".wrl"}, ("application/octet-stream", "model/step")),
    "datasheet": ({".pdf"}, ("application/pdf",)),
}


def _resolve_asset(value: str, kind: str) -> Path:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return Path(value)
    extensions, content_types = _ASSET_DOWNLOADS[kind]
    suffix = Path(urllib.parse.unquote(parsed.path)).suffix.casefold()
    if suffix not in extensions:
        raise CopperbrainError(ErrorCode.INVALID_INPUT, "Asset URL has an unsupported extension")
    identifier = hashlib.sha256(value.encode()).hexdigest()
    destination = settings.cache_dir / "downloads" / f"{identifier}{suffix}"
    if destination.is_file():
        return destination
    return downloads.download(value, destination, allowed_content_types=content_types)


@mcp.tool()
def detect_kicad() -> dict[str, object]:
    """Detect KiCad versions, CLI paths, user data directories, and JLC plugins."""
    return detect_kicad_service().model_dump(mode="json")


@mcp.tool()
def open_project(path: str) -> dict[str, object]:
    """Open a KiCad project read-only and capture hashes for safe later changes."""
    return projects.open_project(Path(path)).model_dump(mode="json")


@mcp.tool()
def get_project_summary(session_id: str) -> dict[str, object]:
    """Return sheets, components, electrical nets, and power symbols."""
    return projects.summary(session_id).model_dump(mode="json")


@mcp.tool()
def analyze_schematic(session_id: str) -> dict[str, object]:
    """Return deterministic, evidence-backed schematic observations."""
    return projects.analyze(session_id)


@mcp.tool()
def trace_net(session_id: str, net_name: str) -> dict[str, object]:
    """Trace every pin KiCad assigns to an exact net name."""
    return projects.trace_net(session_id, net_name).model_dump(mode="json")


@mcp.tool()
def run_erc(session_id: str) -> dict[str, object]:
    """Run KiCad ERC and normalize violations without touching project files."""
    return projects.run_erc(session_id).model_dump(mode="json")


@mcp.tool()
def run_drc(session_id: str) -> dict[str, object]:
    """Run KiCad PCB DRC read-only and return normalized violations."""
    return projects.run_drc(session_id).model_dump(mode="json")


@mcp.tool()
def analyze_pcb_constraints(session_id: str) -> dict[str, object]:
    """Inspect existing netclasses and classify nets using deterministic evidence."""
    return pcb_rules.analyze(session_id).model_dump(mode="json")


@mcp.tool()
def propose_design_rules(
    session_id: str,
    manufacturing_profile: dict[str, object],
    net_requirements: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Propose typed, footprint-aware netclass and local fanout rules."""
    profile = ManufacturingProfile.model_validate(manufacturing_profile)
    requirements = tuple(
        TypeAdapter(list[NetRuleRequirement]).validate_python(net_requirements or [])
    )
    return pcb_rules.propose(session_id, profile, requirements).model_dump(mode="json")


@mcp.tool()
def prepare_pcb_rule_change(
    session_id: str,
    rule_set: dict[str, object],
) -> dict[str, object]:
    """Render typed PCB rules in a private workspace, preview them, and run DRC."""
    normalized = PcbRuleSet.model_validate(rule_set)
    return pcb_rules.prepare(session_id, normalized).model_dump(mode="json")


@mcp.tool()
def validate_pcb_rule_change(change_set_id: str) -> dict[str, object]:
    """Revalidate generated project/rule files and rerun temporary PCB DRC."""
    validation, drc = pcb_rules.validate(change_set_id)
    return {
        "validation": validation.model_dump(mode="json"),
        "drc": drc.model_dump(mode="json"),
    }


@mcp.tool()
def apply_pcb_rule_change(
    change_set_id: str,
    confirmed: bool,
    editor_closed: bool,
) -> dict[str, object]:
    """Apply a validated PCB rule set after explicit confirmation and stale checks."""
    return pcb_rules.apply(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def rollback_pcb_rule_change(
    change_set_id: str,
    confirmed: bool,
    editor_closed: bool,
) -> dict[str, object]:
    """Restore the project and custom-rule snapshot after explicit confirmation."""
    return pcb_rules.rollback(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def get_pcb_summary(session_id: str) -> dict[str, object]:
    """Inspect board outline, footprints, nets, tracks, vias, zones, and IPC availability."""
    return pcb_design.summary(session_id).model_dump(mode="json")


@mcp.tool()
def inspect_pcb_net(session_id: str, net_name: str) -> dict[str, object]:
    """Return PCB pads, references, routed length, vias, and layers for one exact net."""
    return pcb_design.inspect_net(session_id, net_name).model_dump(mode="json")


@mcp.tool()
def get_footprint_placement(session_id: str, reference: str) -> dict[str, object]:
    """Return the exact position, rotation, layer, lock state, and bounds of a footprint."""
    return pcb_design.footprint(session_id, reference).model_dump(mode="json")


@mcp.tool()
def analyze_placement(session_id: str) -> dict[str, object]:
    """Score placement and report deterministic overlap and Edge.Cuts violations."""
    return pcb_design.analyze_placement(session_id).model_dump(mode="json")


@mcp.tool()
def propose_component_placement(session_id: str, request: dict[str, object]) -> dict[str, object]:
    """Propose deterministic collision-free placement operations inside typed bounds."""
    normalized = PlacementRequest.model_validate(request)
    return pcb_design.propose(session_id, normalized).model_dump(mode="json")


@mcp.tool()
def prepare_placement_change(
    session_id: str, operations: list[dict[str, object]]
) -> dict[str, object]:
    """Apply typed placement operations to a private copy, export PDF, and run DRC."""
    normalized = tuple(TypeAdapter(list[PlacementOperation]).validate_python(operations))
    return pcb_design.prepare(session_id, normalized).model_dump(mode="json")


@mcp.tool()
def validate_placement_change(change_set_id: str) -> dict[str, object]:
    """Reparse and rerun DRC against a prepared placement workspace."""
    validation, drc = pcb_design.validate(change_set_id)
    return {
        "validation": validation.model_dump(mode="json"),
        "drc": drc.model_dump(mode="json"),
    }


@mcp.tool()
def apply_placement_change(
    change_set_id: str, confirmed: bool, editor_closed: bool
) -> dict[str, object]:
    """Atomically apply a validated placement after confirmation and stale checks."""
    return pcb_design.apply(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def rollback_placement_change(
    change_set_id: str, confirmed: bool, editor_closed: bool
) -> dict[str, object]:
    """Restore the byte-exact PCB snapshot after explicit confirmation."""
    return pcb_design.rollback(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def export_pcb_preview(session_id: str) -> dict[str, object]:
    """Export a read-only PCB PDF below copperbrain-output/previews/."""
    return {"preview_pdf": str(pcb_design.export_preview(session_id))}


@mcp.tool()
def get_routing_backend_status() -> dict[str, object]:
    """Report local Java, FreeRouting JAR, and KiCad Python bridge availability."""
    return pcb_routing.backend_status().model_dump(mode="json")


@mcp.tool()
def analyze_unrouted_nets(session_id: str, net_names: list[str] | None = None) -> dict[str, object]:
    """Find electrically disconnected pad groups from typed PCB geometry."""
    return pcb_routing.analyze(session_id, tuple(net_names or [])).model_dump(mode="json")


@mcp.tool()
def propose_pcb_routing(session_id: str, request: dict[str, object]) -> dict[str, object]:
    """Propose deterministic, netclass-aware track and via operations for open connections."""
    normalized = RoutingRequest.model_validate(request)
    return pcb_routing.propose(session_id, normalized).model_dump(mode="json")


@mcp.tool()
def prepare_routing_change(session_id: str, plan: dict[str, object]) -> dict[str, object]:
    """Route a private PCB copy, publish a preview, and run comparative DRC."""
    normalized = RoutingPlan.model_validate(plan)
    return pcb_routing.prepare(session_id, normalized).model_dump(mode="json")


@mcp.tool()
def validate_routing_change(change_set_id: str) -> dict[str, object]:
    """Reparse connectivity and rerun DRC against a prepared routing workspace."""
    validation, drc, analysis = pcb_routing.validate(change_set_id)
    return {
        "validation": validation.model_dump(mode="json"),
        "drc": drc.model_dump(mode="json"),
        "routing_analysis": analysis.model_dump(mode="json"),
    }


@mcp.tool()
def apply_routing_change(
    change_set_id: str, confirmed: bool, editor_closed: bool
) -> dict[str, object]:
    """Atomically apply validated tracks and vias after explicit confirmation."""
    return pcb_routing.apply(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def rollback_routing_change(
    change_set_id: str, confirmed: bool, editor_closed: bool
) -> dict[str, object]:
    """Restore the byte-exact PCB snapshot after explicit confirmation."""
    return pcb_routing.rollback(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def restore_routing_snapshot(
    session_id: str,
    snapshot_id: str,
    confirmed: bool,
    editor_closed: bool,
) -> dict[str, object]:
    """Restore a private PCB routing snapshot after explicit confirmation."""
    return pcb_routing.restore_snapshot(
        session_id,
        snapshot_id,
        confirmed=confirmed,
        editor_closed=editor_closed,
    ).model_dump(mode="json")


@mcp.tool()
def get_routing_change_summary(change_set_id: str) -> dict[str, object]:
    """Resume a routing change if needed and return compact review evidence."""
    return pcb_routing.review(change_set_id).model_dump(mode="json")


@mcp.tool()
def assess_pcb_readiness(session_id: str) -> dict[str, object]:
    """Separate electrical validation from unassessed production engineering gates."""
    return pcb_finalization.assess(session_id).model_dump(mode="json")


@mcp.tool()
def prepare_pcb_finalization(
    session_id: str, routing_request: dict[str, object]
) -> dict[str, object]:
    """Prepare and persist a routed PCB preview with a compact readiness report."""
    request = RoutingRequest.model_validate(routing_request)
    return pcb_finalization.prepare(session_id, request).model_dump(mode="json")


@mcp.tool()
def validate_pcb_finalization(change_set_id: str) -> dict[str, object]:
    """Revalidate a persisted PCB finalization change after any MCP restart."""
    return pcb_finalization.validate(change_set_id).model_dump(mode="json")


@mcp.tool()
def apply_pcb_finalization(
    change_set_id: str,
    confirmed: bool,
    editor_closed: bool,
) -> dict[str, object]:
    """Apply final routing after explicit confirmation, then return readiness evidence."""
    return pcb_finalization.apply(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def get_pcb_finalization_report(change_set_id: str) -> dict[str, object]:
    """Return the current compact report for a persisted finalization change."""
    return pcb_finalization.report(change_set_id).model_dump(mode="json")


@mcp.tool()
def prepare_pcb_layout_change(session_id: str, plan: dict[str, object]) -> dict[str, object]:
    """Headlessly build a typed, unrouted PCB preview from the project schematic."""
    normalized = PcbLayoutPlan.model_validate(plan)
    return pcb_layout.prepare(session_id, normalized).model_dump(mode="json")


@mcp.tool()
def validate_pcb_layout_change(change_set_id: str) -> dict[str, object]:
    """Revalidate schematic, PCB, ERC, DRC, and placement in a prepared workspace."""
    validation, erc, drc, analysis = pcb_layout.validate(change_set_id)
    return {
        "validation": validation.model_dump(mode="json"),
        "erc": erc.model_dump(mode="json"),
        "drc": drc.model_dump(mode="json"),
        "placement_analysis": analysis.model_dump(mode="json"),
    }


@mcp.tool()
def apply_pcb_layout_change(
    change_set_id: str, confirmed: bool, editor_closed: bool
) -> dict[str, object]:
    """Atomically apply a validated PCB layout after explicit confirmation."""
    return pcb_layout.apply(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def rollback_pcb_layout_change(
    change_set_id: str, confirmed: bool, editor_closed: bool
) -> dict[str, object]:
    """Restore schematic and PCB snapshots for an applied layout change."""
    return pcb_layout.rollback(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def search_components(
    query: str,
    requirements: dict[str, object],
    quantity: int,
    limit: int = 5,
    refresh: bool = False,
) -> list[dict[str, object]]:
    """Search, hard-filter, and deterministically rank at most five JLC candidates."""
    normalized = RequirementSet.model_validate(requirements)
    return [
        item.model_dump(mode="json")
        for item in sourcing.search(
            query, normalized, quantity=quantity, limit=limit, refresh=refresh
        )
    ]


@mcp.tool()
def get_component_details(lcsc: str) -> dict[str, object]:
    """Return normalized catalog details with source and retrieval timestamp."""
    return sourcing.details(lcsc).model_dump(mode="json")


@mcp.tool()
def compare_components(
    candidates: list[dict[str, object]],
    requirements: dict[str, object],
    quantity: int,
) -> list[dict[str, object]]:
    """Compare up to five candidates using the deterministic scoring matrix."""
    normalized_candidates = tuple(TypeAdapter(list[ComponentCandidate]).validate_python(candidates))
    normalized_requirements = RequirementSet.model_validate(requirements)
    return list(sourcing.compare(normalized_candidates, normalized_requirements, quantity=quantity))


@mcp.tool()
def find_alternatives(
    lcsc: str,
    requirements: dict[str, object],
    quantity: int,
) -> list[dict[str, object]]:
    """Find ranked alternatives and explicitly exclude the source component."""
    normalized = RequirementSet.model_validate(requirements)
    return [
        item.model_dump(mode="json")
        for item in sourcing.alternatives(lcsc, normalized, quantity=quantity)
    ]


@mcp.tool()
def estimate_component_cost(lcsc: str, quantity: int) -> dict[str, object]:
    """Estimate component-only cost at one requested quantity."""
    return estimate_one_component(sourcing.details(lcsc), quantity)


@mcp.tool()
def import_component_assets(
    session_id: str,
    lcsc: str,
    nickname: str,
    symbol: str,
    footprint: str,
    model_3d: str | None = None,
    datasheet: str | None = None,
) -> dict[str, object]:
    """Import a resolved local asset bundle atomically and idempotently."""
    session = projects.get_session(session_id)
    bundle = ComponentAssetBundle(
        lcsc=lcsc,
        nickname=nickname,
        symbol=_resolve_asset(symbol, "symbol"),
        footprint=_resolve_asset(footprint, "footprint"),
        model_3d=_resolve_asset(model_3d, "model_3d") if model_3d else None,
        datasheet=_resolve_asset(datasheet, "datasheet") if datasheet else None,
    )
    return assets.import_bundle(session.root, bundle).model_dump(mode="json")


@mcp.tool()
def prepare_schematic_change(
    session_id: str,
    operations: list[dict[str, object]],
) -> dict[str, object]:
    """Prepare semantic operations in a temporary workspace and return preview/risks."""
    normalized = tuple(TypeAdapter(list[ChangeOperation]).validate_python(operations))
    return changes.prepare(session_id, normalized).model_dump(mode="json")


@mcp.tool()
def validate_change(change_set_id: str) -> dict[str, object]:
    """Revalidate a prepared change in its temporary workspace."""
    return changes.validate(change_set_id).model_dump(mode="json")


@mcp.tool()
def apply_change(
    change_set_id: str,
    confirmed: bool,
    editor_closed: bool,
) -> dict[str, object]:
    """Apply only a validated change after explicit confirmation and editor attestation."""
    return changes.apply(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


@mcp.tool()
def rollback_change(
    change_set_id: str,
    confirmed: bool,
    editor_closed: bool,
) -> dict[str, object]:
    """Restore the byte-exact snapshot after explicit rollback confirmation."""
    return changes.rollback(
        change_set_id, confirmed=confirmed, editor_closed=editor_closed
    ).model_dump(mode="json")


def _bom_with_catalog(session_id: str, quantities: tuple[int, ...]) -> tuple[BomLine, ...]:
    lines = generate_bom_lines(projects.summary(session_id))
    candidates: dict[str, ComponentCandidate] = {}
    for line in lines:
        if line.lcsc:
            try:
                candidates[line.lcsc] = sourcing.details(line.lcsc)
            except CopperbrainError:
                continue
    return enrich_bom(lines, candidates, quantities)


@mcp.tool()
def generate_bom(
    session_id: str,
    output_format: str | None = None,
    destination: str | None = None,
) -> dict[str, object]:
    """Generate a BOM and always export it below the opened project's output folder."""
    lines = _bom_with_catalog(session_id, (1, 10, 100))
    if destination and not output_format:
        raise CopperbrainError(
            ErrorCode.INVALID_INPUT,
            "A destination filename requires an output format",
        )
    formats = (output_format,) if output_format else ("json", "csv", "markdown")
    suffixes = {"json": ".json", "csv": ".csv", "markdown": ".md"}
    session = projects.get_session(session_id)
    exported: list[Path] = []
    for item_format in formats:
        suffix = suffixes.get(item_format)
        if suffix is None:
            raise CopperbrainError(ErrorCode.INVALID_INPUT, "Unsupported BOM output format")
        filename = destination or f"Copperbrain-BOM{suffix}"
        if Path(filename).suffix.casefold() != suffix:
            raise CopperbrainError(
                ErrorCode.INVALID_INPUT,
                "BOM filename suffix does not match output format",
            )
        exported.append(export_bom(lines, output_path(session.root, "bom", filename), item_format))
    return {
        "lines": [line.model_dump(mode="json") for line in lines],
        "exported": [str(item) for item in exported],
    }


@mcp.tool()
def estimate_bom_cost(session_id: str, quantities: list[int]) -> list[dict[str, object]]:
    """Estimate component-only BOM totals for requested board quantities."""
    normalized = tuple(sorted(set(quantities)))
    lines = _bom_with_catalog(session_id, normalized)
    return [estimate_bom(lines, quantity).model_dump(mode="json") for quantity in normalized]


@mcp.tool()
def suggest_bom_substitutions(
    session_id: str,
    requirements: dict[str, object],
    quantity: int,
) -> list[dict[str, object]]:
    """Return catalog alternatives for each BOM line that has an LCSC identifier."""
    normalized = RequirementSet.model_validate(requirements)
    suggestions: list[dict[str, object]] = []
    for line in generate_bom_lines(projects.summary(session_id)):
        if not line.lcsc:
            continue
        alternatives = sourcing.alternatives(line.lcsc, normalized, quantity=quantity)
        suggestions.append(
            {
                "references": line.references,
                "source_lcsc": line.lcsc,
                "alternatives": [item.model_dump(mode="json") for item in alternatives],
            }
        )
    return suggestions


def main() -> None:
    """Start Copperbrain using local stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
