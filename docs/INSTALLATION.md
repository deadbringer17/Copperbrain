# Installation and MCP configuration

## Requirements

- Windows with Python 3.11 or newer and `uv`;
- KiCad 10.x (`kicad-cli.exe` is discovered dynamically);
- Java 25 or newer and a local FreeRouting JAR for controlled PCB routing;
- the official `kicad-python` binding is installed by `uv sync` for optional PCB IPC access;
- optional JLCImport and JLCPCB Tools installations;
- a local MCP client with stdio server support.

## Install

```powershell
git clone https://github.com/deadbringer17/Copperbrain.git
cd Copperbrain
uv sync --all-extras
uv run pytest
```

Start the server with `uv run copperbrain`. Configure an MCP client to execute `uv` with
arguments `run copperbrain` and this repository as its working directory. Copperbrain exposes
local stdio only and never starts a public network listener.

## Configuration

| Environment variable | Purpose |
|---|---|
| `COPPERBRAIN_CACHE_DIR` | SQLite component-search cache and temporary downloads |
| `COPPERBRAIN_DATA_DIR` | Prepared workspaces and restorable snapshots |
| `COPPERBRAIN_ALLOWED_HOSTS` | Comma-separated HTTPS download allowlist |
| `COPPERBRAIN_JLC_CATALOG` | Normalized recorded JLC catalog JSON for deterministic/offline use |
| `COPPERBRAIN_FREEROUTING_JAR` | Optional explicit path to the local FreeRouting JAR |
| `COPPERBRAIN_FREEROUTING_JAVA` | Optional explicit path to a Java 25+ executable |
| `COPPERBRAIN_FREEROUTING_TIMEOUT_SECONDS` | Hard wall-time limit for one autorouter candidate (default 900) |
| `COPPERBRAIN_FREEROUTING_STALL_SECONDS` | Stop a candidate with no log/session progress (default 180) |
| `COPPERBRAIN_FREEROUTING_NORMALIZATION_LIMIT` | Stop repeated known normalization-loop messages (default 100) |

Copperbrain discovers the installed JLCPCB Tools FTS5 database under standard KiCad PCM user
directories and opens it read-only. `COPPERBRAIN_JLC_CATALOG` takes precedence for deterministic
recorded runs. When neither source is available, sourcing tools return an actionable
`integration_unavailable` error; Copperbrain does not scrape an undocumented endpoint silently.

For routing, Copperbrain also searches
`<COPPERBRAIN_DATA_DIR>/integrations/freerouting/freerouting*.jar` and
`<COPPERBRAIN_DATA_DIR>/integrations/java/**/bin/java.exe`, then installed KiCad/plugin locations
and `PATH`. FreeRouting 2.2.4 requires Java 25. Use `get_routing_backend_status` to verify the JAR,
Java major version, and KiCad bundled Python bridge before proposing a route. The integration is
local and fixed-command only; Copperbrain neither downloads runtimes silently nor exposes shell
arguments through MCP.

Scoped routing additionally requires `<jar-name>.capabilities.json` beside the JAR. The record must
contain the exact lowercase SHA-256 in `jar_sha256` and `scoped_net_classes_cli=true`; a missing,
invalid, or stale record causes a fail-safe refusal before Java starts. Automatic discovery prefers
a hash-verified scoped-capable JAR. `get_routing_backend_status` exposes the selected capability
path and verification result.

## Mutation safety

The client must call `prepare_schematic_change`, review its semantic diff, risks and validation,
then call `apply_change` with both `confirmed=true` and `editor_closed=true`. Copperbrain checks
source hashes again, refuses lock files, snapshots affected files and uses atomic replacement.
Rollback requires a separate explicit confirmation.

PCB placement uses the same safety contract through `prepare_placement_change`,
`validate_placement_change`, `apply_placement_change`, and `rollback_placement_change`. PDF
preview and DRC work without an open editor. For optional live IPC access, enable the API server
in KiCad preferences and open the intended board in PCB Editor; Copperbrain refuses an IPC board
whose resolved path does not match the expected temporary workspace file.

Empty-board initialization uses `prepare_pcb_layout_change`, `validate_pcb_layout_change`,
`apply_pcb_layout_change`, and `rollback_pcb_layout_change`. Its input is a typed complete placement
plan; it does not accept KiCad syntax or generate routing. The same confirmation, editor-state,
hash, snapshot, atomic replacement, and rollback requirements apply.

Controlled routing uses `get_routing_backend_status`, `analyze_unrouted_nets`, `propose_pcb_routing`,
`prepare_routing_change`, `validate_routing_change`, `apply_routing_change`, and
`rollback_routing_change`. FreeRouting works only in private DSN/SES workspaces; Copperbrain
imports its result, refuses changed existing copper, and exposes typed segment/via deltas only.
KiCad refills zones on both the imported candidate and prepared typed copy before comparative DRC;
Specctra-only coordinate rounding is matched within 1 um without replacing source-precision copper.
Prepare works on a private project copy and requires both complete selected-net connectivity and
comparative KiCad DRC before apply.
Apply and rollback retain the same explicit confirmation, closed-editor, stale-hash, snapshot,
and atomic replacement requirements.

Routing change manifests are stored under `COPPERBRAIN_DATA_DIR/routing-changes/`; their private
workspaces and snapshots allow the lifecycle to resume after an MCP restart. `copperbrain-output/`
copies are deliverables only and are rejected if passed back to `open_project` as sources.

## Output location

User-facing files never use an arbitrary external destination. Copperbrain creates these paths
inside the opened project:

```text
copperbrain-output/
  previews/<change-set-id>/   validated project copy and schematic/PCB preview PDF
  bom/                        Copperbrain-BOM.json, .csv, and .md
```

`copperbrain-output/` is excluded from schematic discovery, source hashes, prepared workspace
inputs, and version control. `COPPERBRAIN_DATA_DIR` continues to contain private operational
workspaces and rollback snapshots. Published preview copies also omit VCS metadata, KiCad
history/backups, and editor lock files.
