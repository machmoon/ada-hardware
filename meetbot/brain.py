"""Hardy in the call: decide when to say something, and say one short thing.

Where the design comes from
---------------------------

**When to speak** is the Google Meet AI attendance agent's structure
(github.com/code-with-idrees/Google-Meet-AI-Attendence-Agent at ``01d482d``),
read from source:

* a *deterministic* gate decides whether a line is worth a model call at all --
  ``brain.py::detect_keyword`` tries exact keywords, then known
  mis-transcriptions (``NAME_PATTERNS``), then a ``difflib.SequenceMatcher``
  ratio over words and bigrams (``_fuzzy_name_match``). The model is never
  asked "should I talk?" about every sentence; captions mangle names, which is
  why the fuzzy tier exists. :func:`addressed` is that ladder for "Hardy".
* a cooldown between replies -- ``meeting_agent.py`` sets ``cooldown_until =
  time.time() + 30`` after answering, ``config.py::RESPONSE_COOLDOWN_SECONDS``
  says why: "prevents double-triggering".
* a rolling context buffer handed to the model with the trigger --
  ``brain.py::TranscriptBuffer.get_recent(seconds=120)``.
* output sanitising before anything is voiced (``_sanitize_llm_output``,
  ``clean_chat_text``): a small model prefixes its own name, wraps in quotes,
  or returns a paragraph. :func:`clean_reply` does the same job.

Two deviations, each for a stated reason. That agent answers only when named;
the demo this exists for is a human voicing doubt without naming anyone ("I
don't think this will work"), so :func:`voices_doubt` is a second, narrower
gate -- and the model may still answer ``""`` to stay quiet. And it answers the
moment a chunk transcribes; here a reply waits for :attr:`BrainPolicy.quiet_s`
of silence and re-waits if someone starts talking while the model thinks,
because "never talk over people" is the one rule a voice in a meeting cannot
break twice.

**Consuming the stream** follows Vexa's live transcript client
(github.com/Vexa-ai/vexa at ``59e2c41``,
``clients/terminal/src/surfaces/meetingLive.ts``): pending segments arrive
repeatedly as recognition refines, and the consumer *upserts* the line rather
than appending a duplicate. Our :class:`~meetbot.types.Utterance` carries no
segment id, so :meth:`Brain.hear` upserts on "same speaker, and the new text
extends the last line" -- ``meetbot.listen`` already yields settled captions,
so this is a guard, not the mechanism.

Honesty rules: a reply is recorded with the :class:`SpokenReceipt` that says
whether audio actually left; a trigger that was not answered is recorded with
why (cooldown, cap, the conversation moved on, the model chose silence, the
call ended); the bot never answers itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from silkscreen.agents.model import Model, ModelError, strip_code_fence

from .types import SpokenReceipt, Utterance

__all__ = [
    "Brain",
    "BrainPolicy",
    "Reply",
    "SkippedTrigger",
    "REPLY_MARKER",
    "addressed",
    "voices_doubt",
    "clean_reply",
    "transcript_text",
]

log = logging.getLogger("meetbot.brain")

#: Appears verbatim in every reply prompt so ``ScriptedModel.by_marker`` can key
#: on it (the ``SOURCING_MARKER`` convention).
REPLY_MARKER = "HARDY-MEET-REPLY v1"

#: Caption renderings of "Hardy" seen in speech recognisers. Exact-match tier,
#: the attendance agent's ``NAME_PATTERNS``.
NAME_VARIANTS = ("hardy", "hardie", "harty", "hearty", "hardey")

#: SequenceMatcher ratio at or above which a word counts as the name. The
#: attendance agent's ``_FUZZY_THRESHOLD`` tier; 0.8 accepts one substituted
#: letter in a five-letter name and rejects "party" (0.6) and "hard" (0.89 --
#: see :data:`_NOT_NAME`).
FUZZY_THRESHOLD = 0.8

#: Words close enough to the name to pass the ratio but meaning something else.
#: The attendance agent keeps the same list (``_IGNORE_LIST``) for the same
#: reason: a fuzzy match on a common word makes the bot interrupt.
_NOT_NAME = frozenset(
    {"hard", "hardly", "harder", "hardware", "handy", "harry", "tardy", "lardy"}
)

#: Doubt and ideas, the second gate. Deliberately phrases, not single words:
#: "idea" alone fires on "no idea where the cable went".
DOUBT_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bdon'?t think (?:this|that|it)\b",
        r"\b(?:won'?t|wouldn'?t|doesn'?t|isn'?t going to|not going to) work\b",
        r"\bnot (?:so |really )?sure (?:about|this|that|it)\b",
        r"\bi doubt\b",
        r"\bwhat if we\b",
        r"\bhow about (?:we|using|a|an)\b",
        r"\b(?:we|you) could (?:just |also )?(?:use|try|add|make|build|swap)\b",
        r"\bi have an idea\b",
        r"\bshould we\b",
    )
)

REPLY_SYSTEM = (
    "You are {name}, a hardware engineer on the team, speaking out loud in a "
    "live video call. You talk like a colleague, not an assistant."
)

REPLY_PROMPT = """\
{marker}
You are {name}, in a live product meeting. After the call you will draft the \
board on the engineer's laptop and message them on Slack with any open spec \
questions.

