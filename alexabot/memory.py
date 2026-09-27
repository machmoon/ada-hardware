"""Design preferences across conversations: Amazon Bedrock AgentCore Memory
behind a seam, with an in-process stand-in and an honest "off".

What is remembered is narrow on purpose: the design preferences a person
states to Ada ("powered from USB-C", "3.3 V logic", "no LED"), so a new
conversation can offer them back ("Last time you chose: USB-C power input.
Same again?"). Boards themselves stay in alexabot's SQLite store and reach
speech only through ``recall_my_boards``: a model-extracted copy of "one open
blocker" would go stale when the board changes and still be spoken as truth.

Three implementations of one :class:`Memory` contract (``recall`` and
``record`` never raise; a failure is ``unavailable``/``failed`` with a
sentence from :data:`REASONS`, never an empty ``ok``):

:class:`AgentCoreMemory`
    boto3's ``bedrock-agentcore`` client, injected (the ``polly.make_client``
    convention). One ``CreateEvent`` per spoken turn, carrying the person's
    words and the question they were answering, never Ada's reply; one
    ``RetrieveMemoryRecords`` per conversation, when it opens. AgentCore's
    built-in user-preference strategy extracts the preferences (about a
    minute later, per the samples) into ``/users/{actorId}/preferences/``.
:class:`ScriptedMemory`
    For ``--scripted`` and the tests: a few regular expressions over the
    person's words, extracted at once and kept in this process. It stands in
    for extraction and says so everywhere it shows.
:class:`MemoryOff`
    No memory configured: the page, the banner and the agent say so.

Sources, read as source (Apache-2.0; nothing copied):

- aws/bedrock-agentcore-sdk-python ``c7423e5``: ``memory/client.py``
  (``create_event``, ``retrieve_memories``, ``create_or_get_memory``),
  ``memory/constants.py`` ``DEFAULT_NAMESPACES``,
  ``memory/integrations/strands/session_manager.py`` and ``config.py``
  (``RetrievalConfig.relevance_score`` 0.2, the floor used here). Read to
  decide and **not a dependency**: ``retrieve_memories`` answers ``[]`` on a
  ``ClientError`` (a refusal would read as "nothing remembered"),
  ``create_event`` sends no ``clientToken``, ``create_or_get_memory`` matches
  a resource by id prefix, and the Strands session manager writes one event
  per message, tool results and host notes included.
- awslabs/amazon-bedrock-agentcore-samples ``e1a55b3``,
  ``01-features/04-manage-context-of-your-agent/memory/``:
  ``02-long-term-memory/01-built-in-strategies/user-preference.py`` (the
  boto3 path and the ``/users/{actorId}/preferences/`` template) and
  ``04-namespaces/README.md`` (the trailing slash).
- botocore ``bedrock-agentcore/2024-02-28/service-2.json`` and
  ``bedrock-agentcore-control/2023-06-05/{service-2,waiters-2}.json``
  (develop ``86201a3``; the venv's botocore 1.43.95 carries the same shapes):
  the request shapes, the ``ActorId``/``MemoryId`` patterns, the error names.
- KayLerch/alexa-skill-mcp-bridge ``ca2c2ef``
  ``packages/agent/src/memory/agentcore-memory.ts`` and ``docs/decisions.md``
  D30/D31: one event per exchange, preferences read once into the prompt, an
  explicit preferences namespace. Deviation, stated: past events are not
  rehydrated into the history (a replayed "the board is routing" is stale).

Standard library only; boto3 is imported inside :func:`make_clients`.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from . import speech

__all__ = [
    "CATEGORIES",
    "MEMORY_NAME",
    "NAMESPACE_TEMPLATE",
    "REASONS",
    "STRATEGY",
    "AgentCoreMemory",
    "ConversationMemory",
    "Memory",
    "MemoryOff",
    "MemoryWriter",
    "Preference",
    "Recall",
    "Recorded",
    "ScriptedMemory",
    "TurnRecord",
    "actor_id",
    "ask_sentence",
    "category",
    "chip_label",
    "make_clients",
    "memory_note",
    "parse_records",
    "probe",
    "turn_record",
    "unstated",
]

# -- the resource: shared with scripts/aws/agentcore_memory.py -----------------------

#: ``CreateMemory.name``: ``[a-zA-Z][a-zA-Z0-9_]{0,47}``.
MEMORY_NAME = "AdaDesignPreferences"
#: Starts and ends with "/" (the samples' ``04-namespaces/README.md``): the
#: trailing slash stops ``acct-1`` from matching ``acct-12``.
NAMESPACE_TEMPLATE = "/users/{actorId}/preferences/"
STRATEGY_NAME = "DesignPreferences"
STRATEGY = {"userPreferenceMemoryStrategy": {
    "name": STRATEGY_NAME,
    "description": ("Stable board-design preferences a person states to Ada: power "
                    "input, logic voltage, indicators, connectors, size."),
    # ``namespaces`` is "a legacy parameter" in the service model.
    "namespaceTemplates": [NAMESPACE_TEMPLATE],
}}
#: ``CreateMemory.eventExpiryDuration`` (days, 3 to 365). The sim never reads
#: events back; they exist to feed extraction, and the records persist.
EVENT_EXPIRY_DAYS = 7
MEMORY_DESCRIPTION = ("Ada's simulated Alexa+ agent: design preferences people state, "
                      "offered back in a later conversation.")
#: A fixed query, KayLerch's shape: the preferences are read once, not per turn.
PREFERENCE_QUERY = ("printed circuit board design preferences: power input, "
                    "connectors, logic voltage, indicator LEDs, board size")
TOP_K = 5
#: The Strands integration's default ``RetrievalConfig.relevance_score``.
RELEVANCE_FLOOR = 0.2
SHORT_MAX = 60
RECALL_WAIT_S = 2.0
WRITER_QUEUE = 32
WRITER_DRAIN_S = 5.0
DEFAULT_MAX_CALLS = 60
DEFAULT_REGION = "us-east-1"
#: botocore's ``MemoryId`` pattern, the id alone (not the ARN form).
MEMORY_ID = re.compile(r"\A[a-zA-Z][a-zA-Z0-9-_]{0,99}-[a-zA-Z0-9]{10}\Z")
#: botocore's ``ActorId`` pattern.
ACTOR_ID = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*"
                      r"[a-zA-Z0-9-_/]*\Z")

OFF_FIX = ("Set ADA_AGENTCORE_MEMORY_ID to remember design preferences between "
           "conversations.")
SCRIPTED_NOTE = ("Scripted memory: extracted by rules on this machine, at once; "
                 "AgentCore takes about a minute.")
SOURCE_SCRIPTED = "scripted, on this machine"

#: Every failure detail any :class:`Memory` may carry: a sentence, never
#: exception text (which holds ARNs and account numbers).
REASONS = {
    "access_denied": "this AWS identity may not use AgentCore Memory",
    "not_found": "the memory resource was not found",
    "throttled": "AgentCore Memory is busy",
    "unreachable": "AgentCore Memory could not be reached",
    "no_credentials": "no AWS credentials were found for AgentCore Memory",
    "invalid": "AgentCore Memory refused the request",
    "budget": "this run's memory call limit is spent",
    "failed": "AgentCore Memory answered with an error",
}
WRITE_NOTICE = "I couldn't save that to memory; your board is unaffected."
_CODES = {
    "AccessDeniedException": "access_denied",
    "UnauthorizedException": "access_denied",
    "ResourceNotFoundException": "not_found",
    "ThrottledException": "throttled",
    "ThrottlingException": "throttled",
    "ServiceQuotaExceededException": "throttled",
    "ValidationException": "invalid",
    "InvalidInputException": "invalid",
}
_UNREACHABLE = frozenset({"EndpointConnectionError", "ConnectTimeoutError",
                          "ReadTimeoutError", "ConnectionClosedError",
                          "ProxyConnectionError"})


def reason_for(exc: BaseException) -> str:
    """The :data:`REASONS` key for ``exc``; never its message."""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        code = str((response.get("Error") or {}).get("Code") or "")
        return _CODES.get(code, "failed")
    name = type(exc).__name__
    if name in ("NoCredentialsError", "PartialCredentialsError"):
        return "no_credentials"
    if name in _UNREACHABLE or isinstance(exc, (OSError, TimeoutError)):
        return "unreachable"
    return "failed"


# -- who, and what a turn writes ------------------------------------------------------


def actor_id(account: str) -> str:
    """The AgentCore actor for an alexabot account: hashed, so the account name
    never leaves the machine, and so ``a.b`` (legal in an account,
    ``billing/accounts.py``) cannot break botocore's ``ActorId`` pattern or
    put a ``/`` into the namespace."""
    digest = hashlib.sha256(str(account).encode("utf-8")).hexdigest()
    return "acct-" + digest[:32]


def namespace_for(actor: str) -> str:
    return NAMESPACE_TEMPLATE.replace("{actorId}", actor)


@dataclass(frozen=True)
class TurnRecord:
    """What one spoken turn may write: the person's words, and the question
    they were answering. ``skip`` names why nothing is written."""

    session: str
    turn_id: str
    words: str
    question: str | None = None
    skip: str | None = None

    @property
    def client_token(self) -> str:
        """One per turn, so boto3's own retry of a timed-out write stores it once."""
        return f"{self.session}-{self.turn_id}"


