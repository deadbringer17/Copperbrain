# Copperbrain — Devpost "About the project" story

*Draft for the Devpost submission form. Solo entry, three-day build. Markdown + LaTeX below, ready to paste into the "About the project" box.*

---

## Inspiration

I design hardware for a living, and every "AI that designs your PCB" demo I'd seen worked the same way: point an LLM at a `.kicad_sch` file and let it write S-expressions directly. It looks magical in a five-minute video, and it is completely unacceptable the moment a real board is on the other end of it. I've spent enough hours tracing a shorted ground pour or a silently-dropped net back to a "small" manual edit to know exactly what that failure mode looks like at 2 a.m. before a fab run — I wasn't going to let a model reproduce it faster.

So I picked the opposite bet for this hackathon, working solo: what if the model was never allowed to touch a KiCad file directly, and *that constraint* became the entire product? Copperbrain started from one question I could actually answer from experience — can an agent be genuinely useful for schematic and PCB work if every action it proposes has to survive a strict `prepare → preview → explicit confirmation → validate → apply` gate, with byte-exact rollback always on the table? The engineering judgment about *where* those gates need to bite — ground topology, current sizing, when an autorouter result isn't actually done — is the part of this project that came straight out of my day job, not out of a spec sheet.

## What it does

Copperbrain is a local MCP server that gives an AI agent structured, typed hands on a KiCad 10 project instead of a text editor. Through it, an agent can:

- open and analyze an existing schematic — components, pins, nets, power rails, ERC — without changing a single byte;
- translate a natural-language request ("add a 5 V / 2 A buck section to this 12 V board") into normalized electrical and commercial requirements;
- search, filter, and rank real JLCPCB/LCSC candidates by stock, price break, and Basic/Extended status, then import the winning symbol, footprint, 3D model, and datasheet;
- propose a schematic change as a typed diff, show a PDF preview, run ERC before and after, and only mutate the real file after I explicitly confirm it;
- generate a BOM with LCSC/MPN metadata and priced cost estimates at multiple quantities;
- move into the PCB itself: propose manufacturing-aware design rules, analyze and optimize component placement, build ground planes from typed domain definitions, and hand a bounded net set to KiCadRoutingTools for controlled, evidence-ranked routing;
- roll back any of the above, byte-for-byte, at any point.

Nothing here is unbounded circuit generation. Copperbrain refuses to guess at current, voltage, or impedance intent it wasn't given, and it will not claim a board is "production ready" while thermal, EMC, SI/PI, DFM, or stackup review is still outstanding — because I know from professional practice that's exactly the corner an automated tool is tempted to cut silently.

## How I built it

The stack is Python 3.11 with the official MCP SDK and FastMCP as a thin tool layer over a set of application services — project analysis, sourcing, schematic mutation, PCB rules, placement, grounding, and routing — that never talk to MCP, KiCad, or vendor APIs directly. Every public contract is a Pydantic model; no tool anywhere on the public boundary accepts raw KiCad S-expressions or free-text `.kicad_dru` syntax.

Under the services sit adapters I treat as swappable and optional, detected at runtime rather than hard-coded:

- `kicad-sch-api` for schematic reads/writes, `kicad-cli` for ERC/DRC, and the official `pcbnew`/`kicad-python` IPC binding for anything that needs to transform pads, footprints, and 3D models together (side flips, rotations);
- a JLCImport/JLCPCB Tools adapter for component search, pricing, and BOM enrichment;
- KiCadRoutingTools, driven headlessly through a fixed Python CLI and Rust-accelerated A* core, with my own scope, delta-validation, watchdog, and ranking layer around its candidates.

Every mutation — schematic patch, rule change, placement move, grounding pass, routing batch, even brand-new project creation — goes through the same private-workspace-first pipeline: copy, mutate, comparative ERC/DRC, PDF preview, source-hash check, explicit confirmation, atomic apply, restorable snapshot. Working alone against the clock, I used that one pipeline as leverage: build it once, correctly, and every new capability (rules, placement, grounding, routing) became "one more typed operation behind the same gate" instead of a new class of risk. I proved it end to end on three real reference boards built in three days: a 12 V/20 A brushed-DC H-bridge driver, a compact DRV8311S three-phase BLDC driver, and a 12 V→48 V boost converter — the kind of boards I'd normally lay out by hand on the job.

