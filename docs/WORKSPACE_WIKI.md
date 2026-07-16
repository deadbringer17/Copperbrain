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
| `src/copperbrain/adapters/kicad_detection.py` | Dynamic PATH/environment/Windows-registry KiCad CLI discovery, numeric newest-version selection, user-data, and optional JLC plugin discovery | Installation/plugin discovery changes |
| `src/copperbrain/adapters/kicad_cli.py` | Fixed-command KiCad netlist/ERC/DRC/PDF operations with byte-exact restoration of incidental `.kicad_prl` state, plus footprint parsing validation | CLI invocation, side-effect containment, or KiCad output formats change |
| `src/copperbrain/adapters/kicad_specctra_worker.py` | Fixed-action worker executed by KiCad's bundled Python for headless DSN export and SES import | KiCad Specctra bridge behavior changes |
| `src/copperbrain/adapters/kicad_project_worker.py` | Fixed-action worker using KiCad's bundled Python to create/relayer boards, apply validated footprint transforms, and fill typed clipped ground regions/fanouts/vias | Empty-board API creation, layer policy, placement transforms, or grounding execution changes |
| `src/copperbrain/adapters/project_scaffold.py` | Typed empty-project composer using `kicad-sch-api`, JSON project metadata, and the KiCad board worker | New-project scaffold behavior changes |
| `src/copperbrain/adapters/freerouting.py` | Dynamic local Java/JAR/KiCad-Python detection, exact requested-net DSN filtering, fixed-command FreeRouting candidate generation, progress watchdogs, and process-tree cleanup | Autorouter discovery, routing scope, CLI policy, watchdog, timeout, or strategy changes |
| `src/copperbrain/adapters/orthogonal_router.py` | Deterministic F.Cu/B.Cu A* fallback with orthogonal layer preferences, typed segments/vias, and pad/copper obstacle tracking | Two-layer fallback routing policy or search behavior changes |
| `src/copperbrain/adapters/footprint_geometry.py` | Resolves footprint libraries, measures rotated/custom/same-net-aware pad geometry, and atomically generates missing project-local courtyards | Fanout analysis or footprint-scope generation changes |
| `src/copperbrain/adapters/pcb_rules.py` | Typed KiCad 10 netclass writer, managed preferred/fanout-width metadata, private router-project staging, same-parent-safe pair constraints, and managed `.kicad_dru` migration preserving user rules | PCB rule serialization, router staging, migration, or structural validation changes |
| `src/copperbrain/adapters/pcb_design.py` | Typed KiCad 9/10 PCB parser with copper-shape-aware pad/track/via/zone connectivity, placement and allowlisted segment/via writers, plus project-path-verified official KiCad IPC transactions | PCB geometry, connectivity, net-format compatibility, placement/routing serialization, or live IPC behavior changes |
| `src/copperbrain/adapters/pcb_placement.py` | Fixed-command wrapper around KiCad's bundled Python worker for coordinated move/rotate/side changes in a private PCB copy | Footprint flip execution, KiCad runtime detection, or worker failure handling changes |
| `src/copperbrain/adapters/pcb_grounding.py` | Fixed-command wrapper around KiCad's bundled Python worker for target stackup, typed board/local regions, pad fanouts, through vias, and reviewed replacement | Ground-domain manifest, KiCad execution, or worker failure handling changes |
| `src/copperbrain/adapters/pcb_layout.py` | Typed empty-board composer resolving physical footprint templates, schematic nets, rectangular Edge.Cuts, complete placement, and fixed M3 holes while excluding nonphysical power symbols | Headless PCB initialization primitives change |
| `src/copperbrain/adapters/jlc_catalog.py` | Read-only installed JLCPCB Tools FTS5 adapter, recorded responses, and unavailable fallback | JLC integration boundary or schema normalization changes |
| `src/copperbrain/adapters/downloads.py` | HTTPS allowlist, redirect/type/size validation, and atomic downloads | External asset download policy changes |
| `src/copperbrain/adapters/library_tables.py` | Validated atomic `sym-lib-table`/`fp-lib-table` entries | Project library registration changes |
| `src/copperbrain/adapters/schematic_api.py` | Allowlisted semantic operations through pinned `kicad-sch-api`, including controlled project-library registration, standard-field-aware properties, and allowlisted paper sizes | Add/replace/property/wire/label/no-connect/paper mutation or local-library resolution changes |
| `src/copperbrain/services/projects.py` | Read-only project sessions, generated-output source refusal, history/backup-excluding discovery and hashes, summary, trace, analysis, and ERC orchestration | Project analysis or discovery behavior changes |
| `src/copperbrain/services/project_creation.py` | Preview-first, restart-safe empty-project creation with private manifests, validation, confirmation, atomic apply, hash-guarded rollback, and nonempty-target refusal | New-project lifecycle changes |
| `src/copperbrain/services/sourcing.py` | SQLite evidence cache, hard filters, deterministic scoring, ranking, and comparison | Sourcing rules or pricing selection changes |
| `src/copperbrain/services/assets.py` | Extension/root/pin-pad validated, atomic, idempotent local asset installation | Asset layout or import validation changes |
| `src/copperbrain/services/changes.py` | Private workspaces, project-local preview/PDF publication, parser/ERC gates including blocking multiple-net-name warnings, stale checks, snapshots, atomic apply, and rollback | Schematic mutation safety or preview workflow changes |
| `src/copperbrain/services/pcb_rules.py` | Net classification, fabrication-minimum versus preferred-width policy, net-aware footprint track/clearance fanout proposal, temporary DRC, safe apply, and rollback | PCB constraint policy or mutation workflow changes |
| `src/copperbrain/services/pcb_design.py` | PCB summary/net inspection, outline/empty-board-aware placement scoring/proposal, PDF preview, DRC-gated apply, and byte-exact rollback | PCB placement policy or workflow changes |
| `src/copperbrain/services/placement_optimizer.py` | Deterministic pad-connectivity placement optimizer for orthogonal rotation, guarded top/bottom selection, routing corridors, edge affinity, and compactness | Placement cost model, side eligibility, or pre-routing metrics change |
| `src/copperbrain/services/pcb_grounding.py` | Post-placement multi-domain selection, two-layer-default shaped-region planning, opt-in four-layer assignment, bridge validation, fanout/vias, DRC, preview, apply, and rollback | Ground-domain/stackup policy, region/fanout/via planning, bridge validation, or mutation workflow changes |
| `src/copperbrain/services/pcb_layout.py` | Empty-board schematic synchronization, typed composition, comparative ERC/DRC, placement gate, preview, safe apply, and multi-file rollback | Headless PCB initialization workflow changes |
| `src/copperbrain/services/pcb_routing.py` | FreeRouting orchestration, safe pre-route policy, typed copper deltas, candidate ranking, compact review, persisted change manifests, preview, restart-safe apply/rollback, and board-bound snapshot recovery | PCB routing backend, ranking, persistence, snapshot recovery, or workflow changes |
| `src/copperbrain/services/pcb_finalization.py` | Compact routing-finalization orchestration and deterministic readiness audit separating electrical gates from unassessed production engineering | Finalization workflow, readiness gates, or report changes |
| `src/copperbrain/services/outputs.py` | Enforces `copperbrain-output/`, rejects recursive output roots, validates filenames, strips VCS/history/backups/preferences/locks, and atomically publishes preview copies | Project-local output layout or publication rules change |
| `src/copperbrain/services/bom.py` | BOM grouping, catalog enrichment, component-only estimates, and atomic JSON/CSV/Markdown rendering | BOM fields, cost assumptions, or export formats change |
| `src/copperbrain/services/reference_design.py` | Bounded LM2596/LM5576 sections plus the provisional 12 V/20 A brushed-motor benchmark schematic, compact 120×100 mm two-sided placement, 2 oz profile, and reviewed net-role requirements, all expressed as typed operations/models | Reference topology, assumptions, placement, or metadata changes |
| `src/copperbrain/server.py` | Thin FastMCP stdio entry point exposing 62 core and approved-extension tools, including safe project creation, post-placement grounding, persistent routing, and finalization/readiness contracts | MCP tools or transport serialization change |
| `tests/` | Offline unit and adapter tests mirroring package responsibilities | Any tested behavior changes |
| `tests/fixtures/kicad10_minimal/` | Small unmodified KiCad 10 demo project copied from the KiCad distribution | Parser/mutation compatibility fixtures change |
| `tests/fixtures/kicad10_placement/` | Minimal KiCad PCB with outline, footprints, pads, track, and via for deterministic placement tests | PCB placement fixture coverage changes |
| `tests/fixtures/jlc_catalog.json` | Timestamped recorded component evidence for deterministic offline tests | Catalog normalization examples change |
| `tests/integration/` | Real KiCad CLI/parser integration and byte-exact rollback | KiCad adapter behavior changes |
| `tests/integration/test_grounding_integration.py` | Real KiCad 10 single/multi-domain zone fill, fanout/via, bridge, and zone-aware connectivity verification | Grounding worker or zone-connectivity behavior changes |
| `tests/integration/test_project_scaffold_integration.py` | Real KiCad API creation of a parseable four-layer empty project | Project scaffold integration changes |
| `tests/e2e/` | Reference flow from project analysis through sourcing, mutation, BOM, and rollback | Demo workflow changes |
| `scripts/run_demo.py` | Non-destructive executable reference workflow using temporary copies | Demo sequence changes |
| `docs/INSTALLATION.md` | Windows/uv setup, MCP stdio configuration, environment, and mutation safety | Installation or configuration changes |
| `docs/DEMO.md` | Offline and interactive demo procedure | Demo contract or expected output changes |
| `docs/ACCEPTANCE.md` | Acceptance-criterion-to-test traceability and final validation gate | MVP criteria or evidence changes |

