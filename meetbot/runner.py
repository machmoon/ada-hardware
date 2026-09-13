"""One kickoff, end to end: join, listen and talk, then clarify and build.

This is the only module that knows the meeting, Slack and the engine at once
-- the ``meetings/runner.py`` / ``zoombot/runner.py`` shape. Nothing below it
reimplements a gate:

* **in the call**, :class:`meetbot.brain.Brain` hears what
  :func:`meetbot.listen.transcript_stream` yields and answers through
  :func:`meetbot.speak.say`;
* **after it**, :func:`meetings.intent.extract_requests` finds the board
  requests (the quote filter, verbatim) and ``meetings.runner``'s
  ``DEFAULT_CONFIDENCE_FLOOR`` decides what may be built;
* **the clarification** is a Slack thread (:mod:`meetbot.clarify`, over
  ``slackbot.slack.SlackClient``);
* **the build** is the desktop overlay's inbox, the exact request
  ``slackbot/bridge.py::EngineClient.post_idea`` sends to ``POST /inbox``
  (``service/inbox.py``), followed by the bridge's own
  :meth:`slackbot.bridge.Bridge.watch` -- ``GET /inbox/<id>`` until the
  overlay starts the approval-gated step run, then ``GET /steps/<session>``
  -- so progress lands in the same thread with no second follower to drift.

The in-call phase is asyncio (Playwright's async API); the after-call phase is
synchronous stdlib HTTP and is run in a worker thread by :func:`run`.

Honesty, as a report: :class:`CallReport` keeps what was spoken with its
:class:`~meetbot.types.SpokenReceipt`, what was considered separately from
what was handed off, every Slack post with Slack's own ``ok``, and the inbox
status code. Nothing here orders anything, and only one board is handed off
per call -- each hand-off is a paid ``propose`` step on the laptop.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from silkscreen.agents.model import Model, ModelError

from meetings.intent import BoardRequest, IntentError, extract_requests
from service.inbox import MAX_TEXT_CHARS
from slackbot.bridge import Bridge, BridgeConfig, EngineClient

from .brain import Brain, BrainPolicy, Reply, SkippedTrigger, transcript_text
from .clarify import (
    QuestionsResult,
    SlackAnswer,
    SlackPost,
    ThreadClient,
    format_recap,
    propose_questions,
    wait_for_answer,
)
from .config import HardyConfig, parse_meet_url
from .types import JoinReceipt, SpokenReceipt, Utterance

__all__ = [
    "CallReport",
    "IdeaReceipt",
    "MeetEngineClient",
    "SOURCE",
    "after_call",
    "attend",
    "idea_key",
    "run",
]

log = logging.getLogger("meetbot.runner")

#: The inbox ``source`` for ideas from a meeting (the bridge sends ``slack``).
SOURCE = "meet"
#: At most this many requests are extracted; one is handed off.
MAX_REQUESTS = 3

SessionFactory = Callable[..., Any]
StreamFactory = Callable[[Any], AsyncIterator[Utterance]]
SayFn = Callable[[Any, str], Awaitable[SpokenReceipt]]


class MeetEngineClient(EngineClient):
    """The bridge's engine client, filing ideas as ``source: "meet"``.

    ``EngineClient.post_idea`` hard-codes the bridge's ``slack`` source; the
    request is otherwise byte-for-byte the one ``service/inbox.py::handle_post``
    validates, and ``idea``/``steps`` (what :meth:`Bridge.watch` polls) are
    inherited unchanged.
    """

    def post_idea(
        self, text: str, *, key: str, reply_to: dict[str, str], user: str
    ) -> tuple[int, dict[str, Any]]:
        return self._call(
            "POST",
            "/inbox",
            {
                "text": text,
                "source": SOURCE,
                "key": key,
                "reply_to": reply_to,
                "user": user,
            },
        )


@dataclass(frozen=True)
class IdeaReceipt:
    """What ``POST /inbox`` answered. ``status`` 0 = the engine was never reached."""

    status: int
    idea_id: str = ""
    error: str = ""

    @property
    def filed(self) -> bool:
        return self.status in (200, 201) and bool(self.idea_id)


@dataclass
class CallReport:
    meeting_code: str
    join: JoinReceipt | None = None
    ended: str = ""
    transcript: list[Utterance] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)
    skipped: list[SkippedTrigger] = field(default_factory=list)
    considered: list[BoardRequest] = field(default_factory=list)
    handed_off: BoardRequest | None = None
    questions: QuestionsResult | None = None
    answer: SlackAnswer | None = None
    slack_posts: list[SlackPost] = field(default_factory=list)
    idea: IdeaReceipt | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def spoke_aloud(self) -> list[Reply]:
        return [r for r in self.replies if r.receipt.spoken]

    def lines(self) -> list[str]:
        """The terminal summary. Each line is a fact the run recorded."""
        out = [f"meeting {self.meeting_code}"]
        if self.join is None:
            out.append("join: never attempted")
        else:
            out.append(f"join: {self.join.state} ({self.join.detail})")
        if self.ended:
            out.append(f"ended: {self.ended}")
        humans = [u for u in self.transcript if not u.is_self]
        out.append(f"heard: {len(humans)} utterance(s) from people")
        for reply in self.replies:
            state = "SPOKEN" if reply.receipt.spoken else "NOT SPOKEN"
            out.append(
                f"said [{state}, {reply.reason}]: {reply.text!r}"
                f" -- {reply.receipt.detail}"
            )
        for skip in self.skipped:
            out.append(f"did not reply to {skip.trigger.text!r}: {skip.reason}")
        if not self.considered:
            out.append("board requests: none that could be quoted")
        for request in self.considered:
            tag = "HANDED OFF" if request is self.handed_off else "considered"
            out.append(f"request [{tag}]: {request}")
        if self.questions is not None and self.questions.questions is not None:
            out.append(f"open questions: {len(self.questions.questions)}")
        if self.answer is not None:
            out.append(f"answer from {self.answer.user}: {self.answer.text!r}")
        for post in self.slack_posts:
            out.append(
                f"slack: delivered ts={post.ts}"
                if post.ok
                else f"slack: NOT delivered ({post.error})"
            )
        if self.idea is not None:
            out.append(
                f"inbox: filed {self.idea.idea_id} (HTTP {self.idea.status})"
                if self.idea.filed
                else f"inbox: NOT filed (HTTP {self.idea.status}: {self.idea.error})"
            )
        out.extend(f"warning: {w}" for w in self.warnings)
        return out


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _default_session_factory(profile_dir, headless):  # pragma: no cover - live
    from .session import MeetSession

    session = MeetSession()
    await session.start(profile_dir, headless)
    return session


def _default_stream(display_name: str) -> StreamFactory:  # pragma: no cover - live
    from . import listen

    return lambda session: listen.transcript_stream(session, self_name=display_name)


def _default_say() -> SayFn:  # pragma: no cover - live
    from . import speak

    return speak.say


def _default_init_scripts() -> list[str]:  # pragma: no cover - live
    from . import listen, speak

    return [
        module.init_script()
        for module in (listen, speak)
        if callable(getattr(module, "init_script", None))
    ]


# ------------------------------------------------------------ in the call


async def attend(
    url: str,
    config: HardyConfig,
    *,
    reply_model: Model,
    session_factory: SessionFactory | None = None,
    stream: StreamFactory | None = None,
    say: SayFn | None = None,
    init_scripts: list[str] | None = None,
    brain_factory: Callable[..., Brain] = Brain,
) -> CallReport:
    """Join, hold the conversation until the call ends, leave. No Slack, no build."""
    report = CallReport(meeting_code=parse_meet_url(url))
    session_factory = session_factory or _default_session_factory
    stream = stream or _default_stream(config.display_name)
    say = say or _default_say()
    scripts = _default_init_scripts() if init_scripts is None else init_scripts

    session = await _maybe_await(session_factory(config.profile_dir, config.headless))
    brain: Brain | None = None
    try:
        for js in scripts:
            await _maybe_await(session.add_init_script(js))
        report.join = await session.join(url, display_name=config.display_name)
        if not report.join.admitted:
            report.warnings.append(
                f"Hardy was not in the call ({report.join.state}): {report.join.detail}"
            )
            return report

        policy = BrainPolicy(
            name=config.display_name,
            quiet_s=config.quiet_s,
            cooldown_s=config.reply_cooldown_s,
            max_replies=config.max_replies,
            reply_to_doubt=config.reply_to_doubt,
        )
        brain = brain_factory(reply_model, lambda text: say(session, text), policy)

        async def consume() -> None:
            async for utterance in stream(session):
                await brain.hear(utterance)

        listen_task = asyncio.create_task(consume())
        end_task = asyncio.create_task(_maybe_await(session.wait_until_ended()))
        done, _ = await asyncio.wait(
            {listen_task, end_task},
            timeout=config.max_call_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if end_task in done:
            exc = end_task.exception()
            if exc is None:
                detail = getattr(session, "end_detail", "")
                report.ended = (
                    f"{end_task.result()}: {detail}"
                    if detail
                    else str(end_task.result())
                )
            if exc is not None:
                report.warnings.append(f"could not tell how the call ended: {exc}")
        elif listen_task in done:
            exc = listen_task.exception()
            if exc is None:
                report.ended = "the transcript stream ended (the call closed)"
            else:
                # Deaf for the rest of the call: say so, and stay until it
                # ends so leaving does not look like a hang-up to the room.
                log.error("listening stopped mid-call: %s", exc)
                report.warnings.append(
                    f"listening stopped mid-call, so the transcript is incomplete: "
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    report.ended = str(
                        await asyncio.wait_for(end_task, config.max_call_s)
                    )
                except Exception as end_exc:  # noqa: BLE001
                    report.warnings.append(
                        f"stopped waiting for the call to end: {end_exc}"
                    )
        else:
            report.warnings.append(
                f"left after {config.max_call_s:.0f} s (HARDY_MAX_CALL_S)"
            )
        for task in (listen_task, end_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(listen_task, end_task, return_exceptions=True)
        await brain.settle()
    finally:
        if brain is not None:
            report.transcript = brain.transcript
            report.replies = list(brain.replies)
            report.skipped = list(brain.skipped)
            report.warnings.extend(brain.errors)
        try:
            await _maybe_await(session.leave())
        except Exception as exc:  # noqa: BLE001 - leaving must not hide the report
            report.warnings.append(f"leaving the call failed: {exc}")
    return report


# ----------------------------------------------------------- after the call


def _idea_text(
    request: BoardRequest,
    questions: QuestionsResult | None,
    answer: SlackAnswer | None,
    warnings: list[str],
) -> str:
    text = request.intent.strip()
    if answer is None:
        return text[:MAX_TEXT_CHARS]
    asked = ""
    if questions is not None and questions.questions:
        asked = "\nQuestions asked:\n" + "\n".join(
            f"{n}. {q.question}" for n, q in enumerate(questions.questions, 1)
        )
    head = f"{text}\n{asked}\nAnswers from the engineer on Slack:\n"
    room = MAX_TEXT_CHARS - len(head)
    body = answer.text
    if len(body) > room:
        body = body[: max(0, room)]
        warnings.append(
            f"the Slack answer was cut to fit the inbox's "
            f"{MAX_TEXT_CHARS}-character limit"
        )
    return (head + body)[:MAX_TEXT_CHARS]


def idea_key(meeting_code: str, request: BoardRequest) -> str:
    """One request's identity: a re-run of the same call files no second idea."""
    digest = hashlib.sha256(
        " ".join(request.quote.casefold().split()).encode()
    ).hexdigest()
    return f"meet:{meeting_code}:{digest[:24]}"


