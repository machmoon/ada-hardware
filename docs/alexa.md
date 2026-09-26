# Alexa+ voice tools (`alexabot/`)

Six MCP tools an Alexa+ agent calls to design a printed circuit board by voice.
`python -m alexabot` serves them over Streamable HTTP (MCP 2025-11-25) at
`http://127.0.0.1:8789/mcp`. Every call returns in well under a second: the
work runs on a background thread through `service/steps.py`, and the agent
asks `board_status` how it is going.

**Unverified live.** No Alexa+ agent has called this server. The only
independent clients it has met are python-sdk's. Nothing here is an Alexa
skill or talks to Amazon. It never orders anything.

## Run it

```bash
python -m alexabot --scripted          # offline, no key: every request gets the practice regulator board
python -m alexabot                     # the configured model (ANTHROPIC_API_KEY or GOOGLE_API_KEY)
python -m alexabot --scripted --scripted-delay 1.5   # a slow model, to watch the tools not wait
```

Live mode refuses to start without a model provider and names `--scripted`.
The command reads `.env` (setdefault, `ADA_REPO_ROOT` then the cwd).

| Setting | Default | What it does |
|---|---|---|
| `--host` | `127.0.0.1` | Anything else needs a credential below, or it refuses to start |
| `--port` / `ALEXABOT_PORT` | `8789` | 8788 is `silkscreen.mcp.http` |
| `--db` / `ALEXABOT_DB` | `~/.kaleo/alexa-boards.sqlite3` | The board history |
| `ALEXABOT_MAX_ACTIVE` | `4` | Boards being worked on at once, process-wide |
| `SILKSCREEN_STEPS_DIR` | `~/.kaleo/alexa-steps` | Where the KiCad files are written |
| `MCP_TOOL_CALLS_PER_MINUTE` | `60` | The MCP rate limit |
| `--allow-origin` | | An extra browser origin |

## Who is calling

- `Authorization: Bearer ada_…` (or the path `/mcp/ada_…`), when
  `SILKSCREEN_API_KEYS_DB` is set: that key's account. Mint one with
  `python -m service.auth create --account <name>`. Each account has its own
  history.
- `MCP_HTTP_TOKEN`: the `local` account. **Every caller with the shared token
  shares one history.**
- Neither set: `local`, with no auth, on loopback only.

## The tools

| Tool | Returns at once with | Background work |
|---|---|---|
| `start_board_design(intent, request_id)` | `reading` | read + plan (`steps.start_once`, `plan_first`, `prefetch: false`) |
| `answer_design_questions(session_id, answers?, you_choose?)` | `questions` (next one) or `proposing` | propose, once every question has an answer or a default |
| `continue_design(session_id)` | `placing`, or queued while planning or drafting | place, route, review |
| `board_status(session_id?)` | the state, one sentence, the next question, the summary | none |
| `explain_finding(session_id, which)` | one review finding in plain speech | none |
| `recall_my_boards(query?, limit?)` | this account's boards, newest first | none |

Every result is `structuredContent` plus a `speech` string, sent twice as
text: the sentence alone, then the JSON. `speech` follows KayLerch's voice
rules: at most three sentences, one question and it last, no symbols, no raw
error text. A review that did not run is spoken as not run, a failed one as
nothing known. Neither is ever spoken as zero findings. Speech never says
KiCad checked the board; the hosted stack has no KiCad.

The data carries no exception text either, since the agent is told to prefer
it over the speech. A failure's `failure.reason`, and a review that raised
its `review.detail`, say the exception's class and the cause in words
(`ModelError: the AI model didn't answer`). The message itself, which can
quote a provider's error or a URL with a key in it, stays in the board's row
in the 0600 database (`failure_reason`, and `review.error` in the summary)
for whoever runs the server; stderr gets the class name only.

`request_id` is a UUID the agent mints per new board. A retry with the same
one returns the same board, whatever happened to it. The same id with a
different request is refused. Two accounts never share one. The JSON-RPC id
is not used, because clients number those per connection from zero.

## What survives a restart

The history does; the work does not. `service/steps.py` keeps its sessions in
memory, so at startup every board that was mid-run is marked `failed` with
"the service restarted". That is safe only because one process owns the
database. Run one instance per database.

