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
5. `prepare_schematic_change`, review, `validate_change`, then use `accept_schematic`;
6. `generate_bom` and `estimate_bom_cost` for 10 and 100 boards;
7. demonstrate recovery with `rollback_accepted_phase`.

For a brand-new project, first call `prepare_project_creation`, review the generated project copy
and validation report, rerun `validate_project_creation`, then use `accept_schematic` with
`change_kind=project_creation`. The target directory may contain only Copperbrain's preview output;
existing source files or unrelated user files cause a safe refusal.

For the bounded motor benchmark, prepare `motor_driver_bench_operations`, then propose the rule
set from `motor_driver_bench_manufacturing_profile` and
`motor_driver_bench_rule_requirements`, and initialize the empty PCB with
`motor_driver_bench_layout_plan`. Review both PDFs and the 20 A/2 oz assumptions before any apply.
The reference intentionally leaves routing open if FreeRouting cannot satisfy the reviewed rules;
never reduce the current constraint merely to obtain a complete candidate.

To demonstrate the PCB-rule extension:

1. call `analyze_pcb_constraints` and review each suggested role and its connected references;
2. call `propose_design_rules` with fabrication minima and explicit current/voltage intent;
3. review the assumptions, semantic diff, generated preview, and temporary DRC returned by
   `prepare_pcb_rule_change`;
4. rerun `validate_pcb_rule_change`, save and close KiCad, then use `accept_design_rules`;
5. call `run_drc` on the live project and optionally prove snapshot restoration with
   `rollback_accepted_phase` with `phase=design_rules`.

The extension configures routing constraints only. It neither routes nor rewrites PCB tracks.

To demonstrate controlled PCB routing after rules and placement are ready:

1. call `get_routing_backend_status` and verify Java 25+, FreeRouting, KiCad Python, and
   `scoped_routing_supported=true` for a subset run;
2. call `analyze_unrouted_nets` and review the disconnected pad groups;
3. call `propose_pcb_routing` with exact nets or an empty list for every routable net, then compare
   completion, DRC regression, open-connection, via, length, and per-pass work-queue metrics for the candidates; a
   non-empty list is enforced in the exported DSN so unrelated nets are not routed silently;
4. pass the selected typed plan to `prepare_routing_change` and review the PDF, semantic diff,
   connectivity result, assumptions, and comparative DRC;
5. combine the reviewed routing batches with grounding in `prepare_pcb_acceptance`, inspect its
   single preview, and rerun `validate_pcb_acceptance`;
6. save and close PCB Editor, call `accept_pcb`, then optionally demonstrate byte-exact recovery
   with `rollback_accepted_phase` and `phase=pcb`.

The generated geometry is not an SI/PI/EMC, thermal, impedance, or regulatory certification.

`prepare_schematic_change` returns the project-local preview directory under
`copperbrain-output/previews/<change-set-id>/`, including `Copperbrain-preview.pdf`.
`generate_bom` writes all three formats to `copperbrain-output/bom/` by default. If one format is
requested, an optional destination is interpreted only as a simple filename in that same folder.

Cost reports cover components only and explicitly exclude PCB, assembly, stencil, shipping,
taxes, and duties.
