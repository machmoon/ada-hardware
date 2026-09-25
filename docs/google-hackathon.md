# The Google hackathon entry (All Things Agentic, 2026-08-31)

This page holds the narrative from the earlier submission. The project entered
All Things Agentic on 2026-08-31 under the name Silkscreen, against three
Google-stack requirements: a Gemini 3.5 or newer model called through the Gemini
API or Vertex AI, at least one Google agent framework, and at least one Google
Cloud infrastructure service. The per-requirement analysis, with file citations,
is in [docs/gemini.md](gemini.md), [docs/agent-framework.md](agent-framework.md)
and [docs/cloud-infrastructure.md](cloud-infrastructure.md); the prepared judge
answers are in [docs/judging-notes.md](judging-notes.md).

The text below was moved here verbatim from `DEVPOST.md` and `README.md` on
2026-09-24, when those two files were rewritten for the Shipaton entry
(`DEVPOST.md`). It describes the tree as it stood at the end of August and is
not kept current: the Cloud Run URL it names was down on 2026-09-06 (`/readyz`
answered 500), the default engine driver and the model tiering are as recorded
in `CLAUDE.md`, and `SILKSCREEN_PROVIDER` can now select a non-Gemini provider
for the worker calls. Read it as a record of that entry, not as a description
of today's product.

The live demo script for that entry (a three-minute stage run with cloud
beats) was in `docs/demo-script.md` until 2026-09-24 and is in git history.

---

## From the README's "Hackathon submission" section

**What it is.** Ada is a multi-step AI hardware engineer. You describe a board (typed,
spoken, in a Google Meet, or in Slack); it proposes a circuit with Gemini, validates it,
places parts with a CP-SAT solver, routes copper, designs a 3D-printable case with a
CAD kernel, sources parts, reviews its own design, and hands you a real KiCad project.
Every paid step waits for your approval in a desktop overlay. The problem: going from
an idea to a buildable board takes an engineer days of datasheets, schematic capture,
layout and enclosure work.

---

## How we built it (moved from DEVPOST.md)

**STATUS:** the ADK driver is the default engine; `SILKSCREEN_ENGINE=sdk` keeps the
straight-line driver one environment variable away.

The agent layer is Google's Agent Development Kit. The pipeline — read → propose →
validate → place → verifier repair → schematic → route → review — is an ADK
dynamic **Workflow** in
`engine/silkscreen/agents/adk/`, where each stage is a node that calls the same stage
body the plain SDK path calls. `generate_pcb(engine=...)` chooses the driver, and both
drivers emit the same events from inside those shared bodies, so which one ran is not
something a client can observe. The topology is deliberate rather than a flat pile of
prompts:

- an **orchestrator node** for the main pipeline, running the stages as successive
  `await ctx.run_node(...)` calls, so the order is ordinary program text and a stage
  that fails comes back out of the run as the original exception
- a **bounded repair cycle** inside the propose node: every IR failure in a batch goes
  back to the model as one repair prompt, and the loop ends when the IR validates
- a dedicated **adversarial reviewer** node, prompted to *refute* the design rather than
  confirm it, because an agent asked "is this correct?" will say yes — and its findings
  are filtered against the spec, so a part reference the circuit does not contain is
  stripped out of the finding that named it, while the finding itself is still shown
- a **parallel fan-out** over datasheets, one reader per component, since parts are
  independent: an `asyncio.gather` inside the read node (`agents/stages.py:217`), bounded to
  `MAX_CONCURRENT_READS` at a time by a semaphore, each read on its own thread. One part
  failing does not abandon the others — it becomes a `read.failed` event and the rest still
  land — and the run fails only if *every* read failed. `gather` preserves request order in
  its results, so a completion order that varies never makes the *result* vary

**[built]** Placement repair is a separate bounded agent loop. Gemini reads the board,
company profile, and verifier feedback, then proposes absolute `PLACE` or relative
`MOVE` actions. Unknown references are ignored, fixed parts cannot move, and a batch is
accepted only when its geometry and preference score improves. The deterministic
repairer also exports synthetic board-to-action trajectories for future Qwen supervised
fine-tuning. With the default-off experimental gate and separate trace consent
enabled, rejected proposals are stored with verifier receipts and a better Gemini or
deterministic target for preference training. Portable reward functions expose legality
first, progress second, and a small company-preference reward last for a future RL run.
This submission does not claim that a trained checkpoint exists or beats the
deterministic baseline.

Model tiering: `gemini-3.7-flash` for datasheet vision and reasoning, dropping to
`gemini-3.5-flash-lite` behind it, and — after four failed Gemini attempts only — one try
on open-weights `gemma-4-31b-it` through the same API as a last resort; a result names
which rung served it in `served_by`, so a Gemma run is never presented as a Gemini run. It
is a failover chain rather than per-task routing, and every provider's output is checked
for usable text before it is accepted, because a fallback path nobody has exercised is a
second bug and not a backup. The selectable ADK root model is held to the Gemini 3.5 floor
by the service (`gemini-3.1-pro-preview` is refused unless an operator opts in). Deployment
is Cloud Run; extracted datasheet facts persist to Firestore so the second run on a part is
free. The live URL in the README is verified before a demo with
`curl -s -o /dev/null -w '%{http_code}' <url>/readyz` — a recorded deploy is not a running
one.

**[not yet built]** Tool confirmation gates any step that writes a file.

**[built]** The deterministic engine kernel is deliberately boring and makes no
network calls; Gemini and the opt-in placement providers sit behind policy adapters:

- **OR-Tools CP-SAT** for placement
- **kiutils** for `.kicad_pcb` I/O — pure Python, no KiCad install
- Pure-integer nanometre arithmetic end to end, because unit confusion between
  millimetres, mils, and KiCad's internal nanometres is a silent, board-destroying class
  of bug
- 3807 tests that run with no network, no API key, and no KiCad installed

Splitting it this way is the point. The parts that must be *correct* are testable
offline. The parts that must be *smart* are the ones talking to a model.
