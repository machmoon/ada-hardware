> **Note for Pat.** This is a factual draft in short plain paragraphs, written so you
> can rewrite it in your own voice before submitting. RevenueCat's Shipaton guide says
> the description should answer what problem, who it is for, what it lets them do, how it
> makes money and what makes it different, and warns "Don't let AI write your whole
> description." Every number below is quoted from a file in this repository; check the
> paths before you keep a number.

# Ada

Ada is an AI hardware engineer that works beside KiCad: describe a board, get a
schematic and a placed and routed board checked by KiCad's own ERC and DRC, and a
printable case checked clause by clause on the solid. For a beginner before the first
dead board, and a senior engineer before fab.

Shipaton 2026, Next Gen (student) category. The entry is a public open-source repository
(engine MIT at the root `LICENSE`, desktop app GPL-3.0 in `app/LICENSE`), a video under
two minutes, and one purchase powered by the RevenueCat Web SDK. There is no store
release, on purpose and by the category's rules; see the tags below for what that means.

## What problem

Designing even a simple printed circuit board takes days, and most of that time is
lookup rather than thought: which pin is AVDD, which page has the reference schematic,
which capacitor the regulator needs on its output. The first board a beginner orders
usually comes back dead, and the cause is almost always something a datasheet stated
plainly. Every EDA tool checks that a wire reaches a pin. None of them checks that it was
the right pin.

## Who it is for

Two people who want the same thing. A beginner before the first dead board, who cannot
yet tell a correct schematic from a plausible one. And a senior engineer before fab, who
can, but wants a second reader that cites the page. It is the rare feature where the
novice product and the expert product are the same product.

## What it lets them do

Describe a board in plain language in the desktop app, next to the KiCad you already
have. Ada reads the datasheets, proposes a circuit into a validated intermediate
representation, refuses to build one that does not validate, draws a `.kicad_sch`,
places the parts with OR-Tools CP-SAT, routes the copper with a two-layer octilinear A*
router with rip-up and a ground pour, and writes a KiCad 8 format project. Each stage is
a real file. KiCad's own `kicad-cli` runs ERC, DRC and schematic parity on the result as
external verifiers. The 2026-09-16 run recorded in
`docs/measurements/board-eval-2026-09-16.json` shows six circuits with 0 ERC errors, 0
DRC violations and 0 parity issues, and an 18-part ESP32 dev board 100 % routed with
DRC 0. Then a `build123d` kernel builds a
printable case and measures thirteen signed-margin clauses on the solid, a sourcing pass
produces a BOM with each part number marked `verified`, `proposed` or `none`, and an
adversarial review argues against the design with datasheet citations. Every step the
engineer runs waits for a press; the case and sourcing designs are prefetched when
placement lands, and the step envelope says so (`background: ["case", "sourcing"]`).

## How it makes money

**Ada Pro** is one entitlement, `pro`, sold as the monthly subscription product
`ada_pro_monthly` in the `default` offering. It is bought inside the desktop app through
the RevenueCat Web SDK (`@revenuecat/purchases-js`, MIT). It unlocks one step, **Prepare
fab order**: it runs only when pressed, and it is the step that turns a design into
something you send to a fab (the case and sourcing designs are prefetched server-side at
placement, so a client gate on those would not stop the model call). The gate is enforced on the client and, when
`REVENUECAT_SECRET_API_KEY` and `REVENUECAT_PROJECT_ID` are set, on the service, which
answers `402` with `reason: entitlement_required` when the customer lacks `pro`. When the
service has no key it says so in the order step's envelope rather than pretending to
have checked.

The entry uses the RevenueCat Test Store: simulated purchases through the SDK's own
modal, no money moves, and a Test Store key must never ship in a store build. A verdict
the app cannot obtain (SDK not configured, offline, an error) is `unknown`, and `unknown`
does not lock the step: this is a loopback-first app and an offline laptop must not lose
a paid feature. `free` locks it behind "Prepare fab order · Ada Pro".

The existing `billing/` package is Ada's usage-metering design and the launch path, not
what this entry sells. It meters engine time (1 KCU is one minute of engine wall-clock,
stored as integer milli-KCU) in an append-only ledger, with overage rather than cutoff
and a capped auto-reload, built on Stripe. It has never run against live Stripe and is
off unless `KALEO_METERING` is set. The division of labour is one sentence: RevenueCat
records that a purchase happened, the ledger records what it is worth.

## What makes it different

