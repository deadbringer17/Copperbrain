# Copperbrain Workspace Wiki

This wiki is the concise navigation map for the Copperbrain repository. Update it in the same change set whenever code, configuration, tests, or repository structure changes.

For product scope and public contracts, read [`README.md`](../README.md). For agent operating rules, read [`AGENTS.md`](../AGENTS.md).

## Repository Map

| Path | Responsibility | Change here when |
|---|---|---|
| `AGENTS.md` | Real-project workflow, architecture, safety, mandatory connectivity/routing metrics, testing, Git, and documentation rules | The operational workflow or agent guardrails change |
| `docs/WORKSPACE_WIKI.md` | Fast path-oriented map of the workspace | Any relevant file, responsibility, entry point, relationship, or test location changes |
| `pyproject.toml` | Package metadata, runtime/dev dependencies, and validation tool configuration | Dependencies, entry points, or validation policy changes |
| `src/copperbrain/cli.py` | Default MCP-server launch plus explicit maintenance commands such as the guarded source updater | CLI dispatch or maintenance command behavior changes |
| `README.md` | Installation, server launch, and user-facing workflow | Setup or public usage changes |
| `assets/copperbrain-mcp-icon*.png` | Square Copperbrain MCP icons combining a protected shield, brain, and PCB traces; the `kicad-style` variant adds blue EDA styling | MCP branding or icon artwork changes |
| `.gitignore` | Excludes virtual environments, developer scratch trees, caches, runtime databases, locks, and generated fabrication outputs | Generated/local artifact policy changes |
| `src/copperbrain/models.py` | Immutable Pydantic domain and boundary contracts | Public data contracts or shared domain values change |
| `src/copperbrain/errors.py` | Stable actionable application errors | Error taxonomy or mapping changes |
| `src/copperbrain/config.py` | Explicit environment-backed runtime settings | Cache, data, download, or timeout configuration changes |
| `src/copperbrain/adapters/kicad_detection.py` | Dynamic PATH/environment/Windows-registry KiCad CLI discovery, numeric newest-version selection, user-data, and optional JLC plugin discovery | Installation/plugin discovery changes |
| `src/copperbrain/adapters/repository_updates.py` | Fixed-command Git boundary for official-origin fetch, revision checks, and fast-forward-only source updates | Source-update Git operations, timeouts, or command policy changes |
| `src/copperbrain/adapters/kicad_cli.py` | Fixed-command KiCad netlist/ERC/DRC/PDF operations with byte-exact restoration of incidental `.kicad_prl` and `.kicad_pro` writes, plus footprint parsing validation | CLI invocation, side-effect containment, or KiCad output formats change |
| `src/copperbrain/adapters/kicad_specctra_worker.py` | Fixed-action worker executed by KiCad's bundled Python for headless DSN export, SES import, and post-route zone refill | KiCad Specctra bridge or routed-zone behavior changes |
| `src/copperbrain/adapters/kicad_project_worker.py` | Fixed-action worker using KiCad's bundled Python to create/relayer boards, apply validated footprint transforms, and fill typed clipped ground regions/fanouts/vias | Empty-board API creation, layer policy, placement transforms, or grounding execution changes |
| `src/copperbrain/adapters/project_scaffold.py` | Typed empty-project composer using `kicad-sch-api`, JSON project metadata, and the KiCad board worker | New-project scaffold behavior changes |
| `src/copperbrain/adapters/freerouting.py` | Dynamic local runtime detection with verified-capability preference, hash-bound DSN preserve-class scope validation, reviewed plane-net exclusion from private DSN input, incremental-safe splitting of KiCad multi-segment wire paths, three hard-bounded fixed-command attempt configurations (one candidate by default), bounded per-pass progress parsing, score-aware semantic-stagnation watchdog (default 8 flat passes; completed 0-open passes never count), and process cleanup | Autorouter discovery, routing scope, attempt cap/configuration, plane exclusion, DSN wiring normalization, CLI capability policy, telemetry, watchdog, timeout, or strategy changes |
| `src/copperbrain/adapters/footprint_geometry.py` | Resolves footprint libraries, measures rotated/custom/same-net-aware pad geometry, and atomically generates missing project-local courtyards | Fanout analysis or footprint-scope generation changes |
| `src/copperbrain/adapters/pcb_rules.py` | Typed KiCad 10 netclass writer, explicit managed net-role and preferred/fanout-width metadata, private router-project staging, same-parent-safe pair constraints, and managed `.kicad_dru` migration preserving user rules | PCB rule serialization, role metadata, router staging, migration, or structural validation changes |
| `src/copperbrain/adapters/pcb_design.py` | Typed KiCad 9/10 PCB parser with copper-shape-aware pad/track/via/zone connectivity, zone net-name discovery, placement and board-layer-validated segment/via writers, plus project-path-verified official KiCad IPC transactions | PCB geometry, connectivity, layer/net-format compatibility, placement/routing serialization, or live IPC behavior changes |
| `src/copperbrain/adapters/pcb_placement.py` | Fixed-command wrapper around KiCad's bundled Python worker for coordinated move/rotate/side changes in a private PCB copy | Footprint flip execution, KiCad runtime detection, or worker failure handling changes |
| `src/copperbrain/adapters/pcb_grounding.py` | Fixed-command wrapper around KiCad's bundled Python worker for target stackup, typed board/local regions, pad fanouts, through vias, and reviewed replacement | Ground-domain manifest, KiCad execution, or worker failure handling changes |
| `src/copperbrain/adapters/pcb_layout.py` | Typed empty-board composer resolving physical footprint templates, schematic nets, rectangular Edge.Cuts, complete placement, and fixed M3 holes while excluding nonphysical power symbols | Headless PCB initialization primitives change |
| `src/copperbrain/adapters/jlc_catalog.py` | Read-only installed JLCPCB Tools FTS5 adapter, recorded responses, and unavailable fallback | JLC integration boundary or schema normalization changes |
| `src/copperbrain/adapters/downloads.py` | HTTPS allowlist, redirect/type/size validation, and atomic downloads | External asset download policy changes |
| `src/copperbrain/adapters/library_tables.py` | Validated atomic `sym-lib-table`/`fp-lib-table` entries | Project library registration changes |
| `src/copperbrain/adapters/schematic_api.py` | Allowlisted semantic operations through pinned `kicad-sch-api`, including geometry-derived outward label stubs, controlled project-library registration, KiCad-private-property compatibility, standard-field-aware properties, and allowlisted paper sizes | Add/replace/property/wire/label/no-connect/paper mutation or local-library resolution changes |
| `src/copperbrain/adapters/schematic_readability.py` | Read-only label-to-wire endpoint attachment, pin attachment, overlap, duplicate-position, component-spacing, occupied-area, and readability scoring for parsed schematics | Schematic presentation metrics or readability gates change |
| `src/copperbrain/services/projects.py` | Read-only project sessions, generated-output source refusal, history/backup-excluding discovery and hashes, summary, trace, analysis, and ERC orchestration | Project analysis or discovery behavior changes |
| `src/copperbrain/services/updates.py` | Deterministic clean-main, official-origin, fast-forward-only source-update policy and actionable refusals | Update safety gates or outcomes change |
| `src/copperbrain/services/project_creation.py` | Preview-first, restart-safe empty-project creation with private manifests, validation, confirmation, atomic apply, hash-guarded rollback, and nonempty-target refusal | New-project lifecycle changes |
| `src/copperbrain/services/sourcing.py` | SQLite evidence cache, hard filters, deterministic scoring, ranking, and comparison | Sourcing rules or pricing selection changes |
| `src/copperbrain/services/assets.py` | Extension/root/pin-pad validated, atomic, idempotent local asset installation | Asset layout or import validation changes |
| `src/copperbrain/services/changes.py` | Private workspaces, restart-safe private change records, project-local preview/PDF publication, parser/ERC gates, mandatory readability gates for layout operations, stale checks, snapshots, atomic apply, and rollback | Schematic mutation safety, readability validation, persistence, or preview workflow changes |
| `src/copperbrain/services/pcb_rules.py` | Net classification, fabrication-minimum versus preferred-width policy, net-aware footprint track/clearance fanout proposal including one-net footprints, temporary DRC, restart-safe preview/apply state, safe apply, and rollback | PCB constraint policy, persistence, or mutation workflow changes |
| `src/copperbrain/services/pcb_design.py` | PCB summary/net inspection, outline/empty-board-aware placement scoring/proposal, restart-safe PDF preview/apply state, DRC-gated apply, and byte-exact rollback | PCB placement policy, persistence, or workflow changes |
| `src/copperbrain/services/placement_optimizer.py` | Deterministic pad-connectivity placement optimizer for orthogonal rotation, guarded top/bottom selection, routing corridors, edge affinity, compactness, opt-in `routing_coherent` power/critical-net clustering, global MST high-current corridor reservation, obstruction penalties, and existing-copper anchor detection | Placement cost model, side eligibility, or pre-routing metrics change |
| `src/copperbrain/services/pcb_grounding.py` | Restart-safe post-placement single/multi-domain selection, domain-scoped layer normalization, two-layer shaped-region planning, opt-in four-layer assignment, bridge validation, fanout/vias, DRC, preview, apply, and rollback | Ground-domain/stackup policy, persistence, region/fanout/via planning, bridge validation, or mutation workflow changes |
| `src/copperbrain/services/pcb_layout.py` | Restart-safe empty-board schematic synchronization, typed composition, comparative ERC/DRC, placement gate, preview, safe apply, and multi-file rollback | Headless PCB initialization workflow changes |
| `src/copperbrain/services/connectivity_metrics.py` | Atomic private schema-2/3/4 JSON persistence, parent/child lifecycle correlation, same-baseline batch comparison, throughput summaries, and historical pass-budget advice | Connectivity metric storage, compatibility, corpus advice, durability, or MCP readback changes |
| `src/copperbrain/services/pcb_routing.py` | FreeRouting-only orchestration, rule-first net roles, single-candidate default (up to three on request) ranking and best-only prepared-PCB application, zone-derived default plane-net exclusion when the caller passes none, opt-in geometry-derived fine-pitch stubs and opposite-layer dogbones, local routing-hotspot detection, diagnostic-only partial candidates, safe scoped copper deltas, candidate/lifecycle metrics, compact review, preview, restart-safe apply/rollback, and snapshot recovery | PCB routing scope gates, attempt cap, plane-exclusion defaults, escape policy, role policy, hotspot/placement evidence, metrics, ranking, persistence, recovery, or workflow changes |
| `src/copperbrain/services/pcb_finalization.py` | Compact routing-finalization orchestration and deterministic readiness audit separating electrical gates from unassessed production engineering | Finalization workflow, readiness gates, or report changes |
| `src/copperbrain/services/pcb_phase.py` | Restart-safe aggregate placement, grounding, and routing workspace behind the single final PCB acceptance; propagates exact reviewed ground domains as router-only plane exclusions, then refills and validates them with correlated routing metric IDs, preview, revalidation, atomic apply, and rollback | Three-gate orchestration, router plane policy, or final PCB acceptance changes |
| `src/copperbrain/services/outputs.py` | Enforces `copperbrain-output/`, rejects recursive output roots, validates filenames, strips VCS/history/backups/preferences/locks, removes read-only generated trees safely on Windows, and atomically maintains the bounded schematic/design-rules/PCB preview slots | Project-local output layout or publication rules change |
| `src/copperbrain/services/bom.py` | BOM grouping, catalog enrichment, component-only estimates, and atomic JSON/CSV/Markdown rendering | BOM fields, cost assumptions, or export formats change |
| `src/copperbrain/services/reference_design.py` | Bounded LM2596/LM5576 sections, 12→48 V LT3757A boost benchmark, brushed-motor benchmark, and compact DRV8311S BLDC benchmark, all expressed as typed operations/models | Reference topology, assumptions, placement, rules, or metadata changes |
| `src/copperbrain/server.py` | Thin FastMCP stdio entry point exposing 40 tools, three preview phases, and exactly three explicit acceptance gates: schematic, design rules, and aggregate PCB | MCP tools, preview/gate count, or transport serialization change |
| `benchmark_bldc_drv8311/` | Generated 85×50 mm DRV8311S BLDC KiCad benchmark; user artifacts remain under its ignored `copperbrain-output/` tree | Compact BLDC fixture topology, placement, rules, or reproducible benchmark evidence changes |
| `benchmark-test3/` | Gated 9–15 V to 48 V / 0.5 A LT3757A boost-converter benchmark and its ignored preview artifacts | Boost benchmark schematic, PCB, or reproducible connectivity metrics change |
| `tests/` | Offline unit and adapter tests mirroring package responsibilities | Any tested behavior changes |
| `tests/services/test_updates.py` and `tests/adapters/test_repository_updates.py` | Source-update policy refusals, outcomes, and fixed Git command coverage | Updater behavior or Git adapter operations change |
| `tests/fixtures/kicad10_minimal/` | Small unmodified KiCad 10 demo project copied from the KiCad distribution | Parser/mutation compatibility fixtures change |
| `tests/fixtures/kicad10_placement/` | Minimal KiCad PCB with outline, footprints, pads, track, and via for deterministic placement tests | PCB placement fixture coverage changes |
| `tests/fixtures/jlc_catalog.json` | Timestamped recorded component evidence for deterministic offline tests | Catalog normalization examples change |
| `tests/services/test_schematic_readability.py` | Private-copy regression proving the BLDC benchmark removes pin-attached/overlapping labels, expands sheet use, and preserves the source | Schematic readability policy or BLDC layout changes |
| `tests/integration/` | Real KiCad CLI/parser integration and byte-exact rollback | KiCad adapter behavior changes |
| `tests/integration/test_grounding_integration.py` | Real KiCad 10 single/multi-domain zone fill, fanout/via, bridge, and zone-aware connectivity verification | Grounding worker or zone-connectivity behavior changes |
| `tests/integration/test_project_scaffold_integration.py` | Real KiCad API creation of a parseable four-layer empty project | Project scaffold integration changes |
| `tests/e2e/` | Reference flow from project analysis through sourcing, mutation, BOM, and rollback | Demo workflow changes |
| `scripts/run_demo.py` | Non-destructive executable reference workflow using temporary copies | Demo sequence changes |
| `scripts/setup_dependencies.py` | Explicit, checksum-verified, user-invoked downloader for optional Java/FreeRouting/JLC-plugin integrations from official sources only; never runs automatically or through MCP | Dependency sources, checksum policy, target directories, or CLI flags change |
| `docs/INSTALLATION.md` | Windows/uv setup, MCP stdio configuration, environment, and mutation safety | Installation or configuration changes |
| `docs/DEMO.md` | Offline and interactive demo procedure | Demo contract or expected output changes |
| `docs/ACCEPTANCE.md` | Acceptance-criterion-to-test traceability and final validation gate | Acceptance criteria or evidence changes |