def turn_record(session: str, turn_id: str, words: str, *, question: str | None,
                source: str | None,
                tool_calls: Sequence[Mapping[str, Any]] = ()) -> TurnRecord:
    """The turn as memory sees it. Skipped, with the reason, when:

    - ``chip``: a button label is not the person's words;
    - ``you_choose``: the only tool call left the answer to Ada, and "I'll
      assume no LED" must never become "prefers no LED";
    - ``empty``: nothing was said.

    ``question`` is kept only when it is one (ends in ``?``): Ada's other
    sentences are her own words, and writing them would teach the memory what
    Ada chose and attribute it to the person.
    """
    said = " ".join(str(words or "").split())
    asked = " ".join(str(question or "").split())
    asked_or_none = asked if asked.endswith("?") else None
    skip = None
    calls = [c for c in tool_calls if isinstance(c, Mapping)]
    if source == "chip":
        skip = "chip"
    elif not said:
        skip = "empty"
    elif (len(calls) == 1 and calls[0].get("name") == "answer_design_questions"
          and (calls[0].get("input") or {}).get("you_choose") is True):
        skip = "you_choose"
    return TurnRecord(session=session, turn_id=turn_id, words=said,
                      question=asked_or_none, skip=skip)


# -- preferences ----------------------------------------------------------------------