Verification is the product. The output is checked by KiCad's own ERC and DRC, by a
SPICE verifier that answers pass or fail with a signed margin, and by a CAD kernel that
measures the case rather than rendering it. The model may propose; a deterministic
verifier decides, and the model does not get to argue with it. Every integration reports
what actually happened in a fixed vocabulary (`verified` / `proposed` / `none`,
`FEASIBLE` rather than `OPTIMAL`, a router that names every net it could not finish),
and nothing returns a quiet zero. The KiCad integration is a file format, not a robot
arm: no plugin, no mouse control, and the project opens on a machine with no KiCad
libraries installed. The earlier version of this project, which won a prize and could not
survive a second user, is why; the next section is the thesis that came out of it.

## Status tags

Each item further down is tagged **[built]** or **[not yet built]** against the code in
this repository today. The tags that apply to the entry as a whole:

- **[not yet built]** A store release. The desktop app runs from a checkout with a local
  `.venv`; there is no signed `.dmg` and no notarization (`docs/release.md`).
- **[not yet built]** Anything but macOS for the desktop app.
- **[not yet built]** Live billing. The purchase is a RevenueCat Test Store purchase, and
  the Stripe metering in `billing/` has never run against live Stripe and is off unless
  `KALEO_METERING` is set.
- **[not yet built]** A live run of the service-side entitlement gate against a
  RevenueCat project: every test drives a recorded transport. With no
  `REVENUECAT_SECRET_API_KEY` the service does not gate the order step and says so,
  and when RevenueCat cannot be reached it fails open and says that too.
- **[not yet built]** A model-proposed ESP32 board. The ESP32 result quoted above is a
  scripted circuit run through the real engine.
- **[not yet built]** Any live meeting or chat front end: the Zoom, Teams, Meet and Slack
  packages have never run against a live account.
- The Cloud Run deployment recorded in the README was down on 2026-09-06 (`/readyz`
  answered 500). A recorded deploy is not a running one.

The earlier entry, for the All Things Agentic hackathon on 2026-08-31 (Gemini, ADK, Cloud
Run), is kept verbatim in [docs/google-hackathon.md](docs/google-hackathon.md).

---

## How the video was made

The video is one live run of the golden intent (`docs/demo-script.md`) through Ada's
desktop UI against the local engine, recorded on 2026-09-25. It was recorded headless:
the desktop app's own React UI, served by its Vite dev server, ran in Chrome with the
Tauri shell stubbed for capture. Engine calls went through the browser's `fetch` instead
of Rust's, and the step session was started with `kicad_live` off so no KiCad window
opened on the desktop; the "Review in KiCad, stage by stage" toggle still reads on,
which is why the strip says each stage is "on disk". The app UI on screen is the real
app's, not a mock-up: the plan, the proposal, the placement, the routing, the review,
sourcing, the case, the Ada Pro purchase through the RevenueCat SDK's Test Store modal
and the order step are that run's real responses. The window frames, the terminal window
(a replay of `kicad-cli`'s recorded output on the run's files, KiCad 10.0.6) and the
cursor (drawn from the logged click positions) are drawn in the edit. Waits are cut or
sped up, and every stretch sped up more than 1.5x carries a label saying by how much.
The schematic and the 3D board are KiCad's own exports (`kicad-cli sch export svg`,
`kicad-cli pcb render` of a copy with the ground pour filled by KiCad) of the files the
run wrote. The ESP32 card is a scripted circuit through the same engine, re-measured on
2026-09-25 with `scripts/board_eval.py --only esp32_devboard`, and its picture is that
board. In this run the service's server-side entitlement check was not configured (the
order response says so); the unlock on screen is the desktop's client gate reading
RevenueCat's `CustomerInfo`. The narration is Kokoro text-to-speech, the voice Ada speaks
with in the app. The Test Store purchase is simulated by RevenueCat, so no money moved,
and the narration says so.

The staging scripts in `scripts/demo/` (a seeded Gmail inbox, Slack cast and a 2 am
page for an earlier "first day at Perch Robotics" cut) are not used in this video.

## Why "checkably right"

Every device in your life runs on a printed circuit board, and designing even a simple
one still takes days of work that is mostly *lookup*, not *thought*. You find a chip.
You open a 300-page datasheet. You hunt for the one table that tells you which pin is
AVDD. You find the reference schematic buried on page 214 and copy the decoupling
network by hand. Then you do it again for the next chip. Then you place everything,
route it, and hope.

