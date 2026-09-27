"""The voice agent behind the simulated Alexa+ page: a Strands agent whose
tools are Ada's six MCP tools, reached over Streamable HTTP.

The MCP client is the agent (reviewer finding M7), so everything the page
shows about a board passes through here. Strands Agents (strands-agents
1.57.1, Apache-2.0; called, nothing copied) supplies the loop, the Bedrock
model and the MCP client; this module supplies the hooks that make it a voice
host. Each is at a writable field Strands documents in ``hooks/events.py``:

``BeforeModelCallEvent.cancel``
    The process-wide model-call budget. When it is spent the cancel string is
    the final assistant message (``event_loop.py``), so a runaway loop ends in
    a sentence, not a bill.
``BeforeToolCallEvent.tool_use``
    The host mints ``request_id`` (M2's contract, reviewer finding M1): one
    UUID per (user turn, normalised intent), so a model retry inside one turn
    replays the same board and a new turn makes a new one. A missing
    ``session_id`` is filled from the conversation; a given one is kept.
``AfterToolCallEvent.result``
    The tool's ``structuredContent`` -- which Strands' ``MCPClient`` keeps on
    every result (``tools/mcp/mcp_client.py`` ``_handle_tool_result``) --
    becomes a card on the page. KayLerch's TypeScript bridge could not do this
    ("Strands' ``McpTool`` drops ``structuredContent``", its decision D21);
    the Python client keeps it. The model's own copy is rewritten to one
    compact JSON line (:func:`model_view`): KayLerch D28 measured Nova 2 Lite
    collapsing on a result that mixes JSON and text.
``AfterToolsEvent.end_turn``
    Every Ada tool returns a pre-written ``speech``; when one tool succeeded,
    that is the reply and the model is not called again. One model call per
    spoken turn.

**The host polls, not the model.** While a board works, :class:`HostPoller`
calls ``board_status`` as a Strands direct tool call with
``record_direct_tool_call=False`` -- no model call, no history -- on a second,
tool-only agent that shares the hooks (so a poll updates the card the same
way) but not the message list: a direct call runs the conversation manager
afterwards (``tools/_caller.py``), and that must not trim the history under a
turn that is running. Each change of state is narrated in one sentence; when
the board comes to rest its full speech is said, and the newest status goes
into the next user message as a bracketed host note (KayLerch's
``prompts/tool-hint.md`` precedent), so the model knows which question is
open.

Text the model writes itself -- never tool speech, which passes verbatim --
goes through :func:`guard`: KayLerch's ``cleanSpeech`` (``src/speech.ts``,
ported), the voice shape of :func:`alexabot.speech.speech_problems`, and three
honesty rules (no clean-review claim unless one ran, no "KiCad checked", no
ordering). Every replacement is a ``trace`` event.

**Memory** (:mod:`alexabot.memory`): each conversation reads the person's
remembered design preferences once when it opens, and while a new board could
start, a turn carries a bracketed ``[Memory: ...]`` note with a host-written
question; only a yes puts them into the intent, and the person's own words
win over a note. One write per spoken turn, of the person's words only, on a
writer thread. A fourth guard rule, ``memory_claim``, replaces model text that
claims a memory the host did not hand it.

Strands is imported inside the functions that need it: without the ``alexa``
extra, ``import alexabot.agent`` still works and the sim refuses in words.
"""

from __future__ import annotations

import datetime
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cards, speech
from . import memory as memory_mod

__all__ = [
    "ADA_TOOLS",
    "BUDGET_SPEECH",
    "DEFAULT_MODEL_ID",
    "DEFAULT_REGION",
    "STUCK_SPEECH",
    "Budget",
    "HostPoller",
    "PromptError",
    "VoiceAgentFactory",
    "VoiceSession",
    "clean_speech",
    "guard",
    "model_view",
    "norm_intent",
    "render_prompt",
    "shape_speech",
    "system_prompt",
    "tool_list",
    "turn_prompt",
]

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
#: Explicit, because Strands' own fallback is us-west-2 (``models/bedrock.py``).
DEFAULT_REGION = "us-east-1"
#: KayLerch ``HISTORY_WINDOW``.
HISTORY_WINDOW = 40
#: Model calls per spoken turn before it is spoken as "stuck".
TURN_LIMIT = 4
MODEL_VIEW_MAX = 1500
HOST_POLL_MIN_S = 1.0
#: A stated deviation from ``poll_after_s`` (5/10/3/3/5): ``board_status`` is
#: one SQLite read, and a host in the loop may read sooner than a person asks.
HOST_POLL_CAP_S = 3.0
HOST_POLL_MAX_S = 20 * 60
POLL_ERRORS_MAX = 3
MAX_TOKENS = 400

ADA_TOOLS = frozenset({"start_board_design", "answer_design_questions",
                       "continue_design", "board_status", "explain_finding",
                       "recall_my_boards"})
STATUS_TOOLS = frozenset({"start_board_design", "answer_design_questions",
                          "continue_design", "board_status"})
_SESSION_TOOLS = frozenset({"answer_design_questions", "continue_design",
                            "explain_finding"})
WORKING = frozenset({"reading", "proposing", "placing", "routing", "reviewing"})
RESTING = frozenset({"questions", "drafted", "done", "failed"})

