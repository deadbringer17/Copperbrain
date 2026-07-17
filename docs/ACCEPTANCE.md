# MVP acceptance evidence

This checklist maps the approved acceptance criteria to executable evidence. External plugin and
KiCad checks skip only when that optional installation is absent; on the development workstation
KiCad 10, JLCImport, and JLCPCB Tools are all detected and exercised.

| Criterion | Evidence |
|---|---|
| Local MCP client can discover the server | `tests/test_server.py` verifies all 63 core and approved-extension FastMCP tools; `copperbrain` starts stdio only |
| KiCad 10.x and both JLC plugins are detected | `tests/adapters/test_kicad_detection.py`; real detection reports KiCad 10, JLCImport, and JLCPCB Tools |
| Existing project analyzed read-only | `tests/integration/test_kicad_workflow.py` exports a real KiCad 10 netlist without touching the source |
| History and backup copies excluded | `tests/services/test_projects.py` proves `.history`, `*-backups`, and generated output schematics are never selected as sources |
| Structured ERC | Real KiCad 10 JSON ERC integration test and normalized unit tests |
| Search includes price, stock, category | Installed JLCPCB Tools FTS5 integration plus recorded deterministic fixture |
| Choice is requirement-motivated | Deterministic filter/scoring tests in `tests/services/test_sourcing.py` |
| Symbol, footprint, 3D, datasheet import | Atomic/idempotent asset tests in `tests/services/test_assets.py`; restricted downloader tests |
| Circumscribed preview before mutation | Change preparation proves the source remains byte-identical and publishes a PDF/project copy under the opened project's `copperbrain-output/previews/` |
| Valid change set and explicit confirmation required | Confirmation, validation status, editor-state, and stale-hash tests |
| Apply and rollback verified | Real KiCad integration and golden byte-exact rollback test |
| BOM has LCSC/MPN and multiple quantities | Offline E2E flow plus BOM grouping/enrichment/cost tests and enforced project-local JSON/CSV/Markdown exports |
| Cost exclusions are explicit | `CostEstimate.excluded_costs` and generated Markdown disclaimer |
| Demo requires no manual file correction | `uv run python scripts/run_demo.py`; covered by `tests/e2e/test_demo_pipeline.py` |

## Empty-project creation evidence

| Criterion | Evidence |
|---|---|
| No handwritten KiCad S-expression | `ProjectScaffoldAdapter` uses `kicad-sch-api.create_schematic` and the bundled `pcbnew.BOARD`/`SaveBoard` API |
| Preview before source creation | Service tests verify that only `copperbrain-output/previews/<id>/` exists before confirmation |
| Explicit confirmation and target safety | Service tests reject missing confirmation and nonempty target directories |
| Validation and rollback | KiCad integration parses a real four-layer scaffold; rollback requires unchanged post-apply hashes |

## PCB-rule extension evidence

| Criterion | Evidence |
|---|---|
| No raw KiCad rule syntax enters MCP | Public tools accept only Pydantic profiles, requirements, and rule sets; adapter renderer tests |
| Deterministic net analysis and sizing | `tests/services/test_pcb_rule_service.py` covers role evidence, high-current sizing, and rejected unsafe/unknown intent |
| User custom rules are preserved | `tests/adapters/test_pcb_rules.py` verifies managed-block replacement without overwriting user rules |
| Preview does not mutate source | PCB rule service test verifies byte-identical live project before confirmation and project-local preview output |
| Generated rules are KiCad-valid | Real KiCad integration invokes JSON DRC on a temporary board with generated `.kicad_dru` |
| Safe apply and rollback | Confirmation, stale/new-file conflict, atomic apply, byte-exact project restore, and new-rule-file removal tests |
| Fine-pitch packages remain routable | Geometry adapter and PCB-rule service tests measure pad width/pitch/edge clearance, derive local track and clearance caps, render `intersectsCourtyard`, and preserve the original class outside the package |
| Missing courtyards remain controlled | Tests prove generated project-local courtyards are preview-only before confirmation, KiCad-validated, hashed, applied atomically, and rolled back byte-for-byte |

## PCB-placement extension evidence