We know this because we built a tool for it before and got it wrong in an instructive
way.

Our previous attempt won a prize and could not survive contact with a second user. The
"KiCad integration" was a script that moved the operator's mouse to screen coordinate
(1600, 590) and pressed Ctrl+V, pasting in a board file a human had already laid out by
hand. The datasheet cache was twelve committed JSON files, so the demo never called a
model at all. The netlist generator picked the main IC's schematic symbol by fuzzy-
searching the *last word* of whatever the user typed — ask for "STM32F103C8T6
microcontroller" and it searched for "microcontroller" and wired the datasheet's pin
numbers onto whatever came back first. It looked spectacular for four minutes.

That experience produced the only opinion we actually trust: **in hardware, the demo is
not the hard part. Being right is the hard part, and being *checkably* right is the
whole game.**

So we went looking for where "checkably right" is worth the most. It is not layout.
Layout automation is crowded — Quilter, Cadence Allegro X AI, Zuken, DeepPCB — and
practitioners are openly hostile to it; a working RF designer put the objection best,
that with enough automation "the designers will be clueless." The genuinely underserved
job is one step earlier and one step less glamorous: **checking that a schematic
actually matches the datasheets it was drawn from.** Every EDA tool on the market
enforces structural connectivity — this wire reaches that pin — and none of them
verifies *semantic* correctness: that the pin was the right pin, that the regulator's
feedback divider produces the voltage you asked for, that the capacitor on the enable
line isn't ten times too large.

That check is what a beginner needs before their first board comes back dead. It is
also what a senior engineer wants before releasing to fab. It is the rare feature where
the novice product and the expert product are the same product.

And there is prior art for the demand, running on unpaid human labour: the "roast my
board" ritual, where people post schematics and wait days for a stranger to spot the
swapped pin. We want to be that stranger, in ninety seconds, with the datasheet page
cited.

---

## What it does, stage by stage

Ada takes a plain-language description of what you want to build and produces a
validated circuit, a placed and routed board, a case, a BOM, and a review of its own work
with citations. Each stage below is tagged **[built]** or **[not yet built]** against
the code in this repository today.

**Placement repair and company profiles. [built]**
An engineer opens the placement lab, selects Compact Control or Thermal First, and
watches the same broken motor-controller board become two different legal layouts.
The screen shows the starting violations, accepted actions, exact score deltas, and
the final geometry. The engineer can reject a move and pin that component into the
company profile. The demo keeps that correction in tab-local session storage, isolated
from other visitors. The repaired placement downloads as JSON. Server-side team
memory stays disabled until an authenticated tenant boundary exists.

Hard rules cover board boundaries, clearance, fixed components, and keepouts. Soft
preferences cover connector access, functional grouping, compactness, and thermal
separation. Gemini may propose actions, but a deterministic verifier accepts or rejects
every move.

**Approved build constraints. [built]**
The normal prompt-only path remains unchanged, but an engineer can open an optional
constraint contract, name exact nets and physical limits, and approve it for one run.
Any edit clears approval. The service rejects malformed or unapproved version 2
contracts before cache access or model spend, includes an approved contract in circuit
proposal context, and then checks the validated circuit, final placement, and routed
copper. The chat trace exposes this as a separate constraint-verification event, and
the review screen shows every blocker and its evidence.

The receipt is deliberately fail-closed. Missing routing, stackup, field-solver,
component-height, or full voltage-drop evidence is `unresolved`, not silently clean.
Today this is post-build production-promotion eligibility: the artifact remains
available for engineering inspection, and the declared limits do not yet configure
CP-SAT or A* directly. Soft preferences provide an advisory score for the generated
board; they do not claim that alternative layouts were ranked.

**1. Understand the parts. [built]**
Point Ada at a component and it reads the actual datasheet. Gemini's native PDF
vision matters here in a way that text extraction does not: pinout tables, package
drawings, and reference schematics are *pictures*, and the numbers we need live inside
them. Every extracted fact carries the page it came from.

**2. Propose a circuit. [built]**
The model emits a `CircuitSpec` — devices, passives, and nets — into a validated
intermediate representation.

**3. Refuse to build a broken circuit. [built]**
This is the load-bearing piece. Nothing reaches KiCad until it validates. The IR
rejects a net referencing a pin the device doesn't have, a part that doesn't exist, a
capacitor wired on only one leg, a bare part name where a specific terminal is required.
All failures are collected at once and handed back to the model as a single repair
prompt, so the loop converges instead of retrying blindly.

