# The agent harness: which SDK, what the moat actually is, and what shipped

*Written 2026-09-15. Analysis first, a four-agent red-team review second,
the build third. Section 9 lists the review's findings and what changed
because of them; section 8 is the honest status of the build.*

## The answer

**Which SDK for what.** Keep Google ADK exactly where it is: the Gemini
adapter, the fixed-graph `Workflow` that runs the pipeline
(`agents/adk/workflow.py`), and the conversational `LlmAgent` root that
answers quick questions and, once it has lookup tools, runs the quick
searches (`agents/adk/orchestrator.py`; today its only tools are
`propose_board`/`generate_board`, so "ADK for quick queries" is a plan, not a
description). Do **not** build the harness on the Claude Agent SDK: it is not
a harness library but a control-protocol client over the `claude` CLI
subprocess, so its loop is Claude Code's, cannot run on Gemini, cannot be
driven by a scripted model offline, and needs a Node binary beside a Tauri
app. Do **not** depend on the `openai-agents` package either: its `Model` ABC
speaks the OpenAI Responses API's item types, so Gemini and Claude would
both be translated into a third vendor's wire format. **Build the loop
yourself, small and synchronous, in the proposer's own seam**, copying its
primitives by name from the OpenAI Agents SDK (MIT) and its tool-failure
recovery from ADK's `ReflectAndRetryToolPlugin` (Apache-2.0), with model
access behind this repo's `Model` seam extended with tool calls. That is
`engine/silkscreen/agents/harness/`, built today.

**What the moat is.** Not the loop. The review measured the bug Pat sees:
a regulator whose ground pin is on no net ran to "success" and KiCad's ERC
reported **zero** violations, because the schematic emitter drew a
no-connect flag on every unwired pin, telling ERC the forgotten ground was
intentional. No harness fixes that; an IR field does. The moat is the set
of deterministic verifiers a run cannot finish without, a receipt naming
which verifier proved which claim, and a scoreboard that reports the number
the product used to hide. Section 5.

## 1. What was actually wrong, measured

The pipeline (`agents/pipeline.py`, `agents/stages.py`) is a compiler: read,
plan, propose, place, placement repair, schematic, route, review, with the
critic, case, sourcing, SPICE, mechanism and research as parallel lanes.
Five bounded repair loops exist (`propose.py:420`, `plan.py:945`,
`simulate.py`, `sourcing.py`, `web_research.py`), each batching every
validator error into one repair prompt. The proposer's is the one that
decides the circuit, and until today it checked syntax (`netlist.validate`:
names, net shape, `part.pin` endpoints, one pin one net, passives two-ended),
packages, part numbers, library pin numbers and two bus rules
(`signals.py`). It did not ask whether a ground pin was on the ground net,
and nothing after it could:

- `schematic.py` emitted `(no_connect ...)` for **every** device pin absent
  from the nets, and `board.py` gave every unwired pad KiCad's
  `unconnected-(...)` net. A forgotten pin and a deliberately open pin were
  the same picture.
- No stage of a generated run called `kicad-cli` at all
  (`service/kicad_cli.py` only exports 3D models); ERC, DRC and parity were a
  gate a person ran afterwards.
- The critic's prompt asks "does every part reach ground?" and its answer
  cannot fail a run: `PipelineResult.blockers` is counted into `summary()`
  and nothing refuses on it.

Measured 2026-09-15 with a scripted model and KiCad 10.0.6, no key spent
(`scratchpad/measure_gnd.py`, now `engine/tests/test_verify.py`):

| Circuit | Before | After |
|---|---|---|
| LDO, correct | run ok, ERC 0 | run ok, ERC 0 |
| LDO, regulator GND on no net | **run ok, ERC 0** | proposer refuses in round 1 naming `AMS1117-3.3.GND`; ERC `pin_not_connected` + `power_pin_not_driven` |
| LDO, GND declared `no_connect` | run ok, ERC 0 | run ok, ERC 0, completeness *warning* "power pin declared open" |

