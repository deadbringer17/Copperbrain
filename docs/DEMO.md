# Reproducible MVP demo

Run the offline reference flow:

```powershell
uv run python scripts/run_demo.py
```

The script copies the KiCad 10 fixture to a temporary directory, marks its input as 12 V, ranks a
timestamped LM2596 LCSC candidate, and prepares a bounded 5 V / 2 A buck section with regulator,
Schottky diode, inductor, capacitors, input/output connectors, labels, and wiring. It confirms the
change, calculates the priced component portion of the BOM for 100 boards, reports missing price
lines, then verifies byte-exact rollback. No repository fixture or user project is modified.

For the interactive MCP demo, follow the same sequence:

1. `detect_kicad`, then `open_project` and `get_project_summary`;
2. `analyze_schematic` and `run_erc`;
3. `search_components`, `compare_components`, and explicit candidate selection;
4. `import_component_assets` from resolved local/allowlisted assets;
5. `prepare_schematic_change`, review, `validate_change`, then explicitly confirm `apply_change`;
6. `generate_bom` and `estimate_bom_cost` for 10 and 100 boards;
7. demonstrate `rollback_change` with explicit confirmation.

To demonstrate the PCB-rule extension:

1. call `analyze_pcb_constraints` and review each suggested role and its connected references;
2. call `propose_design_rules` with fabrication minima and explicit current/voltage intent;
3. review the assumptions, semantic diff, generated preview, and temporary DRC returned by
   `prepare_pcb_rule_change`;
4. rerun `validate_pcb_rule_change`, save and close KiCad, then explicitly confirm
   `apply_pcb_rule_change`;
5. call `run_drc` on the live project and optionally prove snapshot restoration with
   `rollback_pcb_rule_change`.

The extension configures routing constraints only. It neither routes nor rewrites PCB tracks.

`prepare_schematic_change` returns the project-local preview directory under
`copperbrain-output/previews/<change-set-id>/`, including `Copperbrain-preview.pdf`.
`generate_bom` writes all three formats to `copperbrain-output/bom/` by default. If one format is
requested, an optional destination is interpreted only as a simple filename in that same folder.

Cost reports cover components only and explicitly exclude PCB, assembly, stencil, shipping,
taxes, and duties.
