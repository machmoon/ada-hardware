"""Tiered model failover for the ADK orchestrator root.

The worker pipeline has had failover since the start
(:class:`silkscreen.agents.resilience.FallbackModel`); the conversational root
did not, so one ``503 UNAVAILABLE`` from ``gemini-3.7-flash`` ended a demo run
before a single board call was made ("Run failed", 2026-09-13), while the
worker a moment later would have ridden the same 503 onto the next tier.

The shape is ADK's own adapter seam rather than a callback trick: an
``LlmAgent`` accepts any :class:`google.adk.models.BaseLlm` as its ``model``,
and ADK ships adapters that route one ``generate_content_async`` onto another
backend (``google/adk/models/lite_llm.py``, whose ``LiteLlm`` hands the call to
LiteLLM, whose ``Router`` in turn walks ``fallbacks`` in
``litellm/router.py::async_function_with_fallbacks`` and parks a deployment
that answered 429 in a router-lifetime ``CooldownCache``). This is that
Router idea, sized to three Gemini tiers and kept out of LiteLLM so the root
stays on ADK-native Gemini access.

Rules carried over from ``resilience.py`` unchanged, so the two layers cannot
disagree about what a failure means:

* a quota refusal (``RESOURCE_EXHAUSTED``) spends no further attempts on that
  tier and parks it in the shared cooldown table
  (:data:`~silkscreen.agents.resilience.SHARED_COOLDOWNS`), until the day's
  reset when the refusal names a per-day quota;
* a ``503`` is transient load and keeps its in-tier retry;
* every failed attempt is reported (``model.retry``, ``layer: orchestrator``)
  and every tier is named when all of them fail -- nothing is swallowed;
* if every tier is cooling, every tier is asked anyway.

Responses are buffered per attempt. The root runs ADK's non-streaming mode, in
which a tier yields exactly one ``LlmResponse`` (``base_llm.py``'s contract),
and buffering is what guarantees a tier that fails half way never leaves a
fragment in the session before the next tier answers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from pydantic import Field, PrivateAttr

from .. import resilience as _resilience
from ..claude import CLAUDE_CHEAP_MODEL, claude_primary_model, is_claude_model
from ..model import CHEAP_MODEL, FALLBACK_MODEL, ModelError, primary_model
from ..resilience import provider_is_down, quota_cooldown_s

__all__ = ["FailoverLlm", "orchestrator_ladder", "build_failover_llm"]

#: Attempts per tier, first to last. Two on the reasoning tiers because a 503
#: retried after half a second succeeded one time in two when measured
#: (``resilience.py``'s note on ``UNAVAILABLE``); one on the last rung, where
#: the caller has already waited through every other attempt.
DEFAULT_ATTEMPTS = (2, 2, 1)


def _providers() -> list[str]:
    """The configured provider order, or Gemini alone when nothing is.

    With nothing configured the root keeps its old behaviour -- Gemini tiers
    that fail at call time with Gemini's own "GOOGLE_API_KEY is not set" --
    because the chat route has always reported that as a run error rather
    than refusing before the stream opens.
    """
    from ..providers import provider_order

    try:
        return provider_order()
    except ModelError:
        return ["gemini"]


def orchestrator_ladder(model: str, providers: list[str] | None = None) -> list[str]:
    """The requested model's provider first, then every other configured one.

    Gemini's tiers are ``model`` (or the primary), the full-Flash tier, then
    flash-lite; Claude's are ``model`` (or the reasoning tier), then the cheap
    tier. Picking a Gemini model in the web client leads with Gemini and keeps
    Claude behind it, and the reverse -- the provider the person chose is the
    one asked first. De-duplicated.
    """
    order = list(providers) if providers is not None else _providers()
    owner = "claude" if is_claude_model(model) else "gemini"
    order = [owner] + [p for p in order if p != owner]
    ladder: list[str] = []
    for provider in order:
        if provider == "claude":
            ids = (
                model if owner == "claude" else claude_primary_model(),
                CLAUDE_CHEAP_MODEL,
            )
        else:
            ids = (
                model if owner == "gemini" else primary_model(),
                FALLBACK_MODEL,
                CHEAP_MODEL,
            )
        for model_id in ids:
            if model_id and model_id not in ladder:
                ladder.append(model_id)
    return ladder


class FailoverLlm(BaseLlm):
    """A ``BaseLlm`` that tries each tier in order and answers from the first."""

    tiers: list[BaseLlm]
    attempts: list[int] = Field(default_factory=lambda: list(DEFAULT_ATTEMPTS))
    backoff_s: float = 0.5
    #: ``(event dict) -> None``; receives one ``model.retry`` per failed or
    #: skipped attempt.
    on_retry: Callable[[dict[str, Any]], None] | None = Field(default=None, exclude=True)
    #: Called before every attempt *after the first*; the first is already
    #: paced by the agent's ``before_model_callback``.
    before_attempt: Callable[[], None] | None = Field(default=None, exclude=True)
    #: ``Any`` on purpose: a ``dict`` annotation makes pydantic validate -- and
    #: so copy -- the table, and a copy of the process-wide table is a cooldown
    #: that dies with this object, the bug the shared table exists to fix.
    cooldowns: Any = Field(
        default_factory=lambda: _resilience.SHARED_COOLDOWNS, exclude=True
    )
    clock: Callable[[], float] = Field(default=time.monotonic, exclude=True)

    _served: str | None = PrivateAttr(default=None)

    @property
    def served_model(self) -> str | None:
        """The tier that answered the most recent successful call."""
        return self._served

    def _key(self, tier: BaseLlm) -> str:
        return f"orchestrator:{tier.model}"

    def _attempts_for(self, index: int) -> int:
        if not self.attempts:
            return 1
        return max(1, self.attempts[min(index, len(self.attempts) - 1)])

    def _report(self, **event: Any) -> None:
        if self.on_retry is not None:
            self.on_retry({"event": "model.retry", "layer": "orchestrator", **event})

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        now = self.clock()
        indexed = list(enumerate(self.tiers))
        ready = [(i, t) for i, t in indexed if self.cooldowns.get(self._key(t), 0.0) <= now]
        if not ready:
            for _, tier in indexed:
                self.cooldowns.pop(self._key(tier), None)
            ready = indexed
        errors: list[str] = []
        ready_indexes = {i for i, _ in ready}
        for i, tier in indexed:
            if i in ready_indexes:
                continue
            remaining = self.cooldowns[self._key(tier)] - now
            reason = (
                f"skipped: quota refused recently; {remaining:.1f}s before it "
                "is asked again"
            )
            errors.append(f"{tier.model}: {reason}")
            self._report(provider=tier.model, model=tier.model, error=reason, elapsed_s=0.0)

        first = True
        for index, tier in ready:
            for try_no in range(self._attempts_for(index)):
                if not first and self.before_attempt is not None:
                    self.before_attempt()
                first = False
                request = llm_request.model_copy(deep=True)
                request.model = tier.model
                started = self.clock()
                try:
                    responses = [
                        response
                        async for response in tier.generate_content_async(
                            request, stream=stream
                        )
                    ]
                    if not responses:
                        raise ModelError(f"{tier.model} returned no response")
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    errors.append(f"{tier.model}: {error}")
                    self._report(
                        provider=tier.model,
                        model=tier.model,
                        error=error[:500],
                        elapsed_s=round(self.clock() - started, 3),
                    )
                    if provider_is_down(error):
                        self.cooldowns[self._key(tier)] = self.clock() + quota_cooldown_s(error)
                        break
                    if try_no + 1 < self._attempts_for(index):
                        await asyncio.sleep(self.backoff_s * (2**try_no))
                    continue
                self._served = tier.model
                for response in responses:
                    yield response
                return
        raise ModelError(
            f"all {len(self.tiers)} orchestrator tiers failed -- " + "; ".join(errors)
        )


def build_failover_llm(
    model: str,
    *,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
    before_attempt: Callable[[], None] | None = None,
) -> FailoverLlm:
    """The live ladder for one requested root model id.

    Gemini tiers are ADK's native ``Gemini``; Claude tiers are ADK's own
    ``AnthropicLlm`` adapter (:mod:`.claude_llm`). A Claude tier only appears
    when Claude is configured, so a Google-key-only service builds exactly
    the three Gemini tiers it did before.
    """
    from google.adk.models import Gemini

    ladder = orchestrator_ladder(model)
    tiers: list[BaseLlm] = []
    for model_id in ladder:
        if is_claude_model(model_id):
            from .claude_llm import build_claude_llm

            tiers.append(build_claude_llm(model_id))
        else:
            tiers.append(Gemini(model=model_id))
    return FailoverLlm(
        model=model,
        tiers=tiers,
        on_retry=on_retry,
        before_attempt=before_attempt,
    )
