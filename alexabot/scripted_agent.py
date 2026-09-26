"""``--scripted`` for the voice agent: a rule-based stand-in for Nova, offline.

Reviewer finding m2: ``alexabot --scripted`` lets a judge run Ada's workers
with no key, but the simulated Alexa+ page also needs an *agent* -- the MCP
client that picks a tool for each thing the person says -- and the live one is
Amazon Nova on Bedrock. This module replaces only that language model. The
Strands agent loop, the Strands ``MCPClient`` over real Streamable HTTP, the
hooks and the alexabot server all stay real, so a scripted session exercises
everything a live one does except the model's choice.

Two layers, so the rules test without Strands installed:

* :func:`decide` is pure: Strands messages (plain dicts, ``types/content.py``)
  in, ``{"tool": name, "input": {...}}`` or ``{"text": str}`` out. First
  matching rule wins; the rules are the numbered comments in :func:`decide`.
* :func:`scripted_model` imports Strands and returns a ``strands.models.Model``
  whose ``stream`` plays :func:`decide`'s answer as model stream events, in the
  shape of Strands' own test fixture
  (``strands-py/tests/fixtures/mocked_model_provider.py``
  ``map_agent_message_to_events``, sdk-python ``6da3f48``, Apache-2.0; read,
  not copied). Its shape follows KayLerch/alexa-skill-mcp-bridge's
  ``packages/agent/src/testing/scripted-model.ts`` (``ca2c2ef``, the bridge's
  plan D10): tool-use ids ``scripted-<n>``, and a ``calls`` record for tests.

The board context -- which session, which state, which question -- comes from
the same compact JSON line Nova reads (:func:`alexabot.agent.model_view`):
the newest ``[Host note: ...]`` or tool result in the history. So the
scripted path also tests what the model is shown.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "HELP_TEXT",
    "IDENTITY_TEXT",
    "context_of",
    "decide",
    "request_text",
    "scripted_model",
]

#: The identity sentence the system prompt tells Nova to say (``prompts/system.md``).
IDENTITY_TEXT = (
    "This is a simulation of the Alexa+ experience in a web page, running Ada's "
    "tools. I'm not Alexa."
)
HELP_TEXT = (
    "Scripted mode understands a few phrases: ask for a board, answer my "
    "question, say you choose, say yes to place and route, ask how it's going, "
    "ask me to explain a finding, or ask about your boards."
)
ERROR_TEXT = (
    "Sorry, that didn't work on my side. You can ask me to start a new board."
)

_NOTE_SPLIT = re.compile(r"\n\[(?:Host note|Frontend hint):")
_HOST_NOTE = "[Host note:"
_HINT = re.compile(r'\[Frontend hint: this request matched the tool "([a-z_]+)"')
_HINT_VALUES = "and the values "

_IDENTITY = re.compile(r"\b(are you|is this)( the)?( real)? (alexa|amazon)\b")
_RECALL = re.compile(
    r"\bmy boards\b|\bearlier\b|\byesterday\b|\blast board\b|\bhow did\b.*\bgo\b"
    r"|\bwhat boards\b|\bboards have i\b"
)
#: "How did that board go?" with a board in hand is about that board, not the
#: history: the chip "Back to the board" says exactly this.
_THIS_BOARD = re.compile(
    r"\bhow\b.*\b(?:that|this|the)\s+board\b"
    r"|\b(?:that|this|the)\s+board\b.*\b(?:go|going|doing)\b"
)
_QUERY_WORDS = (("usb-c", "USB-C"), ("usb", "USB"), ("regulator", "regulator"),
                ("led", "LED"), ("sensor", "sensor"), ("motor", "motor"),
                ("battery", "battery"))
_STATUS = re.compile(
    r"how's it going|how is it going|\bstatus\b|\bprogress\b|\bis it done\b"
    r"|\bdone yet\b"
)
_EXPLAIN = re.compile(r"\bexplain\b|\bwhy\b|what's wrong|\bblocker\b|\bfinding\b")
#: A question about the review ("Did the design review find any problems?"):
#: the board's status says what the review found. "Sure, no problem" is an
#: answer, not this, so a bare "problem" needs a question word before it.
_REVIEW_QUESTION = re.compile(
    r"\breview\b|\b(?:any|find|found|have|has|were|are)\b.*\b(?:problems?|issues?)\b"
)
_YOU_CHOOSE = re.compile(
    r"you choose|your call|you decide|\bdefaults?\b|doesn't matter|\bwhatever\b"
)
_YES = re.compile(
    r"^(yes|yeah|yep|sure|ok|okay)\b|\bgo ahead\b|\bdo it\b|\bplace\b|\broute\b"
    r"|\bcontinue\b"
)
_START = re.compile(
    r"\bboard\b|\bpcb\b|\bcircuit\b|\bdesign\b|\bregulator\b|\bbuild me\b"
    r"|\bmake me\b"
)
#: With a board already in the conversation, a sentence that merely mentions
#: "board" or "design" is about that board ("Is the design finished?", "Does
#: the board need a heatsink?"); a new one has to be asked for: an imperative
#: verb opening the sentence, "I need/want a ...", or "a new/another board".
_START_VERB = re.compile(
    r"^\s*(?:(?:now|ok|okay|so|and|then|next|please)\s*,?\s+)*"
    r"(?:(?:ask\s+)?ada\b\s*,?\s*(?:(?:for|to)\s+)?|(?:can|could|would)\s+you\s+)?"
    r"(?:please\s+)?(?:design|make|build|create|start|draft|give\s+me)\b"
    r"|\bi(?:\s+would|'d)?\s+(?:need|want|like)\s+(?:\w+\s+)?(?:a|an|another)\b"
    r"|\b(?:new|another|second|different)\s+(?:\w+\s+){0,2}(?:board|pcb|circuit|design)\b"
)
#: "Ask Ada for ...", "Ada, design ...": how a person addresses the agent,
#: not what the board should do; dropped from the intent.
_ADDRESS = re.compile(
    r"^\s*(?:please\s+)?(?:(?:ask\s+)?ada\b\s*,?\s*(?:(?:for|to)\s+)?|"
    r"(?:can|could)\s+you\s+)(?:(?:please\s+)?(?:design|make|build)(?:\s+me)?\s+)?",
    re.IGNORECASE,
)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_ORDINAL = re.compile(
    r"\b(first|second|third|fourth|fifth)\b|\b(?:finding|number|blocker)\s+"
    r"(\d+|one|two|three|four|five)\b"
)


def request_text(text: str) -> str:
    """The person's words: ``text`` without the bracketed host note or hint."""
    return _NOTE_SPLIT.split(str(text or ""), maxsplit=1)[0].strip()


