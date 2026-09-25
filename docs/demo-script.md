# The 115-second demo video

The Shipaton entry is a video under two minutes, not a live run on stage. The
shot list below is in the order RevenueCat's judging guide asks for: the pitch,
the core experience, the purchase, then the category. Every row names what is on
screen, one sentence of narration, and whether the take is live or pre-baked.
Live means the engine runs in the take; pre-baked means a recording or a file
made before the take, and the narration says so where a viewer could be misled.

The hazards list at the bottom is what is true on this Mac today (2026-09-24).
Read it before recording; every item on it has caused a wasted take.

**One-line pitch:** Ada is an AI hardware engineer that works beside KiCad:
describe a board, get a schematic, a placed and routed board and a printable
case, each checked by KiCad's own ERC and DRC.

---

## Shot list

RevenueCat's order: pitch, core experience, purchase, categories. The frame is Ada's first day as the hardware engineer at a company that does not exist; the people are staged, Ada is not (the seeding scripts are in `scripts/demo/`, and DEVPOST.md says which is which). Drop the ESP32 row first if the cut runs long.

| Seconds | On screen | Narration (one sentence) | Take |
|---|---|---|---|
| 0-8 | Title card: the product sentence, then the strip over the desktop. | "Ada is an AI hardware engineer that works beside KiCad. This is its first day at Perch Robotics." | card |
| 8-18 | The menu-bar clock reads 02:13. A PagerDuty banner. Gmail: the PagerDuty mail. (Slack is not in this cut.) | "Two in the morning, the rev A power board browns out in the field again, and the on-call engineer is Ada. It does not sleep." | staged: `scripts/demo/page.sh --mail` |
| 18-38 | Google Meet standup grid. Ada's tile joins (`python -m meetbot join`). Pat states the rev B board; someone says the 5 volt line; Ada answers out loud. Cut to the recap the bot posts. | "At standup Ada joins the call, listens, and takes the request out loud: a 3.3 volt regulator board off USB-C with a green LED. The 5 volt idea was not an order, so it is recorded, not built." | live; crowd tiles staged |
| 38-58 | Gmail: the AVDD thread, twelve replies deep, and Maya's datasheet mail. The strip picks the idea from the inbox (source: meet). Propose circuit, Place parts, Route copper, sped up, the ground pour last. | "It reads the datasheet from the thread nobody could finish, proposes a validated circuit, places the parts with a constraint solver, and routes the copper with a connected ground pour." | inbox staged (`seed_gmail.py`); the run live, waiting cut with a 'sped up' label |
| 58-72 | Review tab: the finding that cites the datasheet page. Then a terminal capture of `kicad-cli sch erc` and `pcb drc --schematic-parity` on the written project, or `docs/measurements/board-eval-2026-09-16.json` on screen if KiCad is not installed. | "Then it argues against its own design, citing the page. KiCad's own ERC and DRC, run on the files, report zero." | live review; KiCad capture only if installed |
| 72-95 | Strip: Prepare fab order · Ada Pro. Settings, Ada Pro, Buy, the RevenueCat Test Store modal, `pro` active in CustomerInfo, back to the strip, Order runs. Gmail: 'Fab order: Feedr rev B' sent with the zip. Calendar: 'Design review, Ada attending' with a Meet link. | "The fab order is Ada Pro. The purchase runs through the RevenueCat SDK, the entitlement unlocks the step, and the order and the review invite go out by Gmail and Calendar. This is the Test Store, so no money moved." | live; rehearse twice |
| 95-105 | ESP32 dev board from `scripts/board_eval.py --keep`: 18 parts, 100 % routed, coupled USB pair, DRC 0. On-screen label: scripted circuit, real engine. | "Same engine on an ESP32 board: eighteen parts, every net routed, USB as a coupled pair, zero DRC errors." | pre-baked |
| 105-115 | Close card: repo URL, MIT engine and GPL-3.0 desktop app, Next Gen. One line: the colleagues are staged, everything Ada did is a real run. | "The colleagues are staged. Everything Ada did is real. I'm a student, this is a macOS app with no store release yet, so it is entered in Next Gen." | card |

## Before recording

The staged company, once, from the repo root (each script says what it did and
what it could not):

- `./.venv/bin/python scripts/demo/seed_gmail.py --datasheet <AMS1117.pdf>`: one
  browser consent for `gmail.insert` into its own token file, then the onboarding
  mails, the AVDD thread and the datasheet mail land in the signed-in inbox.
- `./.venv/bin/python -m meetbot.session sign-in`: the bot's Chromium profile,
  signed into the Google account whose name the Meet tile shows.
- At record time: `scripts/demo/page.sh --mail` for the banner and the page mail,
  then start the Meet, then `./.venv/bin/python -m meetbot join <meet-url>` and
  admit Ada.
- Slack is not part of this cut; `seed_slack.py` exists for when it is.
- On camera, show Gmail through the search `perchrobotics.example` (or a label
  made from it), never the raw inbox: the seeded mail sits between real mail.

1. Install KiCad. The 0-8, 20-32 and 32-48 beats show the schematic and the
   board in KiCad's own editors through `desktop/kicad_live.py`; without it the
   files are still written but nothing appears on screen for those beats, and
   the 48-60 terminal beat is cut. Install FreeCAD too if the 60-72 beat is to
   open the case STEP; otherwise plan to show the clause receipt only.
2. `git fetch` and confirm the checkout is the commit you mean to show.
3. Start the engine from the Engine tab (Start engine) or with
   `./.venv/bin/silkscreen serve --port 8081`; both read `.env`. A bare
   `python -m service.app` does not, and the strip's "Engine unreachable" notice
   says which command to use.
4. Set the model in `.env` and run the whole shot list once, end to end, before
   the camera is on. Rehearse the purchase too: the Test Store modal is the SDK's
   own and its layout is not ours to change.
5. Have the RevenueCat dashboard open on the customer page in a second window so
   the 72-92 cut is a window switch, not a login.
6. Pre-bake the 92-102 shot: `./.venv/bin/python scripts/board_eval.py` writes
   `board_eval.json` at the repo root; the committed copy quoted by the docs is
   `docs/measurements/board-eval-2026-09-16.json`. Render the ESP32 board with
   `kicad-cli pcb render` on a machine that has KiCad, or reuse
   `site/assets/board_top.png`, which is that board.
7. Do Not Disturb on. Microphone off unless you are narrating into it. Close
   Slack and mail.

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
- **KiCad is not installed on this Mac today.** `/Applications/KiCad` does not
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
