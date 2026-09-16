# How open-source AI PCB and AI CAD projects build their loop

*Read at source on 2026-09-15, for the question "which provider, and how do
they wire the loop". Companion to `docs/agent-harness.md`.*

| Project | Provider wiring | Loop shape | Verifier | Path read |
|---|---|---|---|---|
| **tscircuit/prompt-benchmarks** (MIT) | `openai` SDK, `gpt-4o-mini`, key from env | recursive `runAiWithErrorCorrection`: ask, extract the ```tsx block, evaluate, on failure push `{code, error}` to `previousAttempts` and re-ask with every prior attempt replayed as assistant/user pairs; `maxAttempts` | `evaluateTscircuitCode` (compile + circuit-json checks) | `lib/ai/openai.ts`, `lib/tscircuit-coder/run-ai-with-error-correction.ts`, `lib/ask-ai/ask-ai-with-previous-attempts.ts` |
| **PCBSchemaGen** (paper code) | `openai` SDK pointed at **OpenRouter** (`base_url`), default model `google/gemini-3-flash-preview`; a price table covers Gemini, Claude, GPT | one `messages` list, `for attempt in range(num_of_retry)`: extract code block, syntax check, SKiDL runtime + ERC, topology verification; each failure appended as a user message with a `--feedback full|weak|none` ablation | syntax, ERC, topology graph match | `task/run.py:958-1060` |
| **LGAI-Research/PCBWorld** (benchmark) | `APIProvider` with `_call_openai`/`_call_anthropic`/`_call_google`/`_call_together` behind one interface; transient-error markers matched by string across SDKs; local vLLM as the other backend | environment steps (place, route, via, finish) with a rejection streak and "[no effect]" markers fed back into the prompt | the KiCad-engine environment itself (DRC/ERC/routing) | `methods/llm_agent/policy/model_provider.py`, `wrappers/feedback.py` |
| **Adam-CAD/CADAM** (GPL-3.0) | Anthropic Messages API by raw `fetch` (`src/server/anthropic.ts`), Vercel `ai` SDK tools | the model calls `build_parametric_model` (OpenSCAD source), the browser compiles and returns a multi-view preview, the model calls again or `answer_user` | compile + the model's own look at renders | `shared/chatAi.ts` |
| **earthtojake/text-to-cad** (MIT, 15.9k stars) | **no provider code at all**: a `skills/cad/SKILL.md` for Claude Code plus `agents/openai.yaml` for Codex; the agent is whichever coding agent loads the skill | the harness *is* the coding agent; `references/repair-loop.md` is a written procedure (read the failure, classify, smallest fix, rerun, rerun dependents, report residual risk) | `cadgen` build, STEP inspection, snapshots | `skills/cad/` |
| **atopile** | MCP server + agent skill; no loop of its own | the agent runs `ato build`, reads typed ERC faults | `libs/app/erc.py`, `libs/kicad/drc.py` | `src/faebryk/libs/` |

What is the same everywhere: a frozen model, a deterministic checker, the
failure text appended as the next user turn, a small retry cap. Nobody
builds on an agent framework; the two most-starred projects (text-to-cad,
atopile) do not even own a loop, they hand a skill or MCP server to Claude
Code or Codex. The one project with a provider abstraction (PCBWorld) wrote
a 500-line switch over four SDKs plus a string-matched transient-error
list, which is what `agents/resilience.py` already is here.

Where OpenAI specifically appears: as the *client library*, not the model.
tscircuit's benchmark uses `gpt-4o-mini` through the `openai` package;
PCBSchemaGen uses the `openai` package as an OpenRouter client with Gemini as
its default model; PCBWorld uses it for OpenAI and Together. None of them
uses the OpenAI Agents SDK.

What Ada's loop has that none of these do: a required-verifier gate that
refuses to finish on a red check (they all let the model submit after the
cap), and KiCad's own ERC inside the proposal round rather than after it.
What they have that Ada should copy next: PCBSchemaGen's feedback-level
ablation (`full|weak|none`) as a scoreboard axis, and text-to-cad's written
repair procedure as the design agent's instructions for the CAD restyle.
