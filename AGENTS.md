# Copperbrain Agent Guide

## Mission and Sources of Truth

Copperbrain is a Python 3.11+ local MCP server for safe, evidence-backed KiCad work: project
analysis, component sourcing, controlled schematic and PCB changes, ERC/DRC validation, placement,
grounding, routing, BOM estimation, preview, apply, and rollback.

Read `DEVELOPMENT_PLAN.md` before changing product contracts or architecture. Use
`docs/WORKSPACE_WIKI.md` to locate responsibilities and tests. This file defines the operational
rules agents must follow while working in the repository and on real KiCad projects; it is not a
progress tracker or a duplicate product specification.

## Architecture

Preserve this dependency direction:

```text
MCP tools -> application services -> KiCad/JLC/router adapters
```

- Keep MCP handlers thin. They validate inputs, call application services, and serialize structured
  results.
- Keep domain models and business rules independent from MCP transport, filesystem access, KiCad,
  `kicad-sch-api`, JLCImport, JLCPCB Tools, and routing backends.
- Put vendor-, EDA-, and router-specific behavior behind explicit adapters.
- Treat JLCImport, JLCPCB Tools, KiCad IPC, and external routers as optional integrations. Detect
  them at runtime; do not copy, patch, or depend on their installed source layout.
- Detect KiCad versions, executables, plugins, and user data directories dynamically. Never
  hard-code a developer's machine paths.
- Use typed Python throughout and Pydantic models at public MCP, configuration, metrics, and
  persistence boundaries.
- Return structured, actionable errors. Do not make callers parse log text to determine failure
  causes.
- Keep filtering, scoring, classification, candidate ranking, and readiness gates deterministic.
  An LLM may form requirements or explain evidence, but it must not silently override constraints.
- Attach source and UTC retrieval timestamps to external claims and UTC timestamps to operational
  evidence.
- Never expose arbitrary shell or command execution through an MCP tool.

## Real-Project Workflow

Treat every real project as a gated engineering workflow. Do not jump directly from a natural
language request to a source-file mutation, and do not treat a successful autorouter process as a
finished PCB.

### 1. Intake and baseline

- Detect the installed KiCad/runtime capabilities, open the exact source project, and reject
  generated output copies as source roots.
- Record source hashes, KiCad/backend versions, editor state, project summary, ERC/DRC baseline,
  PCB stackup, board outline, placement state, and connectivity baseline before proposing changes.
- Surface missing constraints and engineering assumptions before they affect component choice,
  layout, grounding, or routing.

### 2. Schematic and sourcing

- Analyze the existing circuit and normalize electrical, functional, mechanical, commercial, and
  sourcing requirements.
- Search and rank candidates deterministically, retain evidence and timestamps, and require an
  explicit component choice when alternatives materially differ.
- Import assets and apply schematic changes only through typed operations and the standard mutation
  workflow. Re-run semantic checks and ERC before application.
- Generate BOM and cost evidence separately from PCB, assembly, stencil, shipping, tax, and duty
  estimates.

### 3. PCB foundation and design rules

- Synchronize or inspect the PCB before placement. Establish the reviewed manufacturing profile,
  layer policy, netclasses, clearances, preferred widths, via constraints, and special-net intent.
- Refuse to infer high-current, high-voltage, controlled-impedance, or differential requirements
  from names alone when the required electrical data is absent.
- Validate rule changes in a private copy with KiCad DRC before application.

### 4. Placement

- Analyze placement before routing. Optimize for electrical topology, short critical connections,
  power-flow structure, routing corridors, connector/edge affinity, thermal intent, and
  manufacturability as well as compactness.
- Prepare, preview, validate, and explicitly apply placement changes before grounding or routing.
- Recompute placement and connectivity metrics after every accepted placement iteration. Prefer
  fixing a structurally poor placement over spending more autorouter time on it.

### 5. Grounding and power structure

- Perform grounding after placement and before general signal routing.
- Review exact ground domains, bridges, primary layers, local regions, fanouts, and vias. Do not
  merge domains or introduce via-in-pad/zone replacement implicitly.
- Re-run zone-aware connectivity and DRC after grounding. Nets already connected by reviewed copper
  regions must not be sent to the signal router again.
- Treat wide power paths, motor phases, shunts, return paths, and other special copper as dedicated
  engineering work. Do not mix them blindly into a generic signal-routing batch.

### 6. Controlled routing

- Start every routing attempt from a recorded connectivity baseline and an explicit, nonempty set
  of target nets.
