"""The tool-calling model seam, and the scripted model that keeps it offline.

:class:`~silkscreen.agents.model.Model` answers a prompt with a string. A loop
in which the model *asks* for a verifier needs one more method, and the shape
of that method is where an offline suite can lie: a scripted test passes on a
``Turn`` no real provider emits. So the types here are the intersection of
what Gemini (``google-genai`` 2.22, ``types.Part.function_call`` with a
``thought_signature`` on the *part*) and Claude (``anthropic`` 1.5,
``tool_use`` blocks with mandatory ids, ``thinking`` blocks with signatures)
actually require, with two rules the runner enforces before any adapter runs:

* an assistant :class:`Message` is **never rebuilt** from ``text`` and
  ``tool_calls``; the adapter that produced it stores its own content in
  ``native`` and replays it verbatim, because both providers sign what they
  said (Gemini's thought signature, Claude's thinking signature) and reject a
  reconstruction -- ``agents/adk/claude_llm.py`` records the day a shared
  intermediate form lost one;
* a ``user`` message carrying ``tool_results`` answers every ``ToolCall`` of
  the previous assistant message, one to one, in **one** message: Gemini
  wants all ``function_response`` parts in one ``Content`` and Claude all
  ``tool_result`` blocks in one turn.

``stop_reason`` is a five-word harness vocabulary, never the provider's:
Gemini finishes a function-call turn with ``STOP`` and Claude with
``tool_use``, so a loop that branched on the provider's word would end early
on one of them. The runner branches on ``len(turn.tool_calls)``; the raw word
rides along for the receipt.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..model import ModelError

__all__ = [
    "StopReason",
    "Usage",
    "ToolSpec",
    "ToolCall",
    "ToolResult",
    "Turn",
    "Message",
    "ToolModel",
    "ScriptedToolModel",
    "mint_call_id",
    "MINTED_PREFIX",
]

StopReason = Literal["end", "tool_calls", "max_tokens", "refused", "malformed"]

#: Ids the harness mints when a provider gave none (Gemini's ``FunctionCall.id``
#: is optional). Deterministic -- ``f"{MINTED_PREFIX}{turn}-{index}"`` -- so two
#: identical runs emit identical event streams; a uuid would not.
MINTED_PREFIX = "h-"


def mint_call_id(turn: int, index: int) -> str:
    return f"{MINTED_PREFIX}{turn}-{index}"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.thinking_tokens + other.thinking_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True)
class ToolSpec:
    """What the model is told about a tool: name, description, JSON schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    #: Already parsed. Never string-matched: Claude's input is JSON, Gemini's
    #: ``args`` is a dict, and a test that compares strings hides the difference.
    arguments: dict[str, Any]
    #: Gemini's ``thought_signature`` on the function-call part, replayed by
    #: the Gemini adapter and ignored by every other one.
    signature: bytes | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    output: dict[str, Any] | str
    is_error: bool = False
    seconds: float = 0.0


@dataclass(frozen=True)
class Turn:
    """One assistant answer."""

    provider: str
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = "end"
    raw_stop_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    #: The provider's own assistant content, verbatim (a ``types.Content`` for
    #: Gemini, a list of block dicts for Claude). Replayed as-is.
    native: Any = None

    @classmethod
    def final(cls, text: str, *, provider: str = "scripted") -> Turn:
        return cls(
            provider=provider, text=text, stop_reason="end", raw_stop_reason="scripted"
        )

    @classmethod
    def calls(
        cls, *calls: ToolCall, text: str = "", provider: str = "scripted"
    ) -> Turn:
        return cls(
            provider=provider,
            text=text,
            tool_calls=tuple(calls),
            stop_reason="tool_calls",
            raw_stop_reason="scripted",
        )


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    text: str = ""
    #: Assistant only.
    tool_calls: tuple[ToolCall, ...] = ()
    #: User only; ids equal the previous assistant message's ``tool_calls``.
    tool_results: tuple[ToolResult, ...] = ()
    #: Assistant only: which adapter produced it. Pins the failover rung for
    #: the rest of the conversation.
    provider: str | None = None
    native: Any = None

    @classmethod
    def user(
        cls, text: str = "", *, tool_results: tuple[ToolResult, ...] = ()
    ) -> Message:
        return cls(role="user", text=text, tool_results=tool_results)

    @classmethod
    def from_turn(cls, turn: Turn) -> Message:
        return cls(
            role="assistant",
            text=turn.text,
            tool_calls=turn.tool_calls,
            provider=turn.provider,
            native=turn.native,
        )


class ToolModel(Protocol):
    """Anything that can take a conversation and a tool list and answer a turn."""

    def generate_turn(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec],
        system: str | None = None,
        max_output_tokens: int = 8192,
    ) -> Turn: ...


def conversation_text(messages: list[Message], system: str | None) -> str:
    """Everything a marker could be found in: the system prompt and every
    message's text and tool outputs, in order."""
    chunks = [system or ""]
    for m in messages:
        chunks.append(m.text)
        for r in m.tool_results:
            chunks.append(r.output if isinstance(r.output, str) else str(r.output))
    return "\n".join(chunks)


class ScriptedToolModel:
    """A scripted :class:`ToolModel`: ordered turns per marker, offline.

    ``script`` maps a marker to the *sequence* of turns to answer with, in
    order, each time the marker appears in the conversation (system prompt or
    any message). A per-marker cursor under a lock keeps three lanes racing
    on worker threads deterministic -- the ``ScriptedModel.by_marker``
    convention, which returns one string per marker and so could not script
    "a red verifier, then a green one". A marker that runs dry raises
    :class:`ModelError` (the existing "ran out of responses" rule); a
    conversation matching no marker raises too, naming the markers it has.
    """

    provider = "scripted"

    def __init__(self, script: dict[str, list[Turn]]) -> None:
        self._script = {k: list(v) for k, v in script.items()}
        self._cursor: dict[str, int] = {k: 0 for k in script}
        self._lock = threading.Lock()
        self.calls: list[list[Message]] = []

    def generate_turn(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec],
        system: str | None = None,
        max_output_tokens: int = 8192,
    ) -> Turn:
        del tools, max_output_tokens
        haystack = conversation_text(messages, system)
        with self._lock:
            self.calls.append(list(messages))
            for marker, turns in self._script.items():
                if marker in haystack:
                    i = self._cursor[marker]
                    if i >= len(turns):
                        raise ModelError(
                            f"ScriptedToolModel ran out of turns for marker {marker!r} "
                            f"after {len(turns)}"
                        )
                    self._cursor[marker] = i + 1
                    return turns[i]
        raise ModelError(
            "ScriptedToolModel matched no marker; known markers: "
            + ", ".join(repr(k) for k in self._script)
        )