#: What a preference is about, so a request that names the same thing wins over
#: a remembered one. First match wins; an uncategorised preference is shown but
#: never offered, since the host cannot tell whether a request overrides it.
CATEGORIES = (
    ("power input", r"\busb\b|usb[- ]?c\b|barrel|battery|li-?po|coin cell|\bmains\b"
                    r"|power input|powered (?:by|from|over)"),
    ("logic voltage", r"\b\d+(?:\.\d+)?\s*v(?:olts?)?\b|\b\dv\d\b|\bvolts?\b"
                      r"|logic level"),
    ("indicator", r"\bleds?\b|indicator|status light"),
    ("connector", r"connector|\bheaders?\b|\bjst\b|qwiic|stemma|terminal block"
                  r"|screw terminal"),
    ("size", r"\bsize\b|\bsmall\b|compact|\btiny\b|\bmm\b|\binch(?:es)?\b"),
)
_CATEGORY_PATTERNS = tuple((name, re.compile(p, re.IGNORECASE))
                           for name, p in CATEGORIES)


def category(text: str) -> str | None:
    return next((n for n, p in _CATEGORY_PATTERNS if p.search(str(text or ""))), None)


def named(words: str) -> frozenset[str]:
    """Every category ``words`` mentions."""
    return frozenset(n for n, p in _CATEGORY_PATTERNS if p.search(str(words or "")))


#: AgentCore's extraction writes both "The user prefers X" and a subject-less
#: "Always prefers X" (measured live 2026-09-26: "Always prefers 3.3 V logic").
_LEAD = re.compile(r"^\s*(?:(?:the\s+)?(?:user|person)\s+)?"
                   r"(?:(?:always|usually|generally|typically)\s+)?"
                   r"(?:prefers|wants|needs|likes)\s+(?:to\s+(?:use|have)\s+)?",
                   re.IGNORECASE)


def short_text(text: str) -> str:
    """A preference as the chip and the ask name it: one leading "(The) user
    prefers ..." or "Always prefers ..." dropped, sentence breaks folded, at
    most 60 characters."""
    out = _LEAD.sub("", " ".join(str(text or "").split()), count=1)
    out = re.sub(r"[.!?]+(\s+|$)", lambda m: ", " if m.group(1) else "", out)
    out = out.replace("?", "").replace("!", "").replace('"', "'").strip(" ,;:")
    if len(out) > SHORT_MAX:
        out = out[:SHORT_MAX].rsplit(" ", 1)[0].rstrip(" ,;:")
    return out


@dataclass(frozen=True)
class Preference:
    text: str
    short: str
    category: str | None
    record_id: str | None = None
    created_at: str | None = None
    score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "short": self.short, "category": self.category,
                "created_at": self.created_at}


def preference(text: str, **fields: Any) -> Preference:
    return Preference(text=str(text), short=short_text(text), category=category(text),
                      **fields)