## Sources

- Start/poll instead of MCP tasks: python-sdk marks tasks deferred,
  `examples/stories/tasks/README.md` (main `f1b6589`).
- The poll shape and voice rules: KayLerch/alexa-skill-mcp-bridge (Apache-2.0,
  `ca2c2ef`): `docs/decisions.md` D8/D9, `packages/agent/prompts/voice.md`,
  `bridge.config.ts`. Design only; no code copied.
- `verify_token` and `get_access_token`: python-sdk
  `src/mcp/server/auth/provider.py` and `src/mcp/server/auth/middleware/auth_context.py`.
- Review wording: `frontend/src/lib/voice.js` `reviewSpeech`/`findingSpeech`.
- Key reuse with a different request: the IETF idempotency-key draft.

## Simulated Alexa+ experience (`python -m alexabot.sim`)

A web page that simulates the Alexa+ experience: you talk to it, a Strands
agent picks one of the six tools above for each thing you say, and a board
card on an Echo-Show-like screen fills in from the tools' `structuredContent`.
**It is a simulation.** It is not Alexa, not an Alexa skill, and not made by
Amazon; the page says so in its header pill ("Simulated Alexa+ experience"),
its greeting and its footer, and the agent is told to say "I'm not Alexa" if
asked.

### Run it

```bash
# once: the alexa extra (strands-agents + boto3). In the dev venv, install it
# in the same request as adk, or pip moves OpenTelemetry past google-adk
# 2.8's <=1.42.1 pin (measured with a dry run, 2026-09-26):
./.venv/bin/python -m pip install -e ".[dev,agents,cloud,adk,cad,alexa]"
# a fresh venv for the sim alone:  pip install -e ".[alexa]"
```

**(a) Offline, scripted: no AWS account, no API key.**

```bash
./.venv/bin/python -m alexabot.sim --scripted
# open http://127.0.0.1:8790 in Chrome or Edge (Firefox has no speech
# recognition; type instead), press "Start talking to Ada", and say or type:
#   "Ask Ada for a three point three volt regulator board powered from USB-C."
#   "Up to one amp."   "You choose."   "Yes, place and route it."
#   "Explain the first blocker."   then "New conversation" and
#   "How did my regulator board go?"
```

`--scripted` is `--agent scripted --workers scripted --tts browser`. The
language model is replaced by a rule-based stand-in
(`alexabot/scripted_agent.py`), a real `strands.models.Model`; the Strands
agent loop, the Strands `MCPClient` over real Streamable HTTP, the alexabot
server and `service/steps.py` are all real. Ada's own workers give every
request the practice regulator board (`alexabot/scripted.py`), 1.5 s per
model answer so the progress narration is visible. Voice out is the browser's
own `speechSynthesis`. The page says "Scripted: no model" in its header, and
the greeting, the card badge and the footer say it is scripted.

**(b) Live: Amazon Nova 2 Lite on Bedrock picks the tools, Amazon Polly speaks.**

```bash
aws sts get-caller-identity >/dev/null   # credentials present; us-east-1 with Nova 2 Lite access
./.venv/bin/python -m alexabot.sim --agent bedrock --workers scripted \
    --max-model-calls 20 --max-polly-calls 10   # Bedrock + Polly; Ada's workers canned
./.venv/bin/python -m alexabot.sim --agent bedrock   # also Ada's real worker model
                                                     # (GOOGLE_API_KEY or ANTHROPIC_API_KEY)
```

Startup refuses (exit 2), naming every problem at once, when the `alexa` extra
is missing, when `--agent bedrock` or `--tts polly` has no AWS credentials
("run with --scripted"), when `--workers live` has no model provider, when a
port is busy (`--mcp-port 0` / `--port 0` pick free ones), and on any
non-loopback `--host`: the page has no login.

