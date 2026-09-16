"""Claude for the ADK orchestrator root, through ADK's own adapter.

google-adk ships Claude support: ``google/adk/models/anthropic_llm.py``
defines ``AnthropicLlm`` (the Anthropic API) and ``Claude`` (Vertex AI), both
``BaseLlm`` subclasses that translate an ``LlmRequest`` into a Messages API
call and back. The root uses that adapter rather than a second translation
layer; this module only builds its client from this repo's configuration and
corrects three things the adapter gets wrong against current models and the
``anthropic`` 1.x SDK, each in :meth:`SilkscreenClaudeLlm._build_anthropic_kwargs`
(the adapter's own hook for the request dict):

1. **Thinking display.** Claude Opus 5 thinks by default and, unless asked
   otherwise, returns ``thinking`` blocks with an *empty* ``thinking`` string
   and a signature. The adapter turns such a block into ``Part(text="",
   thought=True, thought_signature=...)`` (``content_block_to_part``), and on
   the next request -- the one that carries the ``generate_board`` tool result
   back -- ``_part_to_message_block`` tests ``part.thought and part.text``,
   finds the text empty, and re-sends the signature as a
   ``redacted_thinking`` block's ``data``, which is not what it is. Asking for
   ``display: "summarized"`` makes the text non-empty, so the block
   round-trips as the ``thinking`` block it was.
2. **Sampling parameters.** The adapter forwards ``temperature``/``top_p``/
   ``top_k`` when thinking is not configured; the 1.x SDK no longer accepts
   ``temperature`` on ``messages.create`` and Opus 5 / Sonnet 5 reject all
   three, so they are dropped.
3. **Budget and effort.** ``max_output_tokens`` (2048 on the root) was sized
   for Gemini's answer, and Claude counts thinking inside ``max_tokens``, so
   :data:`~silkscreen.agents.claude.THINKING_HEADROOM_TOKENS` is added. The web
   control's Gemini ``thinking_level`` (``low``/``medium``/``high``), which
   the adapter ignores with a warning, becomes ``output_config.effort`` -- the
   three words mean the same thing on both APIs.

The adapter is used non-streaming by the root, so ``max_tokens`` is also
kept under the SDK's non-streaming ceiling.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from typing import Any

from google.adk.models.anthropic_llm import AnthropicLlm

from ..claude import (
    API_KEY_ENV_VAR,
    THINKING_HEADROOM_TOKENS,
    VERTEX_PROJECT_ENV_VAR,
    VERTEX_REGION_ENV_VAR,
    claude_backend,
    claude_missing,
    supports_effort,
)
from ..model import ModelError, request_timeout_ms

__all__ = ["SilkscreenClaudeLlm", "build_claude_llm"]

#: The ``anthropic`` SDK refuses a non-streaming request whose ``max_tokens``
#: implies more than ten minutes at its assumed output rate (roughly 21K
#: tokens); stay clear of it.
_NON_STREAMING_MAX_TOKENS = 20_000


class SilkscreenClaudeLlm(AnthropicLlm):
    """ADK's ``AnthropicLlm`` with the request corrections described above."""

    def _build_anthropic_kwargs(self, llm_request, messages, tools, tool_choice, thinking):
        with warnings.catch_warnings():
            # The adapter warns that ``thinking_level`` is ignored; it is not
            # ignored here, it is translated below.
            warnings.simplefilter("ignore", UserWarning)
            kwargs = super()._build_anthropic_kwargs(
                llm_request, messages, tools, tool_choice, thinking
            )
        for key in ("temperature", "top_p", "top_k"):
            kwargs.pop(key, None)
        model_id = str(kwargs.get("model") or self.model)
        if supports_effort(model_id):
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            level = getattr(
                getattr(getattr(llm_request, "config", None), "thinking_config", None),
                "thinking_level",
                None,
            )
            word = str(getattr(level, "value", level) or "").lower()
            if word in ("low", "medium", "high") and "output_config" not in kwargs:
                kwargs["output_config"] = {"effort": word}
            kwargs["max_tokens"] = min(
                int(kwargs.get("max_tokens") or self.max_tokens) + THINKING_HEADROOM_TOKENS,
                _NON_STREAMING_MAX_TOKENS,
            )
        return kwargs


def build_claude_llm(
    model: str,
    *,
    env: Mapping[str, str] | None = None,
    client: Any = None,
) -> SilkscreenClaudeLlm:
    """One root tier on the configured Claude backend.

    The Vertex client is built from ``ANTHROPIC_VERTEX_PROJECT_ID`` and
    ``CLOUD_ML_REGION`` (the ``anthropic`` SDK's names) and handed to the
    adapter, rather than using ADK's ``Claude`` class, whose client reads
    ``GOOGLE_CLOUD_PROJECT``/``GOOGLE_CLOUD_LOCATION``: one pair of variables
    decides where Claude runs for the workers and the root alike.
    """
    env = os.environ if env is None else env
    if client is None:
        backend = claude_backend(env)
        if backend is None:
            raise ModelError(f"Claude is not configured: {claude_missing(env)}.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - import guard
            raise ModelError(
                "the anthropic package is not installed; pip install -e '.[anthropic]'"
            ) from exc
        timeout_s = request_timeout_ms() / 1000.0
        if backend == "vertex":
            client = anthropic.AsyncAnthropicVertex(
                project_id=env.get(VERTEX_PROJECT_ENV_VAR, "").strip(),
                region=env.get(VERTEX_REGION_ENV_VAR, "").strip(),
                timeout=timeout_s,
                max_retries=0,
            )
        else:
            client = anthropic.AsyncAnthropic(
                api_key=env.get(API_KEY_ENV_VAR, "").strip(),
                timeout=timeout_s,
                max_retries=0,
            )
    return SilkscreenClaudeLlm(model=model, client=client)