## Operational Workflow

Real-project work follows the engineering sequence documented in `AGENTS.md`: intake/baseline,
schematic and sourcing, PCB rules, placement, grounding/power structure, routing by explicit
net class and bounded batch, then final validation/readiness. The public MCP has exactly three
explicit acceptances: `accept_schematic`, `accept_design_rules`, and `accept_pcb`. Published
previews are bounded to the stable `schematic`, `design-rules`, and `pcb` slots. Granular PCB
operations are composed without intermediate preview artifacts and validated in private workspaces;
source hashes, preview, editor-state checks, snapshots, atomic replacement, and recovery remain.

Every connectivity analysis, grounding check, router candidate, routing validation, and applicable
test must emit a versioned structured metrics record under the configured private data directory's
`metrics/connectivity/` tree (or a test temporary directory). Records correlate phases with run IDs
and capture runtime, backend/configuration, connectivity deltas, DRC deltas, routing geometry,
board-wide versus queued per-pass work, copper per second, connections resolved per pass, and failure evidence without storing sensitive project contents. Schema 4 correlates prepare, validate, apply, and rollback through `parent_run_id`, and compares bounded batches sharing the same baseline fingerprint. The read-only
`get_connectivity_metrics` contract returns bounded optimization and historical pass-budget signals by run ID without exposing paths or raw backend artifacts. Future metrics code should
sit at the application-service boundary; backend adapters provide raw observations, and MCP handlers
only validate requests and serialize typed results.

User-facing artifacts are rooted at `<opened-project>/copperbrain-output/`. Project discovery and
hashing exclude that tree; private workspaces, metrics, caches, and rollback snapshots remain under
the configured Copperbrain data directory.

## Navigation Rules

- Start here to locate likely files, then confirm the information against the current repository.
- Describe responsibilities and relationships; do not duplicate implementation details or public contract documentation.
- Prefer repository-relative paths in backticks.
- Keep entries short enough to scan quickly.
- Remove stale entries in the same change that removes or moves their targets.
