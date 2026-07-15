# Installation and MCP configuration

## Requirements

- Windows with Python 3.11 or newer and `uv`;
- KiCad 10.x (`kicad-cli.exe` is discovered dynamically);
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

Copperbrain discovers the installed JLCPCB Tools FTS5 database under standard KiCad PCM user
directories and opens it read-only. `COPPERBRAIN_JLC_CATALOG` takes precedence for deterministic
recorded runs. When neither source is available, sourcing tools return an actionable
`integration_unavailable` error; Copperbrain does not scrape an undocumented endpoint silently.

## Mutation safety

The client must call `prepare_schematic_change`, review its semantic diff, risks and validation,
then call `apply_change` with both `confirmed=true` and `editor_closed=true`. Copperbrain checks
source hashes again, refuses lock files, snapshots affected files and uses atomic replacement.
Rollback requires a separate explicit confirmation.

## Output location

User-facing files never use an arbitrary external destination. Copperbrain creates these paths
inside the opened project:

```text
copperbrain-output/
  previews/<change-set-id>/   validated project copy and Copperbrain-preview.pdf
  bom/                        Copperbrain-BOM.json, .csv, and .md
```

`copperbrain-output/` is excluded from schematic discovery, source hashes, prepared workspace
inputs, and version control. `COPPERBRAIN_DATA_DIR` continues to contain private operational
workspaces and rollback snapshots. Published preview copies also omit VCS metadata, KiCad
history/backups, and editor lock files.