That last check is subtler than it sounds. Connecting *one specific leg* of a decoupling
capacitor to a specific pin is the most common operation in this entire domain — and an
IR that can only join whole parts to nets, as ours previously did, cannot express it at
all. Making that unrepresentable-by-construction is most of the value.

**4. Place the board. [built]**
A CP-SAT model places components to minimise board size and total wirelength, with real
courtyard clearance, optional 90° rotation, edge constraints for connectors and
antennas, and symmetry breaking over interchangeable passives. On the 11-footprint STM32 +
regulator + motor-driver board in `engine/tests/fixtures/` it returns a placement in the
region of 18–20 mm square with 53–62 mm of total half-perimeter wirelength, reported as
`FEASIBLE` rather than `OPTIMAL` because that is what the solver proved inside a 20-second
budget. **No exact figure is quoted here on purpose:** the run ends at the time limit, so a
busy machine returns a worse feasible answer — measured 2026-09-06, six idle runs gave
18.25 × 18.00 mm / 53.0 mm and six concurrent runs of the same command gave
19.60 × 18.55 mm / 62.0 mm. `workers=1` removes CP-SAT's own non-determinism; it cannot
remove the clock. What *is* invariant is what the tests check: every part placed, no
overlapping courtyards, and a file that reparses.

**5. Write a real file. [built]**
Ada reads and writes KiCad files directly. No KiCad installation, no `pcbnew`
DLLs, no platform lock, and — emphatically — no controlling the user's mouse. It runs
identically on macOS, Linux, and Windows, which is the difference between a demo and a
tool.

A run leaves a whole project, one file per stage: the `.kicad_pro`, the `.kicad_sch`
schematic, the placed board before any copper, and the routed board. Symbols and
footprints are both generated and embedded, so the project opens on a machine with no
KiCad libraries installed and cannot silently resolve to a different part than the one
it was drawn for. The schematic and the board number parts from one shared call, so
`C3` on the drawing is `C3` on the board — numbered separately, the two files would each
be self-consistent and describe different circuits.

The copper is laid by a two-layer A* grid maze router. **It is not a competitive
autorouter and the output says so:** a uniform 0.25 mm grid cannot reach every pin of a
fine-pitch package. The corners a sequential router paints itself into are escaped by a
bounded, deterministic rip-up-and-retry pass (a blocked net lifts the copper in its way,
routes, and re-routes what it lifted; pads are never ripped), but a board can still be
genuinely out of channels. On a dense fine-pitch LQFP board it finishes 6 of 50 nets. Every net it cannot
finish is named, with the reason, and left as ratsnest for a human — a router that
silently dropped a connection would be worse than no router at all.

Since 2026-09-16 the router is octilinear, ground is a copper pour on both layers with
a fan-out via per ground pad and a stitching grid, and a differential pair is laid as one
centreline and offset to a fixed gap, with the gap, coupled length and skew measured and
reported. `docs/measurements/board-eval-2026-09-16.json` (written by
`scripts/board_eval.py` on 2026-09-16; the file names the `kicad-cli` it ran, KiCad
10.0.6 on that machine, and does not record the version itself) records six circuits
with 0 ERC errors, 0 DRC violations, 0 unconnected items and 0 parity issues, including
an 18-part ESP32 dev board routed 100 % with 16 vias. **[not yet built]** The
ESP32 result is a scripted circuit run through the real engine (the circuit is fixed in
`scripts/board_eval.py`; the placer, router, emitter and KiCad checks are the live code),
not a board a model proposed.

Both emitters are checked against KiCad itself, not only against a parser: `kicad-cli
sch erc` and `pcb drc` are run on the output. That is how we found a via shorting a
foreign track on a board the entire test suite passed. Since 2026-09-15 those checks run
inside the proposal loop too (`engine/silkscreen/verify/`): an unwired power pin or an
ERC error goes back to the model as a repair, and the model does not get to argue with a
deterministic verifier.

**6. Review it, and say why. [built]**
An adversarial reviewer re-reads the datasheets and argues against the design: this pin
is an input, you drove it; this cap is on the wrong side of the regulator; this part is
end-of-life. Findings cite the datasheet page, and each one is checked against the spec
before it is shown. **[not yet built]** The approval gate that would let you accept a
suggested fix and have it applied: the fix buttons in the review UI are deliberately
inert until that exists.

