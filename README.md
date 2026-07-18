# Copperbrain

Copperbrain is a local MCP server for safe KiCad 10 schematic/PCB analysis, deterministic
JLCPCB/LCSC sourcing, controlled changes, ERC/DRC validation, typed PCB design rules, and
component-only BOM estimates.

See this README for scope and public contracts, `docs/INSTALLATION.md` for setup, and
`docs/DEMO.md` for the reproducible reference flow.

## Installation

Requires Windows, Python 3.11 or newer, `uv`, and KiCad 10.x (`kicad-cli.exe` is discovered
dynamically). Java 25+ and a local FreeRouting JAR are required only for controlled PCB routing;
JLCImport and JLCPCB Tools are optional and enable local component sourcing.

```powershell
git clone https://github.com/deadbringer17/Copperbrain.git
cd Copperbrain
uv sync --all-extras
uv run pytest
```

Start the server with `uv run copperbrain`. Configure an MCP client to execute `uv` with arguments
`run copperbrain` and this repository as its working directory. Copperbrain exposes local stdio
only and never starts a public network listener. See `docs/INSTALLATION.md` for the full
environment-variable reference and mutation-safety details.

To update a source checkout explicitly from the official `origin/main`, run:

```powershell
uv run copperbrain update
```

The updater accepts only a clean `main` worktree with the official Copperbrain GitHub origin and
applies a Git fast-forward only. It refuses dirty worktrees, detached or different branches,
unexpected remotes, and divergent history; it never stashes, resets, rebases, or discards local
work. Restart Codex or open a new task after a successful update.

To fetch the optional runtime integrations (Java, FreeRouting, JLCImport, JLCPCB Tools)
automatically instead of installing them by hand, run:

```powershell
uv run python scripts/setup_dependencies.py
```

The script only ever contacts official sources over HTTPS (GitHub, Adoptium, `kicad.github.io`),
verifies a checksum whenever the source publishes one, and asks for confirmation before writing
anything — including the JLC plugins, which land in KiCad's own plugin directory, outside this
repository. See "Automated dependency setup" in `docs/INSTALLATION.md` for exactly what it does
and does not do.

## Installing in Codex CLI

Complete the installation above first. Codex (the `codex` CLI) reads MCP server definitions from
its `config.toml` — `~/.codex/config.toml` on Linux/macOS, `%USERPROFILE%\.codex\config.toml` on
Windows. Add a `[mcp_servers.copperbrain]` entry:

```toml
[mcp_servers.copperbrain]
command = "uv"
args = ["run", "--directory", "C:\\path\\to\\Copperbrain", "copperbrain"]
enabled = true
```

Codex launches `command` directly, without a shell and without setting a working directory of its
own, so `uv run` needs `--directory <repo>` instead of relying on `cd`; use the absolute path to
where you cloned this repository. To override an environment variable from
`docs/INSTALLATION.md#configuration` for this server only, add it under
`[mcp_servers.copperbrain.env]`, the same way other entries in `config.toml` do. Restart Codex (or
start a new session) after editing the file. Copperbrain still exposes local stdio only; registering
it this way adds no network exposure.

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

## Three acceptance gates

The public MCP exposes exactly three tools that accept a validated mutation:

1. `accept_schematic` for the project scaffold or schematic change set.
2. `accept_design_rules` for reviewed widths, clearances, vias, netclasses, and assignments.
3. `accept_pcb` for one aggregate placement, grounding, and routing change set prepared with
   `prepare_pcb_acceptance` and rechecked with `validate_pcb_acceptance`.

Granular PCB prepare/preview/validate/apply/rollback functions are internal and are not MCP tools.
They modify only the private aggregate workspace. `rollback_accepted_phase` is an explicit recovery
command, not another acceptance gate.