| Criterion | Evidence |
|---|---|
| Typed PCB summary and net inspection | `tests/adapters/test_pcb_design_adapter.py` verifies outline, footprint, net, pad, track, via, layer, and routed-length extraction |
| Deterministic placement analysis/proposal | `tests/services/test_pcb_design.py` verifies stable connectivity-aware proposals and reduced ratsnest/envelope metrics; `tests/services/test_placement_optimizer.py` covers guarded bottom-side eligibility and THT preservation |
| No arbitrary KiCad syntax enters MCP | Public placement tools accept only Pydantic request and operation models; the adapter tests apply an allowlisted placement |
| Source remains unchanged before confirmation | Service tests compare live PCB bytes before and after prepare/preview/validation |
| Project-local PDF preview | Service and transport tests verify output below `copperbrain-output/previews/<id>/` |
| DRC-gated safe apply | Unit tests verify comparative DRC gates, confirmation, editor state, and stale hash refusal |
| Byte-exact rollback | Service test applies a placement and restores the original `.kicad_pcb` bytes |
| Coordinated F.Cu/B.Cu changes | `tests/integration/test_placement_flip_integration.py` verifies KiCad API transformation of footprint, pads, courtyard, and mirrored text |
| Real KiCad compatibility | Integration test moves one footprint in a temporary KiCad 10 demo, runs JSON DRC, and exports a real PDF |
| Optional official IPC backend | `kicad-python` is locked; adapter status and path verification keep unavailable/running instances explicit |

## Headless PCB-initialization extension evidence

| Criterion | Evidence |
|---|---|
| Typed code-only initialization | MCP wrapper tests validate `PcbLayoutPlan`; adapter tests compose a board without GUI automation or raw public KiCad syntax |
| Complete deterministic plan | Model and adapter tests reject duplicate references, missing physical schematic footprints, and populated boards while excluding nonphysical power symbols |
| Electrical and geometric gates | Service runs comparative ERC/DRC, parser validation, and requires placement score 100 before apply |
| Managed pair-rule safety | Rule adapter tests scope clearance/creepage to different parent footprints, preserve user rules, and prove migration idempotence |
| Safe preview/apply/rollback | Layout service uses private workspaces, project-local preview, source hashes, confirmation, editor-state checks, snapshots, atomic copies, and rollback |
| Routing remains excluded | Contracts and adapter generate only footprint placement, rectangular Edge.Cuts, and fixed M3 holes |

## Controlled PCB-routing extension evidence

| Criterion | Evidence |
|---|---|
| Typed connectivity and operations | `tests/adapters/test_pcb_routing_adapter.py` verifies open-net detection and allowlisted segment writing |
| Fixed-command specialized backend | `tests/adapters/test_freerouting.py` verifies Java/JAR/KiCad-Python status, DSN sanitization, bounded arguments, hash-bound capability validation, and unavailable-backend refusal |
| Exact requested-net scope | FreeRouting adapter regressions retain all DSN nets/planes, split non-target members into preserve classes, pass verified class exclusions, and refuse absent targets or an incapable/tampered JAR before routing starts |
| Zone and precision-safe round trip | FreeRouting adapter tests cover the fixed refill command; routing service tests accept only sub-micron Specctra rounding while still rejecting actual existing-copper changes, and comparative DRC runs after KiCad zone refill |
| Typed import and KiCad 9/10 compatibility | `tests/adapters/test_pcb_routing_adapter.py` verifies numeric and name-valued copper nets plus allowlisted segment/via extraction on declared outer and inner copper layers |
| Deterministic candidate evaluation | `tests/services/test_pcb_routing.py` verifies typed deltas, rule-first role classification, diagnostic-only partial candidates, local hotspot evidence, schema-2 compatibility, stable ranking, throughput metrics, same-baseline comparison, and historical pass advice |
| Source remains unchanged before confirmation | Routing service test compares live PCB bytes through proposal, prepare, and validation |
| Connectivity and comparative DRC gate | Service validation requires selected nets complete and rejects new DRC errors |
| Confirmation, editor, and stale checks | Service tests cover missing confirmation and stale source refusal; shared workflow rejects lock files |
| Byte-exact rollback | Service test applies routing and restores the original `.kicad_pcb` bytes |
| Public MCP contract | `tests/test_server.py` checks the routing wrappers, metric readback, snapshot recovery, and request/plan validation |