**6b. Say it, hear it. [built]**
The web UI takes spoken intent and reads findings back. Dictation uses the browser's
own Web Speech API — Chrome and Edge have it; Firefox does not, and gets a plain
notice instead of a dead button — and the transcript is appended to whatever was
typed, never replacing it. Nothing listens until the microphone button is pressed,
and a permission refusal is a clear message, not a silent failure. On the review,
each finding has a read-aloud button and the whole review can be read in order by
`speechSynthesis`, with a stop control; a skipped review is read as "not run" and a
failed one as "not known", never as zero findings. This is the browser's recognition
service, not ours: no audio reaches our service, and it is a separate path from the
desktop overlay's push-to-talk. **[not yet built]** The local-Whisper/desktop
dictation path — browser dictation depends on the browser's recognition service, and
`vendor/openwhispr/` is the vendored reference for removing that dependency.

**7. Source the parts. [built]**
A senior engineer reviews the placed board in KiCad; a junior one looks up the parts
meanwhile. That is what the sourcing pass is: from placement on, in the background, the
model is shown each part's value and land pattern and asked for a manufacturer, a part
number and a datasheet URL, and the answer is checked to the extent it can be — which is
now further than it was. A part number goes to a real distributor: with a `MOUSER_API_KEY`
set, `agents/distributor.py` asks Mouser's Search API v2 for an exact part-number match, and
the status becomes `verified` only when a distributor answered that it lists that exact
`ManufacturerPartNumber`, carrying the distributor SKU and product URL with it. Everything
else stays `proposed` **and says why in words** — `Mouser does not list <mpn>`, or the
failure that stopped the question being answered — and the first unanswerable question stops
the batch, with every remaining row marked "not asked" rather than left looking checked.
With no key there is no verifier and every MPN stays `proposed`, which is the honest word
for a part number nobody checked; the prompt still says a null beats an invention, because a
wrong MPN gets ordered. Two things `verified` deliberately does **not** claim: not in stock,
and not the right package — Mouser's `Package / Case` is free text that cannot be compared
to a footprint mechanically, so it is passed to a human rather than used to pass or fail a
row. A datasheet URL is checked the same way: fetched and reported `verified` only when the
first bytes read `%PDF-`, since distributors serve HTML viewer pages from `.pdf`
links. Every part also names its 3D model from KiCad's own library, so the GLB the
order step exports shows components rather than a bare substrate — and only where the
library model matches the land pattern drawn; the rest say why they have none. The
BOM lands in the order manifest, the order zip, a CSV on disk, and the overlay's order
panel — and the exported GLB now opens **in the app**, in a WebGL viewer that says what it
is doing (`ModelViewer`, mounted from the order panel), not only in KiCad's viewer.

**8. Show, don't tell. [half built]**
Professional EDA tools are dense — KiCad has dozens of panels, and knowing *where to
click* is a real barrier that no chatbot removes. The idea is an animated cursor that lands
on the exact control we mean, so "add a net class" becomes something you watch once and can
then do yourself. The tool teaches its own UI, which is the difference between automating a
beginner out of the loop and bringing them into it.

**Built:** the pointer inside our own window. A finding carries a "Show me" button; the
pointer resolves its target declaratively (a `data-testid` plus identity attributes, never
an index), draws a halo and a caption, re-measures on scroll and resize, and advances only
when the human presses Next — it never detects clicks and never moves the OS cursor. A step
whose target does not resolve renders the caption alone and says so, because a pointer
confidently on the wrong element teaches the wrong thing, which is the one failure a
teaching tool must not have.

**[not yet built]:** pointing at anything outside that window. Guiding someone through
KiCad needs screen-bounds capture, a transparent OS overlay and per-display DPI handling,
and none of that exists — no screen capture, no accessibility-tree read, no overlay window,
and therefore none of the permission prompts they would require. The protocol was built so
the bounds source can be swapped without touching the guide, which is the cheap half.

**8b. Put it in a case. [built]**
The case is built like a CAD engineer works, not picked from a style list. The model
proposes a JSON spec (mount, inserts, material, a snap lid); every mechanical number
lives in a rules table sourced from open-source enclosure generators; `build123d` builds
real B-rep solids with standoffs at the board's mounting holes and cutouts sized from the
mating plug; and a kernel measures thirteen frozen clauses on the solid, each with a
signed margin in millimetres (board clash volume, headroom, boss concentric with its
hole, every cutout admits its plug, lid mates, minimum wall, overhang in the printed
orientation). The result is a labelled STEP assembly plus two STLs. Without the `cad`
extra installed the step refuses in words rather than degrading to a lesser case.

