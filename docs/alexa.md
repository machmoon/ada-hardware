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
#   "How did my regulator board go?"   The memory pill in the header now reads
#   "Remembering (scripted): USB-C power input, 3.3 V logic"; say
#   "Ask Ada for a sensor board" and Ada asks "Last time you chose: USB-C
#   power input and 3.3 volt logic. Same again?"; "Yes" adds them to the intent.
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

**(c) With Amazon Bedrock AgentCore Memory** (design preferences between
conversations; see [Memory](#memory-design-preferences-across-conversations)):

```bash
./.venv/bin/python scripts/aws/agentcore_memory.py create   # once; idempotent; prints the export line
export ADA_AGENTCORE_MEMORY_ID=AdaDesignPreferences-XXXXXXXXXX
./.venv/bin/python -m alexabot.sim --scripted     # AgentCore for memory, everything else offline
./.venv/bin/python -m alexabot.sim --agent bedrock --workers scripted   # and Nova + Polly
./.venv/bin/python scripts/aws/agentcore_memory.py teardown --yes       # delete it and every preference
```

Startup refuses (exit 2), naming every problem at once, when the `alexa` extra
is missing, when `--agent bedrock` or `--tts polly` has no AWS credentials
("run with --scripted"), when `--workers live` has no model provider, when a
port is busy (`--mcp-port 0` / `--port 0` pick free ones), on any
non-loopback `--host` (the page has no login), and for memory: `--memory
agentcore` with no id, an id that is not `<name>-<10 characters>`, AgentCore
Memory with no AWS credentials, `--mcp-url` with a named memory and no
`--memory-actor`, and a resource that one `GetMemory` finds missing, not
`ACTIVE`, or without the user-preference strategy on
`/users/{actorId}/preferences/`. An operator who named a memory gets a
refusal, never a silent downgrade.

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
| `--memory {agentcore,scripted,off}` | `agentcore` when an id is set; else `scripted` with `--scripted`; else `off` | where design preferences live between conversations |
| `--memory-id` / `ADA_AGENTCORE_MEMORY_ID` | none | the AgentCore memory id `create` prints |
| `--memory-region` / `ADA_AGENTCORE_REGION` | `--region` | AgentCore Memory's region |
| `--memory-actor` | the in-process account | whose preferences; required with `--mcp-url` and a named memory (the implicit scripted memory turns itself off instead, and says why) |
| `--max-memory-calls` / `ADA_AGENTCORE_MAX_CALLS` | 60 | process-wide AgentCore Memory call cap |

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

### Memory: design preferences across conversations

`alexabot/memory.py`. What is remembered is narrow on purpose: the **design
preferences a person states** ("powered from USB-C", "3.3 V logic", "no
LED"), so a new conversation can offer them back. Boards stay in alexabot's
SQLite store and reach speech only through `recall_my_boards`; a
model-extracted copy of "one open blocker" would go stale when the board
changes and still be spoken as truth.

- **One seam, three implementations, one contract.** `AgentCoreMemory` (boto3's
  `bedrock-agentcore` client, injected), `ScriptedMemory` (a few regular
  expressions over the person's words, in this process, for `--scripted` and
  the tests; labelled scripted everywhere it shows) and `MemoryOff`. `recall`
  and `record` never raise; a failure is `unavailable`/`failed` with a
  sentence from `memory.REASONS` ("this AWS identity may not use AgentCore
  Memory", "AgentCore Memory is busy", ...), never an empty `ok` and never
  exception text. `alexabot/tests/test_alexa_memory_contract.py` runs the same
  assertions over all three, the adapter behind botocore's `Stubber`.
- **Write: one `CreateEvent` per spoken turn**, after the reply, on a writer
  thread (a queue of 32; a full queue drops the write and says so in the
  trace), so a write never delays speech. The payload is the question the
  person was answering (`ASSISTANT`, only when Ada's last utterance was a
  question) and their own words (`USER`), with `clientToken`
  `<conversation>-<turn>` so boto3's own retry stores a turn once. **Never
  written:** Ada's reply, a default she chose, a host note, a hint, a tool
  result, a chip label (`source: chip`), or a turn whose only tool call was
  "you choose": the built-in extractor reads both roles, and "I'll assume no
  LED" must never become "prefers no LED".
- **Read: one `RetrieveMemoryRecords` per conversation**, when it opens (after
  the greeting, which does not wait; the first turn waits at most 2 s), topK 5
  in `/users/{actorId}/preferences/` with a fixed query. Records below a 0.2
  relevance score (the Strands integration's default) or in another
  namespace are dropped; the text is the documented JSON's `preference`, else
  the raw text. What this conversation teaches is extracted about a minute
  later and belongs to the next one.
- **Ada offers, never assumes, and the request's own words win.** While a new
  board could start, the turn carries a bracketed note for the model:
  `[Memory: from earlier conversations with this person: "USB-C power input";
  "3.3 V logic". Ask first, exactly: "Last time you chose: USB-C power
  input and 3.3 volt logic. Same again?"]`. The host first drops every
  preference whose category (power input, logic voltage, indicator,
  connector, size) the request already names, so "a 5 V board with a barrel
  jack" gets no note and no question; an uncategorised record shows on the
  pill but is never offered. Only a yes puts the survivors into the
  `start_board_design` intent; the next turn's note is re-filtered against
  the answer ("yes, but a barrel jack" drops USB-C). The offer is made once
  per conversation.
- **A fourth guard rule, `memory_claim`.** Model text that claims to remember
  ("last time", "I remember", "you usually", "as before", ...) is kept only
  when this turn carries a note and the sentence names something in it;
  otherwise it becomes the ask, "I'll go by what you just asked for.", "I
  don't have any preferences saved from earlier conversations.", or the
  memory-off sentence. Like the other three it is a regular expression: it
  catches the tested phrasings, not every possible one.
- **Who.** The actor is `acct-` plus 32 hex characters of the SHA-256 of the
  alexabot account (the one whose boards these are), so the account name
  never leaves the machine and a `.` in it cannot break botocore's `ActorId`
  pattern. The session is the sim's `conv_<16 hex>`.
- **What the page shows.** A pill in the header ("Memory off", "Memory:
  checking", "Remembering: USB-C power input, 3.3 V logic", "Memory on:
  nothing saved yet", "Memory unavailable", or the scripted forms), built on
  the server by `memory.chip_label` and sent as `memory` events, so the page
  never says it remembers something the server did not report. Tapping it
  lists each preference with the date it was saved and the source. The "What
  the agent did" drawer lists every memory call and the memory budget; the
  footer says what is sent where. A failed write is one notice ("I couldn't
  save that to memory; your board is unaffected.") and the pill turns
  `unavailable`. `GET /api/config` carries `memory: {kind, name, strategy,
  namespace, region, reason}`, never the memory id, the account or a
  credential.
- **Direct boto3, not the AgentCore SDK** (read as source at `c7423e5`, not a
  dependency): its `retrieve_memories` answers `[]` on a `ClientError` (a
  refusal would read as "nothing remembered"), `create_event` sends no
  `clientToken`, `create_or_get_memory` matches a resource by id prefix, and
  its Strands session manager writes one event per message (tool results and
  host notes included, 4 to 6 per spoken turn) and retrieves on every user
  message. Past events are not rehydrated into the history (a stated
  deviation from KayLerch D30): a replayed "the board is routing" is stale.

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

Memory, offline on this Mac (2026-09-26): the scripted agent and
`ScriptedMemory` drove a first conversation that asked for "a three point
three volt regulator board powered from USB-C" and a second whose pill read
"Remembering (scripted): USB-C power input, 3.3 V logic", whose "Ask Ada for
a sensor board" got the ask and whose "Yes" started "a sensor board, with
USB-C power input and 3.3 V logic"; a third asked for "a 5 V board with a
barrel jack" and got no note and no ask. **AgentCore Memory itself has not
been run live from this repo yet**: `scripts/aws/agentcore_memory.py` and the
adapter are tested against botocore's `Stubber` (which checks every request
against the service model offline), and the record text shape, extraction
latency, the 0.2 score floor and the IAM actions on the account are
unverified until they are.

### Sources

- Amazon Bedrock AgentCore Memory, read as source to decide and not a
  dependency: aws/bedrock-agentcore-sdk-python `c7423e5`
  (`memory/client.py`, `memory/constants.py`,
  `memory/integrations/strands/session_manager.py` and `config.py`);
  awslabs/amazon-bedrock-agentcore-samples `e1a55b3`
  (`01-features/04-manage-context-of-your-agent/memory/02-long-term-memory/01-built-in-strategies/user-preference.py`,
  `04-namespaces/README.md`); botocore `bedrock-agentcore/2024-02-28` and
  `bedrock-agentcore-control/2023-06-05` `service-2.json`/`waiters-2.json`
  (`86201a3`); KayLerch's `packages/agent/src/memory/agentcore-memory.ts` and
  `docs/decisions.md` D30/D31.
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

## AWS integrations

The simulated Alexa+ experience uses four AWS services. Each is behind a seam
with an offline stand-in, so `--scripted` runs the same code paths with no AWS
account and no key; with AWS, each is used only when configured, and a
missing piece is a refusal or an "off" said out loud, never a silent pretend.

```text
 Browser: alexabot/web (Chrome or Edge)
   | voice in: the browser's own Web Speech API
   | POST /api/conversations/<cid>/turns, EventSource .../events
   v
 Sim server: python -m alexabot.sim, 127.0.0.1:8790 (alexabot/sim.py)
   |  one Strands Agent per conversation (Strands Agents 1.57.1, alexabot/agent.py)
   |
   |-- Amazon Bedrock Runtime, ConverseStream ...... model us.amazon.nova-2-lite-v1:0
   |     (us-east-1)                                 picks one of Ada's six tools per turn
   |
   |-- Amazon Polly, SynthesizeSpeech .............. Joanna, neural, mp3
   |     (us-east-1)                                 speaks what Ada already said
   |
   |-- Amazon Bedrock AgentCore Memory ............. resource AdaDesignPreferences
   |     (us-east-1)                                 RetrieveMemoryRecords: once per conversation
   |                                                 CreateEvent: once per spoken turn
   |                                                 built-in user-preference strategy,
   |                                                 /users/{actorId}/preferences/
   |
   '-- MCP 2025-11-25, Streamable HTTP .............. http://127.0.0.1:8789/mcp
         alexabot: six voice tools (alexabot/tools.py), SQLite board store
           '-- service/steps.py -> the Ada engine (silkscreen):
               propose, place (CP-SAT), route, review -> a KiCad project
```

| Service | Region | Called from | What for | Offline stand-in | Cap |
|---|---|---|---|---|---|
| Amazon Bedrock, Nova 2 Lite (`us.amazon.nova-2-lite-v1:0`, a cross-region inference profile) | `--region`, default `us-east-1` | `strands.models.BedrockModel` (`agent.bedrock_model_factory`), `ConverseStream` | the agent's language model: one call per spoken turn, which picks a tool; tool results end the turn on their own pre-written speech | `alexabot/scripted_agent.py`, a rule-based `strands.models.Model` | `--max-model-calls` 60 |
| Amazon Polly, `SynthesizeSpeech` | `--region` | `alexabot/polly.py` | Ada's voice: neural `Joanna`, mp3, of an utterance already said, lazily and cached | the browser's `speechSynthesis` | `--max-polly-calls` 60 |
| Amazon Bedrock AgentCore Memory (`bedrock-agentcore`, `bedrock-agentcore-control`) | `--memory-region`, default `--region` | `alexabot/memory.py` (boto3 directly), `scripts/aws/agentcore_memory.py` | design preferences between conversations: the person's words in, preferences extracted by the built-in user-preference strategy, offered back as a question | `ScriptedMemory` (`--scripted`), or off | `--max-memory-calls` 60 |
| Strands Agents (the open-source SDK, Apache-2.0) | local | `alexabot/agent.py` | the agent loop, hooks, the Bedrock model class and the MCP client | none needed: it runs offline with the scripted model | |

Credentials come from the standard AWS chain (`aws configure`, `aws sso
login`, or the environment); the sim never prints them, and startup refuses
in words when a configured service has none. IAM, least privilege:

- the agent: `bedrock:InvokeModelWithResponseStream` on the Nova 2 Lite
  inference profile and the foundation model it routes to;
- the voice: `polly:SynthesizeSpeech`;
- memory, the sim: `bedrock-agentcore:GetMemory`, `CreateEvent`,
  `RetrieveMemoryRecords` on the one memory resource;
- memory, the setup script: `bedrock-agentcore:CreateMemory`, `ListMemories`,
  `GetMemory`, `DeleteMemory`.

Cost, from the AWS pricing pages on 2026-09-26: AgentCore Memory is $0.25 per
1,000 short-term events (one per written turn) and $0.50 per 1,000
retrievals (one per conversation), plus $0.75 per 1,000 stored records a
month; the extraction model is included for built-in strategies with no
override, and this resource sets none (no execution role). A ten-turn
conversation is about a quarter of a cent in memory calls.

Without AWS: `python -m alexabot.sim --scripted` (no account, no key, nothing
leaves the machine). With AWS, pick what to turn on:

```bash
python -m alexabot.sim --scripted                                    # none
ADA_AGENTCORE_MEMORY_ID=... python -m alexabot.sim --scripted        # AgentCore Memory only
python -m alexabot.sim --agent bedrock --workers scripted            # Bedrock + Polly (+ memory if the id is set)
python -m alexabot.sim --agent bedrock --tts browser --memory off    # Bedrock only
```

Hosting is not part of this: everything above runs on one machine, and the
sim refuses a non-loopback `--host`.

## Not built

Cancel, booking a design review, an MCP Apps card, per-judge daily caps, and
an `/integrations` entry. For the simulated experience: a real Alexa device
or skill (there is none, and no Amazon sign-in), AgentCore Runtime, MCP Apps
(`ui://ada/board.html`), the hosted deployment, and per-judge access codes.
For memory: forgetting a preference from the page (`BatchDeleteMemoryRecords`
exists), rehydrating past messages, a semantic or summary strategy (boards
stay in SQLite), and memory on a hosted deployment.
