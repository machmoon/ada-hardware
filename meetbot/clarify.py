"""After the call: the open spec questions, and asking them in Slack.

Two halves, both synchronous (the runner calls them in a worker thread).

**The questions** are validated model output in the ``netlist.py`` /
``specreview.py`` shape: every problem batched into one
:class:`QuestionsError`, one repair round, then give up loudly. Three states
stay three representations -- ``None`` (the model gave nothing usable, with a
warning), ``[]`` (a real answer: the spec as stated is enough to draft), and no
call at all when there is no board request to ask about, because asking a model
for the gaps in nothing is how an invented question is born
(``agents/specreview.py``'s "no evidence, no call" rule).

**The Slack thread** reuses :class:`slackbot.slack.SlackClient` and its
recorded-transport seam rather than a second client. Two methods it lacks are
added in a subclass, each read from Slack's own client
(``slackapi/python-slack-sdk`` main as fetched 2026-09-13,
``slack_sdk/web/client.py``): ``conversations_replies`` is
``api_call("conversations.replies", http_verb="GET", params=kwargs)`` with
``channel``/``ts``/``oldest``/``limit`` as query parameters, and ``auth_test``
is ``api_call("auth.test")`` (whose ``user_id`` says which messages are ours).
``chat.postMessage``'s response carries ``channel`` -- the only way to learn
the ``D…`` id when the message was addressed to a user id, which the reply
poll needs. A post is recorded as delivered only when Slack answered ``ok``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from silkscreen.agents.model import Model, strip_code_fence

from meetings.intent import BoardRequest
from slackbot.blocks import escape_mrkdwn
from slackbot.slack import API_ROOT, HttpRequest, SlackClient, SlackError

__all__ = [
    "QUESTIONS_MARKER",
    "MAX_QUESTIONS",
    "SpecQuestion",
    "QuestionsError",
    "QuestionsResult",
    "parse_questions",
    "propose_questions",
    "SlackPost",
    "SlackAnswer",
    "ThreadClient",
    "wait_for_answer",
]

log = logging.getLogger("meetbot.clarify")

QUESTIONS_MARKER = "HARDY-SPEC-QUESTIONS v1"
#: A kickoff follow-up a person answers from a phone. More than five is a form.
MAX_QUESTIONS = 5
MAX_QUESTION_CHARS = 300
#: The tail of the call handed to the questions prompt.
MAX_CONTEXT_CHARS = 20_000

QUESTIONS_PROMPT = """\
{marker}
You are a hardware engineer about to draft a printed circuit board from a \
kickoff meeting. Below are the board requests that were stated, and the \
meeting transcript.

List the questions you MUST have answered before drafting a first schematic -- \
only facts the transcript does not already state: input voltage and source, \
load current, connectors, board size or mounting, interfaces, environment. \
At most {max_questions}, most important first, each one short enough to answer \
from a phone. If the requests already say enough to draft, return an empty \
list; that is a correct answer.

Return JSON only:
{{"questions": [{{"question": "...", "why": "what it changes in the design"}}]}}

BOARD REQUESTS:
{requests}

