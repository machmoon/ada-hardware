# The demo video

The Shipaton entry is a video under two minutes, not a live run on stage. The cut
below is in the order RevenueCat's judging guide asks for: the pitch, the core
experience, the purchase, then the category. It was recorded on 2026-09-25 as one live
run of the golden intent (below) through Ada's desktop UI against the local engine.

**One-line pitch:** Ada is an AI hardware engineer that works beside KiCad:
describe a board, get a schematic and a placed and routed board checked by KiCad's
own ERC and DRC, and a printable case checked clause by clause on the solid.

---

## The cut (about 110 s)

Every row after the title is the same run. Waits are sped up and labelled with the
factor; the narration is Kokoro text-to-speech (`af_heart`, the voice Ada speaks with).

| Beat | On screen | Narration |
|---|---|---|
| Title | Title card | "A first circuit board costs a day of datasheets and drawing. Ada, an AI hardware engineer, does that day beside KiCad." |
| Intent | The strip: the golden intent typed, then shown whole with the ceramic output capacitor highlighted; run options opened, the AMS1117 datasheet URL added, submit | "Describe the board in one sentence, and hand Ada the regulator's datasheet." |
| Plan | Planning (sped up), then the plan's three questions with their defaults; "300 mA" and "No" typed; Propose circuit | "Ada plans before it draws, and asks only what changes the design: the load current, the connector, a power switch." |
| Build | Propose, Place, Route on the step rail (sped up) | "It proposes a circuit that has to pass validation, places the parts with a constraint solver, and routes the copper." |
| KiCad | `kicad-cli sch export svg` of the run's schematic, then `kicad-cli pcb render` of the routed board with its ground pour filled by KiCad | "These are KiCad's own renders of the files Ada wrote, with ground as a filled copper pour." |
| Review | Review on the strip; the blocker (power LED wired backwards), then the output-capacitor finding with its datasheet p.4 citation | "Then Ada argues against its own design. It catches the power LED wired backwards, a blocker. And it questions the ceramic output capacitor the prompt asked for, citing the datasheet, page four." |
| Checks | A terminal replaying `kicad-cli` ERC, DRC and schematic parity on the run's files, with their real output | "KiCad's own checks on those files: zero ERC errors, zero DRC errors, and the schematic and board agree." |
| ESP32 | The ESP32 card: that board rendered by KiCad, numbers from `board_eval.py --only esp32_devboard` on 2026-09-25 (scripted circuit, real engine) | "Same engine, bigger board: an eighteen-part ESP32, every net connected, all three KiCad checks at zero." |
| Parts and case | Source the parts, Design the case, the rail rows with their receipts; the case kernel's own render of the case | "It proposes part numbers for eleven of twelve parts, none confirmed yet, and a printable case: twelve of thirteen checks pass." |
| Gate | "Prepare fab order · Ada Pro" on the strip, pressed | "Nothing up to here needs Ada Pro. Preparing the fab order is the one paid step: twelve dollars a month." |
| Purchase | Settings › Ada Pro (`active: no`, Buy), the RevenueCat Test Store modal, `pro` active in `CustomerInfo` | "The purchase runs through the RevenueCat SDK, and the pro entitlement unlocks the step. This is the Test Store, so no money moved." |
| Order | Back on the strip, "Prepare fab order · 1 call" (unlocked) is pressed; the order row reads not orderable, 1 blocker; "Every stage has run. Nothing was ordered." | "The fab pack is prepared, and it says what still blocks an order. Ada never orders on its own." |
| Close | Repo URL, licences, Next Gen, Test Store note | "Ada is open source, and the engine is free. Ada Pro, through RevenueCat, is the one paid step." |

### How it was recorded

Headless, so nothing drove the desktop: the app's own React UI from its Vite dev server
(`cd app && npm run dev`, port 1420) in Chrome through Playwright, with two shims applied
from outside the page. `@tauri-apps/plugin-http`'s `fetch` became the browser's, and the
engine's responses were fulfilled by Playwright with an allow-origin header (the service
itself still ships no CORS, on purpose); `window.__TAURI_INTERNALS__` answered the few
shell commands the strip asks at start. The step session was started with `kicad_live`
off so the engine did not open KiCad windows. Two pages in one browser context stand in
for the two webviews, so the pane request and the purchase verdict cross between them
over `localStorage` exactly as they do in the app. Frames were captured with Playwright
screenshots at 2x, the cursor was drawn afterwards from the logged click positions, and
the cut was assembled with Pillow and ffmpeg. KiCad's renders and checks ran with
`kicad-cli` 10.0.6 on the files the run wrote.

## Before recording

For a live take on the desktop (the recorded cut above was headless):

1. `git fetch` and confirm the checkout is the commit you mean to show.
2. Start the engine from the Engine tab (Start engine) or with
   `./.venv/bin/silkscreen serve --port 8081`; both read `.env`. A bare
   `python -m service.app` does not, and the strip's "Engine unreachable" notice
   says which command to use.
3. Set the model in `.env` and run the whole cut once, end to end, before the
   camera is on. Rehearse the purchase too: the Test Store modal is the SDK's own
   and its layout is not ours to change.
4. With KiCad installed and "Review in KiCad, stage by stage" on, each stage opens
   in KiCad's own editors through `desktop/kicad_live.py`; install FreeCAD too if
   the case STEP is to be opened.
5. Do Not Disturb on. Microphone off unless you are narrating into it.