- Classify targets before invoking FreeRouting:
  - route ordinary signals in small, homogeneous batches through the verified local autorouter;
  - keep high-current, high-voltage, impedance-sensitive, differential, timing-sensitive, and
    thermally constrained nets out of generic routing until their dedicated strategy is reviewed.
- Do not run whole-board monolithic autorouting by default. Prefer an initial single candidate and
  launch another strategy only when recorded evidence justifies the extra runtime.
- Establish wall-time, stall, normalization-loop, and semantic-stagnation budgets before each run.
  A process that remains active without reducing open connections is stalled for workflow purposes.
- After every batch, extract only typed copper deltas, verify that existing copper was not removed,
  rerun connectivity and comparative KiCad DRC, and record the metrics described below.
- Keep a candidate only when it produces a measurable accepted improvement without new blocking
  violations. Stop, repartition the nets, or return to placement/rules when repeated attempts do not
  improve the best result.
- Never introduce an implicit routing fallback. FreeRouting capability, incremental-copper policy,
  and any partial-result acceptance must be explicit and visible in the evidence.

### 7. Final validation and readiness

- Revalidate the prepared workspace immediately before apply. Require the selected connectivity
  policy, structural parsing, and KiCad DRC gates to pass.
- Separate electrical connectivity from production readiness. A connected, DRC-accepted PCB is not
  automatically validated for SI, PI, EMC, thermal behavior, impedance, creepage certification,
  stackup, DFM, assembly, or regulatory compliance.
- Publish review artifacts, preserve a restorable snapshot, apply atomically after explicit
  confirmation, and keep rollback available.

## Mandatory Connectivity and Routing Metrics

Every test or real-project operation that analyzes, attempts, changes, or validates electrical
connectivity must emit reusable structured metrics. This includes unit/integration/E2E tests,
  manual real-board experiments, grounding checks, autorouter candidates and attempts,
prepared routing validation, apply verification, failures, watchdog stops, timeouts, and cancelled
runs.

- Write one versioned JSON or JSONL record per attempt and phase. Human-readable logs may accompany
  it, but they are not the source of truth.
- Store runtime records under Copperbrain's private data directory, grouped below
  `metrics/connectivity/`; tests must use their temporary directory. Metrics are operational state,
  not user-facing deliverables, and must never be written into the source project, committed, or
  used as project input.
- Assign a stable `run_id` and optional `parent_run_id` so baseline, candidate, validation, apply,
  and rollback records can be correlated across process restarts.
- Emit a record on success and on every failure path. Flush the best-known metrics before returning
  a structured error or terminating a backend process.
- Capture at least:
  - schema version, run/parent identifiers, operation/test kind, phase, UTC start/end, duration,
    outcome, and structured error/watchdog reason;
  - anonymized project fingerprint, source hashes, board dimensions, copper-layer count, footprint
    and pad counts, and relevant density/placement context;
  - KiCad version, backend name/version, strategy, effective configuration, resource/time budgets,
    requested net count and deterministic net-role categories;
  - baseline and final routed/unrouted net counts, open-connection counts, accepted deltas, segment
    count, via count, and routed length;
  - baseline/final/new DRC errors and warnings;
  - when available, pass number, board-wide incomplete count, actual queued item count, best pass,
    open connections per pass, failed-route count, normalization count, stagnation count, CPU time,
    and peak memory. Keep board-wide totals distinct from the scoped work queue.
- Do not record full customer project contents, unrestricted DSN/SES text, secrets, or sensitive net
  names. Prefer aggregate counts, deterministic categories, and hashed identifiers; retain exact
  names only for repository fixtures or when the user explicitly authorizes diagnostic evidence.
- Bound log size, sanitize backend output, retain only useful tails/raw artifacts, and apply an
  explicit retention policy. A noisy backend log must not become an unbounded operational failure.
- Benchmark and regression reports must be derived from these records rather than hand-copied
  observations. Any routing optimization is incomplete until its before/after metrics are
  comparable on the same recorded corpus.

## KiCad Mutation Safety

The LLM must never write KiCad S-expressions directly. All schematic and PCB changes must use
validated domain operations and follow this workflow:

```text
prepare -> preview -> explicit confirmation -> validate -> apply
```

- A prepared change set must identify operations, affected files, source hashes, semantic diff,
  risks, validation results, and relevant metrics run identifiers.
- Require explicit user confirmation for every change set. Selecting a project does not authorize
  silent mutations.
- Perform mutations in a temporary workspace first.
- Before applying, verify that source hashes still match and refuse stale changes.
- Require a restorable snapshot, appropriate ERC/DRC validation, atomic file replacement, and
  rollback support for every applied mutation.
- Refuse to write if the relevant KiCad editor has unsaved changes or safe editor state cannot be
  established.