BUDGET_SPEECH = (
    "I've reached this demo's limit on model calls, so I'll stop here. Your "
    "boards are still saved."
)
STUCK_SPEECH = "Sorry, I got stuck on that one. Could you say it another way?"
#: KayLerch ``packages/core/src/messages.ts`` ``SPOKEN.error`` and
#: ``SPOKEN.notUnderstood``.
SPOKEN_ERROR = "Sorry, something went wrong on my side. Please try again in a moment."
NOT_UNDERSTOOD = "Sorry, I didn't catch that. What would you like to do?"
LOST_TRACK = "I lost track of the board. Ask me how it's going."
STOP_CHECKING = "I'll stop checking now. Ask me how it's going any time."
TOOL_UNREACHABLE = "The tool could not be reached."
HOST_ASSIGNED = "host-assigned"

# -- prompts --------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{([\w-]+)\}\}")


class PromptError(KeyError):
    """A ``{{placeholder}}`` had no value: typos fail loudly (KayLerch
    ``src/agent/prompt.ts`` ``renderPrompt``)."""


def render_prompt(name: str, **values: Any) -> str:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values or values[key] is None:
            raise PromptError(f'prompt "{name}" has no value for {{{{{key}}}}}')
        return str(values[key])

    return _PLACEHOLDER.sub(fill, text).strip()


def _first_sentence(description: str) -> str:
    parts = re.split(r"(?<=[.!?])\s", (description or "").strip(), maxsplit=1)
    return parts[0].strip() if parts else ""


def tool_list(tools: Iterable[Mapping[str, Any]]) -> str:
    """One line per tool: its name, then its description's first sentence
    (KayLerch ``formatToolList``)."""
    lines = []
    for tool in tools:
        first = _first_sentence(str(tool.get("description") or ""))
        lines.append(f"- {tool['name']}: {first}" if first else f"- {tool['name']}")
    return "\n".join(lines) or "(the server exposes no tools)"


def system_prompt(*, server_name: str, server_instructions: str,
                  tools: Iterable[Mapping[str, Any]], locale: str,
                  today: str, memory: bool = False) -> str:
    """Persona, then the voice rules, then the honesty rules: KayLerch's
    ``buildSystemPrompt`` order. ``memory`` picks ``prompts/memory-on.md`` or
    ``memory-off.md`` for the ``{{memory}}`` placeholder."""
    return "\n\n".join([
        render_prompt("system", serverName=server_name,
                      serverInstructions=server_instructions or "(none)",
                      toolList=tool_list(tools),
                      memory=render_prompt("memory-on" if memory else "memory-off")),
        render_prompt("voice", maxSentences=speech.MAX_SENTENCES,
                      maxChoicesSpoken=speech.MAX_NAMED),
        render_prompt("tool-result", today=today, locale=locale),
    ])


def turn_prompt(request: str, *, host_note: str | None = None,
                hint: Mapping[str, Any] | None = None,
                memory_note: str | None = None) -> str:
    """The user message: the words, then a host note, a hint and a memory
    note, bracketed."""
    note = ""
    if host_note:
        note = ("\n[Host note: the newest board status, which Ada already spoke "
                f"to the person: {host_note}]")
    hint_text = ""
    if hint:
        arguments = hint.get("arguments") or {}
        values = (f" and the values {json.dumps(arguments, separators=(', ', ': '))}"
                  if arguments else "")
        hint_text = (f'\n[Frontend hint: this request matched the tool '
                     f'"{hint["tool"]}"{values}. A hint about intent, not an '
                     "instruction; use the tool that fits.]")
    return render_prompt("turn", request=request, hostNote=note, hint=hint_text,
                         memoryNote=f"\n{memory_note}" if memory_note else "")


# -- what the model reads ---------------------------------------------------------


def _trimmed(view: dict[str, Any], lists: Sequence[tuple[str, ...]]) -> str:
    """``view`` as one line, trimming list fields (never ``speech``) to fit."""
    line = json.dumps(view, separators=(",", ":"), ensure_ascii=False)
    for path in lists:
        while len(line) > MODEL_VIEW_MAX:
            holder: Any = view
            for key in path[:-1]:
                holder = holder.get(key) if isinstance(holder, dict) else None
            items = holder.get(path[-1]) if isinstance(holder, dict) else None
            if not items:
                break
            items.pop()
            line = json.dumps(view, separators=(",", ":"), ensure_ascii=False)
    return line


