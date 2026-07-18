# Installation and MCP configuration

## Requirements

- Windows with Python 3.11 or newer and `uv`;
- KiCad 10.x (`kicad-cli.exe` is discovered dynamically);
- the managed KiCadRoutingTools runtime installed by the dependency setup command for controlled
  PCB routing;
- the official `kicad-python` binding is installed by `uv sync` for optional PCB IPC access;
- optional JLCImport and JLCPCB Tools installations;
- a local MCP client with stdio server support.

## Install

```powershell
git clone https://github.com/deadbringer17/Copperbrain.git
cd Copperbrain
uv sync --all-extras
uv run python scripts/setup_dependencies.py --skip-jlc-plugins
uv run pytest
```

Start the server with `uv run copperbrain`. Configure an MCP client to execute `uv` with
arguments `run copperbrain` and this repository as its working directory. Copperbrain exposes
local stdio only and never starts a public network listener.

## Updating a source checkout

Run the explicit maintenance command from any directory while using the source checkout's
environment:

```powershell
uv run --directory C:\path\to\Copperbrain copperbrain update
```

The command verifies that the package is running from a Git source checkout, the current branch is
`main`, the worktree is clean, and `origin` is the official Copperbrain GitHub repository. It then
fetches only `origin/main` and applies `git merge --ff-only origin/main` when the remote is ahead.
It reports an already-current or locally-ahead checkout without mutation and refuses unexpected
remotes, local changes, other branches, detached HEAD, and divergent history. It does not stash,
reset, rebase, delete, or resolve conflicts. Restart the MCP client after a successful update.

## Managed dependency setup

`scripts/setup_dependencies.py` fetches the routing runtime and optional integrations. It is never
run silently or through MCP; invoke it explicitly before starting the server:

```powershell
uv run python scripts/setup_dependencies.py
```

It downloads, over HTTPS from official sources only, verifying a published checksum whenever the
source provides one:

- the pinned, hash-verified KiCadRoutingTools 0.18.2 PCM release and platform-specific Rust core into
  `<COPPERBRAIN_DATA_DIR>/integrations/kicad-routing-tools/<version>/`;
- the JLCImport and JLCPCB Tools KiCad plugins (via KiCad's own official PCM repository metadata
  at `kicad.github.io`) into the detected KiCad user `3rdparty/plugins` directory.

The JLC plugin step is the only one that writes outside this repository: it installs into the
local KiCad installation's plugin directory, the same location KiCad's own Plugin and Content
Manager would use. The script prints exactly what it is about to do and asks for confirmation
first, unless `--yes` is passed; `--skip-routing`, `--skip-jlc-plugins`, and
`--data-dir`/`--kicad-plugin-dir` narrow or redirect it.

The release remains immutable in Copperbrain's versioned private integration directory. The setup
selects the matching prebuilt Rust core, creates its canonical import filename, and refuses an
unexpected archive layout or an incomplete existing target rather than patching it in place.

If KiCad's official addon repository changes shape or has no JLC listing at the time you run it,
the script reports the failure per-component and continues with the rest; install JLCImport or
JLCPCB Tools manually through KiCad's Plugin and Content Manager in that case.

## Configuration

| Environment variable | Purpose |
|---|---|
| `COPPERBRAIN_CACHE_DIR` | SQLite component-search cache and temporary downloads |
| `COPPERBRAIN_DATA_DIR` | Prepared workspaces and restorable snapshots |
| `COPPERBRAIN_ALLOWED_HOSTS` | Comma-separated HTTPS download allowlist |
| `COPPERBRAIN_JLC_CATALOG` | Normalized recorded JLC catalog JSON for deterministic/offline use |
| `COPPERBRAIN_KICAD_ROUTING_TOOLS_ROOT` | Optional explicit runtime root containing `route.py`, `VERSION`, and `rust_router/` |
| `COPPERBRAIN_KICAD_ROUTING_TOOLS_PYTHON` | Optional explicit Python interpreter for the backend; defaults to Copperbrain's interpreter |
| `COPPERBRAIN_ROUTING_TIMEOUT_SECONDS` | Hard wall-time limit for one autorouter candidate (default 900) |
| `COPPERBRAIN_ROUTING_STALL_SECONDS` | Stop a candidate with no bounded stdout/stderr progress (default 180) |

Copperbrain discovers the installed JLCPCB Tools FTS5 database under standard KiCad PCM user
directories and opens it read-only. `COPPERBRAIN_JLC_CATALOG` takes precedence for deterministic
recorded runs. When neither source is available, sourcing tools return an actionable
`integration_unavailable` error; Copperbrain does not scrape an undocumented endpoint silently.

For routing, Copperbrain searches only versioned runtime roots below
`<COPPERBRAIN_DATA_DIR>/integrations/kicad-routing-tools/` unless the explicit root override is set.
Use `get_routing_backend_status` to verify the release, Python interpreter, platform Rust core, and
KiCad bundled Python bridge before proposing a route. The integration is local and fixed-command
only; MCP callers supply typed routing intent, never executable names or arbitrary arguments.

## Mutation safety

The client must call `prepare_schematic_change`, review its semantic diff, risks and validation,
then call `accept_schematic` with `confirmed=true` and `editor_closed=true`. Copperbrain checks
source hashes again, refuses lock files, snapshots affected files and uses atomic replacement.
Recovery is an explicit `rollback_accepted_phase` invocation.

PCB placement operations feed directly into `prepare_pcb_acceptance`; placement, grounding, and
routing are applied to its private workspace without intermediate previews. For optional live IPC access, enable the API server
in KiCad preferences and open the intended board in PCB Editor; Copperbrain refuses an IPC board
whose resolved path does not match the expected temporary workspace file.

Empty-board initialization is an internal composition step driven by a typed complete placement
plan; it does not accept KiCad syntax or generate routing. Reviewed PCB work is published and
applied only through the aggregate PCB acceptance.

Controlled routing uses `get_routing_backend_status`, `analyze_unrouted_nets`,
`propose_pcb_routing`. The reviewed requests, placement, and grounding are composed by
`prepare_pcb_acceptance`, revalidated, and applied once by
`accept_pcb`. KiCadRoutingTools works only in private workspaces; Copperbrain passes an explicit,
nonempty net set, never permits ripping pre-existing routes, refuses changed existing copper, and
exposes typed segment/via deltas only. KiCad refills zones on both the candidate and prepared typed
copy before comparative DRC.
Prepare works on a private project copy and requires both complete selected-net connectivity and
comparative KiCad DRC before apply.
The aggregate apply and recovery retain closed-editor, stale-hash, snapshot, and atomic replacement
requirements.

Routing change manifests are stored under `COPPERBRAIN_DATA_DIR/routing-changes/`; their private
workspaces and snapshots allow the lifecycle to resume after an MCP restart. `copperbrain-output/`
copies are deliverables only and are rejected if passed back to `open_project` as sources.

## Output location

User-facing files never use an arbitrary external destination. Copperbrain creates these paths
inside the opened project:

```text
copperbrain-output/
  previews/schematic/         current schematic checkpoint and PDF
  previews/design-rules/      current rules checkpoint
  previews/pcb/               current aggregate PCB checkpoint and PDF
  bom/                        Copperbrain-BOM.json, .csv, and .md
```

`copperbrain-output/` is excluded from schematic discovery, source hashes, prepared workspace
inputs, and version control. `COPPERBRAIN_DATA_DIR` continues to contain private operational
workspaces and rollback snapshots. Published preview copies also omit VCS metadata, KiCad
history/backups, and editor lock files.