On the six fixed circuits of `scripts/board_eval.py` the new
`electrical_completeness` verifier reports zero false positives, and KiCad
ERC on the drawn schematic found undeclared open pins in two of them
(ATtiny PB3/PB4, CH340 V3/UDP/UDM/DTR/RTS, ESP32 IO2, USB-C SBU1/SBU2), now
declared. The earlier proposal-quality numbers (`docs/design-quality.md`,
0 of 16 correct then 5 of 6) were measured on `gemini-3.5-flash-lite`, not
the default model, and the two are "not the same kind of number" per that
file; they are not repeated here as evidence.

Two seam facts shaped the build. `Model.generate` takes a prompt and
returns a string; a loop where the model asks for a verifier needs a tool
call in the seam. And both front ends key their progress rails on a frozen
stage list and tick "validate and repair" from `propose.round` frames
(`app/src/lib/silkscreen/describe.ts`, `frontend/src/lib/run.js`), so a
harness that emitted under a new stage name would run invisibly.

## 2. What each SDK is, read at source

### Claude Agent SDK (`anthropics/claude-agent-sdk-python@e773e44`)

`_internal/transport/subprocess_cli.py` spawns `claude --output-format
stream-json --input-format stream-json` (`MINIMUM_CLAUDE_CODE_VERSION =
"2.0.0"`); `_internal/query.py` speaks a bidirectional control protocol to
it. `types.py::ClaudeAgentOptions` is the CLI's knob list: `tools`,
`allowed_tools`, `mcp_servers`, `permission_mode`, `can_use_tool`, `hooks`
(`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, `SubagentStop`,
`PreCompact`...), `agents`, `max_turns`, `max_budget_usd`, `task_budget`,
`sandbox`, `session_store`. In-process tools are MCP servers over
`sdk_mcp_bridge.py`. The loop, compaction, file and shell tools, subagents
and sessions are Claude Code's. As a design choice it is an executor: the
best there is for "edit these files, run this command, repeat", reachable
from Ada through the MCP server it already ships, and the wrong place for a
Gemini-first desktop product's loop.

### OpenAI Agents SDK (`openai/openai-agents-python@fbf59a4`)

In-process. `Agent` (`src/agents/agent.py`) is a dataclass: `instructions`,
`tools`, `handoffs`, `model`, `model_settings`, `input_guardrails`,
`output_guardrails`, `output_type`, `hooks`, `tool_use_behavior`.
`Runner.run` (`run.py`, `async`) loops to `max_turns`; each turn ends in
`NextStepFinalOutput`, `NextStepHandoff`, `NextStepRunAgain` or
`NextStepInterruption` (defined in `run_internal/run_steps.py`; a tool with
`needs_approval` pauses the run with a resumable `RunState`). Guardrails
(`guardrail.py`) return `GuardrailFunctionOutput(tripwire_triggered,
output_info)`. `lifecycle.py` gives `on_llm_start/end`, `on_tool_start/end`,
`on_handoff`, and at agent scope `on_start`/`on_end`. Tool failure goes back
as a synthetic output through `FunctionTool.failure_error_function`
(`tool.py`), without guidance text or a counter. `models/interface.py::Model`
takes `input: str | list[TResponseInputItem]` and returns a `ModelResponse` of
Responses items: the item vocabulary is OpenAI's.

### Google ADK (`google/adk-python@dfc96d0`, installed 2.8.0)

In-process, larger, already in the tree. `LlmAgent` carries `instruction`,
`tools`, `output_schema`, `planner`, `code_executor` and six callbacks;
`BaseLlmFlow._run_one_step_async` (`flows/llm_flows/base_llm_flow.py`) is
the loop, an async generator of events; `request_confirmation.py` is the
human gate; `plugins/base_plugin.py` is the same hook set at process scope;
`ReflectAndRetryToolPlugin` turns a tool failure into a structured
`ToolFailureResponse(error_type, error_details, retry_count,
reflection_guidance)` with a per-tool counter keyed by invocation in tool
state; `LoopAgent(max_iterations)` repeats until a child escalates;
`models/registry.py` picks the class by the regexes each class's
`supported_models()` returns (`AnthropicLlm`: `claude-.*`), so this repo
already runs Claude through it (`agents/adk/claude_llm.py`).

### The same loop, three homes

| Need | Claude Agent SDK | OpenAI Agents SDK | Google ADK |
|---|---|---|---|
| Runs against Gemini (default, hackathon) | no | via LiteLLM translation | native |
| Runs against Claude | native | via LiteLLM translation | `AnthropicLlm`, in use here |
| Offline, scripted, deterministic tests | no (a real CLI) | own `Model` ABC | fake `BaseLlm` (in use here) |
| Loop in the desktop's process | no (Node subprocess) | yes | yes |
| Tool-failure reflection | inside the CLI | error text only | `ReflectAndRetryToolPlugin` |
| Human gate before a paid step | `can_use_tool`, hooks | `needs_approval` | `request_confirmation` |
| Hard budgets | turns, USD, task budget | turns | `max_llm_calls` (process-wide), `LoopAgent` |
| File/shell agent for free | yes | no | `code_executor` |
| Dependency weight | Node + CLI | `openai` + item types | pydantic, large, fast-moving |

## 3. Why own the loop, and where the precedent stops

Every coding agent that works owns a small loop and treats its checks as
tools: aider (`aider/coders/base_coder.py`: `run_one`, `lint_edited`, lint
and test output fed back as `reflected_message`, `max_reflections = 3`),
SWE-agent (`sweagent/agent/agents.py::DefaultAgent.forward`,
`per_instance_cost_limit`), OpenHands (the V0 controller
`openhands/controller/agent_controller.py` was removed in `180a35f`; the live
loop is `OpenHands/agent-sdk`'s `openhands-sdk/openhands/sdk/agent/agent.py`).
None of them runs on a framework's loop, and Ada's should be about five
hundred lines and tested like the placer, with a scripted model.

Where the precedent stops, stated plainly: **none of those loops refuses to
finish while a check is red.** aider reflects failures back and lets the
model submit after three tries; SWE-agent's `submit` is ungated. The verifier
gate in section 5 is Ada's own rule. The primitives around it are copied:

| Primitive | Copied from | Deviation, and why |
|---|---|---|
| `Agent` as data (instructions, tools, output type, guardrails) | OpenAI `agent.py` | no `handoffs`; Ada's lanes are threads joined at fixed points |
| `Runner.run(agent, input, budget)`, typed step outcomes | OpenAI `run.py`, `run_steps.py` | **synchronous**: every stage body is a plain function; budgets in model calls and tool seconds too, since a run is billed in engine minutes |
| `GuardrailFunctionOutput(tripwire_triggered)` | OpenAI `guardrail.py` | the receipt guardrail is one of them |
| `needs_approval` → interruption | OpenAI `tool.py` | the desktop step route is already the approval gate; an interruption maps to a step |
| `ToolFailureResponse` with guidance and per-tool count | ADK `reflect_retry_tool_plugin.py` | a plain object, so the count reaches the receipt |
| JSON schema from the signature | OpenAI `function_schema.py` | plain types only |

Why not an ADK `LlmAgent` with plugins, which was the closest to free. Not
the session-state rule the first draft cited (the tool already closes over
Python objects, `orchestrator.py`), but four things in the source: the loop
is an async generator and every stage body is synchronous
(`stages.py::_run_coro_blocking` is the bridge and it would sit on every
turn); `RunConfig.max_llm_calls` is one process-wide default, not a
per-stage budget; the reflect-retry counter lives in ADK tool state keyed by
invocation, out of the receipt's reach; and nothing gates `final_response`
on a set of tools having returned `ok`.

What the Claude Agent SDK gets, later. A developer-time tool, not a product
path: the repo already drives Claude Code through `.mcp.json`. For it to
repair a real `.kicad_pcb` it needs the `verify/kicad.py` verifiers (built)
and an MCP `check_board(pcb_path)` tool (not built); until then it would be
editing S-expressions by hand with no engine check on the result, billed
outside the KCU ledger and on a non-Gemini model.

## 4. What was rejected

**Build on the Claude Agent SDK.** Untestable offline, cannot run on the
default provider, ships Node with the desktop app.

**Depend on `openai-agents`.** Every Gemini and Claude response re-encoded as
Responses items and back; its LiteLLM model is that translation and its gaps
would be Ada's bugs. The ~600 lines of design that matter were copied.

**A bigger fixed graph.** Adding the verifiers as stages with the repair
prompt fed back. This is in fact what shipped first (section 8, step 2),
because it fixes the measured bug with one extra call per failing round and
the review was right that it should come before any loop. The loop exists
for what it cannot do: let the model *ask* for a check on a draft, and refuse
to finish on a red one.

## 5. The moat: verifiers a run cannot finish without

"Grounded" has one meaning here: a claim about the design is backed by a
deterministic check the model did not perform, and the run says which.

**Rule 1: verifiers are tools, and a required one that is red stops the
run.** A verifier returns a `Verdict` (`engine/silkscreen/verify/verdict.py`)
of `Clause(name, passed, detail, severity, margin, refs)`, modelled on the
case kernel's and SPICE's clause types so a signed margin passes through
unflattened. Three states, three representations: `ok`, `blocked` (a
blocking clause failed, and it says which), `unverified` (the verifier could
not run and `unverified_reason` names the fix; never `ok`, never `blocked`).
`Agent.required_verifiers` are run by the runner on every final output;
red ones become the next user turn, emitted as `propose.round`;
`unverified` ends the run `unverified`, since no repair installs
`kicad-cli`. The catalogue, with the precedent each rule was read from:

| Verifier | Proves | Status | Precedent |
|---|---|---|---|
| `validate_circuit` | well-formed IR plus package, part-number, library-pin and bus rules | built (the proposer's rules, as one verdict) | `netlist.py` |
| `electrical_completeness` | every power-class pin wired or declared open; one ground (0 R bridges count, an isolator downgrades to a warning); no rail on ground, no output on output; each IC decoupled (warning) | **built**, `verify/circuit.py` | tscircuit `checks/lib/check-no-ground-pin-defined.ts`, `check-pin-must-be-connected.ts`; azonenberg `pcb-checklist/schematic-checklist.md` |
| `erc` | KiCad's ERC on the schematic this spec draws | **built**, `verify/kicad.py::erc_from_spec`; in the proposer loop when `kicad-cli` exists | atopile `src/faebryk/libs/kicad/drc.py::run_drc` (JSON in a temp dir, `--severity-all`) |
| `drc`, `parity` | KiCad's board check and schematic-to-board parity | built as functions, not yet on a stage | same |
| `signal_rules` | I2C pull-ups, UART crossing, diff pairs | existed (`signals.py`), now inside `validate_circuit` | atopile `requires_pulls.py` |
| `simulate` | SPICE clauses with signed margins | exists (`agents/simulate.py`), not yet a `Verdict` | |
| `case_clauses` | the enclosure kernel's thirteen clauses | exists (`enclosure/kernel.py`), not yet a `Verdict` | |

Required versus advisory matters: ERC *errors* and completeness blockers
are required; decoupling, a supply pin on an oddly named net, and a power
pin declared open are warnings that ride the receipt. ERC's known-benign
types (`lib_symbol_issues`, `footprint_link_issues`, `lib_footprint_issues`)
are counted, never clauses, and each ERC type is followed by what it means
in the IR, because KiCad's "add a PWR_FLAG" is a fix the model cannot express.

**Rule 2: the receipt is structured, not filtered prose.** The first draft
proposed dropping unproven *sentences*; the review showed that deletes the
honest ones ("ERC was not run") and keeps the confident ones. Instead a
`summary` output is `Summary(claims: [Claim(text, verifier, evidence_id)],
notes)` and the output guardrail is a tripwire: a claim whose verifier did
not return `ok` in this run, or whose evidence id was never issued, fails
the whole output and comes back as a repair item (`harness/receipt.py`; the
in-tree precedent is `audit/judgment.py` fixing `Origin.SUGGESTED` in the
constructor). Notes render verbatim under "not verified".

**Rule 3: measure the loop.** `scripts/board_eval.py` runs ERC, DRC and
parity on fixed circuits with no key; with `no_connect` in the IR its ERC
column now reports what the emitter used to hide. `design_quality.py` is the
paid half. A harness that makes boards no better than the repair loop stays
off the default path.

Why this compounds and a bigger model does not: every verifier is
permanent, provider-independent engine work; a hardware engineer trusts
KiCad's report over a chatbot's confidence; and a developer can call
`erc(sch_path)`/`drc(pcb_path)` on a KiCad file they already have and get a
`Verdict`, which is the product's API once it is on the MCP server and
`/generate`. The one sentence about competitors the review would let stand:
tscircuit and atopile both ship their checks as typed output beside the
design; Ada's difference is that the checks are required, KiCad's own, and
run *inside* the proposal loop.

## 6. The contract, as built

```
engine/silkscreen/verify/
  verdict.py   Clause(name, passed, detail, severity="blocker"|"warning", margin, refs)
               Verdict(verifier, clauses, evidence, unverified_reason)
                 .status "ok"|"blocked"|"unverified"; .ok; .failures; .repair_items()
  circuit.py   electrical_completeness(spec, *, index=None) -> Verdict
  kicad.py     erc(sch), drc(pcb), parity(pcb), erc_from_spec(spec) -> Verdict
               kicad_cli_path(); BENIGN_TYPES

engine/silkscreen/netlist.py
  Device.no_connect: tuple[str, ...]   # declared open pins; validated
engine/silkscreen/schematic.py
  PlacedSymbol.no_connect              # only declared pins get the flag
engine/silkscreen/agents/propose.py
  verifier_errors(spec, on_event)      # completeness always; ERC when kicad-cli
  ERC_IN_LOOP_ENV = "SILKSCREEN_ERC_IN_LOOP"   # "0" turns ERC off; conftest does
  event "propose.verdict" {verifier, status, failed, blocking, first, unverified_reason}

engine/silkscreen/agents/harness/
  model.py     Usage(input, output, thinking, cache_read)
               ToolSpec(name, description, parameters)
               ToolCall(id, name, arguments: dict, signature: bytes|None)
               ToolResult(call_id, name, output, is_error, seconds)
               Turn(provider, text, tool_calls, stop_reason, raw_stop_reason, usage, native)
                 stop_reason in end|tool_calls|max_tokens|refused|malformed
               Message(role user|assistant, text, tool_calls, tool_results, provider, native)
               ToolModel.generate_turn(messages, *, tools, system, max_output_tokens) -> Turn
               ScriptedToolModel({marker: [Turn, ...]})   # per-marker cursor, locked
               mint_call_id(turn, index) -> "h-<turn>-<index>"
  tools.py     Tool(spec, fn(context, **args), needs_approval, is_verifier)
               function_tool, verifier_tool(name, description, fn(context)->Verdict)
  guardrails.py GuardrailFunctionOutput, InputGuardrail, OutputGuardrail, *Tripwire
  reflect.py   ToolFailureResponse, ReflectAndRetry(max_retries=3)
  receipt.py   Claim, Summary, Receipt(verdicts, evidence_ids, spent), summary_guardrail
  loop.py      Agent(name, instructions, tools, required_verifiers, output_type
                 text|json|summary, artifact_key, guardrails, max_output_tokens)
               Budget(max_turns, max_model_calls, max_tool_seconds, max_seconds)
                 .from_effort(EffortProfile)
               Runner().run(agent, input, *, model, budget, context, on_event, reflect)
                 -> RunResult(status ok|blocked|unverified|interrupted|budget, reason,
                    final_output, summary, messages, turns, receipt, usage, interruption)
  gemini.py    GeminiToolModel(model, client).from_model(GeminiModel)
  claude.py    ClaudeToolModel(base).from_model(ClaudeModel)
  design.py    design_agent(erc=None), run_design(intent, *, model, ...), DESIGN_MARKER
  __init__.py  tool_model_for(model)   # wraps GeminiModel/ClaudeModel; passes through
                                        # anything with generate_turn
engine/silkscreen/agents/resilience.py   FallbackModel.generate_turn (provider pinned
                                        once the history has an assistant turn)
engine/silkscreen/agents/pipeline.py     _EventingModel.generate_turn (model.call with
                                        tool_calls, input_tokens, output_tokens)
```

Invariants the runner enforces: the loop continues on `len(turn.tool_calls)`,
never on the provider's stop word; an assistant message is never rebuilt
from text plus calls, the adapter replays `native`; every tool result of a
turn goes back in one user message with ids matching the calls; the provider
may not change once an assistant turn exists (`ModelError`); a verifier tool
must return a `Verdict` or the run is a `ModelError`, not a quiet string.

Events ride the existing `on_event` seam and frame key: `propose.round` per
repair round; `harness.turn`, `harness.tool.start`, `harness.tool.done`,
`harness.verdict`, `harness.blocked`; and the tap's `model.call` frames for
every paid turn. Not in the contract, on purpose: handoffs, sessions,
streaming partial tokens, a second event callback, any provider's item types.

## 7. Build order

1. `Device.no_connect`, emitters draw the flag only for declared pins,
   prompt rule 3 rewritten. **Done.**
2. `verify/` package; `electrical_completeness` and `erc` inside
   `propose_circuit`'s repair loop; fixtures declare their open pins;
   `propose.verdict` events. **Done.**
3. Harness core, offline, with the scripted tool model and sixteen tests
   covering a clean run, red-then-green, a tool-checked draft, budget death,
   `unverified`, interruption, reflection, unknown tool, input tripwire,
   summary tripwire, refusal, provider pinning, scripted cursors, budget from
   effort, and both adapters' parsing and replay. **Done.**
4. `GeminiToolModel`, `ClaudeToolModel`, `FallbackModel.generate_turn`,
   `_EventingModel.generate_turn`. **Done, offline only.**
5. A `design` stage on both drivers behind `SILKSCREEN_HARNESS=1`. **Not
   done**; `stages.py` still calls `propose_circuit`, which now runs the same
   verifiers.
6. **The system agent** (Pat, 2026-09-15: "build AI CAD for a robot arm,
   then import and assemble the entire thing with the electronics"): the same
   loop with a different tool set -- `design_mechanism` (a build123d model
   script from the brief, run in the sandbox `enclosure/restyle.py` already
   uses, with earthtojake/text-to-cad's `skills/cad/references/repair-loop.md`
   procedure as the instructions), `mechanism_clauses` (signed margins: reach,
   torque per joint, clearance, printability), `board_step` (`kicad-cli pcb
   export step`), `assemble` (`enclosure/assembly.py`: mates, fasteners,
   harness lengths) and `case_clauses`, gated exactly as `erc` gates the
   circuit. Existing pieces: `agents/mechanism.py`, `mechanism/`,
   `enclosure/assembly.py`. **Not started.**
7. Scoreboard before/after with variance; `simulate` and `case_clauses` as
   `Verdict`s; `check_board` on the MCP server and the receipt on
   `/generate`; delegation to Claude Code. **Not done.**

## 8. Status, honestly

Built and green offline: everything in section 6. `engine/tests/test_verify.py`
(13 tests, three gated on `kicad-cli`) and `engine/tests/test_harness.py`
(16 tests) pass; the schematic, netlist, proposer and ADK parity tests were
updated for the no-connect rule and the new event and pass.

Live check 2026-09-15, Claude: `run_design` on an LDO prompt through `ClaudeToolModel` (claude-opus-5): the model called `check_circuit` on its draft first, then answered; all three required verifiers (`validate_circuit`, `electrical_completeness`, real KiCad `erc`) returned `ok`; 2 model calls, 29 s, 10.3k input / 1.7k output tokens, no repair round. One caveat: it added a power LED nobody asked for, which is a scope question for the prompt, not the loop. Gemini's adapter has not made a live call yet.

Not exercised live before that check: neither adapter had made a real call. The Gemini key is
on the free tier (twenty calls a day on the default model) and the review
was explicit that the live scoreboard cannot be run on it; the adapters'
request shapes are pinned by the offline tests against the installed SDKs'
types (`google-genai` 2.22, `anthropic` 1.5) and nothing more. A live turn
against each is the first thing to run once billing is on, gated the
`test_live_model.py` way.

Known gaps in what shipped: `FallbackModel.generate_turn` pins the provider
but the runner does not yet restart from the original input when the pinned
rung dies (it raises); `Budget.from_effort` derives from `max_repairs`
rather than a field on the profile; `electrical_completeness` classifies by
library `power_in`/`power_out` only when the KiCad library index is on
(`SILKSCREEN_KICAD_LIBRARY=1`) and by pin name otherwise; the receipt is on
`RunResult` and nowhere a user sees yet.

## 9. What the red team found, and what changed

Four reviewers, one lens each, read-only, evidence by file and line.

- **Architecture.** The harness as first drafted emitted under a stage name
  both UIs drop and bypassed the failover ladder and the `model.call` tap;
  the ADK rejection cited a constraint already worked around. Changed: the
  loop emits `propose.round` and rides `on_event`; `FallbackModel` and the
  tap gained `generate_turn`; section 3's ADK reasons are the four from the
  source; `Verdict` carries clauses with margins, not strings; the scripted
  model has per-marker sequences.
- **Grounding.** The critical finding: both emitters laundered "forgotten"
  into "deliberately open", so ERC, and the proposed verifier, would have
  vouched for the unwired ground. Changed: `Device.no_connect`; the rule text
  for R1 to R4 with the false positives each naive rule produces (thermal
  pads never forced to ground, isolators, `CH_V3`-style rails as warnings,
  shared decoupling accepted); the receipt made structural instead of prose
  surgery; one net classifier (`schematic.net_class`) instead of a fifth.
- **Seams.** The `Turn`/`Message` fields that both providers actually
  require (native replay, signatures, one tool-result message, harness
  minted ids, a five-word stop vocabulary); the scripted model's cursor and
  lock; token counts on `model.call`; the note that `board_eval.py` cannot
  yet score the harness because it drives a text-only scripted model.
- **Product.** The diagnosis was unmeasured and the precedent partly
  misquoted; the ADK root has no lookup tools; the Claude Code delegation
  is a distraction before the KiCad verifiers exist; Friday's order is
  verifiers first, loop after. Changed: section 1 is a measurement; section
  3 says where the precedent stops; delegation moved to "later"; the build
  order in section 7 is the reviewer's, and steps 1 and 2 shipped before
  the loop.

## 10. Risks still open

- **ERC in the loop costs a schematic draw and a `kicad-cli` run per round**
  (about one to two seconds); the budget must show in the receipt.
- **Library-bound symbols on mixed sheets.** A library `input` pin fed only
  through generated-symbol `passive` pins may raise `pin_not_driven`; not
  measured, and it must be before ERC gates the default desktop path.
- **The model now has to declare every open pin.** For a 38-pin module that
  is a longer answer and, on a weak model, more repair rounds; the
  alternative, an "all other pins open" shorthand, is the laundering again.
- **Determinism.** A loop with a real model is not reproducible; the
  scoreboard has to report variance across runs, not one number.
