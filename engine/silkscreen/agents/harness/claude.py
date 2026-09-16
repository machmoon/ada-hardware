"""Claude behind the tool-calling seam, on the Messages API's ``tools``.

The assistant's content blocks -- ``text``, ``tool_use`` and the signed
``thinking`` blocks -- are stored verbatim as ``Turn.native`` (a list of
block dicts) and replayed as the assistant message, since a thinking block
must round-trip unchanged on the same model. ``tool_use.id`` is mandatory and
every ``tool_result`` of one turn goes in **one** user message. Thinking and
effort follow :class:`~silkscreen.agents.claude.ClaudeModel` (adaptive
thinking plus ``output_config.effort`` where the model supports it, no
sampling parameters), and the SDK's own retries stay off because the
failover ladder owns retrying.
"""

from __future__ import annotations

from typing import Any

from ..claude import (
    _MAX_TOKENS_CEILING,
    THINKING_HEADROOM_TOKENS,
    ClaudeModel,
    _map_error,
)
from ..model import ModelError
from .model import Message, StopReason, ToolCall, ToolSpec, Turn, Usage

__all__ = ["ClaudeToolModel"]

_STOP: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_calls",
    "pause_turn": "tool_calls",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refused",
}


class ClaudeToolModel:
    provider = "claude"

    def __init__(self, base: ClaudeModel) -> None:
        self._base = base
        self.model = base.model

    @classmethod
    def from_model(cls, model: ClaudeModel) -> ClaudeToolModel:
        return cls(model)

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "assistant":
                if m.native is None or m.provider != "claude":
                    raise ModelError(
                        "an assistant turn from another provider cannot be replayed "
                        "to Claude"
                    )
                out.append({"role": "assistant", "content": m.native})
                continue
            content: list[dict[str, Any]] = []
            for r in m.tool_results:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.output
                    if isinstance(r.output, str)
                    else _json(r.output),
                }
                if r.is_error:
                    block["is_error"] = True
                content.append(block)
            if m.text:
                content.append({"type": "text", "text": m.text})
            if not content:
                content.append({"type": "text", "text": "(continue)"})
            out.append({"role": "user", "content": content})
        return out

    def request_kwargs(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec],
        system: str | None,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        base = self._base
        kwargs: dict[str, Any] = {
            "model": base.wire_model,
            "messages": self._messages(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        if base.effort is not None:
            kwargs["max_tokens"] = min(
                max_output_tokens + THINKING_HEADROOM_TOKENS, _MAX_TOKENS_CEILING
            )
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": base.effort}
        else:
            kwargs["max_tokens"] = min(max_output_tokens, _MAX_TOKENS_CEILING)
        return kwargs

    def generate_turn(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec],
        system: str | None = None,
        max_output_tokens: int = 8192,
    ) -> Turn:
        kwargs = self.request_kwargs(
            messages, tools=tools, system=system, max_output_tokens=max_output_tokens
        )
        try:
            with self._base._client.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except ModelError:
            raise
        except Exception as exc:
            raise _map_error(self.model, self._base.backend, exc) from exc
        return self._turn_of(message)

    def _turn_of(self, message: Any) -> Turn:
        blocks = list(getattr(message, "content", None) or [])
        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        native: list[dict[str, Any]] = []
        for block in blocks:
            btype = getattr(block, "type", None)
            dump = (
                block.model_dump(exclude_none=True)
                if hasattr(block, "model_dump")
                else dict(block)
            )
            native.append(dump)
            if btype == "text":
                text_chunks.append(str(getattr(block, "text", "")))
            elif btype == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        arguments=dict(getattr(block, "input", None) or {}),
                    )
                )
        raw = str(getattr(message, "stop_reason", None) or "")
        stop = _STOP.get(raw, "end")
        if stop == "end" and calls:
            stop = "tool_calls"
        usage = getattr(message, "usage", None)
        return Turn(
            provider=self.provider,
            text="".join(text_chunks),
            tool_calls=tuple(calls),
            stop_reason=stop,
            raw_stop_reason=raw,
            usage=Usage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_tokens=int(
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                ),
            ),
            native=native,
        )


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)
