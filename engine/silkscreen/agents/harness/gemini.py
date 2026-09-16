"""Gemini behind the tool-calling seam, on ``google-genai`` function calling.

Two facts about the wire drive the shape (``google/genai/types.py`` 2.22):
``Part.thought_signature`` rides on the *function-call part*, and
``GenerateContentResponse.function_calls`` discards the part, so the
adapter reads ``candidate.content.parts`` itself and replays the whole
``Content`` verbatim (``Turn.native``) rather than reconstructing it -- ADK
ships ``SKIP_THOUGHT_SIGNATURE_VALIDATOR`` because a rebuilt part is
rejected for its missing signature. ``FunctionCall.id`` is optional; a
harness-minted id is never sent back, since Gemini would not recognise it.
All function responses of one turn go in **one** ``Content(role="user")``.
"""

from __future__ import annotations

from typing import Any

from ..model import ModelError, request_timeout_ms
from .model import (
    MINTED_PREFIX,
    Message,
    StopReason,
    ToolCall,
    ToolSpec,
    Turn,
    Usage,
)

__all__ = ["GeminiToolModel"]

_STOP: dict[str, StopReason] = {
    "STOP": "end",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refused",
    "RECITATION": "refused",
    "BLOCKLIST": "refused",
    "PROHIBITED_CONTENT": "refused",
    "SPII": "refused",
    "IMAGE_SAFETY": "refused",
    "MALFORMED_FUNCTION_CALL": "malformed",
    "UNEXPECTED_TOOL_CALL": "malformed",
    "TOO_MANY_TOOL_CALLS": "malformed",
}


class GeminiToolModel:
    provider = "gemini"

    def __init__(
        self, model: str, *, client: Any | None = None, api_key: str | None = None
    ):
        self.model = model
        if client is None:
            from google import genai

            client = genai.Client(
                api_key=api_key, http_options={"timeout": request_timeout_ms()}
            )
        self._client = client

    @classmethod
    def from_model(cls, model: Any) -> GeminiToolModel:
        """Wrap an ``agents.model.GeminiModel``, sharing its client and id."""
        return cls(model.model, client=model._client)

    # ---- request

    @staticmethod
    def _contents(messages: list[Message]) -> list[Any]:
        from google.genai import types

        out: list[Any] = []
        for m in messages:
            if m.role == "assistant":
                if m.native is None or m.provider != "gemini":
                    raise ModelError(
                        "an assistant turn from another provider cannot be replayed "
                        "to Gemini"
                    )
                out.append(m.native)
                continue
            parts: list[Any] = []
            for r in m.tool_results:
                response = (
                    r.output if isinstance(r.output, dict) else {"output": r.output}
                )
                if r.is_error:
                    response = {"error": response}
                fr = types.FunctionResponse(name=r.name, response=response)
                if not r.call_id.startswith(MINTED_PREFIX):
                    fr.id = r.call_id
                parts.append(types.Part(function_response=fr))
            if m.text:
                parts.append(types.Part(text=m.text))
            if not parts:
                parts.append(types.Part(text=""))
            out.append(types.Content(role="user", parts=parts))
        return out

    def generate_turn(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec],
        system: str | None = None,
        max_output_tokens: int = 8192,
    ) -> Turn:
        from google.genai import types

        config: dict[str, Any] = {"max_output_tokens": max_output_tokens}
        if system:
            config["system_instruction"] = system
        if tools:
            config["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in tools
                    ]
                )
            ]
            config["automatic_function_calling"] = {"disable": True}
        try:
            resp = self._client.models.generate_content(
                model=self.model, contents=self._contents(messages), config=config
            )
        except Exception as exc:
            raise ModelError(f"{self.model} call failed: {exc}") from exc
        return self._turn_of(resp)

    # ---- response

    def _turn_of(self, resp: Any) -> Turn:
        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            raise ModelError(f"{self.model} returned no candidates")
        cand = candidates[0]
        content = getattr(cand, "content", None)
        parts = list(getattr(content, "parts", None) or [])
        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc is not None:
                calls.append(
                    ToolCall(
                        id=str(fc.id or ""),
                        name=str(fc.name or ""),
                        arguments=dict(fc.args or {}),
                        signature=getattr(part, "thought_signature", None),
                    )
                )
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                text_chunks.append(part.text)
        raw = getattr(cand, "finish_reason", None)
        raw_name = getattr(raw, "name", None) or (str(raw) if raw else "")
        stop = _STOP.get(raw_name, "end")
        if stop == "end" and calls:
            stop = "tool_calls"
        usage = getattr(resp, "usage_metadata", None)
        return Turn(
            provider=self.provider,
            text="".join(text_chunks),
            tool_calls=tuple(calls),
            stop_reason=stop,
            raw_stop_reason=raw_name,
            usage=Usage(
                input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
                output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
                thinking_tokens=int(getattr(usage, "thoughts_token_count", 0) or 0),
                cache_read_tokens=int(
                    getattr(usage, "cached_content_token_count", 0) or 0
                ),
            ),
            native=content,
        )