| Flag / env | Default | Meaning |
|---|---|---|
| `--scripted` | off | `--agent scripted --workers scripted --tts browser` |
| `--agent {bedrock,scripted}` | `bedrock` | who picks the tools |
| `--workers {live,scripted}` | `live` | Ada's own worker model: the provider ladder, or canned |
| `--tts {polly,browser}` | `polly` with bedrock, else `browser` | Ada's voice |
| `--model-id` / `ALEXA_SIM_MODEL_ID` | `us.amazon.nova-2-lite-v1:0` | Bedrock model or inference profile |
| `--region` / `AWS_REGION` | `us-east-1` | explicit, because Strands falls back to us-west-2 |
| `--voice` / `ALEXA_SIM_POLLY_VOICE` | `Joanna` | Polly voice, neural engine |
| `--max-model-calls` / `ALEXA_SIM_MAX_MODEL_CALLS` | 60 | process-wide Bedrock call cap |
| `--max-polly-calls` / `ALEXA_SIM_MAX_POLLY_CALLS` | 60 | process-wide Polly call cap |
| `--port` / `ALEXA_SIM_PORT` | 8790 | the page and its API |
| `--mcp-port` | 8789 | the in-process alexabot |
| `--mcp-url` | none | use another alexabot; board images are then off and the card says why |
| `ALEXA_SIM_MCP_KEY` | none | an `ada_` key for the agent, when the tools need a credential |
| `--db`, `--scripted-delay` | alexabot's; 1.5 s with scripted workers | passed through |

### How it works

One process, loopback only: the alexabot MCP server (:8789, unchanged), and
the sim server (:8790, `alexabot/sim.py`, stdlib `ThreadingHTTPServer`)
serving the page, its JSON API and an EventSource stream per conversation.
Each browser conversation gets a Strands `Agent` (`alexabot/agent.py`) whose
tools are one shared Strands `MCPClient`'s `list_tools_sync()`. The sim never
calls the runner or `steps` itself; every tool call, including the host's
polls, goes over MCP.

- **Cards come from the agent** (reviewer finding M7). Strands' Python
  `MCPClient` keeps `structuredContent` on every tool result
  (`tools/mcp/mcp_client.py` `_handle_tool_result`); an `AfterToolCallEvent`
  hook turns it into a card (`alexabot/cards.py`) and rewrites the model's own
  copy to one compact JSON line (KayLerch D28: Nova 2 Lite collapses on mixed
  JSON and text). A card is logged only when it changed; when a tool the
  model called answers with an unchanged card ("Back to the board" after a
  finding), a `focus` event brings that card back to the screen, live and on
  a resumed page. Host polls keep the plain dedupe.