def unstated(prefs: Iterable[Preference], words: str) -> tuple[Preference, ...]:
    """The preferences ``words`` does not already settle.

    Kept only when it has a category and ``words`` names none of that
    category: "a 5 V board with a barrel jack" settles both a remembered
    USB-C input and 3.3 V logic. It only ever removes, so its failure is a
    missed offer, never an overridden request.
    """
    taken = named(words)
    return tuple(p for p in prefs if p.category is not None and p.category not in taken)


def _stamp(value: Any) -> str | None:
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return None


def parse_records(summaries: Any, namespace: str) -> tuple[Preference, ...]:
    """``RetrieveMemoryRecords.memoryRecordSummaries`` as preferences.

    A record is kept when its first namespace is ours (defence in depth), its
    ``score`` reaches :data:`RELEVANCE_FLOOR` (a record with no score is kept:
    the field is optional in the service model and the floor cannot apply),
    and it has text. The text is the documented JSON's ``preference`` when it
    parses ("JSON objects with context, preference, and categories", the
    user-preference strategy's devguide page), else the raw text.
    Case-folded duplicates are dropped.
    """
    out: list[Preference] = []
    seen: set[str] = set()
    for summary in summaries or []:
        if not isinstance(summary, Mapping):
            continue
        spaces = summary.get("namespaces") or []
        if not spaces or spaces[0] != namespace:
            continue
        score = summary.get("score")
        if isinstance(score, (int, float)) and score < RELEVANCE_FLOOR:
            continue
        raw = str((summary.get("content") or {}).get("text") or "").strip()
        text = raw
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, Mapping) and isinstance(parsed.get("preference"), str):
            text = parsed["preference"].strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        out.append(preference(
            text, record_id=summary.get("memoryRecordId"),
            created_at=_stamp(summary.get("createdAt")),
            score=float(score) if isinstance(score, (int, float)) else None))
    return tuple(out)


# -- what Ada says, and what the model and the page read ------------------------


def _mid(text: str) -> str:
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def ask_sentence(prefs: Sequence[Preference]) -> str:
    """The host-written question: at most two named (the chip shows the rest),
    after a colon, because AgentCore's records are noun phrases ("USB-C power
    input") or participles ("Powered from USB-C") and only a list reads as
    English with both; in spoken units, and it passes
    :func:`speech.speech_problems`."""
    names = [_mid(speech.clause(p.short)) for p in list(prefs)[:2]]
    return speech.spoken(f"Last time you chose: {speech.join_and(names)}. "
                         "Same again?")


def memory_note(prefs: Sequence[Preference], *, ask: str | None = None,
                asked: bool = False) -> str:
    """The bracketed note appended to the person's words for the model."""
    if asked and not prefs:
        return ("[Memory: the person's own words settle what you asked about; "
                "use their words alone.]")
    listed = "; ".join(f'"{p.short}"' for p in prefs)
    lead = f"[Memory: from earlier conversations with this person: {listed}."
    if asked:
        return (f"{lead} You already asked about these; if the person agreed, "
                "include exactly these in the intent.]")
    return f'{lead} Ask first, exactly: "{ask or ask_sentence(prefs)}"]'


def chip_label(state: str, prefs: Sequence[Preference], kind: str) -> str:
    """The page's memory pill, built here so tests pin the words."""
    if kind == "off" or state == "off":
        return "Memory off"
    if state == "checking":
        return "Memory: checking"
    if state != "ok":
        return "Memory unavailable"
    if not prefs:
        return ("Scripted memory: nothing saved yet" if kind == "scripted"
                else "Memory on: nothing saved yet")
    shorts = [p.short for p in prefs]
    listed = ", ".join(shorts[:3])
    if len(shorts) > 3:
        listed += f" and {len(shorts) - 3} more"
    lead = "Remembering (scripted)" if kind == "scripted" else "Remembering"
    return f"{lead}: {listed}"


# -- the guard on model text that claims a memory -------------------------------

#: A sentence that claims to remember. Regular expressions, like the M3 guards:
#: they catch the tested phrasings, not every possible one.
_CLAIM = re.compile(
    r"\b(?:last time|(?:earlier|previous|past) (?:conversation|session)s?"
    r"|I (?:remember|recall)|you (?:usually|always|normally)|as (?:before|usual)"
    r"|same as (?:before|last time))\b",
    re.IGNORECASE,
)
_STOP = frozenset({
    "board", "boards", "time", "last", "same", "again", "you", "your", "wanted",
    "want", "wants", "mentioned", "said", "asked", "earlier", "previous", "past",
    "conversation", "conversations", "session", "sessions", "remember", "recall",
    "usually", "always", "normally", "before", "usual", "the", "and", "or", "of",
    "to", "for", "with", "on", "in", "it", "is", "was", "be", "me", "my", "we", "us",
    "this", "that", "these", "those", "what", "which", "as", "at", "by", "from",
    "like", "prefer", "prefers", "preferred", "mention", "one"})