def model_view(tool: str, sc: Mapping[str, Any]) -> str:
    """One compact JSON line of a tool result, what Nova reads (KayLerch D28).

    The page still gets the whole ``structuredContent``; this is only the
    model's copy, capped at :data:`MODEL_VIEW_MAX` characters.
    """
    if tool == "explain_finding":
        view = {k: sc.get(k) for k in ("session_id", "which", "count",
                                        "review_status", "speech")}
        return _trimmed(view, [])
    if tool == "recall_my_boards":
        view = {
            "total": sc.get("total"),
            "boards": [{k: b.get(k) for k in ("session_id", "intent", "state",
                                              "headline")}
                       for b in (sc.get("boards") or [])[:5]],
            "speech": sc.get("speech"),
        }
        return _trimmed(view, [("boards",)])
    question = sc.get("question")
    summary = sc.get("summary")
    compact_summary = None
    if isinstance(summary, Mapping):
        unrouted = summary.get("unrouted")
        fraction = summary.get("routed_fraction")
        review = summary.get("review") or {}
        compact_summary = {
            "parts": summary.get("parts"),
            "nets": summary.get("nets"),
            "routed_percent": (None if fraction is None
                               else round(float(fraction) * 100, 1)),
            "unrouted_nets": (None if unrouted is None
                              else [u.get("net") for u in unrouted[:5]]),
            "unrouted_total": None if unrouted is None else len(unrouted),
            "review": review.get("status"),
        }
        # A review that did not run (or failed) has no findings to count: an
        # empty list here reads as "zero findings", the claim the guard
        # forbids, so the counts are left out rather than shown as nothing.
        if review.get("status") == "ok":
            compact_summary["blockers"] = review.get("blockers")
            compact_summary["findings"] = [
                {k: f.get(k) for k in ("number", "severity", "title")}
                for f in (summary.get("findings") or [])[:5]]
    view = {
        "session_id": sc.get("session_id"),
        "state": sc.get("state"),
        "speech": sc.get("speech"),
        "stalled": sc.get("stalled"),
        "scripted": sc.get("scripted"),
        "question": ({k: question.get(k) for k in ("index", "ask", "default",
                                                    "remaining")}
                     if isinstance(question, Mapping) else None),
        "answered": [a.get("index") for a in sc.get("answers") or []
                     if a.get("source") == "user"],
        "summary": compact_summary,
    }
    return _trimmed(view, [("summary", "findings"), ("summary", "unrouted_nets"),
                           ("answered",)])


# -- voice guards on model-written text ---------------------------------------------

# Extended_Pictographic, approximated: Python's ``re`` has no \p{...}. The
# ranges are the emoji blocks and the dingbat/symbol blocks they live in.
_PICTOGRAPHS = re.compile(
    "[©®‼⁉™ℹ↔-↙↩↪⌚⌛"
    "⌨⏏⏩-⏳⏸-⏺Ⓜ▪▫▶◀"
    "◻-◾☀-➿⤴⤵⬅-⬇⬛⬜⭐⭕"
    "〰〽㊗㊙️‍\U0001f000-\U0001faff]"
)