**8c. Simulate the circuit. [built, opt-in]**
`spice/` is a verifier, not a waveform viewer: it answers pass or fail with a signed
margin on each clause. With `--simulate` the model proposes a testbench in the same JSON
format the MCP tool takes, ngspice runs it, and a failed clause becomes a finding with
its provenance kept apart from the datasheet review. A part with no behavioural model is
reported `unsimulatable` by name, and no ngspice on the machine is `unavailable` with
the install hint, both decided before a model call is spent.

**8d. The desktop app. [built, macOS only, runs from a checkout]**
`app/` is a Tauri 2 overlay that sits beside a running KiCad and shows the run as a strip
of approval-gated steps: plan, propose, place, route, review, case, sourcing, order.
Every step the engineer runs waits for a press; the case and sourcing designs start in
the background once placement succeeds and the envelope reports them as such. It is a
GPL-3.0 fork of Pluely (`app/NOTICE.md`), and
the engine under it is MIT. **[not yet built]** A store release: the app runs from a
checkout with a local `.venv` and there is no signed `.dmg`, no notarization and no
Mac App Store listing (`docs/release.md`). **[not yet built]** Windows and Linux.

**9. Be where the requirement is stated. [built, and unverified live]**
A hardware requirement is spoken long before it is typed. `meetings/` already reads a
finished Google Meet transcript; `zoombot/` and `teamsbot/` are the same idea in Zoom and
Microsoft Teams, with the same gates — a request whose quote is not literally in the
transcript is dropped, one below the confidence floor is recorded but not built, every
skipped request is still reported, and nothing is ever ordered. Zoom's Realtime Media
Streams arrive as fragments while the call is happening, so the transcript is gathered
into overlapping windows before a model sees it, and duplicate quotes are collapsed so
overlap cannot double the bill.

Two boundaries are stated rather than hidden, because they are the whole difference
between this and a demo. **Neither package has ever run against a live account** — every
network boundary is a Protocol seam with a recorded stand-in, so the suite proves the
parsing, the gates and the refusals, and nothing about Zoom's or Microsoft's live
behaviour. And **[not yet built]** the half that speaks out loud: RTMS is receive-only
and a Teams calling bot needs Microsoft's .NET media libraries, so audio out requires a
container that this repository specifies but does not implement. Zoom's stub refuses
every `/say`; Teams' answers 501 with the sentence naming what is missing. A stub that
answered "spoken" would let a report claim the agent talked in a meeting the room heard
nothing in — which is the exact bug the whole design is shaped to prevent. Every run
report names *which* speaker was used, so "the agent replied" can never be read as "the
agent spoke out loud".

**10. Book the meeting instead of writing the report. [built]**
A run ends with things a machine cannot decide: a blocker, three nets left as ratsnest, a
kernel clause that failed by 0.4 mm. Rather than print a wall of text, the run proposes a
short agenda — validated model output whose bounds *are* the feature: a 400-character
summary, one to eight items, fifteen to sixty minutes total, so it cannot quietly grow
back into the report it replaces. An item naming a part the board does not contain is
dropped, the same hallucination filter the reviewer's findings pass through. The agenda
then becomes a Calendar hold with a Meet link and the people who can answer it. Two
outcomes are stated refusals rather than an empty booking: the review has not run, so
what the meeting would decide is not known; or nothing is blocking, so nothing needs a
meeting. Run options carry a Structured/Prose toggle for which way a finished run reports
itself. As with the rest of the Workspace layer, the Calendar path is **unverified
against live Google APIs**.

**11. Say what is actually wired up. [built]**
`GET /integrations` answers with every surface — Workspace, Slack, Meet, Zoom, Teams, the
enclosure kernel, sourcing, MCP, SPICE, `kicad-cli` — in four states that a single boolean
would have blurred: not installed here, importable but unconfigured, half-configured, and
ready. `ready` is a claim about configuration and nothing else; nothing in that route makes
a live call, and no secret is ever echoed, not even a tail. Unbuilt integrations still
appear, marked unavailable, because "not built here" and "not on the menu" are different
facts and only one of them is true.

---

## Challenges we ran into

**The solver was lying about being optimal.** Our earlier CP-SAT model derived the
board's vertical domain from a sum over `for h, _ in rects` — which binds `h` to the
*width*. Any component taller than the total width of the board was declared infeasible.
It also packed parts flush at 0 mm clearance, producing layouts no assembler could
build, and raised an exception on timeout, throwing away a perfectly good feasible
solution. Each of those is now a named regression test.

