"""Every sentence the voice tools say, written here and nowhere else.

A voice agent reads a tool's ``speech`` field aloud, so the server writes it
rather than leaving a model to improvise one from a JSON blob. The rules are
KayLerch/alexa-skill-mcp-bridge's (Apache-2.0, read at ``ca2c2ef``, design
only, no code copied): ``packages/agent/prompts/voice.md`` -- "At most
{{maxSentences}} short sentences", "No markdown, lists, URLs, code, emoji, or
symbols", "Ask exactly one question at a time ... Put the question last",
"Name at most {{maxChoicesSpoken}} things and stop; offer to go on", "Never
read raw data or error text aloud" -- with ``bridge.config.ts`` setting both
numbers to three. Each rule has a test (``test_alexa_speech.py``) that runs
over every template here, and :func:`speech_problems` is the check.

The review wording is a port of the SPA's read-aloud,
``frontend/src/lib/voice.js`` ``reviewSpeech``/``findingSpeech``: a review
that never ran is spoken as not run, one that failed as nothing known, and
neither is ever spoken as zero findings -- an absent check, a broken one and a
clean one are three different claims. A clean review is scoped to what the
reviewer read, and over voice that is no datasheets at all, so it is spoken as
a first pass rather than a sign-off.

Speech never says KiCad checked a board: the hosted stack has no KiCad
(reviewer finding M5), so the check is "the design review" and nothing more.

Pure functions over plain values; standard library only.
"""

from __future__ import annotations

import datetime
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "CAUSE_WORDS",
    "MAX_SENTENCES",
    "REFUSALS",
    "STAGE_WORDS",
    "cause_key",
    "clause",
    "explain_speech",
    "headline",
    "recall_speech",
    "replay_speech",
    "sentences",
    "speakable_net",
    "speech_problems",
    "spoken",
    "start_speech",
    "status_speech",
    "when_phrase",
]

#: ``bridge.config.ts`` ``speech.maxSentences``.
MAX_SENTENCES = 3
#: ``bridge.config.ts`` ``speech.maxChoicesSpoken``: names spoken before
#: "and N more".
MAX_NAMED = 3
#: Characters a text-to-speech engine reads as a symbol or skips unevenly.
FORBIDDEN = frozenset("{}[]_%#*|<>")

#: What a stage is called out loud, for a failure and a headline.
STAGE_WORDS = {
    "reading": "planning the board",
    "proposing": "drafting the schematic",
    "placing": "placing the parts",
    "routing": "routing the copper",
    "reviewing": "reviewing the design",
    "restart": "this board before the service restarted",
}

#: Why a stage failed, chosen by the exception's **class** (see
#: :func:`cause_key`), never read from its message: the message is raw text
#: the rules say never to speak.
CAUSE_WORDS = {
    "model": "the AI model didn't answer",
    "validation": "the circuit it drafted didn't pass validation, even after a repair",
    "package": "it needs a part package I can't lay out yet",
    "expired": "its working copy expired",
    "cancelled": "it was cancelled",
    "restart": "the service restarted",
    "other": "something went wrong on my side",
}

#: Exception class names, most specific first, to the cause they mean. Names
#: rather than classes so this module imports nothing from the engine; the
#: whole MRO is searched, so a subclass (``AllProvidersFailed`` is a
#: ``ModelError``) finds its family.
_CAUSE_BY_CLASS = (
    ("NoProviderConfigured", "model"),
    ("ModelError", "model"),
    ("ProposalError", "validation"),
    ("PlanValidationError", "validation"),
    ("UnsupportedPackage", "package"),
    ("ValidationError", "validation"),
    ("StepNotFound", "expired"),
    ("RunCancelled", "cancelled"),
)