MEMORY_CLAIM_FALLBACK = "I'll go by what you just asked for."
NOTHING_SAVED = "I don't have any preferences saved from earlier conversations."
OFF_SPEECH = ("I don't keep your preferences between conversations here, but I can "
              "tell you about your saved boards.")
UNAVAILABLE_SPEECH = ("I can't reach my memory of your preferences right now, but I "
                      "can tell you about your saved boards.")


def _distinct(text: str) -> set[str]:
    words = (w.strip(".-+") for w in re.findall(r"[a-z0-9][a-z0-9.+-]*",
                                                 str(text).casefold()))
    return {w for w in words if len(w) >= 2 and w not in _STOP}


@dataclass(frozen=True)
class GuardView:
    """What the claim guard knows about memory on this turn."""

    state: str
    preferences: tuple[Preference, ...] = ()
    note: tuple[Preference, ...] = ()
    offered: bool = False


def claims_memory(text: str, note: Sequence[Preference]) -> bool:
    """``text`` claims a memory the host did not hand over on this turn."""
    known: set[str] = set()
    for p in note:
        known |= _distinct(p.text) | _distinct(p.short)
    for sentence in speech.sentences(text):
        if not _CLAIM.search(sentence):
            continue
        if not note or not (_distinct(sentence) & known):
            return True
    return False


def claim_replacement(view: GuardView) -> str:
    if view.note and not view.offered:
        return ask_sentence(view.note)
    if view.state == "checking" or (view.state == "ok" and view.preferences):
        return MEMORY_CLAIM_FALLBACK
    if view.state == "ok":
        return NOTHING_SAVED
    if view.state == "unavailable":
        return UNAVAILABLE_SPEECH
    return OFF_SPEECH


# -- the contract ---------------------------------------------------------------------

RECALL_STATES = frozenset({"ok", "unavailable", "off"})
RECORD_STATES = frozenset({"written", "skipped", "failed", "off"})


@dataclass(frozen=True)
class Recall:
    state: str
    preferences: tuple[Preference, ...] = ()
    detail: str | None = None
    ms: int = 0


@dataclass(frozen=True)
class Recorded:
    state: str
    detail: str | None = None
    ms: int = 0


class Memory(Protocol):
    kind: str

    def describe(self) -> dict[str, Any]: ...

    def recall(self, actor: str) -> Recall: ...

    def record(self, actor: str, session: str, rec: TurnRecord) -> Recorded: ...


class MemoryOff:
    """No memory: every call says so, and why."""

    kind = "off"

    def __init__(self, reason: str = OFF_FIX) -> None:
        self.reason = reason

    def describe(self) -> dict[str, Any]:
        return {"kind": "off", "reason": self.reason}

    def recall(self, actor: str) -> Recall:
        return Recall("off", detail=self.reason)

    def record(self, actor: str, session: str, rec: TurnRecord) -> Recorded:
        if rec.skip:
            return Recorded("skipped", rec.skip)
        return Recorded("off", self.reason)


#: The scripted stand-in's whole "extractor": the person's words only.
SCRIPTED_RULES = (
    (re.compile(r"\busb[- ]?c\b", re.IGNORECASE), "USB-C power input"),
    (re.compile(r"\b3\.3\s*v(?:olts?)?\b|\b3v3\b|\bthree point three volts?\b",
                re.IGNORECASE), "3.3 V logic"),
    (re.compile(r"\bno (?:power )?(?:indicator )?leds?\b"
                r"|\bwithout an? (?:\w+ )?leds?\b", re.IGNORECASE), "no indicator LED"),
    (re.compile(r"\bwith an? (?:\w+ )?leds?\b", re.IGNORECASE),
     "a power indicator LED"),
)