## Current State

All 19 core MCP contracts are wired to application services and the reference E2E is complete.
The approved PCB-rule extension adds 7 MCP contracts for analysis, deterministic proposal, DRC,
safe application, and rollback of netclass/custom constraints.
The approved PCB-placement extension adds 10 contracts for board/net inspection, deterministic
placement, project-local PDF preview, DRC-gated application, and byte-exact rollback. The official
KiCad IPC adapter is optional at runtime; offline workflows use the typed file adapter and CLI.
Connectivity-aware proposals evaluate pad distance, orthogonal rotations, routing corridors and
compactness. Coordinated side changes use the bundled `pcbnew` worker only in private previews;
automatic bottom eligibility is restricted to small SMD passives.
The post-placement grounding extension adds 4 contracts in the required sequence
`apply_placement_change -> grounding_pcb -> routing`: it supports one or more exact reviewed ground
domains, defaults to two copper layers, derives clipped board/local regions, validates the
two-terminal bridge graph, and adds rotated-pad-aware fanout/vias where pads do not touch their
primary plane. The automatic `GND`/`PGND` policy uses `GND -> B` and `PGND -> F`; four layers and
the former `GND -> In1/B`, `PGND -> F/In2` split require explicit opt-in. Via-in-pad and
existing-zone replacement are explicit opt-ins. Zone-aware routing,
preview, comparative DRC, confirmation, snapshot, and rollback gates remain mandatory. Grounding
apply/rollback blocks on PCB-document locks, while a Project Manager-only lock is permitted.
The headless PCB-initialization extension adds 4 contracts for a complete typed layout plan on an
empty board, including schematic footprint synchronization, rectangular outline, M3 holes,
comparative ERC/DRC, preview, confirmed apply, and rollback. It never routes tracks or zones.
The controlled-routing extension adds 8 contracts for backend status, open-connection analysis,
local FreeRouting candidate generation through KiCad DSN/SES, typed segment/via deltas,
deterministic evaluation, project-local preview, comparative DRC, confirmed apply, and byte-exact
rollback. Zone fills, keepouts, and SI/PI/EMC certification remain outside it.
The routing-hardening extension adds 6 compact contracts for restart-safe review/finalization and
readiness assessment. Persistent manifests resume validate/apply/rollback after server restarts;
watchdogs stop stalled/normalization-loop Java jobs, and output copies cannot become source roots.
Non-empty routing requests are enforced in the exported DSN so FreeRouting cannot silently route
unrelated nets; an empty public selection is materialized from the exact open-net analysis before
backend invocation, and missing requested nets fail before Java starts. Routing apply/rollback
blocks on PCB-document locks, not on a Project Manager-only lock.
The empty-project extension adds 4 contracts that prepare a validated API-generated scaffold,
publish it below the future project output tree, require confirmation before creating source
files, and permit rollback only while the created files remain unchanged.
The bounded motor-benchmark template adds no unrestricted generation contract: it composes a
provisional DRV8701/ATtiny1616/THVD1429 reference from semantic schematic operations, a typed
placement plan, and deterministic 2 oz rule requirements. High-current copper zones, thermal/EMC
proof, and production readiness remain unassessed.

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