TRANSCRIPT:
{transcript}
"""


class QuestionsError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            f"{len(self.errors)} problem(s) in the spec questions:\n  - "
            + "\n  - ".join(self.errors)
        )


@dataclass(frozen=True)
class SpecQuestion:
    question: str
    why: str = ""


@dataclass
class QuestionsResult:
    #: ``None`` = no usable answer; ``[]`` = nothing needs asking.
    questions: list[SpecQuestion] | None
    asked_model: bool
    warnings: list[str] = field(default_factory=list)


def parse_questions(raw: str) -> list[SpecQuestion]:
    try:
        data = json.loads(strip_code_fence(raw or ""))
    except json.JSONDecodeError as exc:
        raise QuestionsError(
            [f"response is not valid JSON: {exc}; first 200 chars: {raw[:200]!r}"]
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise QuestionsError(["expected an object with a 'questions' array"])
    items = data["questions"]
    errors: list[str] = []
    if len(items) > MAX_QUESTIONS:
        errors.append(f"{len(items)} questions; at most {MAX_QUESTIONS} are allowed")
    out: list[SpecQuestion] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        where = f"questions[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object")
            continue
        question = item.get("question")
        why = item.get("why", "")
        if not isinstance(question, str) or not question.strip():
            errors.append(f"{where}: 'question' must be a non-empty string")
            continue
        if not isinstance(why, str):
            errors.append(f"{where}: 'why' must be a string")
            continue
        if len(question) > MAX_QUESTION_CHARS or len(why) > MAX_QUESTION_CHARS:
            errors.append(f"{where}: longer than {MAX_QUESTION_CHARS} characters")
            continue
        key = " ".join(question.casefold().split())
        if key in seen:
            errors.append(f"{where}: duplicate question")
            continue
        seen.add(key)
        out.append(SpecQuestion(question.strip(), why.strip()))
    if errors:
        raise QuestionsError(errors)
    return out


def propose_questions(
    model: Model,
    transcript: str,
    requests: list[BoardRequest],
    *,
    max_repairs: int = 1,
) -> QuestionsResult:
    """Ask for the open spec questions. :class:`ModelError` is not wrapped."""
    if not requests:
        return QuestionsResult(questions=None, asked_model=False)
    prompt = QUESTIONS_PROMPT.format(
        marker=QUESTIONS_MARKER,
        max_questions=MAX_QUESTIONS,
        requests="\n".join(f"- {r.intent}" for r in requests),
        transcript=transcript[-MAX_CONTEXT_CHARS:],
    )
    attempt = prompt
    for round_ in range(max_repairs + 1):
        raw = model.generate(attempt, temperature=0.0, max_output_tokens=2048)
        try:
            return QuestionsResult(parse_questions(raw), asked_model=True)
        except QuestionsError as exc:
            if round_ == max_repairs:
                return QuestionsResult(
                    None,
                    asked_model=True,
                    warnings=[
                        f"could not read the open questions after "
                        f"{max_repairs + 1} attempt(s): {exc}"
                    ],
                )
            attempt = (
                prompt
                + "\n\nYour previous answer was rejected:\n"
                + "\n".join(f"- {e}" for e in exc.errors)
                + "\nReturn corrected JSON only."
            )
    raise AssertionError("unreachable")  # pragma: no cover


# ------------------------------------------------------------------ Slack


@dataclass(frozen=True)
class SlackPost:
    """One message, and whether Slack said ``ok``. Never assumed delivered."""

    ok: bool
    channel: str = ""
    ts: str = ""
    error: str = ""


@dataclass(frozen=True)
class SlackAnswer:
    text: str
    user: str
    ts: str


class ThreadClient(SlackClient):
    """:class:`SlackClient` plus the two calls a clarifying thread needs."""

    def __init__(self, token: str, **kwargs: Any):
        super().__init__(token, **kwargs)
        self._bot_user: str | None = None

    def post(
        self, channel: str, text: str, *, thread_ts: str | None = None
    ) -> SlackPost:
        try:
            data = self._call(
                "chat.postMessage",
                {
                    "channel": channel,
                    "text": text,
                    "thread_ts": thread_ts,
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
            )
        except SlackError as exc:
            return SlackPost(False, channel, "", str(exc))
        ts = str(data.get("ts", ""))
        if not ts:
            return SlackPost(False, channel, "", "bad_response: no ts in reply")
        return SlackPost(True, str(data.get("channel") or channel), ts)

    def bot_user_id(self) -> str:
        """``auth.test``'s ``user_id``: whose messages are our own."""
        if self._bot_user is None:
            self._bot_user = str(self._call("auth.test", {}).get("user_id", ""))
        return self._bot_user

    def replies(
        self, channel: str, ts: str, *, oldest: str = ""
    ) -> list[dict[str, Any]]:
        params = {"channel": channel, "ts": ts, "limit": "200"}
        if oldest:
            params["oldest"] = oldest
        request = HttpRequest(
            "GET",
            f"{API_ROOT}conversations.replies?{urllib.parse.urlencode(params)}",
            {"Authorization": f"Bearer {self._token}"},
        )
        data = self._send_api(request, "conversations.replies")
        messages = data.get("messages")
        return (
            [m for m in messages if isinstance(m, dict)]
            if isinstance(messages, list)
            else []
        )