The staged-company scripts in `scripts/demo/` (`seed_gmail.py`, `seed_slack.py`,
`page.sh`, and the Meet bot) were written for an earlier "first day at Perch
Robotics" cut and are not used in this one.

## The golden intent

> An AMS1117-3.3 regulator in SOT-223 fed from a USB-C receptacle, with a 10 uF
> input capacitor, a 22 uF ceramic output capacitor and a power LED with its
> series resistor.

Add the AMS1117-3.3 datasheet URL in the datasheets row of the run options:
`http://www.advanced-monolithic.com/pdf/ds1117.pdf` (the manufacturer's own; it
answers `%PDF-`, which the read stage and the BOM probe both require, where
distributor links serve an HTML viewer page).

Why this intent: the AMS1117 is an old bipolar LDO that needs output-capacitor
ESR in a stability band, and a low-ESR ceramic is the classic subtle mistake.
The adversarial reviewer is prompted to probe regulator output capacitors and
ESR (`engine/silkscreen/agents/review.py`), so this run reliably produces a
finding a hardware viewer recognises as real. The exact wording varies run to
run because it is a live model.

The 2-pin connector and LED in this intent are fine now: package selection in
`engine/silkscreen/board.py` (`_footprint_for_device`, around lines 532-536) dispatches
on the device's kind before its pin count, so a 2-pin connector or LED gets its
own land pattern instead of falling through to a 4-pin SOIC. The earlier
version of this script told you to exclude every connector and LED from the
intent; that hazard is gone.

---

## Hazards that are true today (2026-09-24)

- **Model tier.** `SILKSCREEN_PROVIDER=claude` is set in `.env` today, so the
  worker calls go to Claude, not Gemini. Pin one model for the recording and
  rehearse once on it; a failover mid-take costs minutes and reads as a hang.
  Measured 2026-09-24 on the CLI with that `.env` (lead `claude-opus-5`, default
  `fast` effort, `--case --bom`, the datasheet URL above): **156 s end to end**;
  read 26 s, plan 30 s, propose 42 s with two repair rounds, place 5 s, review
  26 s overlapping placement, sourcing 14 s, case 16 s. The review returned the
  ESR finding citing datasheet page 4, plus a blocker on a TVS the proposer had
  drawn forward-biased. Two things to expect on camera from that run: the plan
  asked four questions and, unanswered on the CLI, added a PTC and a TVS on
  VBUS (answer "no input protection" if you want the eight-part board); and the
  case kernel passed twelve clauses and failed `min_wall` by 0.35 mm on the lid,
  which demo-fast reports as a note, so say "twelve of thirteen" if it repeats.
- **KiCad (updated 2026-09-25): 10.0.6 is installed again** at `~/Applications/KiCad`, linked from `/Applications/KiCad`, and every KiCad beat above ran on it. The note that follows is what was true on 2026-09-24. **KiCad was not installed on this Mac then.** `/Applications/KiCad` does not
  exist and `kicad-cli` is not on `PATH`. The 0-8, 20-32 and 32-48 beats put
  the schematic and the board on screen in KiCad's own editors, so they have
  nothing to show without it, and the 48-60 terminal beat is cut; install KiCad
  before recording. If you cannot, say the KiCad numbers come from
  `docs/measurements/board-eval-2026-09-16.json` (run on 2026-09-16 on a machine
  that had KiCad 10.0.6). Without KiCad the step's where-chip reads "on disk"
  rather than "in KiCad", and the order step's Show 3D board button (KiCad's
  own 3D viewer) reports `KiCad did not open the 3D viewer`.
- **FreeCAD is not installed on this Mac today.** `/Applications/FreeCAD.app`
  does not exist and `freecad` is not on `PATH`, so the case step's Open 3D
  model button will report `FreeCAD did not open the case`. Show the clause
  receipt instead, or install FreeCAD first. The in-app Preview here viewer is
  the order step's GLB of the board, not the case STEP.
- **`MOUSER_API_KEY` is empty today**, so every BOM row stays `proposed` and
  none says `verified`. Say "nobody checked these" rather than skipping the
  row; that is the honest vocabulary and the point of the column.
- **Do Not Disturb on, microphone off** during the app takes. A banner or a
  wake-word trigger in the middle of a stage ruins the take.
- **Crop the capture** to the app windows. Never record the full desktop; the
  screen has private content on it.
- **The Test Store key must be a `test_` key** in `app/.env.local`, and that
  file must not be committed (`app/.gitignore` ignores `*.local`). Check the
  purchase panel shows "Test Store: simulated purchase. No money moves." before
  you press Buy on camera.

## If a take goes wrong

| What happened | What to do |
|---|---|
| The model stalls or the stage fails | Stop, restart the engine, re-run the intent. Never improvise a different intent to dodge it; the golden intent is the one that was rehearsed. |
| The review misses the capacitor finding | Whatever findings it produced are real; narrate those. The finding class is reliably elicited, not contractual. |
| The Test Store modal does not appear | The key is missing or not a `test_` key. Check `app/.env.local`, restart the app, and confirm `Purchases.isConfigured()` in the purchase panel before recording again. |
| Order refuses with a 402 | The service gate is on (`REVENUECAT_SECRET_API_KEY` set) and this customer is not entitled. Buy first, then order; the 402 body names the entitlement it wanted. |
| The engine says "Engine unreachable" | Start it from the Engine tab or `silkscreen serve --port 8081`; the notice names both. |