Someone just said something that may call for a reply from you. Decide whether \
to speak, and if so what to say out loud.

Rules:
1. At most two short sentences, under 35 words. It will be spoken aloud.
2. Sound like a teammate. No bullet points, no lists, no emoji, no markdown.
3. Do not invent numbers, parts or deadlines nobody said.
4. If the remark was a doubt or an idea, acknowledge it and commit to trying \
it, e.g. "Fair point, I'll try it and send you a first pass after the call."
5. If the remark was not really for you and you have nothing useful to add, \
reply with an empty string.

Return JSON only: {{"reply": "what you say, or empty"}}

RECENT CONVERSATION (oldest first):
{context}

THE REMARK:
{speaker}: {text}
"""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", text.casefold().replace("\u2019", "'"))


def addressed(text: str, name: str = "Hardy") -> bool:
    """Does this line name the bot? Exact, then variants, then fuzzy.

    The three tiers of the attendance agent's ``detect_keyword``; the spaced-
    letters tier is left out because captions do not spell names out.
    """
    target = name.casefold()
    variants = {target, *NAME_VARIANTS} if target == "hardy" else {target}
    words = _words(text)
    if any(word in variants for word in words):
        return True
    for word in words:
        if word in _NOT_NAME or len(word) < 4:
            continue
        if difflib.SequenceMatcher(None, word, target).ratio() >= FUZZY_THRESHOLD:
            return True
    return False


def voices_doubt(text: str) -> bool:
    """Is this line a doubt about the plan, or a proposed alternative?"""
    # Captions typeset apostrophes ("don’t"); the patterns are written ASCII.
    plain = text.replace("\u2019", "'").replace("\u2018", "'")
    return any(pattern.search(plain) for pattern in DOUBT_PATTERNS)


def clean_reply(raw: str, *, name: str = "Hardy", max_chars: int = 240) -> str:
    """Turn a model answer into one speakable line, or ``""`` for silence.

    Accepts the requested ``{"reply": ...}`` JSON and, as the attendance
    agent's sanitiser does, a bare string. Strips a ``Hardy:`` prefix, quotes
    and markdown; keeps at most two sentences; refuses an over-long answer by
    cutting at a sentence boundary rather than mid-word, so what is heard is
    always a whole sentence.
    """
    text = strip_code_fence(raw or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ""
        if not isinstance(data, dict) or not isinstance(data.get("reply"), str):
            return ""
        text = data["reply"]
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(rf"^(?:{re.escape(name)}|assistant)\s*:\s*", "", text, flags=re.I)
    text = text.strip().strip('"').strip("“”").strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = ""
    for sentence in sentences[:2]:
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > max_chars:
            break
        kept = candidate
    if not kept:
        # One sentence longer than the budget: a monologue, not a reply.
        return ""
    return kept


def transcript_text(utterances: list[Utterance], *, include_self: bool = False) -> str:
    """``speaker: text`` lines, the shape :mod:`meetings.intent` expects."""
    return "\n".join(
        f"{u.speaker or 'participant'}: {u.text.strip()}"
        for u in utterances
        if u.text.strip() and (include_self or not u.is_self)
    )


@dataclass(frozen=True)
class BrainPolicy:
    name: str = "Hardy"
    #: Silence after the last human line before Hardy may start speaking.
    quiet_s: float = 1.2
    #: Give up on a trigger if the room has not gone quiet within this long.
    max_wait_s: float = 12.0
    #: Minimum gap between two replies (attendance agent: 30 s).
    cooldown_s: float = 20.0
    #: Hard cap on replies per call. A voice that keeps talking is a bug.
    max_replies: int = 8
    #: Seconds of conversation handed to the model (attendance agent: 120 s).
    context_s: float = 120.0
    reply_to_doubt: bool = True
    max_reply_chars: int = 240


@dataclass(frozen=True)
class Reply:
    trigger: Utterance
    reason: str  # "addressed" | "doubt"
    text: str
    receipt: SpokenReceipt


@dataclass(frozen=True)
class SkippedTrigger:
    trigger: Utterance
    reason: str


SayFn = Callable[[str], Awaitable[SpokenReceipt]]


@dataclass
class _Heard:
    utterance: Utterance
    at: float


class Brain:
    """Consumes utterances, keeps the transcript, and speaks when it should.

    ``say`` is ``lambda text: speak.say(session, text)``; ``model.generate`` is
    synchronous (the :class:`~silkscreen.agents.model.Model` protocol) and runs
    in a worker thread so the listen loop keeps reading captions meanwhile.
    ``clock``/``sleep`` are the test seams.
    """

    def __init__(
        self,
        model: Model,
        say: SayFn,
        policy: BrainPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ):
        self.model = model
        self._say = say
        self.policy = policy or BrainPolicy()
        self._clock = clock
        self._sleep = sleep
        self._to_thread = to_thread
        self._heard: list[_Heard] = []
        self._human_count = 0
        self._last_human_at = float("-inf")
        self._last_reply_at = float("-inf")
        self._speaking = False
        self._pending: tuple[Utterance, str] | None = None
        self._task: asyncio.Task | None = None
        self.replies: list[Reply] = []
        self.skipped: list[SkippedTrigger] = []
        self.errors: list[str] = []

    # -- transcript -------------------------------------------------------

    @property
    def transcript(self) -> list[Utterance]:
        return [h.utterance for h in self._heard]

    def _is_self(self, u: Utterance) -> bool:
        # listen.py marks the bot's own captions is_self; the speaker-name check
        # is a second guard, because answering our own sentence is a loop.
        speaker = (u.speaker or "").strip().casefold()
        return u.is_self or speaker in (self.policy.name.casefold(), "you")

    def _record(self, u: Utterance, now: float) -> None:
        if self._heard:
            last = self._heard[-1].utterance
            same = (last.speaker, last.is_self) == (u.speaker, u.is_self)
            if same and u.text.strip().startswith(last.text.strip()):
                # A refinement of the line we already have: upsert, Vexa's rule.
                self._heard[-1] = _Heard(u, now)
                return
        self._heard.append(_Heard(u, now))

    # -- inbound ----------------------------------------------------------

    async def hear(self, u: Utterance) -> None:
        now = self._clock()
        self._record(u, now)
        if self._is_self(u):
            return
        self._human_count += 1
        self._last_human_at = now
        reason = ""
        if addressed(u.text, self.policy.name):
            reason = "addressed"
        elif self.policy.reply_to_doubt and voices_doubt(u.text):
            reason = "doubt"
        if not reason:
            return
        if self._speaking:
            self.skipped.append(SkippedTrigger(u, "Hardy was already speaking"))
            return
        if len(self.replies) >= self.policy.max_replies:
            self.skipped.append(
                SkippedTrigger(u, f"reply cap of {self.policy.max_replies} reached")
            )
            return
        if now - self._last_reply_at < self.policy.cooldown_s:
            self.skipped.append(
                SkippedTrigger(
                    u, f"within the {self.policy.cooldown_s:.0f} s reply cooldown"
                )
            )
            return
        if self._pending is not None:
            # A newer trigger replaces the one still waiting for quiet: answer
            # what was said last, once.
            self.skipped.append(
                SkippedTrigger(self._pending[0], "superseded by a later remark")
            )
        self._pending = (u, reason)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._respond())

    # -- the reply --------------------------------------------------------

    async def _wait_for_quiet(self, started: float) -> bool:
        while True:
            silent_for = self._clock() - self._last_human_at
            if silent_for >= self.policy.quiet_s:
                return True
            if self._clock() - started > self.policy.max_wait_s:
                return False
            await self._sleep(self.policy.quiet_s - silent_for)

    def _context(self, now: float) -> str:
        recent = [
            h.utterance for h in self._heard if now - h.at <= self.policy.context_s
        ]
        return transcript_text(recent, include_self=True) or "(nothing yet)"

    async def _respond(self) -> None:
        try:
            await self._respond_once()
        except asyncio.CancelledError:
            if self._pending is not None:
                self.skipped.append(
                    SkippedTrigger(self._pending[0], "the call ended before a reply")
                )
                self._pending = None
            raise

    async def _respond_once(self) -> None:
        started = self._clock()
        if not await self._wait_for_quiet(started):
            u, _ = self._pending
            self._pending = None
            self.skipped.append(SkippedTrigger(u, "the room never went quiet"))
            return
        u, reason = self._pending
        heard_before = self._human_count
        prompt = REPLY_PROMPT.format(
            marker=REPLY_MARKER,
            name=self.policy.name,
            context=self._context(self._clock()),
            speaker=u.speaker or "participant",
            text=u.text.strip(),
        )
        try:
            raw = await self._to_thread(
                self.model.generate,
                prompt,
                system=REPLY_SYSTEM.format(name=self.policy.name),
                temperature=0.4,
                max_output_tokens=512,
            )
        except ModelError as exc:
            self._pending = None
            self.errors.append(f"reply model failed: {exc}")
            self.skipped.append(SkippedTrigger(u, f"the reply model failed: {exc}"))
            return
        text = clean_reply(
            raw, name=self.policy.name, max_chars=self.policy.max_reply_chars
        )
        if not text:
            self._pending = None
            self.skipped.append(SkippedTrigger(u, "the model chose to stay quiet"))
            return
        if self._human_count != heard_before and not await self._wait_for_quiet(
            self._clock()
        ):
            # Someone started talking while the model thought, and kept going.
            self._pending = None
            self.skipped.append(
                SkippedTrigger(u, "the conversation moved on before Hardy could reply")
            )
            return
        if self._pending is not None and self._pending[0] is not u:
            # A newer trigger arrived while thinking; answer that one instead.
            await self._respond_once()
            return
        self._pending = None
        self._speaking = True
        try:
            receipt = await self._say(text)
        except Exception as exc:  # noqa: BLE001 - a receipt, never a crash
            receipt = SpokenReceipt(False, f"not spoken: say() raised {exc!r}")
        finally:
            self._speaking = False
        self._last_reply_at = self._clock()
        self.replies.append(Reply(u, reason, text, receipt))
        log.info("reply (%s) spoken=%s: %s", reason, receipt.spoken, text)

    async def settle(self, timeout_s: float = 0.0) -> None:
        """Stop thinking about replies. ``timeout_s`` lets a reply in flight finish."""
        task = self._task
        if task is None or task.done():
            return
        if timeout_s > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout_s)
                return
            except (TimeoutError, Exception):  # noqa: BLE001
                pass
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