def _ts_value(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def wait_for_answer(
    slack: ThreadClient,
    channel: str,
    thread_ts: str,
    *,
    after_ts: str,
    timeout_s: float,
    only_user: str = "",
    poll_s: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    warnings: list[str] | None = None,
) -> SlackAnswer | None:
    """Poll the thread for the first human reply after ``after_ts``.

    Bounded by ``timeout_s``; ``None`` means nobody answered in time (or the
    poll kept failing, which is appended to ``warnings`` in words). Bot
    messages -- Hardy's own and the build follower's -- never count.
    """
    if timeout_s <= 0:
        return None
    try:
        me = slack.bot_user_id()
    except SlackError as exc:
        me = ""
        if warnings is not None:
            warnings.append(
                f"auth.test failed ({exc}); cannot tell my own messages apart"
            )
    started = clock()
    last_error = ""
    while True:
        try:
            messages = slack.replies(channel, thread_ts, oldest=after_ts)
            last_error = ""
        except SlackError as exc:
            messages = []
            last_error = str(exc)
        for message in messages:
            if message.get("bot_id") or message.get("subtype"):
                continue
            user = str(message.get("user", ""))
            ts = str(message.get("ts", ""))
            if not user or user == me or _ts_value(ts) <= _ts_value(after_ts):
                continue
            if only_user and user != only_user:
                continue
            text = str(message.get("text", "")).strip()
            if text:
                return SlackAnswer(text, user, ts)
        if clock() - started + poll_s > timeout_s:
            if last_error and warnings is not None:
                warnings.append(f"reading the Slack thread failed: {last_error}")
            return None
        sleep(poll_s)


def format_recap(
    *,
    meeting_code: str,
    spoken: list[tuple[str, bool, str]],
    considered: list[BoardRequest],
    to_build: BoardRequest | None,
    floor: float,
    questions: QuestionsResult | None,
    warnings: list[str],
    will_wait_s: float,
    build_on_timeout: bool = True,
) -> str:
    """The recap message. Every line is something the code did or saw."""
    e = escape_mrkdwn
    lines = [f"*Kickoff recap* — meet.google.com/{e(meeting_code)}"]
    if spoken:
        for text, ok, detail in spoken:
            if ok:
                lines.append(f"I said in the call: “{e(text)}”")
            else:
                lines.append(
                    f"I tried to say “{e(text)}” but no audio left ({e(detail)})"
                )
    else:
        lines.append("I didn't speak in the call.")
    if not considered:
        lines.append(
            "I didn't hear a board request I could quote, so nothing will be built."
        )
    for request in considered:
        mark = (
            "→ building"
            if request is to_build
            else (
                f"recorded, below the {floor:.2f} floor"
                if request.confidence < floor
                else "recorded, one build per call"
            )
        )
        lines.append(
            f"• {e(request.intent)} ({request.confidence:.2f}, {mark})\n"
            f"   _“{e(request.quote)}”_ — {e(request.speaker or 'someone')}"
        )
    if questions is not None and questions.questions:
        lines.append("*Before I start, a few questions:*")
        for n, q in enumerate(questions.questions, 1):
            why = f" — {e(q.why)}" if q.why else ""
            lines.append(f"{n}. {e(q.question)}{why}")
        if will_wait_s > 0:
            then = (
                "I'll start from the spec as stated"
                if build_on_timeout
                else "I'll hold off building"
            )
            lines.append(
                f"Reply in this thread and I'll fold your answers in. If I don't "
                f"hear back in {max(1, round(will_wait_s / 60))} min, {then}."
            )
    elif questions is not None and questions.questions == [] and to_build:
        lines.append("The spec as stated is enough for a first pass; no questions.")
    for warning in warnings:
        lines.append(f"⚠ {e(warning)}")
    return "\n".join(lines)
