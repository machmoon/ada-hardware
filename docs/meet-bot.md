# Hardy in a Google Meet call

`python -m meetbot join <meet-url>` puts Hardy in a Meet call as a participant.
It listens to Meet's live captions, answers out loud when someone names it or
voices doubt, and when the call ends it:

1. finds the board request in what was said (`meetings.intent.extract_requests`:
   every request must quote the transcript, and anything under the 0.60
   confidence floor is recorded but not built),
2. lists the open spec questions (missing voltage, connector, size …),
3. posts a recap and the questions to you on Slack and waits, bounded, for a
   reply in the thread,
4. hands the clarified request to the Hardy overlay on your laptop through the
   engine's inbox (`POST /inbox`, the same hand-off the Slack bridge uses), and
5. follows the approval-gated step run, posting progress into the same thread.

Nothing is ever ordered, and only one board is handed off per call.

## Modules

| file | owns |
|---|---|
| `meetbot/session.py` | the Chromium participant: sign-in profile, lobby, admission, end of call, leave |
| `meetbot/listen.py` | Meet captions → `Utterance`s (Hardy's own lines marked `is_self`) |
| `meetbot/speak.py`, `tts.py` | text → audio played into the call, with a `SpokenReceipt` saying whether it left |
| `meetbot/brain.py` | when to speak and what to say in the call |
| `meetbot/clarify.py` | the open-questions prompt and the Slack thread (post, `conversations.replies` poll) |
| `meetbot/runner.py` | the whole run and the `CallReport` |
| `meetbot/config.py` | environment, validated once |

Where the in-call judgement comes from (details in `brain.py`'s docstring):
the name trigger, cooldown, 120 s context buffer and output sanitising follow
the Google Meet AI attendance agent
(`code-with-idrees/Google-Meet-AI-Attendence-Agent` at `01d482d`,
`brain.py::detect_keyword`, `_fuzzy_name_match`, `TranscriptBuffer`,
`meeting_agent.py`'s 30 s `cooldown_until`). Upserting a caption line that is
still being refined instead of appending it follows Vexa
(`Vexa-ai/vexa` at `59e2c41`, `clients/terminal/src/surfaces/meetingLive.ts`).
Two deviations: Hardy also answers doubt it was not named in (the demo needs
it), and it waits for a pause before speaking rather than answering the moment
a line transcribes.

## Setup

```bash
./.venv/bin/pip install -e ".[meet]" && ./.venv/bin/python -m playwright install chromium
./.venv/bin/python -m meetbot.session sign-in      # sign Hardy's Chrome profile in to Google once
PORT=8081 ./.venv/bin/python -m service.app         # or `silkscreen serve`: the engine the overlay and inbox live in
```

Open the desktop overlay (`app/`) so something accepts the inbox idea.

Slack app (bot token `xoxb-…`) scopes:

- `chat:write` — the recap, the questions and the progress lines.
- `im:history` — to read your reply when `HARDY_SLACK_CHANNEL` is your user id
  (a DM). For a channel instead: `channels:history` (public) or
  `groups:history` (private), and invite the bot to it.

No Socket Mode token is needed: Hardy polls the thread
(`conversations.replies`) rather than receiving events.

## Environment

`python -m meetbot` reads `.env` (exported variables win).

| variable | default | meaning |
|---|---|---|
| `GOOGLE_API_KEY` | required | Gemini, for replies, extraction and questions |
| `SLACK_BOT_TOKEN` | required unless `--no-slack` | the `xoxb-` token |
| `HARDY_SLACK_CHANNEL` | required unless `--no-slack` | your user id (`U…`, a DM) or a channel id (`C…`) |
| `HARDY_SLACK_USER` | any | only this user's thread reply counts as the answer |
| `HARDY_CLARIFY_WAIT_S` | `600` | how long to wait for your reply; `0` = don't wait |
| `HARDY_BUILD_ON_TIMEOUT` | `1` | with no reply in time: `1` build from the spec as stated, `0` hold |
| `HARDY_BUILD` / `HARDY_FOLLOW` | `1` / `1` | hand off to the overlay / post its progress |
| `SILKSCREEN_ENGINE_URL` | `http://127.0.0.1:8081` | the engine with `/inbox` |
| `SILKSCREEN_ACCESS_TOKEN` | none | the engine's bearer gate, when set |
| `HARDY_DISPLAY_NAME` | `Hardy` | the name in the call, and the name the brain listens for |
| `HARDY_PROFILE_DIR` | `~/.hardy/meet-profile` | the signed-in Chromium profile |
| `HARDY_HEADLESS` | `0` | headless Chromium |
| `HARDY_MODEL` | `gemini-3.7-flash` | extraction and questions |
| `HARDY_REPLY_MODEL` | `gemini-3.5-flash-lite` | in-call replies (latency matters more than depth) |
| `HARDY_QUIET_S` | `1.2` | silence after the last human line before Hardy may speak |
| `HARDY_REPLY_COOLDOWN_S` | `20` | minimum gap between replies |
| `HARDY_MAX_REPLIES` | `8` | replies per call |
| `HARDY_REPLY_TO_DOUBT` | `1` | also answer "I don't think this will work" when not named |
| `HARDY_CONFIDENCE_FLOOR` | `0.6` | `meetings.runner.DEFAULT_CONFIDENCE_FLOOR` |
| `HARDY_MAX_CALL_S` | `10800` | leave after this long regardless |
| `KALEO_TTS_URL` | the engine | where `tts.py` asks for a voice first |

Flags on `join`: `--no-slack`, `--no-build`, `--no-follow`, `--headless`,
`--profile DIR`, `--wait S`.

## Demo script: the kickoff

Before: engine running, overlay open, Hardy's profile signed in, `.env` has the
four required variables, Slack open on your phone.

1. Start a Meet from your own account and copy the link.
2. `./.venv/bin/python -m meetbot join https://meet.google.com/abc-defg-hij`
   — a Chromium window opens, walks the lobby and asks to join. Admit **Hardy**.
3. State the board, in one sentence the extractor can quote, e.g.
   *"For the greenhouse, we need a small board that reads a soil moisture
   sensor and runs off a coin cell."*
4. Push back: *"Honestly, I don't think this will work."* Stop talking.
   After about a second of quiet Hardy answers out loud, e.g. *"Fair — I'll try
   it and send you a first pass after the call."* (Naming it works too:
   *"Hardy, can you take this?"*)
5. *"Okay, talk soon."* End the call for everyone (or leave; Hardy notices it is
   alone after 30 s).
6. On Slack: a recap quoting your request and what Hardy said, then its open
   questions (e.g. which coin cell, what probe connector).
7. Reply in that thread, e.g. *"CR2032, and a 2-pin JST-PH for the probe."*
   Hardy answers "folding that in" and files the idea.
8. The overlay picks the idea up and starts the design on your laptop; the
   thread gets "Design started…", "Done: propose", "Waiting for you on the
   laptop — next: …". Approve each step on the laptop.

The terminal prints the `CallReport`: join state, each reply with
`SPOKEN`/`NOT SPOKEN` and the receipt detail, every trigger Hardy did not
answer and why, requests considered vs handed off, every Slack post with
delivered/not delivered, and the inbox status.

## Honesty rules

- A reply counts as said only when `SpokenReceipt.spoken` is true; the recap
  says "I tried to say … but no audio left" otherwise.
- A Slack message counts as delivered only when Slack answered `ok`. If the
  recap is not delivered, no questions are waited on and no progress is
  posted, and the report says so. Progress lines posted by the reused bridge
  follower (`slackbot/bridge.py::Bridge.watch`) are logged on failure, not
  retried.
- "No request found", "the extractor's quotes were all invented", "captions
  stopped mid-call" and "no speech captured" are four different warnings.
- Hardy never answers its own captions (`is_self`, or a speaker named Hardy or
  "You").

## Not verified live

The runner, brain, clarify and config are tested offline only
(`meetbot/tests/test_brain.py`, `test_runner.py`: scripted model, fake room,
recorded Slack and engine transports). Not yet exercised against a real call:
reply latency with `gemini-3.5-flash-lite` plus TTS (whether a ~1 s pause
feels natural or lands on the next speaker), the doubt phrases against real
caption text, `conversations.replies` on a real DM (`im:history` scope), and
the overlay accepting a `source: "meet"` idea. The progress lines come from
the Slack bridge and still say "Hardy" and "Kaleo app" in places.