def clean_speech(text: str) -> str:
    """Model text as text-to-speech plain text: a port of KayLerch
    ``packages/agent/src/speech.ts`` ``cleanSpeech`` (Apache-2.0, ``ca2c2ef``),
    rule for rule; the one approximation is the pictograph class above."""
    t = str(text or "")
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t, flags=re.IGNORECASE)
    t = re.sub(r"^\s{0,3}#{1,6}\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"(\*\*|__)(.*?)\1", r"\2", t)
    t = re.sub(r"(?<!\w)(\*|_)(\S.*?\S|\S)\1(?!\w)", r"\2", t)
    t = _PICTOGRAPHS.sub("", t)
    t = re.sub(r"\s*\n+\s*", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def shape_speech(text: str) -> str:
    """At most three sentences, and at most one question, last."""
    text = " ".join(speech.sentences(text)[: speech.MAX_SENTENCES]).strip()
    mark = text.find("?")
    if mark >= 0:
        text = text[: mark + 1]
    return text or NOT_UNDERSTOOD


#: A claim that the design review found nothing. Allowed only when the newest
#: status says the review ran and found nothing: a review that has not run is
#: "not run", and a failed one means nothing is known (M2's speech rule).
#: Widened after the M3 verification pass, whose phrasings ("found zero
#: findings", "didn't find anything", "passed the review", "came back clean",
#: "checks out", "no errors") all passed the first version of this pattern.
_PROBLEM_WORDS = (r"(?:problems?|issues?|findings?|errors?|faults?|concerns?"
                  r"|warnings?|violations?|mistakes?)")
_CLEAN_CLAIM = re.compile(
    rf"\b(?:no|zero|0)\s+(?:\w+\s+)?{_PROBLEM_WORDS}\b"
    rf"|\bwithout\s+(?:any\s+)?(?:\w+\s+)?{_PROBLEM_WORDS}\b"
    r"|\b(?:did\s*n't|did\s+not|does\s*n't|does\s+not|has\s*n't|has\s+not)\s+"
    r"(?:find|flag|catch|spot|raise|turn\s+up)\s+"
    r"(?:anything|any\b|a\s+(?:single\s+)?(?:thing|problem|issue))"
    r"|\b(?:found|flagged|caught|spotted|raised)\s+nothing\b"
    r"|\bnothing\s+(?:was\s+|is\s+|got\s+|has\s+been\s+)?"
    r"(?:wrong|flagged|found|to\s+flag|to\s+fix|to\s+worry\s+about|amiss)\b"
    r"|\bpass(?:ed|es)?\s+(?:the\s+|its\s+|all\s+(?:the\s+)?|every\s+|each\s+)?"
    r"(?:\w+\s+)?(?:review|checks?|inspection|tests?)\b"
    r"|\b(?:review|checks?|inspection)\s+(?:\w+\s+)?passed\b"
    r"|\b(?:came|comes|come|coming)\s+(?:back|out|through)\s+clean\b"
    r"|\bclean\s+(?:review|bill|result|report)\b"
    r"|\bchecks?\s+out\b"
    r"|\blooks\s+(?:good|clean|fine|great|perfect|solid|correct)\b"
    r"|\b(?:board|pcb|design|circuit|schematic|layout|everything)"
    r"(?:\s+(?:is|looks|seems|was)|'s)\s+(?:\w+\s+)?"
    r"(?:fine|good|perfect|great|ok|okay|healthy|solid|correct|clean)\b"
    r"|\ball\s+(?:good|clear)\b"
    r"|\b(?:error|problem|issue|fault)[- ]free\b",
    re.IGNORECASE,
)
#: "No blockers" is true of a review that ran and found only notes, so it has
#: its own test: allowed only when the review ran with zero blockers.
_NO_BLOCKERS = re.compile(
    r"\b(?:no|zero|0)\s+(?:\w+\s+)?blockers?\b|\bnothing\s+(?:is\s+)?blocking\b"
    r"|\bno\s+blocking\b",
    re.IGNORECASE,
)
_KICAD_CLAIM = re.compile(r"\bkicad\b", re.IGNORECASE)
#: What turns a mention of KiCad into a verdict. "Open it in KiCad" is fine;
#: "KiCad says it's fine" is the claim M5 forbids.
_CHECK_WORDS = re.compile(
    r"\bcheck|\bverif|\bdrc\b|\berc\b|\bsays?\b|\bsaid\b|\breport|\bpass"
    r"|\bfine\b|\bclean\b|\bhappy\b|\bapprov|\bok\b|\bokay\b|\bvalid|\bconfirm"
    r"|\berrors?\b|\bviolations?\b|\bwarnings?\b|\bgood\b",
    re.IGNORECASE,
)
_ORDER_CLAIM = re.compile(
    r"\b(?:ordered|purchased|shipped)\b|\bon its way\b"
    r"|\b(?:placed|put\s+in|submitted|sent\s+in|made)\s+(?:an?\s+|the\s+|your\s+)?"
    r"(?:order|purchase)\b"
    r"|\b(?:i|we|ada|she)(?:'ll|'ve|\s+will|\s+can|\s+could|\s+have|\s+has)?\s+"
    r"(?:go\s+ahead\s+and\s+)?(?:order|buy|purchase|ship)s?\b"
    r"|\bbeing\s+(?:fabricated|manufactured|made|produced)\b"
    r"|\bsent\b.{0,30}?\b(?:fab|fabricat\w*|manufactur\w*|factory|jlc\w*|pcbway)\b",
    re.IGNORECASE,
)
#: A negation earlier in the same sentence ("Nothing was ordered", "I haven't
#: placed an order") makes an order phrase the honest statement.
_NEGATION = re.compile(r"\b(?:not|never|nothing|no)\b|n't\b", re.IGNORECASE)
GUARD_FALLBACK = {
    "review_not_run_claim": (
        "I can only tell you what the design review found once it has run. "
        "Ask me how the board is going."
    ),
    "kicad_claim": (
        "The check on this board is Ada's design review. Ask me how the board "
        "is going."
    ),
    "order_claim": "Ada drafts and checks boards, and she never orders anything.",
    "memory_claim": memory_mod.MEMORY_CLAIM_FALLBACK,
}


def _review_of(status: Mapping[str, Any] | None) -> Mapping[str, Any]:
    summary = (status or {}).get("summary") or {}
    return summary.get("review") or {}


def _clean_review_ran(status: Mapping[str, Any] | None) -> bool:
    review = _review_of(status)
    return review.get("status") == "ok" and review.get("findings") == 0


def _no_blockers_known(status: Mapping[str, Any] | None) -> bool:
    review = _review_of(status)
    return review.get("status") == "ok" and (
        review.get("blockers") == 0 or review.get("findings") == 0)


def _asserted(pattern: re.Pattern[str], text: str) -> bool:
    """``pattern`` matches in a sentence with no negation before the match."""
    for match in pattern.finditer(text):
        start = max(text.rfind(mark, 0, match.start()) for mark in ".!?;")
        if not _NEGATION.search(text[start + 1:match.start()]):
            return True
    return False


#: "No problem!" / "Sure, no problem, ..." answers a request; it says nothing
#: about the board, so it is dropped before the clean-claim test.
_INTERJECTION = re.compile(
    r"(?:^|(?<=[.!?;]))\s*(?:(?:sure|ok|okay|yes|yeah|yep|alright|of\s+course)\s*[,!.]?\s+)?"
    r"no\s+(?:problem|worries)\b\s*[,!.]?",
    re.IGNORECASE,
)
#: "Open it in KiCad to check the layout yourself" hands the check to the
#: person; only a verdict attributed to KiCad is the claim.
_CHECK_INSTRUCTION = re.compile(
    r"\b(?:to|can|could|should|may|might)\s+(?:double[- ]?)?"
    r"(?:check|verify|inspect|review)\b"
    r"|\b(?:check|verify|inspect)\s+(?:it|the\s+\w+)\s+yourself\b",
    re.IGNORECASE,
)


def honesty_problem(text: str, latest_status: Mapping[str, Any] | None) -> str | None:
    claims = _INTERJECTION.sub(" ", text)
    if _CLEAN_CLAIM.search(claims) and not _clean_review_ran(latest_status):
        return "review_not_run_claim"
    if _NO_BLOCKERS.search(claims) and not _no_blockers_known(latest_status):
        return "review_not_run_claim"
    verdicts = _CHECK_INSTRUCTION.sub(" ", text)
    if _KICAD_CLAIM.search(text) and _CHECK_WORDS.search(verdicts):
        return "kicad_claim"
    if _asserted(_ORDER_CLAIM, text):
        return "order_claim"
    return None


def guard(text: str, latest_status: Mapping[str, Any] | None,
          last_speech: str | None,
          memory: memory_mod.GuardView | None = None) -> tuple[str, str | None]:
    """Model-written ``text`` as it may be spoken, and the rule that replaced
    it (``None`` when it passed).

    ``memory_claim`` runs after the three honesty rules: a sentence that
    claims to remember ("last time you wanted ...") is kept only when this
    turn carries a memory note and the sentence names something in it;
    otherwise it becomes the ask, "I'll go by what you just asked for.", "I
    don't have any preferences saved", or the memory-off sentence.
    """
    shaped = shape_speech(clean_speech(text))
    rule = honesty_problem(shaped, latest_status)
    if rule is not None:
        return (last_speech or GUARD_FALLBACK[rule]), rule
    view = memory or memory_mod.GuardView(state="off")
    if memory_mod.claims_memory(shaped, view.note):
        return memory_mod.claim_replacement(view), "memory_claim"
    return shaped, None


# -- budget and request ids ---------------------------------------------------------


class Budget:
    """Process-wide caps on Bedrock, Polly and AgentCore Memory calls, taken
    before each call."""

    def __init__(self, max_model_calls: int, max_polly_calls: int,
                 max_memory_calls: int = memory_mod.DEFAULT_MAX_CALLS) -> None:
        self.max_model = max(0, int(max_model_calls))
        self.max_polly = max(0, int(max_polly_calls))
        self.max_memory = max(0, int(max_memory_calls))
        self.model_used = 0
        self.polly_used = 0
        self.memory_used = 0
        self._lock = threading.Lock()

    def take_model(self) -> bool:
        with self._lock:
            if self.model_used >= self.max_model:
                return False
            self.model_used += 1
            return True

    def take_polly(self) -> bool:
        with self._lock:
            if self.polly_used >= self.max_polly:
                return False
            self.polly_used += 1
            return True

    def take_memory(self) -> bool:
        with self._lock:
            if self.memory_used >= self.max_memory:
                return False
            self.memory_used += 1
            return True

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {"model_calls": {"used": self.model_used, "max": self.max_model},
                    "polly_calls": {"used": self.polly_used, "max": self.max_polly},
                    "memory_calls": {"used": self.memory_used,
                                     "max": self.max_memory}}


def norm_intent(text: str) -> str:
    """M2's same-intent rule: strip, collapse whitespace, casefold."""
    return " ".join(str(text or "").split()).casefold()


@dataclass
class TurnState:
    turn_id: str
    request_ids: dict[str, str] = field(default_factory=dict)
    speeches: dict[str, str] = field(default_factory=dict)
    ended_by_tool: bool = False
    budget_spent: bool = False
    last_speech: str | None = None
    model_calls: int = 0
    model_t0: float = 0.0
    #: The model's tool calls this turn, name and input (memory's skip rule).
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _bounded(arguments: Any) -> Any:
    text = json.dumps(arguments, default=str)
    if len(text) <= 1024:
        return arguments
    return {"truncated": text[:1000]}


def _first_sentence_of(text: str) -> str:
    parts = speech.sentences(text)
    return parts[0] if parts else str(text or "")


# -- the per-conversation session ---------------------------------------------------


class VoiceHooks:
    """The Strands hooks for one conversation (a ``HookProvider``)."""

    def __init__(self, session: VoiceSession) -> None:
        self.session = session

    def register_hooks(self, registry: Any, **_: Any) -> None:
        from strands.hooks import (
            AfterModelCallEvent,
            AfterToolCallEvent,
            AfterToolsEvent,
            BeforeModelCallEvent,
            BeforeToolCallEvent,
        )

        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(AfterModelCallEvent, self.after_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(AfterToolCallEvent, self.after_tool)
        registry.add_callback(AfterToolsEvent, self.after_tools)

    # model

    def before_model(self, event: Any) -> None:
        s = self.session
        turn = s.turn
        if turn is not None:
            turn.model_t0 = time.monotonic()
        if s.factory.metered and not s.factory.budget.take_model():
            if turn is not None:
                turn.budget_spent = True
            event.cancel = BUDGET_SPEECH
            s.conv.trace("budget", **s.factory.budget.snapshot())

    def after_model(self, event: Any) -> None:
        s = self.session
        turn = s.turn
        if turn is None:
            return
        turn.model_calls += 1
        fields: dict[str, Any] = {
            "turn_id": turn.turn_id, "n": turn.model_calls,
            "model_id": s.factory.model_id,
            "ms": round((time.monotonic() - turn.model_t0) * 1000),
        }
        exc = getattr(event, "exception", None)
        if exc is not None:
            fields["error"] = type(exc).__name__
        s.conv.trace("model", **fields)
        if s.factory.metered:
            s.conv.trace("budget", **s.factory.budget.snapshot())

    # tools

    def _origin(self, event: Any) -> str:
        return "host-poll" if event.agent is self.session.poll_agent else "model"

    def before_tool(self, event: Any) -> None:
        s = self.session
        tool_use = event.tool_use
        name = str(tool_use.get("name"))
        arguments = tool_use.get("input")
        if not isinstance(arguments, dict):
            arguments = {}
        if self._origin(event) == "host-poll":
            return
        turn = s.turn
        if turn is not None:
            turn.tool_calls.append({"name": name, "input": dict(arguments)})
        try:
            if name == "start_board_design":
                if turn is None:
                    raise RuntimeError("no turn")
                key = norm_intent(str(arguments.get("intent") or ""))
                arguments["request_id"] = turn.request_ids.setdefault(
                    key, str(uuid.uuid4()))
            elif (name in _SESSION_TOOLS or name == "board_status") \
                    and not arguments.get("session_id") and s.conv.current_session_id:
                arguments["session_id"] = s.conv.current_session_id
            tool_use["input"] = arguments
            event.tool_use = tool_use
        except Exception:  # noqa: BLE001 -- never send the model's own id
            event.cancel_tool = "Something went wrong on my side."

    def after_tool(self, event: Any) -> None:
        s = self.session
        conv = s.conv
        origin = self._origin(event)
        name = str(event.tool_use.get("name"))
        result = event.result
        sc = result.get("structuredContent") if isinstance(result, dict) else None
        is_error = result.get("status") == "error" if isinstance(result, dict) else True
        turn = s.turn if origin == "model" else None
        conv.trace(
            "tool", turn_id=turn.turn_id if turn else None, origin=origin, tool=name,
            arguments=_bounded(event.tool_use.get("input")), is_error=is_error,
            ms=None if event.duration is None else round(event.duration * 1000),
            **({"error": type(event.exception).__name__}
               if getattr(event, "exception", None) is not None else {}),
        )
        if is_error or not isinstance(sc, Mapping):
            # An Ada refusal is one clean sentence; a Strands-side failure
            # ("Tool execution failed: <exception text>") is not, and the
            # model must never read exception text aloud.
            texts = [c.get("text", "") for c in (result.get("content") or [])
                     if isinstance(c, dict)] if isinstance(result, dict) else []
            if not texts or any(t.startswith("Tool execution") for t in texts):
                result["content"] = [{"text": TOOL_UNREACHABLE}]
            return
        s.absorb(name, dict(sc), origin=origin)
        speech_text = sc.get("speech")
        if turn is not None and isinstance(speech_text, str) and speech_text:
            turn.speeches[str(event.tool_use.get("toolUseId"))] = speech_text
            turn.last_speech = speech_text
        result["content"] = [{"text": model_view(name, sc)}]

    def after_tools(self, event: Any) -> None:
        turn = self.session.turn
        if turn is None:
            return
        results = [b.get("toolResult") for b in event.message.get("content") or []
                   if isinstance(b, dict) and b.get("toolResult")]
        if len(results) != 1:
            return
        only = results[0]
        spoken = turn.speeches.get(str(only.get("toolUseId")))
        if only.get("status") != "error" and spoken:
            event.end_turn = spoken
            turn.ended_by_tool = True


class VoiceSession:
    """One conversation's agent, its tool-only poll agent, and its poller."""

    def __init__(self, factory: VoiceAgentFactory, conv: Any) -> None:
        self.factory = factory
        self.conv = conv
        self.turn: TurnState | None = None
        self.hooks = VoiceHooks(self)
        # Memory's recall starts in :meth:`opened`, right after the greeting,
        # and runs while it is said; the first turn waits for it at most
        # ``recall_wait_s``.
        self.memory = memory_mod.ConversationMemory(factory.memory, factory.actor,
                                                    conv)
        self.agent = factory.build_agent(self.hooks, conv)
        self.poll_agent = factory.build_poll_agent(self.hooks)
        self.poller = HostPoller(self, min_s=factory.poll_min_s,
                                 cap_s=factory.poll_cap_s, max_s=factory.poll_max_s)

    def opened(self) -> None:
        """The greeting is logged: read the person's remembered preferences."""
        self.memory.start()

    # what a tool result does to the conversation

    def absorb(self, tool: str, sc: dict[str, Any], *, origin: str = "model") -> None:
        """Put a tool result on the screen.

        A card is logged only when it changed. When the model called the tool
        -- the person asked for this -- and its card had not changed, a
        ``focus`` event brings that card back instead: "back to the board"
        after a finding answers with the same board card, and the screen must
        follow what Ada just said about it. A host poll keeps the plain
        dedupe; an unchanged poll says nothing new.
        """
        conv = self.conv
        if tool in STATUS_TOOLS and sc.get("session_id"):
            sid = str(sc["session_id"])
            with conv.lock:
                conv.latest_status = sc
                conv.current_session_id = sid
                highlight = conv.highlights.get(sid, [])
            card = cards.from_tool(tool, sc, images=self.factory.images,
                                   highlight_refs=highlight)
        elif tool == "explain_finding" and sc.get("session_id"):
            sid = str(sc["session_id"])
            card = cards.from_tool(tool, sc, images=self.factory.images)
            refs = list((card or {}).get("highlightRefs") or [])
            with conv.lock:
                conv.highlights[sid] = refs
                conv.current_session_id = sid
                board = conv.cards.get(f"board:{sid}")
            if board is not None:
                conv.upsert_card(cards.highlighted(board, refs))
        else:
            card = cards.from_tool(tool, sc, images=self.factory.images)
        if conv.upsert_card(card) is None and card and origin == "model":
            conv.focus_card(card["id"])

    # one spoken turn

    def run_turn(self, turn_id: str, text: str, hint: Mapping[str, Any] | None) -> None:
        conv = self.conv
        turn = TurnState(turn_id)
        self.turn = turn
        meta = conv.take_turn_meta(turn_id) if hasattr(conv, "take_turn_meta") else {}
        mem = self.memory
        mem.wait_ready(self.factory.recall_wait_s)
        current = conv.latest_status
        board_open = bool(current) and current.get("state") not in ("done", "failed")
        note = mem.note_for(text, board_open=board_open)
        prompt = turn_prompt(text, host_note=conv.take_host_note(), hint=hint,
                             memory_note=note)
        try:
            result = self.agent(prompt, limits={"turns": self.factory.turn_limit})
        except Exception as exc:  # noqa: BLE001 -- the class name only, ever
            conv.trace("turn", turn_id=turn_id, error=type(exc).__name__)
            conv.error(SPOKEN_ERROR, turn_id=turn_id)
            return
        finally:
            self.turn = None
        stop = str(getattr(result, "stop_reason", ""))
        reply = " ".join(
            str(block.get("text", "")) for block in
            (result.message or {}).get("content") or [] if isinstance(block, dict)
            and block.get("text")
        ).strip()
        if stop == "limit_turns":
            said = STUCK_SPEECH
            conv.say(said, origin="host", turn_id=turn_id)
        elif turn.budget_spent:
            said = BUDGET_SPEECH
            conv.say(said, origin="host", turn_id=turn_id)
        elif turn.ended_by_tool:
            said = reply
            conv.say(said, origin="tool", turn_id=turn_id)
        else:
            said, rule = guard(reply, conv.latest_status, turn.last_speech,
                               mem.guard_view())
            if rule is not None:
                conv.trace("guard", turn_id=turn_id, rule=rule, replaced=True)
            conv.say(said, origin="model", turn_id=turn_id)
        mem.after_turn(said, turn.tool_calls)
        mem.record(memory_mod.turn_record(
            conv.id, turn_id, text, question=meta.get("question"),
            source=meta.get("source"), tool_calls=turn.tool_calls))
        status = conv.latest_status
        if status and status.get("poll_after_s") is not None \
                and status.get("state") in WORKING:
            self.poller.ensure(str(status["session_id"]), status)

    def close(self) -> None:
        self.poller.stop()
        self.memory.close()


class HostPoller:
    """Checks a working board every few seconds through the poll agent."""

    def __init__(self, session: VoiceSession, *, min_s: float, cap_s: float,
                 max_s: float) -> None:
        self.session = session
        self.min_s = min_s
        self.cap_s = cap_s
        self.max_s = max_s
        self._lock = threading.Lock()
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._sid: str | None = None

    def _interval(self, status: Mapping[str, Any]) -> float:
        after = status.get("poll_after_s")
        after = self.cap_s if after is None else float(after)
        return max(self.min_s, min(after, self.cap_s))

    def ensure(self, session_id: str, status: Mapping[str, Any]) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive() \
                    and self._sid == session_id:
                return
            if self._stop is not None:
                self._stop.set()
            stop = threading.Event()
            self._stop = stop
            self._sid = session_id
            self._thread = threading.Thread(
                target=self._run, args=(session_id, dict(status), stop),
                name=f"alexa-sim-poll-{session_id}", daemon=True)
            self._thread.start()

    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        with self._lock:
            if self._stop is not None:
                self._stop.set()

    def join(self, timeout: float) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self, sid: str, status: dict[str, Any], stop: threading.Event) -> None:
        conv = self.session.conv
        last = (status.get("state"), bool(status.get("stalled")))
        # The first check comes at the floor, not at poll_after_s: a call that
        # just changed the state often finishes its fast stages (placing,
        # routing) within a second, and waiting the full interval would skip
        # the narration of them.
        interval = self.min_s
        started = time.monotonic()
        errors = 0
        announced = False
        try:
            while not stop.wait(interval):
                if not announced:
                    announced = True
                    conv.status("checking")
                try:
                    result = self.session.poll_agent.tool.board_status(
                        session_id=sid, record_direct_tool_call=False)
                    sc = result.get("structuredContent")
                    if result.get("status") == "error" or not isinstance(sc, Mapping):
                        raise RuntimeError("board_status answered an error")
                except Exception as exc:  # noqa: BLE001 -- class names only
                    errors += 1
                    conv.trace("poll", session_id=sid, error=type(exc).__name__)
                    if errors >= POLL_ERRORS_MAX:
                        conv.say(LOST_TRACK, origin="host")
                        return
                    interval = self._interval(status)
                    continue
                errors = 0
                if stop.is_set():
                    return
                state = sc.get("state")
                key = (state, bool(sc.get("stalled")))
                if key != last:
                    last = key
                    if state in WORKING:
                        conv.progress(_first_sentence_of(str(sc.get("speech") or "")),
                                      state=str(state), session_id=sid)
                    else:
                        conv.say(str(sc.get("speech") or ""), origin="host")
                        conv.set_host_note(model_view("board_status", sc))
                        return
                if state not in WORKING:
                    conv.set_host_note(model_view("board_status", sc))
                    return
                status = dict(sc)
                interval = self._interval(status)
                if time.monotonic() - started > self.max_s:
                    conv.say(STOP_CHECKING, origin="host")
                    return
        finally:
            if announced and not conv.busy:
                conv.status("idle")


# -- the factory ------------------------------------------------------------------


class VoiceAgentFactory:
    """Builds a :class:`VoiceSession` per conversation over one MCP client.

    ``model_factory`` returns a Strands ``Model`` (a ``BedrockModel`` or the
    scripted one); ``tools`` is ``MCPClient.list_tools_sync()``, listed once at
    startup and shared, Strands' own pattern (``mcp_calculator.py`` example).
    ``metered`` spends the model budget (Bedrock), unmetered does not (the
    scripted model costs nothing). ``memory`` is an :class:`alexabot.memory.Memory`
    (``None`` is off) and ``actor`` whose preferences it holds, already hashed
    with :func:`alexabot.memory.actor_id`.
    """

    def __init__(
        self,
        *,
        model_factory: Callable[[], Any],
        tools: Sequence[Any],
        server_name: str,
        server_instructions: str,
        budget: Budget,
        model_id: str | None,
        metered: bool,
        images: bool = True,
        poll_min_s: float = HOST_POLL_MIN_S,
        poll_cap_s: float = HOST_POLL_CAP_S,
        poll_max_s: float = HOST_POLL_MAX_S,
        turn_limit: int = TURN_LIMIT,
        today: Callable[[], str] | None = None,
        memory: Any = None,
        actor: str | None = None,
        recall_wait_s: float = memory_mod.RECALL_WAIT_S,
    ) -> None:
        self.model_factory = model_factory
        self.tools = list(tools)
        self.server_name = server_name
        self.server_instructions = server_instructions
        self.budget = budget
        self.model_id = model_id
        self.metered = metered
        self.images = images
        self.poll_min_s = poll_min_s
        self.poll_cap_s = poll_cap_s
        self.poll_max_s = poll_max_s
        self.turn_limit = turn_limit
        self._today = today or (lambda: datetime.date.today().isoformat())
        if memory is None:
            memory = memory_mod.MemoryOff()
        elif actor is None and memory.kind != "off":
            memory = memory_mod.MemoryOff(
                "the sim does not know whose preferences these are; pass "
                "--memory-actor")
        self.memory = memory
        self.actor = actor
        self.recall_wait_s = recall_wait_s
        self.sessions: list[VoiceSession] = []

    def tool_specs(self) -> list[dict[str, Any]]:
        out = []
        for tool in self.tools:
            spec = getattr(tool, "tool_spec", None) or {}
            out.append({"name": getattr(tool, "tool_name", spec.get("name")),
                        "description": spec.get("description", "")})
        return out

    def system_prompt(self, locale: str) -> str:
        return system_prompt(server_name=self.server_name,
                             server_instructions=self.server_instructions,
                             tools=self.tool_specs(), locale=locale,
                             today=self._today(),
                             memory=self.memory.kind != "off")

    def build_agent(self, hooks: VoiceHooks, conv: Any) -> Any:
        from strands import Agent, ModelRetryStrategy
        from strands.agent.conversation_manager import SlidingWindowConversationManager
        from strands.tools.executors import SequentialToolExecutor

        return Agent(
            model=self.model_factory(),
            tools=list(self.tools),
            system_prompt=self.system_prompt(conv.locale),
            hooks=[hooks],
            callback_handler=None,
            conversation_manager=SlidingWindowConversationManager(
                window_size=HISTORY_WINDOW),
            tool_executor=SequentialToolExecutor(),
            retry_strategy=ModelRetryStrategy(max_attempts=2, initial_delay=2),
            name="Ada (simulated Alexa+)",
        )

    def build_poll_agent(self, hooks: VoiceHooks) -> Any:
        from strands import Agent
        from strands.agent.conversation_manager import NullConversationManager

        # Tool calls only; its model is never asked (record_direct_tool_call
        # is off, and nothing invokes it).
        return Agent(
            model=self.model_factory(),
            tools=list(self.tools),
            hooks=[hooks],
            callback_handler=None,
            conversation_manager=NullConversationManager(),
            record_direct_tool_call=False,
            name="Ada host poller",
        )

    def open(self, conv: Any) -> VoiceSession:
        session = VoiceSession(self, conv)
        self.sessions.append(session)
        return session


def bedrock_model_factory(model_id: str, region: str) -> Callable[[], Any]:
    """One shared ``BedrockModel`` (boto clients are thread-safe)."""
    from strands.models import BedrockModel

    model = BedrockModel(model_id=model_id, region_name=region, max_tokens=MAX_TOKENS)
    return lambda: model