**Optimality is not available, and pretending otherwise is a lie.** 2D packing with a
wirelength objective is NP-hard. On real boards the solver returns `FEASIBLE`, not
`OPTIMAL`, inside a 20-second budget. We tried coarsening the grid from 0.025 mm to
0.5 mm; it barely moved the result, which told us the bottleneck is combinatorial, not
resolution. So we invested in symmetry breaking instead — a board with 24 identical
capacitors admits 24! relabelings of the same physical layout, and collapsing those
orbits proved optimality 3× faster on a 27-part test. And we made the failure mode
honest: when the solver finds nothing in time, a deterministic shelf packer returns a
valid layout flagged `FALLBACK`, rather than crashing.

**Ground nets destroy the objective.** Expanding every net into a clique of pairwise
connections means a ground net touching 50 pads contributes 1,225 edges and swamps every
signal net in the design — which, in our previous version, collapsed the entire board
into a single placement group and silently disabled the hierarchical layout we thought we
had built. Silkscreen excludes power rails by name and by fan-out, and connects the
remaining multi-pad nets as a star rather than a clique: linear instead of quadratic.

**Y is down.** KiCad's Y axis points down; a bottom-left-origin packer's points up. Mixing
them mirrors every layout vertically, and it is invisible until you look at a rendering
and something feels subtly wrong. There is now exactly one line in the codebase that does
that conversion, and it is commented.

**Nobody's PDF is standard.** Datasheets are inconsistent, frequently multilingual, and
hundreds of pages long. Native PDF vision — reading the pinout *table* as a table and the
package drawing as a drawing — is what makes this tractable at all.

---

## Accomplishments that we're proud of

Mostly, that we deleted things.

The version this replaces had two live API keys committed to a public repository, an
`eval()` on raw model output, a main server that raised `NameError` on import and could
not start, a frontend that failed to build, and a "layout engine" that was three
`pyautogui` clicks at hardcoded screen coordinates. We know all of this precisely
because we went back and audited it line by line before writing anything new. The most
valuable engineering artifact we produced was an honest list of what was actually true.

What we're proud of in the new one:

- **The deterministic kernel has no network calls.** Every correctness-critical path is tested offline.
- **4538 tests, and the interesting ones are regressions** — each pins down a specific bug
  that shipped in the previous version and can never ship again.
- **A validation layer whose job is to say no.** The IR makes a floating capacitor and a
  hallucinated pin unrepresentable rather than merely unlikely.
- **The KiCad integration is a file format, not a robot arm.** Cross-platform, headless,
  testable, and it does not seize the user's mouse.
- **We can state what doesn't work.** `FEASIBLE` is reported as `FEASIBLE`.

---

## What we learned

That in a domain with no cheap oracle, verification *is* the product. LLM coding works
because compiling and running the tests is nearly free. In hardware, ground truth is a
fabricated board four weeks and several thousand dollars away, and design-rule checking
is a weak proxy — it validates geometry, not intent. A board can pass every automated
check and still have a regulator wired to the wrong pin. That asymmetry is why the
category is hard, and it is also why the most valuable thing an agent can do here is not
*generate faster* but *catch what a human would have missed*, with a citation.

We also learned how easy it is to build something that demos beautifully and is hollow.
Our previous project's README claimed a RAG pipeline (there was no retrieval, no
chunking, no embeddings — one model call with a PDF URL), an MCP integration (there was
no MCP anywhere; a directory was named `mcp/`), and "multiple backups to ensure zero
single points of failure" (every fallback path was broken — one returned a streaming
iterator where the caller expected text, another parsed a field that never existed in the
response). None of that was dishonesty. It was a team at hour 22 describing what they
meant to build. The lesson we took is that the README should be written from the tests.

---

## Third-party code

