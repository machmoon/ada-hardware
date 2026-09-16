"""Which model provider leads, decided once from the environment.

The default ladder is **Claude, then Gemini**, each included only when it is
configured: Claude when :func:`~silkscreen.agents.claude.claude_backend` finds
``ANTHROPIC_API_KEY`` or a Vertex project and region, Gemini when
``GOOGLE_API_KEY`` (or ``GEMINI_API_KEY``) is set. A machine with only a Google
key therefore gets exactly the Gemini path it had before this module existed.

The shape is aider's default-model selection
(``aider/onboarding.py::try_to_select_default_model``): an ordered list of
``(environment variable, model)`` pairs, the first whose key is present wins,
``ANTHROPIC_API_KEY`` first. Two deliberate differences. Every configured
provider is kept, in order, rather than only the first, because the rest become
failover rungs (:class:`~silkscreen.agents.resilience.FallbackModel`, itself
modelled on LiteLLM's ``Router`` fallbacks). And nothing configured is a
refusal naming both variables, where aider returns ``None`` and offers an
OAuth flow: a pipeline run that cannot reach any model must say so before it
spends a solve.

``SILKSCREEN_PROVIDER`` overrides the ladder: ``claude``, ``gemini``, or an
ordered comma list (``gemini,claude`` leads with Gemini and falls back to
Claude). Naming a provider that is not configured is an error that says what
to set -- an explicit request is never silently dropped. ``auto`` or empty is
the default ladder.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from .claude import (
    API_KEY_ENV_VAR,
    CLAUDE_CHEAP_MODEL,
    VERTEX_PROJECT_ENV_VAR,
    VERTEX_REGION_ENV_VAR,
    ClaudeModel,
    claude_configured,
    claude_missing,
)
from .model import CHEAP_MODEL, GeminiModel, Model, ModelError

__all__ = [
    "PROVIDER_ENV_VAR",
    "PROVIDERS",
    "NoProviderConfigured",
    "gemini_configured",
    "provider_order",
    "worker_model",
]

PROVIDER_ENV_VAR = "SILKSCREEN_PROVIDER"

#: The default order. Claude leads because it is the primary provider; Gemini
#: follows as the automatic fallback.
PROVIDERS = ("claude", "gemini")


class NoProviderConfigured(ModelError):
    """No model provider has credentials in this environment."""


def _text(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def gemini_configured(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(_text(env, "GOOGLE_API_KEY") or _text(env, "GEMINI_API_KEY"))


def _configured(provider: str, env: Mapping[str, str]) -> bool:
    return claude_configured(env) if provider == "claude" else gemini_configured(env)


def _missing(provider: str, env: Mapping[str, str]) -> str:
    return claude_missing(env) if provider == "claude" else "set GOOGLE_API_KEY"


def provider_order(env: Mapping[str, str] | None = None) -> list[str]:
    """The providers to use, first to last. Never empty; raises instead."""
    env = os.environ if env is None else env
    raw = _text(env, PROVIDER_ENV_VAR).lower()
    if raw in ("", "auto"):
        order = [p for p in PROVIDERS if _configured(p, env)]
        if not order:
            raise NoProviderConfigured(
                "no model provider is configured: set "
                f"{API_KEY_ENV_VAR} (or {VERTEX_PROJECT_ENV_VAR} and "
                f"{VERTEX_REGION_ENV_VAR} for Claude on Vertex AI) to use "
                "Claude, or GOOGLE_API_KEY to use Gemini. The service does not "
                "read .env; export them."
            )
        return order
    names = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown or not names:
        raise ModelError(
            f"{PROVIDER_ENV_VAR}={raw!r} must be 'auto', 'claude', 'gemini', or "
            "an ordered comma list of those"
        )
    order = list(dict.fromkeys(names))
    missing = [p for p in order if not _configured(p, env)]
    if missing:
        raise NoProviderConfigured(
            f"{PROVIDER_ENV_VAR}={raw!r} names "
            + "; ".join(f"{p}, which is not configured ({_missing(p, env)})" for p in missing)
        )
    return order


def worker_model(
    model: str | None = None,
    *,
    cheap: bool = False,
    env: Mapping[str, str] | None = None,
) -> Model:
    """The worker model for a CLI-style caller.

    The full failover ladder (:func:`~silkscreen.agents.resilience.
    default_chain`), led by ``model``'s provider when one is named; with
    ``cheap``, each configured provider's cheap tier in provider order.
    """
    from .resilience import FallbackModel, Provider, default_chain

    if not cheap:
        return default_chain(model)
    env = os.environ if env is None else env
    rungs = [
        Provider(f"{p}-cheap", ClaudeModel(CLAUDE_CHEAP_MODEL, env=env))
        if p == "claude"
        else Provider(f"{p}-cheap", GeminiModel(CHEAP_MODEL))
        for p in provider_order(env)
    ]
    return rungs[0].model if len(rungs) == 1 else FallbackModel(providers=rungs)
