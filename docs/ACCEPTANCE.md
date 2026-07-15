# MVP acceptance evidence

This checklist maps the approved acceptance criteria to executable evidence. External plugin and
KiCad checks skip only when that optional installation is absent; on the development workstation
KiCad 10, JLCImport, and JLCPCB Tools are all detected and exercised.

| Criterion | Evidence |
|---|---|
| Local MCP client can discover the server | `tests/test_server.py` verifies all 19 MVP plus 7 PCB-rule FastMCP tools; `copperbrain` starts stdio only |
| KiCad 10.x and both JLC plugins are detected | `tests/adapters/test_kicad_detection.py`; real detection reports KiCad 10, JLCImport, and JLCPCB Tools |
| Existing project analyzed read-only | `tests/integration/test_kicad_workflow.py` exports a real KiCad 10 netlist without touching the source |
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