`vendor/mudriknow/` is not our code. It is [MudrikNow](https://github.com/abdallahmagdy15/mudriknow)
at revision `ad58192`, MIT licensed, included unmodified with its licence file
intact as a working reference for the guided cursor. Its *design* is what we used — the
consent gate, the human-confirmed advance, the refusal to point when a target does not
resolve — and none of its code: upstream is Windows-only Electron by its authors' own
statement, and the in-window pointer we shipped (feature 8) is a fresh Svelte
implementation. The OS-level overlay that would most resemble MudrikNow is still unbuilt.
Nothing in `engine/`, `service/`, or `scripts/` imports from it, it is
excluded from lint and tests, and it contributes nothing to the 4538 tests or to
any figure quoted in this document.

`vendor/openwhispr/` is not our code either. It is
[OpenWhispr](https://github.com/OpenWhispr/openwhispr) at revision `abfaf2b`,
MIT licensed, included unmodified with its licence file intact as a working
reference for a desktop/local-Whisper dictation path we have not built. The
voice input that **is** built (feature 6b) lives in `frontend/src/lib/voice.js`
and uses the browser's own Web Speech API, importing nothing from here. Same
standing as above: excluded from lint and tests, contributing to no quoted figure.

**Ada's voice** is [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), which is
**Apache-2.0 for its code *and* its weights** — unusually, and that is exactly why it was
chosen over better-sounding alternatives. It is not vendored: the weights are downloaded by
an operator to `~/.kaleo/voices/kokoro/` and the service says so in words when they are
absent, never downloading hundreds of megabytes behind a spoken word. It runs through
[`kokoro-onnx`](https://github.com/thewh1teagle/kokoro-onnx) (MIT) on
[onnxruntime](https://github.com/microsoft/onnxruntime) (MIT), which pull
[eSpeak NG](https://github.com/espeak-ng/espeak-ng) (**GPL-3.0**) as the
grapheme-to-phoneme front end. The GPL is disclosed rather than discovered: eSpeak NG is
invoked as a separate binary and never linked into the engine, and the desktop app it serves
is already GPL-3.0, so it composes one-way. `ELEVENLABS_API_KEY` selects ElevenLabs Flash
v2.5 as a paid hosted upgrade instead; with no key, nothing is sent anywhere.

Two open TTS models that top "best open voice" lists were **rejected on licence**, and are
named here because they will keep coming back: **Breeze TTS 2** (gated repository,
`license: other`, non-commercial — a third-party re-upload tagged `apache-2.0` cannot grant
rights the original withheld) and **F5-TTS** (MIT code, CC-BY-NC-4.0 weights; the code
licence does not rescue the weights).

`app/` is a GPL-3.0 fork of [Pluely](https://github.com/iamsrikanthnani/pluely); the
upstream attribution is in `app/NOTICE.md`, which is kept as received. The engine at the
repository root is MIT and imports nothing from `app/`. The purchase runs on
[`@revenuecat/purchases-js`](https://github.com/RevenueCat/purchases-js) (MIT), used as
a dependency and not vendored.

Everything else in the repository was written by us.

---

## What's next

**Live billing.** Move the entitlement from the Test Store to a Web Billing sandbox key
and then production, add the RevenueCat webhook to the service (the signature scheme is
the one `billing/webhook.py` already verifies for Stripe) so a purchase writes a grant
into the ledger, and turn `KALEO_METERING` on. Then a store build with a signed,
notarized `.dmg`.

**Footprint generation from datasheets.** Wrong footprints are the most common cause of a
dead first-spin board, and unlike layout, correctness is objectively checkable against the
package drawing in the PDF. It is a bounded, verifiable, high-value task that every tool in
this category quietly depends on and almost none of them own.

**Part availability as a design-time constraint.** The sourcing pass now checks a proposed
part number against one distributor (Mouser) and reports `verified` only on an exact
match — but existence is not availability. Nothing here reads stock, lifecycle status, lead
time or price, so a design specifying a listed-but-end-of-life part still passes, and a
second distributor would need a second client. Turning availability into an actual
design-time *constraint* — one the placer and the proposer can see — is the unbuilt part.
The existing sourcing-aware tools have a neutrality problem
— one sells design-intent data to component manufacturers, another is owned by a chipmaker.
There is no trusted, neutral, AI-native sourcing layer.

**Manufacturability as distinct from design rules.** A board can pass DRC and still fail
DFM: an acute-angle trace within spec creates an acid trap that over-etches. Every fab has
slightly different capabilities, published as PDFs that differ per vendor and change over
time — unstructured, per-vendor knowledge, which is precisely the shape of problem language
models are good at.

**Teaching as the product, not a mode.** The guided cursor is the piece we most want to get
right, and only its in-window half exists today (feature 8); the half that could walk
someone through KiCad itself is unbuilt. Hardware has a brutal on-ramp, and the honest risk of every tool in this category is
that it produces engineers who can accept a suggestion and cannot evaluate one. A tool that
shows you where it clicked and why is a different bet: not automating the beginner out of
the loop, but making the loop learnable.
