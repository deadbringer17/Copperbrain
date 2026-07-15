# Copperbrain

Copperbrain is a local MCP server for safe KiCad 10 schematic/PCB analysis, deterministic
JLCPCB/LCSC sourcing, controlled changes, ERC/DRC validation, typed PCB design rules, and
component-only BOM estimates.

See `DEVELOPMENT_PLAN.md` for scope and contracts, `docs/INSTALLATION.md` for setup, and
`docs/DEMO.md` for the reproducible reference flow.

## Development

```powershell
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Run the stdio MCP server with `uv run copperbrain`.

Run the non-destructive offline demo with `uv run python scripts/run_demo.py`.

For an opened KiCad project, Copperbrain writes every deliverable artifact below
`<project>/copperbrain-output/`: prepared project/PDF previews under `previews/` and BOM exports
under `bom/`. Private mutation workspaces, caches, and rollback snapshots are not placed in the
project.

## PCB design rules via MCP

Use `analyze_pcb_constraints` to inspect current netclasses and receive evidence-backed net-role
suggestions. Pass reviewed fabrication limits and electrical intent to `propose_design_rules`,
then use `prepare_pcb_rule_change`, `validate_pcb_rule_change`, and
`apply_pcb_rule_change`. Applying always requires explicit confirmation and a saved, closed KiCad
editor; `rollback_pcb_rule_change` restores the snapshot.

`ManufacturingProfile` covers minimum clearance/track/via dimensions, copper thickness, allowed
temperature rise, and internal/external current layer. `NetRuleRequirement` covers exact net names,
role, current, voltage, explicit clearance/width, creepage, maximum routed length, and optional
differential geometry/maximum uncoupled length. Raw `.kicad_dru` text is never accepted through
MCP.

High-current rules require a current or reviewed width. High-voltage rules require reviewed
clearance. Differential geometry is explicitly marked as not impedance-controlled unless width
and gap are supplied from a verified stackup calculation.

Before proposing routing rules, Copperbrain resolves every connected footprint and measures its
electrical pads, minimum pitch, and edge-to-edge pad clearance. If a class width or clearance does
not fit the package, the generated `.kicad_dru` applies a narrower track width or reduced clearance
only inside that component's courtyard, never below the fabrication profile minimum. The original
class remains active outside the package. Missing project-local courtyards can be generated inside
the same previewed change set and are validated through KiCad CLI; unresolved footprints or
packages narrower than the fabrication minimum produce a safe refusal.

## PCB inspection and placement via MCP

Use `get_pcb_summary`, `inspect_pcb_net`, and `get_footprint_placement` for typed board queries.
`analyze_placement` reports conservative courtyard overlaps and footprints outside the detected
Edge.Cuts extent. `propose_component_placement` accepts exact references, a typed optional region,
spacing/grid limits, and a deterministic `compact` or `grid` strategy.

Pass the returned operations to `prepare_placement_change`. Copperbrain changes only a private
project copy, exports `Copperbrain-PCB-preview.pdf`, runs comparative DRC, and publishes the copy
under `<project>/copperbrain-output/previews/<change-set-id>/`. Use
`validate_placement_change`, then `apply_placement_change` with explicit confirmation and a saved,
closed PCB Editor. `rollback_placement_change` restores the byte-exact snapshot.

The official `kicad-python` IPC binding is installed and detected dynamically for live board
transactions. PCB inspection and safe preview do not require a running editor: the typed file
adapter and `kicad-cli` remain the deterministic offline path. No MCP placement tool accepts raw
KiCad S-expressions, and this extension does not route traces or modify zones, keepouts, or the
board outline.
Move e rotazione restano sul lato corrente del footprint; un cambio `F.Cu`/`B.Cu` viene rifiutato
esplicitamente e deve essere eseguito e revisionato in KiCad.

## Headless PCB initialization via MCP

For an empty board, `prepare_pcb_layout_change` accepts a typed rectangular outline, one placement
for every schematic component, optional fixed M3 mounting holes, and explicit footprint overrides.
It synchronizes footprints in a private copy, builds the unrouted PCB through the adapter, runs
comparative ERC/DRC and placement analysis, and publishes a PDF/project preview below
`copperbrain-output/previews/<change-set-id>/`.

Use `validate_pcb_layout_change` to repeat all gates. `apply_pcb_layout_change` requires explicit
confirmation and a closed editor; `rollback_pcb_layout_change` restores the snapshot. These tools
do not autoroute or generate tracks, copper zones, keepouts, or manufacturing outputs.

## Controlled PCB routing via MCP

After rules and placement are reviewed, use `analyze_unrouted_nets` to identify disconnected pad
groups. Check `get_routing_backend_status`, then call `propose_pcb_routing`: a local FreeRouting
process consumes KiCad's official Specctra DSN, returns one or two isolated candidates, and
Copperbrain deterministically ranks them by completion, new DRC errors, open connections, vias,
and routed length. Only the selected candidate's copper delta becomes typed segments and vias.
There is no implicit fallback to the former internal A* router.

Pass the reviewed plan to `prepare_routing_change`. Copperbrain writes only a private copy,
rechecks selected-net connectivity, runs comparative KiCad DRC, exports
`Copperbrain-PCB-routing-preview.pdf`, and publishes the project preview below
`copperbrain-output/previews/<change-set-id>/`. Only a complete, DRC-valid plan can be applied with
`apply_routing_change`, explicit confirmation, and a closed editor. `rollback_routing_change`
restores the byte-exact PCB snapshot. Prepared routing state is persisted below
`COPPERBRAIN_DATA_DIR`, so validate/apply/rollback and `get_routing_change_summary` survive an MCP
restart. Boards containing pre-existing copper are rejected by default; explicitly select the
`preserve` policy only for intentional incremental routing. A wall-time/stall watchdog also stops
known FreeRouting normalization loops and cleans up the Java process tree.

For the compact end-to-end surface, call `prepare_pcb_finalization`, review its summary, then use
`validate_pcb_finalization` and `apply_pcb_finalization`. `assess_pcb_readiness` and
`get_pcb_finalization_report` intentionally keep `production_ready=false` when DFM, stackup,
thermal, SI/PI, EMC, or impedance checks have not been performed, even if routing/ERC/DRC pass.

The AI may explain and review the structured candidate evidence, but it cannot override
connectivity, DRC, confirmation, stale-hash, or editor-state gates. This workflow does not certify
impedance, SI/PI/EMC, thermal behavior, or regulatory compliance. Raw KiCad expressions, zones,
and keepouts remain unavailable through the routing tools.