To start a new design safely, call `prepare_project_creation` with an existing parent directory
and a typed name/layer count. Copperbrain creates the schematic through `kicad-sch-api`, creates
the empty board through KiCad's bundled Python API, validates both files, and publishes only a
preview under `<new-project>/copperbrain-output/previews/schematic/`.
`accept_schematic` applies the validated scaffold; recovery remains hash guarded.

For an opened KiCad project, Copperbrain writes every deliverable artifact below
`<project>/copperbrain-output/`: at most the stable `schematic`, `design-rules`, and `pcb` previews
under `previews/`, plus BOM exports under `bom/`. Private mutation workspaces, caches, and rollback
snapshots are not placed in the project.

## Project analysis via MCP

Call `detect_kicad` to confirm KiCad, CLI, and JLC plugin availability, then `open_project` to
open a project read-only and hash its sources. `get_project_summary` returns sheets, components,
electrical nets, and power symbols; `analyze_schematic` returns deterministic, evidence-backed
observations; `trace_net` returns every pin KiCad assigns to one exact net name. `run_erc` and
`run_drc` run KiCad's checks and normalize violations without touching project files.

## Schematic changes via MCP

`prepare_schematic_change` stages allowlisted operations in a private workspace and returns a
semantic diff, risks, and a project-local PDF preview; `validate_change` revalidates it in place.
Applying requires the first acceptance gate, `accept_schematic`, with `confirmed=true` and
`editor_closed=true`.

## Component sourcing via MCP

`search_components` ranks JLCPCB/LCSC candidates against typed requirements using the installed
JLCPCB Tools catalog or a recorded `COPPERBRAIN_JLC_CATALOG`. `get_component_details` returns
normalized catalog details for one exact LCSC part; `compare_components` and `find_alternatives`
rank supplied or discovered candidates against the same requirements. `estimate_component_cost`
estimates component-only cost at one requested quantity. `import_component_assets` installs the
resolved symbol/footprint/3D/datasheet assets locally from allowlisted HTTPS sources.

## BOM via MCP

`generate_bom` groups and enriches the schematic BOM and writes JSON/CSV/Markdown under
`copperbrain-output/bom/`. `estimate_bom_cost` totals component-only cost at requested board
quantities; `suggest_bom_substitutions` proposes requirement-motivated alternatives for BOM lines.
All cost figures explicitly exclude PCB, assembly, stencil, shipping, taxes, and duties.

## PCB design rules via MCP

Use `analyze_pcb_constraints` to inspect current netclasses and receive evidence-backed net-role
suggestions. Pass reviewed fabrication limits and electrical intent to `propose_design_rules`,
then use `prepare_pcb_rule_change`, `validate_pcb_rule_change`, and
`accept_design_rules`. Applying requires the second acceptance and a saved, closed KiCad editor;
`rollback_accepted_phase` restores the phase snapshot when explicitly invoked.

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
`analyze_placement` reports conservative courtyard overlaps, footprints outside Edge.Cuts,
estimated ratsnest length, occupied envelope, compactness, side counts, and cross-layer nets.
`propose_component_placement` accepts exact references, a typed optional region, spacing/grid and
routing-corridor limits, plus deterministic `compact` or `grid`, rotation, and layer policies.
Compact placement scores pad connectivity and envelope growth, keeps connectors near edges, and
allows automatic bottom placement only for small SMD passives.

Pass the returned operations directly to `prepare_pcb_acceptance`. Placement is applied and
validated inside its private aggregate workspace without an intermediate PDF or published copy;
no separate placement preview or acceptance is exposed.

The official `kicad-python` IPC binding is installed and detected dynamically for live board
transactions. PCB inspection and safe preview do not require a running editor: the typed file
adapter and `kicad-cli` remain the deterministic offline path. No MCP placement tool accepts raw
KiCad S-expressions, and this extension does not route traces or modify zones, keepouts, or the
board outline.
Un cambio `F.Cu`/`B.Cu` viene eseguito soltanto nella copia temporanea attraverso l'API `pcbnew`
inclusa in KiCad, cosi pad, grafica, testi e modelli 3D vengono trasformati insieme. Preview, DRC,
conferma esplicita, snapshot e rollback restano obbligatori.