class ScriptedMemory:
    """In process, per actor, extracted at once by :data:`SCRIPTED_RULES`.

    A newer statement replaces an older one of the same category ("with an
    LED" after "no LED"). ``fail`` (a :data:`REASONS` key) makes every call
    fail with that reason, so the fake is held to the adapter's rules.
    """

    kind = "scripted"

    def __init__(self, *, fail: str | None = None,
                 clock: Callable[[], datetime.date] = datetime.date.today) -> None:
        if fail is not None and fail not in REASONS:
            raise ValueError(f"fail must be one of {sorted(REASONS)}")
        self.fail = fail
        self._clock = clock
        self._lock = threading.Lock()
        self._by_actor: dict[str, list[Preference]] = {}

    def describe(self) -> dict[str, Any]:
        return {"kind": "scripted", "name": MEMORY_NAME, "strategy": "scripted rules",
                "namespace": NAMESPACE_TEMPLATE, "region": None,
                "reason": SCRIPTED_NOTE}

    def recall(self, actor: str) -> Recall:
        if self.fail:
            return Recall("unavailable", detail=REASONS[self.fail])
        with self._lock:
            return Recall("ok", tuple(self._by_actor.get(actor, ())))

    def record(self, actor: str, session: str, rec: TurnRecord) -> Recorded:
        if rec.skip:
            return Recorded("skipped", rec.skip)
        if self.fail:
            return Recorded("failed", REASONS[self.fail])
        found = [text for pattern, text in SCRIPTED_RULES if pattern.search(rec.words)]
        today = self._clock().isoformat()
        with self._lock:
            kept = self._by_actor.setdefault(actor, [])
            for text in found:
                new = preference(text, record_id=f"scripted-{len(kept) + 1}",
                                 created_at=today)
                kept[:] = [p for p in kept if p.category != new.category] + [new]
        return Recorded("written")


class AgentCoreMemory:
    """Amazon Bedrock AgentCore Memory over an injected boto3 client.

    Every call first takes one unit of the process-wide budget (``take``),
    beside the model and Polly budgets.
    """

    kind = "agentcore"

    def __init__(self, data_client: Any, memory_id: str, *, region: str,
                 take: Callable[[], bool] | None = None,
                 now: Callable[[], datetime.datetime] | None = None) -> None:
        self.client = data_client
        self.memory_id = memory_id
        self.region = region
        self._take = take
        self._now = now or (lambda: datetime.datetime.now(datetime.UTC))

    def describe(self) -> dict[str, Any]:
        return {"kind": "agentcore", "name": MEMORY_NAME, "strategy": "userPreference",
                "namespace": NAMESPACE_TEMPLATE, "region": self.region,
                "reason": None}

    def source(self) -> str:
        return (f"Amazon Bedrock AgentCore Memory, {self.region}, "
                "user-preference strategy")

    def _spend(self) -> bool:
        return self._take is None or bool(self._take())

    def recall(self, actor: str) -> Recall:
        if not self._spend():
            return Recall("unavailable", detail=REASONS["budget"])
        space = namespace_for(actor)
        t0 = time.monotonic()
        try:
            response = self.client.retrieve_memory_records(
                memoryId=self.memory_id, namespace=space,
                searchCriteria={"searchQuery": PREFERENCE_QUERY, "topK": TOP_K},
                maxResults=TOP_K)
        except Exception as exc:  # noqa: BLE001 -- the reason in words, never the text
            return Recall("unavailable", detail=REASONS[reason_for(exc)],
                          ms=_ms(t0))
        return Recall("ok", parse_records(response.get("memoryRecordSummaries"), space),
                      ms=_ms(t0))

    def record(self, actor: str, session: str, rec: TurnRecord) -> Recorded:
        if rec.skip:
            return Recorded("skipped", rec.skip)
        if not self._spend():
            return Recorded("failed", REASONS["budget"])
        payload = []
        if rec.question:
            payload.append({"conversational": {"role": "ASSISTANT",
                                               "content": {"text": rec.question}}})
        payload.append({"conversational": {"role": "USER",
                                           "content": {"text": rec.words}}})
        t0 = time.monotonic()
        try:
            self.client.create_event(
                memoryId=self.memory_id, actorId=actor, sessionId=session,
                eventTimestamp=self._now(), clientToken=rec.client_token,
                payload=payload)
        except Exception as exc:  # noqa: BLE001
            return Recorded("failed", REASONS[reason_for(exc)], ms=_ms(t0))
        return Recorded("written", ms=_ms(t0))


def _ms(t0: float) -> int:
    return round((time.monotonic() - t0) * 1000)