## Routing hardening and finalization evidence

| Criterion | Evidence |
|---|---|
| Restart-safe routing lifecycle | `tests/services/test_pcb_routing.py` prepares, validates, applies, and rolls back through fresh service instances using the persisted manifest |
| Autorouter loop containment | `tests/adapters/test_freerouting.py` proves normalization and semantic no-improvement watchdogs stop boundedly and return structured per-pass evidence |
| Correlated lifecycle evidence | `tests/services/test_pcb_routing.py` verifies prepare, validate, apply, and rollback records point to the proposal through `parent_run_id` |
| Safe incremental-routing default | Routing service regression rejects pre-existing copper unless the caller explicitly selects `preserve` |
| Output copies never become sources | Project/output tests reject opening or publishing recursively below `copperbrain-output/` |
| Honest production-readiness state | `tests/services/test_pcb_finalization.py` proves clean electrical checks remain `production_ready=false` while engineering/DFM gates are unassessed |
| Compact MCP orchestration | `tests/test_server.py` covers summary, readiness, prepare/validate/apply finalization, and persisted report wrappers |

## Bounded motor-benchmark evidence

| Criterion | Evidence |
|---|---|
| Semantic schematic generation only | `tests/services/test_reference_design.py` verifies the DRV8701 bridge, ATtiny1616, THVD1429, four sensor channels, polarity, star ground, A3 sheet, and provisional metadata |
| Deterministic physical plan | The same test verifies all 64 physical references, compact 120 x 100 mm two-sided outline, orthogonal rotations, and four M3 holes |
| Reviewed 20 A constraints | Tests verify 70 um external copper, 20 C rise, 20 A `PGND`/motor classes, and separation from logic `GND` |
| Imported footprint geometry is safe to analyze | `tests/adapters/test_footprint_geometry.py` covers rotation, custom primitives, duplicate/same-net pads, chamfered corners, and conversion tolerances |
| Accidental net merges block apply | `tests/services/test_changes.py` treats new `multiple_net_names` ERC warnings as blocking regressions |
| Routing limits remain honest | The layout contract reports open connections; readiness remains false until routing, power copper, thermal, EMC, stackup, and DFM are assessed |

## Compact BLDC benchmark evidence

| Criterion | Evidence |
|---|---|
| Typed reference generation | `tests/services/test_reference_design.py` verifies the DRV8311S topology, 6-PWM/SPI/CSA/Hall interfaces, protection, and provisional 9--12.6 V operating assumptions |
| Envelope and placement | The typed plan contains all 26 physical circuit references plus four M3 holes inside an 85 x 50 mm four-layer outline; live placement analysis scores 100/100 |
| Grounding and rules | The live project has one reviewed In1.Cu GND region, explicit thermal-pad via-in-pad fanout, 0.15 mm manufacturing minima, and separate power/PWM/CSA/control intents |
| Reusable routing evidence | Schema-3 private records capture the successful Hall batch and bounded digital failures; `get_connectivity_metrics` exposes optimization signals without project paths or net names |
| Honest stop condition | Readiness remains blocked with 45 open connections after repeated scoped-router stagnation/no-delta results; high-current phase/power paths were not sent blindly through generic routing |
| Readable schematic presentation | `tests/services/test_schematic_readability.py` applies only typed move/label-stub operations to a private benchmark copy and requires every label to terminate on a wire, zero pin-attached labels, duplicate label positions, or estimated label overlaps, broader A3 use, and unchanged source bytes |
| Readability-gated preview | `tests/services/test_changes.py` and the benchmark preview workflow require the structured readability report alongside parser and comparative ERC gates before a schematic layout change is validated |

## Validation gate

The repository gate is:

```powershell
uv sync --all-extras
uv run pytest --cov=copperbrain --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Pytest enforces at least 85% line coverage across the package. Live network access is never part
of the default suite; installed databases and recorded evidence keep results deterministic.