- **One model call per spoken turn.** Every tool returns a pre-written
  `speech`; an `AfterToolsEvent` hook sets `end_turn` to it, so Strands stops
  without calling the model again. Text the model writes itself passes a port
  of KayLerch's `cleanSpeech`, the voice shape rules, and three honesty
  guards (no clean-review claim unless one ran, no "KiCad checked", no
  ordering); every replacement is a `trace` event. The guards are regular
  expressions over the sentence, so they catch the phrasings in
  `alexabot/tests/test_alexa_sim_prompts.py` ("found zero findings",
  "didn't find anything", "passed the review", "came back clean", "checks
  out", "KiCad says it's fine", "I placed the order", ...) and not every
  possible one; what Nova reads of a board whose review did not run carries
  `review: "not_run"` and no findings list at all, so it is never shown an
  empty list it could read as zero.
- **The host mints `request_id`** in a `BeforeToolCallEvent` hook, once per
  (turn, normalised intent), and fills a missing `session_id` from the
  conversation.
- **The host polls, not the model.** While a board works, a poller calls
  `board_status` every 1 to 3 s as a Strands direct tool call
  (`record_direct_tool_call=False`) on a second, tool-only agent that shares
  the hooks but not the history; it narrates each change of state in one
  sentence and, when the board comes to rest, speaks the full sentence and
  passes the status to the next turn as a bracketed `[Host note: ...]`.
- **Voice.** In: the browser's Web Speech API through
  `frontend/src/lib/voice.js` `createDictation` (served read-only at `/lib/`),
  one utterance per tap, never an auto-open microphone. Out: Amazon Polly
  (`SynthesizeSpeech`, neural, mp3, `TextType` text) of an utterance Ada
  already said, lazily, cached and within the call budget
  (`GET /api/conversations/<cid>/speech/u_N.mp3`); otherwise, and in scripted
  mode, the browser's `speechSynthesis` through `createSpeaker`. The page says
  which voice is speaking, and says so again if Polly falls back.
- **The board image** is drawn by the host from the routed `.kicad_pcb` the
  tool result names, with `silkscreen.audit.render.render_svg` (an independent
  reader; each part a `<g data-ref>`, which is how a finding highlights its
  parts). `service/steps.py` writes no image. "Open in KiCad" downloads the
  file.

API: `GET /api/config`; `POST /api/conversations` (201, logs the greeting
without a model call); `GET /api/conversations/<cid>` (resume snapshot);
`POST /api/conversations/<cid>/turns` `{text, source: voice|typed|chip,
hint?}` (202, 409 busy, 400, 404); `GET /api/conversations/<cid>/events`
(`text/event-stream` with `id`/`event`/`data` and `Last-Event-ID` resume, or a
JSON snapshot); the speech route; `GET /api/boards/<session_id>/board.svg`
and `board.kicad_pcb`. The server checks `Host` (421) and a POST's `Origin`
(403), limits bodies to 8 KiB, and sends a same-origin-only CSP. The page is
no-build vanilla JS and CSS (`alexabot/web/`), on the Ada site's tokens with
Libre Baskerville self-hosted (OFL, `fonts/OFL.txt`); it makes no external
request.

The **"What the agent did"** drawer lists every model call and every MCP tool
call with its arguments and origin (model or host poll), and the budget left.

### What it has been run against

Offline, on this Mac: the scripted agent drove a whole session
(start, two questions, drafted, place and route, done with the blocker,
explain, recall from a new conversation) through Strands 1.57.1 and `mcp`
2.1.1 against the alexabot server; `mcp` 2.1.1's client first tries a newer
protocol, gets a 400, and falls back to `initialize` at 2025-11-25.
Live, on 2026-09-26, with `--agent bedrock --workers scripted` (Nova 2 Lite
in us-east-1 picking the tools, Ada's workers canned): a whole session from
the first request to explaining a finding and recalling the board from a new
conversation, 8 Bedrock calls and 10 Polly calls, one model call per spoken
turn, 0.5 to 1.2 s per model call. Nova picked the right tool with the right
arguments on all seven tool turns and answered "Are you Alexa?" with the
not-Alexa sentence; every board fact it spoke came from a tool's speech; the
eleventh audio request hit the Polly cap and the page fell back to the
browser's voice. No credential appeared in the server log, the page or
`/api/config`. A second run of three chip turns (3 Bedrock calls) checked that
"Back to the board" after a finding brings the board card back. That is the
whole of the live evidence: the honesty guards have only been exercised
offline, because Nova never wrote a clean-review claim in those turns.

### Sources

- Strands Agents (strands-agents 1.57.1 = sdk-python `python/v1.57.1`,
  `6da3f48`, Apache-2.0): `Agent`, `hooks/events.py`, `tools/_caller.py`,
  `tools/mcp/mcp_client.py`, `models/bedrock.py`; the scripted model's stream
  events follow `strands-py/tests/fixtures/mocked_model_provider.py`. Called,
  not copied.
- KayLerch/alexa-skill-mcp-bridge (`ca2c2ef`, Apache-2.0): the turn contract
  and prompts. `alexabot/prompts/voice.md` and `tool-result.md` are adapted
  from its files of the same names (`alexabot/prompts/NOTICE.md`), and
  `agent.clean_speech` ports `src/speech.ts` `cleanSpeech`.
- APL `alexa-layouts` names (alexa/apl-suggester `fb235dd`): the card fields.
  Names only.
- botocore `polly/2016-06-10/service-2.json` (`86201a3`): the Polly request.
- Libre Baskerville (OFL 1.1), from `@fontsource-variable/libre-baskerville`.

No open-source Alexa+ web simulator exists to copy; the Alexa developer
console's simulator is closed. The page's layout is Ada's own.

## Not built

Cancel, booking a design review, an MCP Apps card, per-judge daily caps, and
an `/integrations` entry. For the simulated experience: a real Alexa device
or skill (there is none, and no Amazon sign-in), AgentCore Runtime and
Memory (cross-session context is `recall_my_boards`), MCP Apps
(`ui://ada/board.html`), the hosted deployment, and per-judge access codes.