def _json_after(text: str, marker: str) -> Any:
    at = text.rfind(marker)
    if at < 0:
        return None
    brace = text.find("{", at + len(marker))
    if brace < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, brace)
    except ValueError:
        return None
    return value


def _blocks(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [b for b in message.get("content") or [] if isinstance(b, Mapping)]


def _result_view(block: Mapping[str, Any]) -> dict[str, Any] | None:
    result = block.get("toolResult")
    if not isinstance(result, Mapping) or result.get("status") == "error":
        return None
    for item in result.get("content") or []:
        text = item.get("text") if isinstance(item, Mapping) else None
        if not text:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def context_of(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The newest board context in ``messages``: a host note or a tool result.

    Either is the compact model view; only a **status** view counts, one that
    names a session and carries its ``state``. An ``explain_finding`` or
    ``recall_my_boards`` view has no state or question, and taking it as the
    context would make the next answer to an open question read as noise
    (the M3 verifier's "Why?" then "Up to one amp." case).
    ``{}`` when the conversation has not touched a board yet.
    """
    for message in reversed(messages):
        for block in reversed(_blocks(message)):
            view = _result_view(block)
            if view is None and isinstance(block.get("text"), str):
                note = _json_after(block["text"], _HOST_NOTE)
                view = note if isinstance(note, dict) else None
            if view and view.get("session_id") and view.get("state"):
                return dict(view)
    return {}


def _last_user(messages: Sequence[Mapping[str, Any]]) -> tuple[str, Mapping | None]:
    """The last user message's text, and its tool result block if it has one."""
    if not messages:
        return "", None
    last = messages[-1]
    if last.get("role") != "user":
        return "", None
    text = ""
    result = None
    for block in _blocks(last):
        if isinstance(block.get("text"), str):
            text += block["text"]
        if isinstance(block.get("toolResult"), Mapping):
            result = block["toolResult"]
    return text, result


def _hint(text: str) -> dict[str, Any] | None:
    match = _HINT.search(text)
    if match is None:
        return None
    values = _json_after(text[match.end():], _HINT_VALUES)
    return {"tool": match.group(1),
            "input": dict(values) if isinstance(values, dict) else {}}


def _ordinal(u: str) -> int:
    match = _ORDINAL.search(u)
    if match is None:
        return 1
    word = match.group(1) or match.group(2)
    return int(word) if word.isdigit() else _ORDINALS.get(word, 1)


def _error_text(result: Mapping[str, Any]) -> str:
    text = " ".join(
        str(item.get("text", "")) for item in result.get("content") or []
        if isinstance(item, Mapping)
    ).strip()
    if not text or any(c in text for c in "_{}[]="):
        return ERROR_TEXT
    return text


def decide(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The scripted agent's next move for ``messages``; the rules are ordered."""
    raw, result = _last_user(messages)
    # 0. A tool failed: say so in words (never raw text), and stop.
    if result is not None:
        if result.get("status") == "error":
            return {"text": _error_text(result)}
        view = _result_view({"toolResult": result}) or {}
        return {"text": str(view.get("speech") or ERROR_TEXT)}
    original = request_text(raw)
    u = original.lower().replace("’", "'")
    context = context_of(messages)
    sid = context.get("session_id")
    state = context.get("state")
    question = context.get("question") if isinstance(context.get("question"),
                                                     dict) else None
    # 1. A tapped chip: the tool it names, with its arguments.
    hint = _hint(raw)
    if hint is not None:
        tool, arguments = hint["tool"], dict(hint["input"])
        if tool == "start_board_design":
            if not arguments.get("intent"):
                arguments["intent"] = original[:500]
            arguments["request_id"] = "host-assigned"
        return {"tool": tool, "input": arguments}
    # 2. Who am I?
    if _IDENTITY.search(u):
        return {"text": IDENTITY_TEXT}
    # 3a. "How did that board go?" while a board is in hand: its status.
    if sid and _THIS_BOARD.search(u):
        return {"tool": "board_status", "input": {"session_id": sid}}
    # 3. Earlier boards.
    if _RECALL.search(u):
        arguments: dict[str, Any] = {}
        for word, query in _QUERY_WORDS:
            if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", u):
                arguments["query"] = query
                break
        return {"tool": "recall_my_boards", "input": arguments}
    # 4. How is it going?
    if _STATUS.search(u):
        return {"tool": "board_status",
                "input": {"session_id": sid} if sid else {}}
    # 5. Explain a finding.
    if sid and _EXPLAIN.search(u):
        return {"tool": "explain_finding",
                "input": {"session_id": sid, "which": _ordinal(u)}}
    # 5b. A question about the review: the board's status says what it found.
    if sid and _REVIEW_QUESTION.search(u):
        return {"tool": "board_status", "input": {"session_id": sid}}
    # 6. "You choose", only while a question is open.
    if sid and state == "questions" and _YOU_CHOOSE.search(u):
        return {"tool": "answer_design_questions",
                "input": {"session_id": sid, "you_choose": True}}
    # 7. "Yes", when the schematic is drafted.
    if sid and state == "drafted" and _YES.search(u):
        return {"tool": "continue_design", "input": {"session_id": sid}}
    # 8. Anything else while a question is open is its answer.
    if sid and state == "questions" and question is not None and original:
        return {"tool": "answer_design_questions",
                "input": {"session_id": sid,
                          "answers": [{"index": int(question.get("index", 0)),
                                       "answer": original[:200]}]}}
    # 9. A new board (with a board in hand, only when asked for with a verb).
    if _START.search(u) and (not sid or _START_VERB.search(u)):
        intent = _ADDRESS.sub("", original, count=1).strip() or original
        return {"tool": "start_board_design",
                "input": {"intent": intent[:500], "request_id": "host-assigned"}}
    # 10. Help.
    return {"text": HELP_TEXT}


def _events(step: Mapping[str, Any], tool_use_id: str) -> list[dict[str, Any]]:
    """``step`` as Strands stream events (``mocked_model_provider.py`` shape)."""
    events: list[dict[str, Any]] = [{"messageStart": {"role": "assistant"}}]
    if "tool" in step:
        events += [
            {"contentBlockStart": {"start": {"toolUse": {
                "name": step["tool"], "toolUseId": tool_use_id}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps(step["input"])}}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    else:
        events += [
            {"contentBlockStart": {"start": {}}},
            {"contentBlockDelta": {"delta": {"text": step["text"]}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    events.append({"metadata": {
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        "metrics": {"latencyMs": 0}}})
    return events


_MODEL_CLASS: Any = None


def _model_class() -> Any:
    global _MODEL_CLASS
    if _MODEL_CLASS is not None:
        return _MODEL_CLASS
    import threading

    from strands.models import Model

    class ScriptedVoiceModel(Model):
        """:func:`decide` behind Strands' ``Model`` interface; no network."""

        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []
            self.decisions: list[dict[str, Any]] = []
            self._counter = 0
            self._lock = threading.Lock()

        def update_config(self, **model_config: Any) -> None:
            return None

        def get_config(self) -> Any:
            return {"model_id": "scripted"}

        def structured_output(self, output_model, prompt, system_prompt=None,
                              **kwargs):  # pragma: no cover - never asked
            raise NotImplementedError("the scripted voice model has no "
                                      "structured output")

        async def stream(self, messages, tool_specs=None, system_prompt=None,
                         **kwargs):
            snapshot = json.loads(json.dumps(list(messages), default=str))
            step = decide(snapshot)
            with self._lock:
                self._counter += 1
                tool_use_id = f"scripted-{self._counter}"
                self.calls.append(snapshot)
                self.decisions.append(step)
            for event in _events(step, tool_use_id):
                yield event

    _MODEL_CLASS = ScriptedVoiceModel
    return _MODEL_CLASS


def scripted_model() -> Any:
    """A fresh ``ScriptedVoiceModel``; needs the ``alexa`` extra (Strands)."""
    return _model_class()()