- Never use live user projects as mutable test fixtures. Tests may modify only repository fixtures
  and temporary copies.
- Prefer a safe refusal with useful evidence over a partially applied or weakly validated change.

## External Data, Logs, and Secrets

- Restrict downloads to configured host allowlists and enforce connection/read timeouts and
  response-size limits.
- Validate identifiers, URLs, content types, and destination paths before downloading or extracting
  files.
- Treat vendor search endpoints as unstable. Isolate them behind adapters, cache defensively, and
  preserve recorded responses for deterministic tests.
- Never commit API keys, credentials, tokens, local user paths, caches, runtime metrics, downloaded
  proprietary data, generated fabrication outputs, or real customer projects.
- Do not log secrets or full sensitive project contents. Use structured logging with the minimum
  context required to reproduce and compare behavior.
- Cost reports must distinguish component estimates from PCB, assembly, stencil, shipping, taxes,
  and duties, and must state pricing timestamps and assumptions.

## Project Output Policy

- Save every user-facing artifact under the opened KiCad project's `copperbrain-output/` directory.
  Use category subdirectories such as `previews/` and `bom/`; never accept an arbitrary external
  destination for deliverables.
- Prepared preview copies and PDFs belong in `copperbrain-output/previews/<change-set-id>/`. BOM
  JSON, CSV, and Markdown belong in `copperbrain-output/bom/`.
- Keep mutation workspaces, metrics, caches, downloads, and restorable snapshots in Copperbrain's
  private data/cache directories. They are operational state, not deliverable output.
- Exclude `copperbrain-output/` from project discovery, source hashes, temporary workspace copies,
  validation inputs, and tests. Exclude VCS metadata, KiCad history/backups, and editor locks from
  published preview copies. An output copy must never be treated as a source project.
- Publish outputs atomically and keep `copperbrain-output/` ignored by version control unless the
  user explicitly asks to track a specific artifact.

## Python Tooling and Validation

Use `uv` with `pyproject.toml`. Commit `uv.lock` and keep dependency changes intentional and
reproducible. The standard full validation sequence is:

```text
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

- Run the narrowest relevant tests during iteration, then the full applicable validation suite
  before handoff.
- Do not run formatters in rewrite mode across unrelated user changes.
- Keep unit tests offline by default.
- Use recorded JLC and router responses for deterministic integration tests. Mark live network and
  long-running router tests explicitly and exclude them from the default test run.
- Use golden-file tests for mutations and verify byte-for-byte rollback.
- Cover Windows paths, spaces in installation paths, missing plugins, multiple KiCad versions,
  unavailable services, backend timeouts, stalls, partial results, and restart recovery.
- Maintain KiCad 10.0.1 compatibility during development and perform final verification against
  KiCad 10.0.4.
- Add or update tests, metrics assertions, and documentation whenever a public MCP tool, model, or
  behavior changes.

## Working in the Repository

- Inspect repository status and nearby code before editing. Preserve unrelated user changes and
  follow established patterns when they exist.
- Work in small vertical slices that leave the repository testable.
- Maintain `docs/WORKSPACE_WIKI.md` in parallel with every repository change. A code,
  configuration, test, or structural change is incomplete until the wiki reflects it in the same
  change set.
- Use the workspace wiki as a fast navigation map, not as a second product specification. Document
  responsibilities, important entry points, subsystem relationships, common change locations, and
  covering tests.
- Keep wiki entries path-oriented and concise. Add new paths when relevant, update responsibilities
  when code moves, and remove stale references when files are deleted.
- When beginning work, consult the wiki to identify likely files, then verify its claims against the
  repository. If the wiki is missing or stale, repair it before handoff.
- Avoid leaking adapter types into application or domain layers.
- Prefer explicit configuration and dependency injection over global state and implicit environment
  behavior.
- Update `DEVELOPMENT_PLAN.md` only when an approved product or architectural decision changes; do
  not rewrite it to match an implementation shortcut.
- Do not commit or push unless the user explicitly requests it.
- Never use destructive Git operations, discard user work, or rewrite history without explicit
  authorization.

## Operational Boundaries

- No automatic purchasing or order placement.
- No silent schematic, PCB, placement, grounding, rule, or routing mutations.
- No unrestricted autonomous generation of arbitrary complex circuits.
- No arbitrary commands or publicly exposed MCP network transport.
- No claim of complete manufacturing cost or production readiness without the missing fabrication,
  assembly, stackup, thermal, SI/PI, EMC, DFM, test, and compliance evidence.

When a request crosses these boundaries, explain the limitation and propose the smallest safe,
reviewable alternative.
