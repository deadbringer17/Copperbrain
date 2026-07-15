# Copperbrain Agent Guide

## Mission and Source of Truth

Copperbrain is a Python 3.11+ local MCP server for safe KiCad schematic analysis, JLCPCB/LCSC component sourcing, controlled schematic changes, ERC validation, and BOM cost estimation.

Read `DEVELOPMENT_PLAN.md` before making architectural or product decisions. It is the source of truth for the MVP scope, tool contracts, data flow, milestones, and acceptance criteria. Keep this file focused on operational rules; do not duplicate the development plan here.

## Architecture

Preserve this dependency direction:

```text
MCP tools -> application services -> KiCad/JLC adapters
```

- Keep MCP handlers thin. They validate inputs, call application services, and serialize structured results.
- Keep domain models and business rules independent from MCP transport, filesystem access, KiCad, `kicad-sch-api`, JLCImport, and JLCPCB Tools.
- Put all vendor- or tool-specific behavior behind explicit adapters.
- Treat JLCImport and JLCPCB Tools as optional external integrations. Detect their availability at runtime; do not copy, patch, or depend on their installed source layout.
- Detect KiCad versions, executables, plugins, and user data directories dynamically. Never hard-code a developer's machine paths.
- Use typed Python throughout and Pydantic models at public MCP, configuration, and persistence boundaries.
- Return structured, actionable errors. Do not make callers parse log text to determine failure causes.
- Keep component filtering, scoring, and ranking deterministic. An LLM may form requirements or explain results, but it must not silently override deterministic constraints.
- Attach source and UTC retrieval timestamps to prices, stock, datasheets, and other external claims.
- Never expose arbitrary shell or command execution through an MCP tool.

## KiCad Mutation Safety

The LLM must never write KiCad S-expressions directly. All schematic changes must use validated domain operations and follow this workflow:

```text
prepare -> preview -> explicit confirmation -> validate -> apply
```

- A prepared change set must identify its operations, affected files, source hashes, semantic diff, risks, and validation result.
- Require explicit user confirmation for every change set. Selecting a project does not authorize silent mutations.
- Perform mutations in a temporary workspace first.
- Before applying, verify that source hashes still match and refuse stale changes.
- Require a restorable snapshot, ERC validation, atomic file replacement, and rollback support for every applied mutation.
- Refuse to write if the schematic has unsaved editor changes or if safe editor state cannot be established.
- Never use live user projects as test fixtures. Tests may modify only repository fixtures and temporary copies.
- Prefer a safe refusal with a useful explanation over a partially applied change.

## External Data and Secrets

- Restrict downloads to configured host allowlists and enforce connection/read timeouts and response-size limits.
- Validate identifiers, URLs, content types, and destination paths before downloading or extracting files.
- Treat vendor search endpoints as unstable. Isolate them behind adapters, cache defensively, and preserve recorded responses for deterministic tests.
- Never commit API keys, credentials, tokens, local user paths, caches, downloaded proprietary data, generated fabrication outputs, or real customer projects.
- Do not log secrets or full sensitive project contents. Use structured logging with minimal necessary context.
- Cost reports must distinguish component estimates from PCB, assembly, stencil, shipping, taxes, and duties, and must state their pricing timestamp and assumptions.

## Project Output Policy

- Save every user-facing artifact under the opened KiCad project's `copperbrain-output/` directory. Use category subdirectories such as `previews/` and `bom/`; never accept an arbitrary external destination for deliverables.
- Prepared preview copies and their PDFs belong in `copperbrain-output/previews/<change-set-id>/`. BOM JSON, CSV, and Markdown belong in `copperbrain-output/bom/`.
- Keep mutation workspaces, caches, downloads, and restorable snapshots in Copperbrain's private data/cache directories. They are operational state, not deliverable output.
- Exclude `copperbrain-output/` from project discovery, source hashes, temporary workspace copies, validation inputs, and tests. Exclude VCS metadata, KiCad history/backups, and editor locks from published preview copies. An output copy must never be treated as a source schematic.
- Publish outputs atomically and keep `copperbrain-output/` ignored by version control unless the user explicitly asks to track a specific artifact.

## Python Tooling and Commands

Use `uv` with `pyproject.toml`. Commit the `uv.lock` file and keep dependency changes intentional and reproducible. Once the scaffold exists, the standard validation sequence is:

```text
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

- Run the narrowest relevant tests during iteration, then the full applicable validation suite before handoff.
- Do not run formatters in rewrite mode across unrelated user changes.
- Keep unit tests offline by default.
- Use recorded JLC responses for deterministic integration tests. Mark live network tests explicitly and exclude them from the default test run.
- Use golden-file tests for schematic modifications and verify byte-for-byte rollback.
- Cover Windows paths, spaces in installation paths, missing plugins, multiple KiCad versions, and unavailable network services.
- Maintain KiCad 10.0.1 compatibility during development and perform final verification against KiCad 10.0.4.
- Add or update tests and documentation whenever a public MCP tool, model, or behavior changes.

## Working in the Repository

- Inspect repository status and nearby code before editing. Preserve unrelated user changes and follow established patterns when they exist.
- Work in small vertical slices that leave the repository testable.
- Maintain `docs/WORKSPACE_WIKI.md` in parallel with every repository change. A code, configuration, test, or structural change is incomplete until the wiki reflects it in the same change set.
- Use the workspace wiki as a fast navigation map, not as a second product specification. Document where responsibilities live, important entry points, subsystem relationships, common change locations, and the tests that cover them.
- Keep wiki entries path-oriented and concise. Add new files and directories when they become relevant, update responsibilities when code moves, and remove stale references when files are deleted.
- When beginning work, consult the wiki to identify likely files, then verify its claims against the repository. If the wiki is missing or stale, repair it as part of the current change before handoff.
- Avoid leaking adapter types into application or domain layers.
- Prefer explicit configuration and dependency injection over global state and implicit environment behavior.
- Update `DEVELOPMENT_PLAN.md` only when an approved product or architectural decision changes; do not rewrite it to match an implementation shortcut.
- Do not commit or push unless the user explicitly requests it.
- Never use destructive Git operations, discard user work, or rewrite history without explicit authorization.

## MVP Boundaries

The following are out of scope unless the development plan is explicitly revised:

- PCB autorouting;
- automatic purchasing or order placement;
- complete manufacturing quotations;
- unrestricted autonomous circuit generation;
- publicly exposed MCP network transport;
- silent schematic mutations.

When a request crosses these boundaries, explain the limitation and propose the smallest safe in-scope alternative.
