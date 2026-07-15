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