def probe(control_client: Any, memory_id: str) -> list[str]:
    """One ``GetMemory`` at startup: every problem with the resource, in words."""
    try:
        answer = control_client.get_memory(memoryId=memory_id) or {}
        memory = answer.get("memory") or {}
    except Exception as exc:  # noqa: BLE001
        return [f"AgentCore Memory: {REASONS[reason_for(exc)]}"]
    problems = []
    status = memory.get("status")
    if status != "ACTIVE":
        problems.append(f"AgentCore Memory is {status or 'in an unknown state'}, not "
                        "ACTIVE; run python scripts/aws/agentcore_memory.py status")
    prefs = [s for s in memory.get("strategies") or []
             if isinstance(s, Mapping) and s.get("type") == "USER_PREFERENCE"]
    if not prefs:
        problems.append("AgentCore Memory has no user-preference strategy; create the "
                        "resource with python scripts/aws/agentcore_memory.py create")
    elif not any((s.get("namespaceTemplates") or s.get("namespaces")) ==
                 [NAMESPACE_TEMPLATE] for s in prefs):
        problems.append("AgentCore Memory's user-preference strategy writes to "
                        f"another namespace than {NAMESPACE_TEMPLATE}; create the "
                        "resource with python scripts/aws/agentcore_memory.py create")
    return problems


def make_clients(region: str) -> tuple[Any, Any]:
    """``(bedrock-agentcore, bedrock-agentcore-control)`` clients with short
    timeouts and three attempts, so a dead endpoint costs seconds, not minutes."""
    import boto3
    from botocore.config import Config

    config = Config(retries={"mode": "standard", "max_attempts": 3},
                    connect_timeout=2, read_timeout=5)
    return (boto3.client("bedrock-agentcore", region_name=region, config=config),
            boto3.client("bedrock-agentcore-control", region_name=region,
                         config=config))


# -- one conversation's side: the recall at open, the note per turn, the writes -------

_STOP_ITEM = object()


class MemoryWriter:
    """One daemon thread and a bounded queue, so a write never delays speech.

    A full queue drops the write (``submit`` answers ``False``); ``close``
    lets the queue drain for up to ``timeout`` seconds.
    """

    def __init__(self, write: Callable[[TurnRecord], None], *, size: int = WRITER_QUEUE,
                 name: str = "ada-memory-writer") -> None:
        self._write = write
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=size)
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, rec: TurnRecord) -> bool:
        if self._closed:
            return False
        try:
            self._queue.put_nowait(rec)
        except queue.Full:
            return False
        return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP_ITEM:
                    return
                self._write(item)
            except Exception:  # noqa: BLE001, S110 -- the writer outlives one bad write
                pass
            finally:
                self._queue.task_done()

    def flush(self, timeout: float) -> bool:
        """Wait until every queued write has been made (tests, and the live check)."""
        with self._queue.all_tasks_done:
            return self._queue.all_tasks_done.wait_for(
                lambda: not self._queue.unfinished_tasks, timeout)

    def close(self, timeout: float = WRITER_DRAIN_S) -> bool:
        if not self._closed:
            self._closed = True
            with contextlib.suppress(queue.Full):
                self._queue.put(_STOP_ITEM, timeout=max(timeout, 0.05))
        if timeout > 0:
            self._thread.join(timeout)
        return not self._thread.is_alive()


def _norm(text: str) -> str:
    return " ".join(str(text or "").casefold().split()).strip(" .!?")


