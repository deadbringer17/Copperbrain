# Copperbrain Workspace Wiki

This wiki is the concise navigation map for the Copperbrain repository. Update it in the same change set whenever code, configuration, tests, or repository structure changes.

For product scope and architecture decisions, read [`DEVELOPMENT_PLAN.md`](../DEVELOPMENT_PLAN.md). For agent operating rules, read [`AGENTS.md`](../AGENTS.md).

## Repository Map

| Path | Responsibility | Change here when |
|---|---|---|
| `AGENTS.md` | Coding-agent architecture, safety, testing, Git, and documentation rules | The repository workflow or agent guardrails change |
| `DEVELOPMENT_PLAN.md` | Approved MVP scope, architecture, MCP contracts, milestones, and acceptance criteria | An approved product or architectural decision changes |
| `docs/WORKSPACE_WIKI.md` | Fast path-oriented map of the workspace | Any relevant file, responsibility, entry point, relationship, or test location changes |
| `pyproject.toml` | Package metadata, runtime/dev dependencies, and validation tool configuration | Dependencies, entry points, or validation policy changes |
| `README.md` | Installation, server launch, and user-facing workflow | Setup or public usage changes |
| `.gitignore` | Excludes virtual environments, caches, runtime databases, locks, and generated fabrication outputs | Generated/local artifact policy changes |
| `src/copperbrain/models.py` | Immutable Pydantic domain and boundary contracts | Public data contracts or shared domain values change |
| `src/copperbrain/errors.py` | Stable actionable application errors | Error taxonomy or mapping changes |
| `src/copperbrain/config.py` | Explicit environment-backed runtime settings | Cache, data, download, or timeout configuration changes |
| `src/copperbrain/adapters/kicad_detection.py` | Dynamic KiCad CLI, numeric newest-version selection, user-data, and optional JLC plugin discovery | Installation/plugin discovery changes |
| `src/copperbrain/adapters/kicad_cli.py` | Fixed-command KiCad netlist/ERC/DRC operations, footprint parsing validation, and atomic schematic PDF export | CLI invocation or KiCad output formats change |
| `src/copperbrain/adapters/footprint_geometry.py` | Resolves footprint libraries, measures pad/pitch/edge-clearance geometry, and atomically generates missing project-local courtyards | Fanout analysis or footprint-scope generation changes |
| `src/copperbrain/adapters/pcb_rules.py` | Typed KiCad 10 netclass writer and managed `.kicad_dru` renderer preserving user rules | PCB rule serialization or structural validation changes |
| `src/copperbrain/adapters/jlc_catalog.py` | Read-only installed JLCPCB Tools FTS5 adapter, recorded responses, and unavailable fallback | JLC integration boundary or schema normalization changes |
| `src/copperbrain/adapters/downloads.py` | HTTPS allowlist, redirect/type/size validation, and atomic downloads | External asset download policy changes |
| `src/copperbrain/adapters/library_tables.py` | Validated atomic `sym-lib-table`/`fp-lib-table` entries | Project library registration changes |
| `src/copperbrain/adapters/schematic_api.py` | Allowlisted semantic operations through pinned `kicad-sch-api`, including controlled project-library registration | Add/replace/property/wire/label/no-connect mutation or local-library resolution changes |
| `src/copperbrain/services/projects.py` | Read-only project sessions, output-excluding discovery/hashes, summary, trace, analysis, and ERC orchestration | Project analysis or discovery behavior changes |
| `src/copperbrain/services/sourcing.py` | SQLite evidence cache, hard filters, deterministic scoring, ranking, and comparison | Sourcing rules or pricing selection changes |
| `src/copperbrain/services/assets.py` | Extension/root/pin-pad validated, atomic, idempotent local asset installation | Asset layout or import validation changes |
| `src/copperbrain/services/changes.py` | Private workspaces, project-local preview/PDF publication, parser/ERC gates, stale checks, snapshots, atomic apply, and rollback | Schematic mutation safety or preview workflow changes |
| `src/copperbrain/services/pcb_rules.py` | Net classification, footprint-aware track/clearance fanout proposal, temporary DRC, safe apply, and rollback | PCB constraint policy or mutation workflow changes |
| `src/copperbrain/services/outputs.py` | Enforces `copperbrain-output/`, validates filenames, strips VCS/history/backups/locks, and atomically publishes preview copies | Project-local output layout or publication rules change |
| `src/copperbrain/services/bom.py` | BOM grouping, catalog enrichment, component-only estimates, and atomic JSON/CSV/Markdown rendering | BOM fields, cost assumptions, or export formats change |
| `src/copperbrain/services/reference_design.py` | Bounded LM2596 12 V→5 V and LM5576 48 V→12 V reference sections expressed only as validated semantic operations | Reference topology, assumptions, or metadata changes |
| `src/copperbrain/server.py` | Thin FastMCP stdio entry point exposing 19 MVP and 7 PCB-rule tools | MCP tools or transport serialization change |
| `tests/` | Offline unit and adapter tests mirroring package responsibilities | Any tested behavior changes |
| `tests/fixtures/kicad10_minimal/` | Small unmodified KiCad 10 demo project copied from the KiCad distribution | Parser/mutation compatibility fixtures change |
| `tests/fixtures/jlc_catalog.json` | Timestamped recorded component evidence for deterministic offline tests | Catalog normalization examples change |
| `tests/integration/` | Real KiCad CLI/parser integration and byte-exact rollback | KiCad adapter behavior changes |
| `tests/e2e/` | Reference flow from project analysis through sourcing, mutation, BOM, and rollback | Demo workflow changes |
| `scripts/run_demo.py` | Non-destructive executable reference workflow using temporary copies | Demo sequence changes |
| `docs/INSTALLATION.md` | Windows/uv setup, MCP stdio configuration, environment, and mutation safety | Installation or configuration changes |
| `docs/DEMO.md` | Offline and interactive demo procedure | Demo contract or expected output changes |
| `docs/ACCEPTANCE.md` | Acceptance-criterion-to-test traceability and final validation gate | MVP criteria or evidence changes |

## Current State

All 19 core MCP contracts are wired to application services and the reference E2E is complete.
The approved PCB-rule extension adds 7 MCP contracts for analysis, deterministic proposal, DRC,
safe application, and rollback of netclass/custom constraints.

```text
The next work should be post-MVP hardening or another explicitly approved scope extension.
```

Application services sit between `server.py` and adapters as subsequent tools are introduced.

User-facing artifacts are always rooted at `<opened-project>/copperbrain-output/`. The project service excludes that tree from source discovery and hashing; private workspaces and rollback snapshots remain under the configured Copperbrain data directory.

## Navigation Rules

- Start here to locate likely files, then confirm the information against the current repository.
- Describe responsibilities and relationships; do not duplicate implementation details or the development plan.
- Prefer repository-relative paths in backticks.
- Keep entries short enough to scan quickly.
- Remove stale entries in the same change that removes or moves their targets.