def after_call(
    report: CallReport,
    config: HardyConfig,
    *,
    model: Model,
    slack: ThreadClient | None,
    engine: EngineClient | None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_s: float = 5.0,
    bridge_factory: Callable[..., Bridge] = Bridge,
) -> CallReport:
    """Extract, ask, wait (bounded), hand off, follow. Mutates ``report``."""
    text = transcript_text(report.transcript)
    if report.join is not None and report.join.admitted and not text:
        report.warnings.append(
            "no speech from people was captured, so there is nothing to build from"
        )
    if text:
        try:
            report.considered = extract_requests(model, text, max_requests=MAX_REQUESTS)
        except IntentError as exc:
            report.warnings.append(
                f"could not read a board request out of the call: {exc}"
            )
        except ModelError as exc:
            report.warnings.append(f"the extraction model failed: {exc}")

    to_build = next(
        (r for r in report.considered if r.confidence >= config.confidence_floor), None
    )
    if report.considered and to_build is None:
        report.warnings.append(
            f"every request was below the {config.confidence_floor:.2f} confidence "
            "floor; recorded, not built"
        )
    if not config.build:
        to_build = None

    if to_build is not None:
        try:
            report.questions = propose_questions(model, text, [to_build])
            report.warnings.extend(report.questions.warnings)
        except ModelError as exc:
            report.warnings.append(f"the questions model failed: {exc}")

    channel, thread_ts = "", ""
    has_questions = bool(report.questions and report.questions.questions)
    wait_s = config.clarify_wait_s if slack is not None else 0.0
    if slack is not None:
        recap = format_recap(
            meeting_code=report.meeting_code,
            spoken=[
                (r.text, r.receipt.spoken, r.receipt.detail) for r in report.replies
            ],
            considered=report.considered,
            to_build=to_build,
            floor=config.confidence_floor,
            questions=report.questions,
            warnings=list(report.warnings),
            will_wait_s=wait_s,
            build_on_timeout=config.build_on_timeout,
        )
        post = slack.post(config.slack_channel, recap)
        report.slack_posts.append(post)
        if post.ok:
            channel, thread_ts = post.channel, post.ts
        else:
            report.warnings.append(
                f"the Slack recap was not delivered ({post.error}); "
                "no questions were asked and no progress will be posted"
            )

    def say(message: str) -> None:
        if channel:
            result = slack.post(channel, message, thread_ts=thread_ts)
            report.slack_posts.append(result)
            if not result.ok:
                report.warnings.append(
                    f"a Slack thread reply was not delivered ({result.error})"
                )

    if to_build is not None and has_questions and channel and wait_s > 0:
        report.answer = wait_for_answer(
            slack,
            channel,
            thread_ts,
            after_ts=thread_ts,
            timeout_s=wait_s,
            only_user=config.slack_user,
            poll_s=poll_s,
            clock=clock,
            sleep=sleep,
            warnings=report.warnings,
        )
        if report.answer is not None:
            say("Thanks — folding that in and starting the build on your laptop.")
        elif config.build_on_timeout:
            say(
                "No answer yet, so I'm starting from the spec as stated. "
                "You approve every step on the laptop."
            )
        else:
            say("No answer in time, so I'm holding off. Nothing was started.")
            report.warnings.append(
                "no answer to the open questions; build held (HARDY_BUILD_ON_TIMEOUT=0)"
            )
            to_build = None

    if to_build is None or engine is None:
        return report

    idea_text = _idea_text(to_build, report.questions, report.answer, report.warnings)
    status, body = engine.post_idea(
        idea_text,
        key=idea_key(report.meeting_code, to_build),
        reply_to={"channel": channel, "thread_ts": thread_ts} if channel else {},
        user=(report.answer.user if report.answer else config.slack_user),
    )
    idea_id = str(body.get("id", "")) if status in (200, 201) else ""
    error = (
        ""
        if idea_id
        else str(
            body.get("error")
            or (
                f"could not reach the engine at {engine.base_url}"
                if status == 0
                else f"HTTP {status}"
            )
        )
    )
    report.idea = IdeaReceipt(status, idea_id, error)
    if not report.idea.filed:
        report.warnings.append(f"the build was not handed to the laptop: {error}")
        hint = (
            " Start it with `silkscreen serve`."
            if status == 0
            else " (SILKSCREEN_ACCESS_TOKEN does not match.)"
            if status == 401
            else ""
        )
        say(f"I couldn't hand the build to your laptop: {error}.{hint}")
        return report
    report.handed_off = to_build
    if status == 200:
        say("This one was already on your laptop's list; I'm not filing it twice.")
        return report
    say("Handed to Hardy on your laptop — it'll start when the overlay picks it up.")
    if config.follow and channel:
        bridge = bridge_factory(
            BridgeConfig(
                bot_token=config.slack_token,
                app_token="",
                engine_url=engine.base_url,
                engine_token=config.engine_token,
            ),
            slack,
            engine,
            sleep=sleep,
            clock=clock,
        )
        bridge.watch(idea_id, channel=channel, thread_ts=thread_ts)
    return report


# ------------------------------------------------------------- the whole run


async def run(
    url: str,
    config: HardyConfig,
    *,
    model: Model,
    reply_model: Model,
    slack: ThreadClient | None,
    engine: EngineClient | None,
    **attend_kwargs: Any,
) -> CallReport:
    report = await attend(url, config, reply_model=reply_model, **attend_kwargs)
    if report.join is None or not report.join.admitted:
        return report
    return await asyncio.to_thread(
        after_call, report, config, model=model, slack=slack, engine=engine
    )