class ConversationMemory:
    """Memory for one conversation of the simulated Alexa+ page.

    Opens with one recall on its own thread (the greeting does not wait; the
    first turn waits up to :data:`RECALL_WAIT_S`), builds the ``[Memory]``
    note each turn may carry, notices when Ada spoke the ask, and queues one
    write per spoken turn. Every change is a ``memory`` event on the
    conversation (``conv.set_memory``) and every call a ``trace`` event.
    """

    def __init__(self, memory: Memory, actor: str | None, conv: Any) -> None:
        self.memory = memory
        self.actor = actor
        self.conv = conv
        self.kind = memory.kind
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self.state = "off" if self.kind == "off" else "checking"
        self.preferences: tuple[Preference, ...] = ()
        self.detail: str | None = getattr(memory, "reason", None) \
            if self.kind == "off" else None
        self.note: tuple[Preference, ...] = ()
        self.ask: str | None = None
        self.offered: tuple[Preference, ...] = ()
        self.request: str | None = None
        self.asked_turn = False
        self.done = False
        self.writer: MemoryWriter | None = None
        self._started = False

    def start(self) -> None:
        """Log the first view and start the recall; once, after the greeting
        (``ConversationHost.create`` calls ``session.opened``), so the greeting
        stays the conversation's first event."""
        with self._lock:
            if self._started:
                return
            self._started = True
        self._emit()
        if self.kind == "off":
            self._ready.set()
            return
        self.writer = MemoryWriter(self._write, name=f"ada-memory-{self.conv.id}")
        threading.Thread(target=self._recall, name=f"ada-recall-{self.conv.id}",
                         daemon=True).start()

    # -- what the page shows --------------------------------------------------------

    def source(self) -> str | None:
        if self.kind == "scripted":
            return SOURCE_SCRIPTED
        source = getattr(self.memory, "source", None)
        return source() if callable(source) else None

    def view(self) -> dict[str, Any]:
        with self._lock:
            state, prefs, detail = self.state, self.preferences, self.detail
            offered = [p.short for p in self.offered]
        if state in ("off", "unavailable"):
            title = detail or OFF_FIX
        elif self.kind == "scripted":
            title = SCRIPTED_NOTE
        else:
            title = self.source()
        return {"kind": self.kind, "state": state,
                "label": chip_label(state, prefs, self.kind), "title": title,
                "detail": detail, "source": self.source(),
                "preferences": [p.as_dict() for p in prefs], "offered": offered}

    def _emit(self) -> None:
        setter = getattr(self.conv, "set_memory", None)
        if callable(setter):
            setter(self.view())

    # -- reading --------------------------------------------------------------------

    def _recall(self) -> None:
        try:
            result = self.memory.recall(self.actor or "")
        except Exception:  # noqa: BLE001 -- the contract says never; hold it anyway
            result = Recall("unavailable", detail=REASONS["failed"])
        self.conv.trace("memory", op="retrieve", ms=result.ms, state=result.state,
                        count=len(result.preferences))
        with self._lock:
            known = result.state in RECALL_STATES
            self.state = result.state if known else "unavailable"
            self.preferences = tuple(result.preferences)
            self.detail = result.detail
        self._emit()
        self._ready.set()

    def wait_ready(self, timeout: float) -> bool:
        self.start()
        return self._ready.wait(timeout)

    # -- the note, and the ask ------------------------------------------------------

    def note_for(self, words: str, *, board_open: bool) -> str | None:
        """The ``[Memory]`` note for this turn, or ``None``.

        Only while a new board could start, only with preferences this turn's
        words do not settle, and only until the offer has been made and
        answered once. The turn after the ask gets the "already asked" form,
        re-filtered against the answer ("yes, but a barrel jack").
        """
        with self._lock:
            self.note = ()
            self.asked_turn = False
            if self.done or self.state != "ok" or not self.preferences or board_open:
                return None
            if self.offered:
                self.asked_turn = True
                left = unstated(self.offered, f"{self.request or ''} {words}")
                self.note = left
                return memory_note(left, asked=True)
            left = unstated(self.preferences, words)
            if not left:
                return None
            self.note = left
            self.ask = ask_sentence(left)
            self.request = words
            return memory_note(left, ask=self.ask)

    def guard_view(self) -> GuardView:
        with self._lock:
            return GuardView(state=self.state, preferences=self.preferences,
                             note=self.note,
                             offered=bool(self.offered) or self.asked_turn)

    def after_turn(self, said: str, tool_calls: Sequence[Mapping[str, Any]]) -> None:
        """What this turn did to the offer: made it, answered it, or skipped it."""
        started = any(c.get("name") == "start_board_design" for c in tool_calls
                      if isinstance(c, Mapping))
        changed = False
        with self._lock:
            if self.asked_turn:
                self.done, self.offered, changed = True, (), True
            elif self.note and self.ask and _norm(said) == _norm(self.ask):
                self.offered, changed = self.note, True
            elif self.note and started:
                self.done = True
            self.note = ()
            self.asked_turn = False
        if changed:
            self._emit()

    # -- writing --------------------------------------------------------------------

    def record(self, rec: TurnRecord) -> None:
        if self.writer is None or rec.skip:
            return
        if not self.writer.submit(rec):
            self.conv.trace("memory", op="create_event", ms=0, state="dropped")

    def _write(self, rec: TurnRecord) -> None:
        try:
            result = self.memory.record(self.actor or "", self.conv.id, rec)
        except Exception:  # noqa: BLE001
            result = Recorded("failed", REASONS["failed"])
        self.conv.trace("memory", op="create_event", ms=result.ms, state=result.state)
        if result.state != "failed":
            return
        self.conv.notice(WRITE_NOTICE, level="warn", once="memory-write")
        with self._lock:
            self.state = "unavailable"
            self.detail = result.detail
        self._emit()

    def flush(self, timeout: float = WRITER_DRAIN_S) -> bool:
        return self.writer is None or self.writer.flush(timeout)

    def close(self, timeout: float = 0.0) -> bool:
        return self.writer is None or self.writer.close(timeout)
