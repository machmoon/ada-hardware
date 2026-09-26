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

## Not built

Cancel, booking a design review, an MCP Apps card, per-judge daily caps, and
an `/integrations` entry.