For nets that do need a calculated copper width, I lean on the standard IPC-2221 approximation for external layers:

$$
I = k \cdot \Delta T^{0.44} \cdot A^{0.725}
$$

where $I$ is current in amps, $\Delta T$ the allowed temperature rise in °C, $A$ the cross-sectional area in mils², and $k \approx 0.048$ for external copper — solved for $A$ (and then width, given copper weight) once the caller supplies current and temperature rise. Copperbrain treats this as a conservative sizing estimate, never a certification, the same way I would on paper before committing to a stackup.

## Challenges I ran into

- **Router scope must be independently enforced.** Copperbrain materializes a nonempty reviewed net set, supplies it through fixed KiCadRoutingTools `--nets` arguments, never enables ripping of pre-existing routes, then independently rejects removed copper or any new copper outside the selected set.
- **Ground is not one net.** Splitting `PGND` and `GND` into distinct shaped regions that never short together, connected only through explicitly reviewed two-terminal bridges, took more modeling time than the routing logic itself — you can't infer a DC bridge just because a two-pin part happens to touch both planes, and I've seen what happens on a real board when that assumption is wrong.
- **Autorouters lie to themselves too.** A router can stay alive without producing useful progress. I built wall-time and output-stall watchdogs that kill the complete process tree and report *why* it stopped, rather than trusting a naive timeout or accepting a partial run as "done."
- **Keeping the model honest under my own time pressure.** Solo, three days, a benchmark board not routing cleanly at 1 a.m. — it's tempting to let the model widen a clearance or shrink a trace just to get the router to finish. I hard-blocked that path: constraints are typed inputs I supply as the engineer, not levers the model can pull to make its own life easier.

## Accomplishments that I'm proud of

- Three real, DRC-clean benchmark boards — a motor driver, a BLDC driver, and a boost converter — designed through the MCP tools inside a three-day solo build, not hand-edited afterward.
- A placement optimizer that measurably improved on itself: the motor-driver benchmark's outline area shrank 29% and estimated ratsnest length 46% after a routing-aware re-optimization pass, with zero new DRC violations.
- One safety model — prepare/preview/confirm/validate/apply/rollback — applied uniformly across seven very different mutation types (schematic, rules, placement, grounding, routing, layout init, project creation), instead of bolting on a one-off script for each as I went.
- A versioned, correlated metrics schema (now on version 4) that lets me compare routing attempts against the same baseline over time instead of trusting hand-copied "it worked" notes — the habit of an engineer who's been burned by "it worked once" before.

## What I learned

The hard part of "AI for hardware design" was never getting a model to propose a circuit — it's building the evidence and refusal machinery that knows when *not* to trust that proposal. That's a professional-engineering problem before it's a software one, and having spent years on the other side of a bad autorouter run or a "quick" manual PCB edit is what told me exactly which gates couldn't be optional. Typed contracts at the MCP boundary, deterministic scoring instead of LLM judgment for anything DRC- or connectivity-relevant, and a confirmation gate that can't be bypassed turned out to be what makes the AI layer trustworthy enough to be worth having at all. I also learned, the expensive way and alone, that every "it just needs one more automation layer" moment (router scope, process stalls, or output import) is exactly where a silent failure would have hidden inside a demo that looked fine on camera.

## What's next for Copperbrain

Copper zones and keepouts as first-class typed objects, DFM/thermal/SI-PI review gates ahead of any `production_ready` claim, multi-vendor sourcing beyond JLCPCB/LCSC, and richer placement strategies for mixed analog/power/digital boards. The reference benchmarks also still have open work logged honestly in the repo — unrouted nets, unreviewed thermal margins — because the whole point of Copperbrain, and of doing this solo the way I have, is to say so instead of hiding it.