## Post-placement PCB grounding

Include the reviewed grounding request in `prepare_pcb_acceptance`. It targets every
pad in one or more reviewed ground domains and derives all zone outlines from the real closed
`Edge.Cuts`, pad geometry, fanouts, and vias. The default is always a two-copper-layer board:
`PGND` owns the main shaped region on `F.Cu`, while `GND` owns the main shaped region on `B.Cu`.
Planner-derived local regions on the opposite side are cut out of the main region with explicit
clearance and distinct priorities. A four-layer stackup is used only when
`copper_layers=4` is explicitly requested; that opt-in policy uses `PGND -> F.Cu/In2.Cu` and
`GND -> In1.Cu/B.Cu` for an unambiguous pair.

The domains remain distinct KiCad nets and are connected only through two-terminal bridges such as
a 0-ohm resistor or net tie explicitly named in `bridge_references`. Pads that do not touch their
assigned plane receive
typed, clearance-screened pad-to-via fanouts. `thermal` and `solid` pad connections are selectable
per domain; local shaped regions are solid and power ground commonly uses `solid`. Via-in-pad is opt-in because it requires a reviewed
fabrication process. Existing selected-net zones are replaced only when
`replace_existing_planes=true` is explicitly requested.

The result is validated inside the private aggregate workspace without a grounding preview. The
KiCad Project Manager may remain open: only a PCB document lock blocks the final aggregate
mutation. `rollback_accepted_phase` restores the aggregate PCB snapshot. Ambiguous
domain roles, overlapping layer assignments, missing/ambiguous bridges, unsafe fanouts, or new DRC
errors cause a structured refusal.
The workflow does not accept polygons or KiCad expressions from callers, and it does not claim
thermal, return-path, EMC, SI/PI, stackup, or DFM certification.

## Headless PCB initialization via MCP

For an empty board, the internal layout composer accepts a typed rectangular outline, one
placement for every physical schematic footprint, optional fixed M3 mounting holes, and explicit
footprint overrides. It synchronizes footprints, builds the unrouted PCB, and runs comparative
ERC/DRC and placement analysis inside the aggregate workspace without publishing an intermediate
preview. There is no separate layout preview or acceptance.

The bounded `test_bench_pico` reference helper demonstrates this flow for a provisional 12 V,
20 A brushed-DC H-bridge using DRV8701, four external 60 V MOSFETs, ATtiny1616, half-duplex
RS-485, and four protected 5–24 V digital sensor inputs. Its typed plan uses a compact 120 x 100 mm board,
four M3 holes, a star-separated `PGND`/logic `GND`, and a 70 um external-copper assumption. It is
a review benchmark, not a production-qualified design; high-current zones, thermal/EMC/DFM,
motor stall behavior, and the final stackup still require engineering validation.

The `benchmark_bldc_drv8311` reference project exercises the same guarded workflow on an
85 x 50 mm four-layer BLDC driver. It uses a DRV8311S integrated three-phase stage for a
provisional 9--12.6 V, 2 A continuous operating point, exposes 6-PWM, SPI, three current-sense
outputs, fault, and Hall signals, and includes input protection, decoupling, a reviewed In1.Cu GND
plane, thermal-pad via-in-pad fanout, and project-local rule classes. The generated BOM and design
report live below `benchmark_bldc_drv8311/copperbrain-output/`. It remains a benchmark, not a
production release: power/phase copper, thermal/SOA, EMC, DFM, and the remaining signal routing are
explicitly blocked by readiness evidence rather than being silently inferred.

## Controlled PCB routing via MCP