#: The speakable refusals (``isError`` results a person caused). The agent
#: reads these aloud as they are.
REFUSALS = {
    "busy": (
        "I'm still working on your last board. Ask me how it's going, and "
        "start the next one when it's finished."
    ),
    "capacity": "I'm at capacity right now. Please try again in a few minutes.",
    "not_found": "I can't find that board. Ask me to list your boards to find it.",
    "no_boards": (
        "You haven't started a board with me yet. Tell me what board you'd like."
    ),
    "still_planning": (
        "I don't have a question for you yet; I'm still planning. Ask me how "
        "it's going in a few seconds."
    ),
    "need_answer": (
        "I need an answer to my question first, or you can say you choose."
    ),
    "already_drafting": (
        "The schematic is already being drafted with your earlier answers."
    ),
    "broken": "Something went wrong on my side. Please try that again.",
}

_WORKING = frozenset({"reading", "proposing", "placing", "routing", "reviewing"})

_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten",
)

# -- normalisation ------------------------------------------------------------

_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
#: Unit spellings after a number, to their spoken words. Longest first, so
#: ``mV`` is not read as ``m`` plus ``V``.
_UNITS = (
    ("uF", "microfarad"), ("nF", "nanofarad"), ("pF", "picofarad"),
    ("uH", "microhenry"), ("nH", "nanohenry"),
    ("mA", "milliamp"), ("uA", "microamp"), ("mV", "millivolt"),
    ("kHz", "kilohertz"), ("MHz", "megahertz"), ("Hz", "hertz"),
    ("kohm", "kilohm"), ("ohm", "ohm"), ("mm", "millimetre"),
    ("V", "volt"), ("A", "amp"), ("W", "watt"),
)
_UNIT = re.compile(
    r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s?("
    + "|".join(re.escape(u) for u, _ in _UNITS)
    + r")(?![A-Za-z0-9])"
)
_UNIT_WORD = dict(_UNITS)
#: KiCad's rail spelling: ``3V3`` is 3.3 volt, ``1V8`` 1.8 volt.
_RAIL = re.compile(r"(?<![A-Za-z0-9.])(\d+)V(\d+)(?![A-Za-z0-9])")


def spoken(text: Any) -> str:
    """``text`` as it should be heard: units in words, no symbols, no URLs.

    Applied to every piece of model- or user-sourced text a template holds.
    """
    out = str(text or "")
    out = out.replace("µ", "u").replace("μ", "u")
    out = out.replace("kΩ", "kohm").replace("Ω", "ohm")
    out = _URL.sub("a link", out)
    out = _RAIL.sub(lambda m: f"{m.group(1)}.{m.group(2)} volt", out)
    out = _UNIT.sub(lambda m: f"{m.group(1)} {_UNIT_WORD[m.group(2)]}", out)
    out = out.replace("%", " percent")
    out = out.replace("_", " ")
    out = out.replace("&", " and ")
    out = re.sub(r"[{}\[\]#*|<>`~^\\]", "", out)
    return re.sub(r"\s+", " ", out).strip()


def speakable_net(name: str) -> str:
    """A net name read aloud: ``+3V3`` is "3.3 volt", ``USB_D-`` is
    "USB D minus", and KiCad's leading ``/`` sheet path is dropped."""
    net = str(name or "").strip().lstrip("/")
    tail = ""
    if net.endswith("+") and len(net) > 1:
        net, tail = net[:-1], " plus"
    elif net.endswith("-") and len(net) > 1:
        net, tail = net[:-1], " minus"
    net = re.sub(r"^\+(?=\d)", "", net)
    net = re.sub(r"^(\d+(?:\.\d+)?)V$", r"\1 volt", net)
    return spoken(net + tail)


def clause(text: Any) -> str:
    """``text`` as one clause inside a template sentence: :func:`spoken`,
    with its own sentence breaks folded to commas and no question mark, so a
    template's sentence count and its one question stay the template's."""
    out = spoken(text)
    out = re.sub(r"[.!?]+(\s+|$)", lambda m: ", " if m.group(1) else "", out)
    out = out.replace("?", "").replace("!", "")
    return out.strip(" ,;:")


def _mid(text: str) -> str:
    """``text`` placed mid-sentence: a capitalised first word is lowered, an
    acronym (``VOUT``, ``LED``) is left alone."""
    if len(text) > 1 and text[0].isupper() and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    return text if text[-1] in ".!?" else text + "."


def _question(text: str) -> str:
    return clause(text) + "?"


def sentences(text: str) -> list[str]:
    """``text`` split into sentences, the way the rules count them."""
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def speech_problems(text: str) -> list[str]:
    """Every voice rule ``text`` breaks; ``[]`` when it may be spoken."""
    problems: list[str] = []
    if not text.strip():
        problems.append("empty")
    count = len(sentences(text))
    if count > MAX_SENTENCES:
        problems.append(f"{count} sentences, more than {MAX_SENTENCES}")
    marks = text.count("?")
    if marks > 1:
        problems.append(f"{marks} questions, more than one")
    if marks == 1 and not text.rstrip().endswith("?"):
        problems.append("the question is not last")
    bad = sorted(set(text) & FORBIDDEN)
    if bad:
        problems.append(f"symbols {''.join(bad)}")
    if "://" in text or "www." in text.lower():
        problems.append("a URL")
    return problems


def count_words(n: int) -> str:
    """Small counts as words, the way people say them; larger ones as digits."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{count_words(n)} {word if n == 1 else (many or word + 's')}"


def join_and(items: Sequence[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _mm(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _percent(fraction: float, exact: bool) -> str:
    """Whole percent. Floored when anything is unrouted, so 99.6 percent with a
    net left is never spoken as a hundred."""
    value = fraction * 100
    return str(round(value)) if exact else str(int(value))


def cause_key(exc: BaseException) -> str:
    """The :data:`CAUSE_WORDS` key for an exception, by class alone."""
    names = [cls.__name__ for cls in type(exc).__mro__]
    for name, key in _CAUSE_BY_CLASS:
        if name in names:
            return key
    return "other"


def cause_text(class_name: str, cause: str | None) -> str:
    """``"<Class>: <cause in words>"``, what a remote agent is told about a
    failure in ``structuredContent``.

    The exception's message is not in it. That text can quote a provider's
    error, a URL with a key in its query or a model's answer, and the agent
    is told to prefer structured data over speech, so the rule ``tools.py``
    keeps for error results holds for data too. The full text stays in the
    board's row (``failure_reason``, and ``review.error`` in the summary) in
    the 0600 database, for whoever runs the server.
    """
    words = CAUSE_WORDS.get(str(cause or "other"), CAUSE_WORDS["other"])
    return f"{class_name}: {words}" if class_name else words


# -- status ---------------------------------------------------------------------

READING = (
    "I'm still reading your request and planning the board. "
    "Ask me again in a few seconds."
)
STALLED = (
    "This is taking much longer than usual, and it may be stuck. "
    "You can wait, or ask me to start it over."
)
START = "On it. I'm planning the board now; ask me how it's going in a few seconds."
SCRIPTED_LEAD = "Scripted mode, so you'll get the practice regulator board."


def start_speech(*, scripted: bool) -> str:
    return f"{SCRIPTED_LEAD} {START}" if scripted else START


def _unrouted_clause(unrouted: Sequence[Mapping[str, Any]]) -> str:
    names = [speakable_net(u.get("net", "")) for u in unrouted]
    shown = names[:MAX_NAMED]
    if len(names) > MAX_NAMED:
        listed = ", ".join(shown) + f" and {count_words(len(names) - MAX_NAMED)} more"
    else:
        listed = join_and(shown)
    return f"with {plural(len(names), 'net')} left unrouted: {listed}"


def _review_counts(findings: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = {"blocker": 0, "marginal": 0, "note": 0}
    for finding in findings:
        key = str(finding.get("severity", "")).lower()
        counts[key if key in counts else "note"] += 1
    return counts


def _done_sentences(summary: Mapping[str, Any]) -> list[str]:
    parts = summary.get("parts")
    fraction = summary.get("routed_fraction")
    unrouted = summary.get("unrouted") or []
    size = f"{plural(parts, 'part')}, " if isinstance(parts, int) else ""
    if fraction is None:
        first = f"Your board is done: {size}and routing was not measured."
    elif not unrouted:
        first = f"Your board is done: {size}fully routed."
    else:
        first = (
            f"Your board is done: {size}{_percent(fraction, False)} percent "
            f"routed, {_unrouted_clause(unrouted)}."
        )
    review = summary.get("review") or {}
    status = review.get("status")
    findings = summary.get("findings") or []
    ask = ""
    if status == "failed":
        second = (
            "The design review failed, so nothing is known about whether this "
            "board works."
        )
    elif status != "ok":
        second = (
            "The design review has not run, so nothing is known about whether "
            "this board works."
        )
    elif not findings:
        if summary.get("datasheets_read"):
            second = (
                "The design review found nothing to flag against the datasheets "
                "it read, which is not a sign-off."
            )
        else:
            second = (
                "The design review found nothing to flag, but it read no "
                "datasheets, so treat that as a first pass, not a sign-off."
            )
    else:
        counts = _review_counts(findings)
        words = {
            "blocker": lambda n: f"{count_words(n)} will not work",
            "marginal": lambda n: f"{count_words(n)} marginal",
            "note": lambda n: plural(n, "note"),
        }
        breakdown = ", ".join(
            words[key](counts[key]) for key in ("blocker", "marginal", "note")
            if counts[key]
        )
        second = (
            f"The design review found {plural(len(findings), 'finding')}: {breakdown}"
        )
        if counts["blocker"]:
            blocker = next(
                f for f in findings if str(f.get("severity")).lower() == "blocker"
            )
            second += f"; the first blocker: {clause(blocker.get('title'))}"
            ask = "Want me to explain the first blocker?"
        else:
            ask = "Want me to explain the first finding?"
        second += "."
    return [s for s in (first, second, ask) if s]


def status_speech(
    state: str,
    *,
    question: Mapping[str, Any] | None = None,
    total_questions: int = 0,
    answered: int = 0,
    acknowledged: bool = False,
    summary: Mapping[str, Any] | None = None,
    failure: Mapping[str, Any] | None = None,
    stalled: bool = False,
    queued: bool = False,
    you_chose: bool = False,
) -> str:
    """What to say about a board in ``state``; every argument is a value
    already in the store, so the words cannot say more than is known."""
    summary = summary or {}
    if stalled and state in _WORKING:
        return STALLED
    if state == "reading":
        if queued:
            return (
                "I'm still planning the board, and I'll place and route it as "
                "soon as the schematic is drafted. Ask me again in a few seconds."
            )
        return READING
    if state == "questions" and question is not None:
        default = clause(question.get("default"))
        ask = _question(str(question.get("ask", "")))
        if answered == 0:
            if total_questions <= 1:
                intro = "Before I draft it, one question."
            else:
                intro = (
                    f"Before I draft it, I have {count_words(total_questions)} "
                    "quick questions."
                )
            return f"{intro} If you don't say, I'll assume {default}. {ask}"
        lead = "Got it. Next, if" if acknowledged else "Next, if"
        return f"{lead} you don't say, I'll assume {default}. {ask}"
    if state == "proposing":
        drafting = "Drafting the schematic now"
        if queued:
            drafting += ", and I'll place and route it as soon as it's drafted"
        out = [f"{drafting}.", "Ask me how it's going in a little while."]
        if you_chose:
            out.insert(0, "Okay, I'll go with my defaults.")
        return " ".join(out)
    if state == "drafted":
        parts, nets = summary.get("parts"), summary.get("nets")
        if isinstance(parts, int) and isinstance(nets, int):
            head = (
                f"The schematic is drafted: {plural(parts, 'part')} on "
                f"{plural(nets, 'net')}."
            )
        else:
            head = "The schematic is drafted."
        return f"{head} Shall I place and route it?"
    if state == "placing":
        return "Placing the parts now. Ask me again in a moment."
    if state == "routing":
        size = summary.get("board_mm")
        if isinstance(size, (list, tuple)) and len(size) == 2:
            placed = (
                f"The parts are placed on a {_mm(size[0])} by {_mm(size[1])} "
                "millimetre board."
            )
        else:
            placed = "The parts are placed."
        return f"{placed} Routing the copper now."
    if state == "reviewing":
        fraction = summary.get("routed_fraction")
        if fraction is None:
            # Not measured is not the same claim as complete.
            routed = "Routing is done."
        elif not summary.get("unrouted"):
            routed = "Routing is done, with every net routed."
        else:
            routed = f"Routing is done, {_percent(fraction, False)} percent routed."
        return f"{routed} The design review is running now."
    if state == "done":
        return " ".join(_done_sentences(summary))
    if state == "failed":
        failure = failure or {}
        stage = str(failure.get("stage") or "")
        cause = CAUSE_WORDS.get(
            str(failure.get("cause") or "other"), CAUSE_WORDS["other"]
        )
        if stage == "restart":
            head = "I couldn't finish this board because the service restarted."
        else:
            words = STAGE_WORDS.get(stage, "this board")
            head = f"I couldn't finish {words}: {cause}."
        return f"{head} Nothing was ordered. Want me to start it over?"
    # A state this module does not know is said as what it is, not guessed.
    return f"The board is at {clause(state)}. Ask me again in a few seconds."


def replay_speech(current: str) -> str:
    """A retried start: say so, then the current state's opening sentence and,
    when it ends in one, its question."""
    parts = sentences(current)
    out = ["I already started that one.", parts[0] if parts else ""]
    if len(parts) > 1 and parts[-1].endswith("?"):
        out.append(parts[-1])
    return " ".join(s for s in out if s)


# -- one finding -------------------------------------------------------------------

_SEVERITY_WORDS = {
    "blocker": "a blocker, so it will not work as drawn",
    "marginal": "marginal",
    "note": "a note",
}


def explain_speech(
    finding: Mapping[str, Any] | None,
    *,
    number: int,
    count: int | None,
    review_status: str,
) -> str:
    """One finding in plain speech, ``findingSpeech``'s order: severity,
    title, detail, the parts, and the fix with "nothing is applied"."""
    if review_status == "failed":
        return (
            "The design review failed, so nothing is known about this board "
            "and I have nothing to explain."
        )
    if review_status != "ok":
        return (
            "The design review hasn't run on this board yet, so there's nothing "
            "to explain."
        )
    if finding is None or not count:
        return (
            "The design review found nothing to flag on this board, so there's "
            "nothing to explain."
        )
    severity = str(finding.get("severity", "")).lower()
    words = _SEVERITY_WORDS.get(severity, clause(severity) or "a note")
    out = [f"Finding {number} of {count} is {words}: {clause(finding.get('title'))}."]
    detail = sentences(spoken(finding.get("detail")))
    if detail:
        out.append(_sentence(clause(detail[0])))
    names = [clause(r) for r in (finding.get("refs") or finding.get("parts") or [])]
    fix = clause(finding.get("suggested_fix"))
    fix_words = (
        f"the suggested fix, which I haven't applied: {_mid(fix)}"
        if fix else ""
    )
    if names and fix_words:
        out.append(f"It involves {join_and(names)}; {fix_words}.")
    elif names:
        out.append(f"It involves {join_and(names)}.")
    elif fix_words:
        out.append(_sentence(fix_words[0].upper() + fix_words[1:]))
    return " ".join(out)


# -- history -------------------------------------------------------------------------


def when_phrase(then: float, now: float) -> str:
    """How long ago, in the server's local time zone -- a stated assumption,
    since a tool call carries no time zone."""
    day_then = datetime.date.fromtimestamp(then)
    day_now = datetime.date.fromtimestamp(now)
    days = (day_now - day_then).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days <= 6:
        return f"{count_words(days)} days ago"
    stamp = time.localtime(then)
    return f"on {time.strftime('%B', stamp)} {stamp.tm_mday}"


def _review_status(summary: Mapping[str, Any] | None) -> str:
    return str(((summary or {}).get("review") or {}).get("status") or "not_run")


def headline(
    state: str, summary: Mapping[str, Any] | None, failure_stage: str | None
) -> str:
    """One line per board for the history list; the structured field."""
    summary = summary or {}
    if state == "done":
        bits = []
        parts = summary.get("parts")
        if isinstance(parts, int):
            bits.append(f"{parts} part{'s' if parts != 1 else ''}")
        unrouted = summary.get("unrouted") or []
        fraction = summary.get("routed_fraction")
        if fraction is not None and not unrouted:
            bits.append("fully routed")
        elif fraction is not None:
            bits.append(f"{_percent(fraction, False)} percent routed")
            count = len(unrouted)
            bits.append(f"{count} net{'s' if count != 1 else ''} unrouted")
        status = _review_status(summary)
        if status == "ok":
            blockers = (summary.get("review") or {}).get("blockers") or 0
            bits.append(
                f"{blockers} blocker{'s' if blockers != 1 else ''}"
                if blockers else "nothing flagged"
            )
        elif status == "failed":
            bits.append("review not known")
        else:
            bits.append("review not run")
        return "done: " + ", ".join(bits)
    if state == "failed":
        if failure_stage == "restart":
            return "failed when the service restarted"
        return f"failed while {STAGE_WORDS.get(failure_stage or '', 'working')}"
    if state == "questions":
        return "waiting on a question"
    if state == "drafted":
        return "schematic drafted, waiting to place and route"
    return f"in progress: {STAGE_WORDS.get(state, state)}"


def _outcome_sentence(board: Mapping[str, Any]) -> str:
    state = board.get("state")
    summary = board.get("summary") or {}
    if state == "done":
        unrouted = summary.get("unrouted") or []
        fraction = summary.get("routed_fraction")
        if fraction is not None and not unrouted:
            routed = "and fully routed"
        elif fraction is not None:
            routed = (
                f"and {_percent(fraction, False)} percent routed with "
                f"{plural(len(unrouted), 'net')} unrouted"
            )
        else:
            # Not measured, which is not the same claim as unrouted.
            routed = "but its routing wasn't measured"
        status = _review_status(summary)
        if status == "ok":
            blockers = (summary.get("review") or {}).get("blockers") or 0
            review = (
                f"the review found {plural(blockers, 'blocker')}"
                if blockers else "the review flagged nothing"
            )
        elif status == "failed":
            review = "the review's result isn't known"
        else:
            review = "the review hasn't run"
        return f"It's done {routed}, and {review}."
    if state == "failed":
        if board.get("failure_stage") == "restart":
            return "It stopped when the service restarted."
        stage = STAGE_WORDS.get(str(board.get("failure_stage") or ""), "working on it")
        return f"It stopped while {stage}."
    if state == "questions":
        return "It's waiting on your answer to a question."
    if state == "drafted":
        return "Its schematic is drafted and waiting for you to say go."
    return f"It's still in progress, {STAGE_WORDS.get(str(state), 'working')}."


def _intent_words(intent: str, limit: int = 12) -> str:
    words = clause(intent).split()
    text = " ".join(words[:limit])
    return text + (" and so on" if len(words) > limit else "")


def recall_speech(
    boards: Sequence[Mapping[str, Any]],
    *,
    total: int,
    query: str | None,
    total_all: int,
    now: float,
) -> str:
    """The newest matching board, and an offer to go on when there are more."""
    if not boards:
        if query and total_all:
            return (
                f"I don't have a board about {clause(query)}. You have "
                f"{plural(total_all, 'other board')}; want to hear the latest?"
            )
        return "You don't have any boards with me yet."
    latest = boards[0]
    kind = "matching board" if query else "board"
    out = [
        f"Your latest {kind} is from {when_phrase(latest['created_at'], now)}: "
        f"{_intent_words(str(latest.get('intent', '')))}.",
        _outcome_sentence(latest),
    ]
    if total > 1:
        out.append(f"I have {count_words(total - 1)} more; want to hear them?")
    return " ".join(out)