After rules and placement/grounding intent are reviewed, use `analyze_unrouted_nets`
to identify the remaining disconnected pad groups. Ground-zone connectivity is included, so the
router does not redundantly route pads already joined by an applied plane. Check
`get_routing_backend_status`, then call `propose_pcb_routing`. FreeRouting consumes KiCad's
official Specctra DSN and runs at most three isolated attempts: prioritized, sequential, and a
single-thread prioritized configuration. Three attempts are used by default and a request for a
fourth is rejected. Copperbrain ranks the candidates by completion, new DRC errors, open
connections, vias, and routed length. Only the selected
candidate's copper delta becomes typed segments and vias; there is no internal routing fallback.
Reviewed grounding domains are omitted only from FreeRouting's private DSN input so plane copper
does not obstruct signal search. Their exact netlist remains present, and the selected typed copper
is applied to the already-grounded private board, followed by KiCad zone refill and comparative DRC.
Fine-pitch escape geometry is opt-in per routing batch. When enabled, Copperbrain derives short
typed stubs from pad and courtyard geometry and, for a nearby opposite-side target, a clearance-
offset dogbone via with an outside-courtyard approach. It includes these seeds in the private router
input and subjects the resulting complete copper delta to the same scope, preservation,
connectivity, and DRC gates.
When `nets` is empty, Copperbrain materializes the exact currently-unrouted net set before DSN
export. FreeRouting retains non-target net and plane geometry but moves those nets into generated
preserve classes during scope validation. The upstream FreeRouting 2.2.4 CLI parses `-inc` but does
not propagate it to the loaded headless board; Copperbrain therefore refuses a scoped run before
Java starts unless the selected JAR has a hash-bound capability record proving that headless class
exclusion works. Every imported result is rejected if it removes existing copper or adds copper
outside the requested net set.

Pass the reviewed routing requests with placement and grounding to `prepare_pcb_acceptance`.
Copperbrain stops after the bounded candidate comparison, applies only the best candidate to the
private prepared PCB, refills zones, rechecks connectivity, and runs comparative DRC inside one
private workspace without routing previews. It does not keep retrying to obtain a complete route.
Final engineering cleanup and the explicit decision to apply the prepared PCB remain with the
user. Only the complete aggregate publishes
`copperbrain-output/previews/pcb/` and can be applied by `accept_pcb` with the third acceptance and
a closed editor. The KiCad Project Manager may remain open; only a PCB-document lock blocks the
aggregate apply/rollback. Boards containing pre-existing copper are rejected by default; explicitly select the
`preserve` policy only for intentional incremental routing. A wall-time/stall watchdog also stops
known FreeRouting normalization loops and cleans up the Java process tree.
Specctra coordinate round trips are matched with a 1 um tolerance when proving that existing copper
was preserved; the typed delta is still applied to the original-precision project copy.
Each proposal writes versioned baseline and per-candidate JSON metrics below the private
`COPPERBRAIN_DATA_DIR/metrics/connectivity/` tree. FreeRouting progress records retain bounded
per-pass board totals, actual queued items, failures, duration, score, CPU, allocated memory, and
normalization evidence. The returned
`RoutingPlan.metrics_run_id` correlates those records with preview, apply, and recovery.
Call `get_connectivity_metrics` with that ID for a bounded, sanitized optimization view including
the best observed pass, connection delta, failed-candidate count, stagnation, and watchdog causes.
New records use schema 4 while the reader remains compatible with persisted schema-2 records.

For the compact end-to-end surface, call `prepare_pcb_acceptance`, review its preview, then use
`validate_pcb_acceptance` and `accept_pcb`. `assess_pcb_readiness` intentionally keeps
`production_ready=false` when DFM, stackup,
thermal, SI/PI, EMC, or impedance checks have not been performed, even if routing/ERC/DRC pass.

The AI may explain and review the structured candidate evidence, but it cannot override
connectivity, DRC, confirmation, stale-hash, or editor-state gates. This workflow does not certify
impedance, SI/PI/EMC, thermal behavior, or regulatory compliance. Raw KiCad expressions, zones,
and keepouts remain unavailable through the routing tools.
