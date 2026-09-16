"""Model discovery for the same-origin web client: Claude and Gemini.

Each provider's API is the authority on what the current key may call.  A
small fallback catalog keeps the UI useful before a key is configured and
during a transient discovery failure; it is a fallback, not a claim that those
models are currently reachable.

Claude entries appear only when Claude is configured
(:func:`silkscreen.agents.claude.claude_backend`), so a Google-key-only
service answers exactly the catalog it always did. On the Anthropic API they
come from the Models API (``client.models.list()``); Claude on Vertex AI has
no Models API (Anthropic's platform-availability table lists it as not
supported there), so Vertex gets the configured tiers, labelled
``source: "claude-configured"`` rather than passed off as discovered.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from silkscreen.agents.model import CHEAP_MODEL, FALLBACK_MODEL, primary_model

__all__ = [
    "is_chat_model",
    "meets_version_floor",
    "model_catalog",
    "select_model",
    "select_quota_rpm",
    "select_thinking_level",
]

QUOTA_RPM_OPTIONS = frozenset({3, 6, 15})

_CACHE_TTL_S = 15 * 60
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache: dict[str, Any] | None = None
_PRO_ORCHESTRATOR_MODEL = "gemini-3.1-pro-preview"

#: The hackathon's model floor: Gemini 3.5 or newer. The worker chain meets it
#: by construction (``DEFAULT_MODEL``/``CHEAP_MODEL``), but the root model is
#: user-selectable, so the floor is enforced where the selection is made. A
#: model below it is still listed -- marked ``legacy`` -- because hiding it
#: would make a stale catalog look like a discovery failure.
_ROOT_VERSION_FLOOR = (3, 5)
_LEGACY_OPT_IN = "SILKSCREEN_ALLOW_LEGACY_MODELS"
_VERSION = re.compile(r"^gemini-(\d+)\.(\d+)")


def _version(model_id: str) -> tuple[int, int] | None:
    match = _VERSION.match(model_id)
    return (int(match[1]), int(match[2])) if match else None


def meets_version_floor(model_id: str) -> bool:
    """Is this model at or above the Gemini 3.5 floor?

    An id that carries no ``gemini-<major>.<minor>`` prefix cannot be
    classified and is passed through: the catalog only ever lists
    ``generateContent`` Gemini models, and refusing an unfamiliar id would
    turn a naming change on Google's side into an outage here.
    """
    version = _version(model_id)
    return version is None or version >= _ROOT_VERSION_FLOOR


#: Name tokens of models that answer ``generateContent`` but cannot run a text
#: conversation with function calling, which is all the orchestrator root and
#: the worker ever ask for.
#:
#: The key's model list does not say this: TTS, "Nano Banana" image models,
#: transcription, computer-use and robotics-ER all advertise
#: ``generateContent`` (measured against the demo key, 2026-09-13), so the
#: retry panel offered forty-odd models of which a dozen could only fail.
#: LiteLLM draws the same line with a per-model ``mode`` in
#: ``model_prices_and_context_window.json`` -- ``image_generation`` for
#: ``gemini/gemini-3.1-flash-image``, ``audio_speech`` for
#: ``gemini/gemini-3.1-flash-tts-preview``, ``audio_transcription`` for
#: ``gemini/gemini-3.5-transcribe``, ``realtime`` for the ``-live`` models --
#: but keyed by exact id, which goes stale on every release; the tokens below
#: are that taxonomy applied to the name. Two further tokens are ours:
#: ``computer-use`` and ``robotics`` are listed as ``chat`` there, and each
#: expects its own tool protocol (screen actions, embodied pointing) rather
#: than an ordinary function declaration.
_NON_CHAT_TOKENS = (
    "tts",
    "image",
    "transcribe",
    "live",
    "native-audio",
    "embedding",
    "computer-use",
    "robotics",
)


def is_chat_model(model_id: str) -> bool:
    """Can this model hold a text conversation with tools?"""
    parts = model_id.lower()
    return not any(token in parts for token in _NON_CHAT_TOKENS)


def _legacy_allowed() -> bool:
    return os.getenv(_LEGACY_OPT_IN, "").strip() == "1"


def _fallback(reason: str = "") -> dict[str, Any]:
    models = []
    for model_id, label in (
        (primary_model(), "Default Gemini model"),
        (FALLBACK_MODEL, "Fallback Gemini model"),
        (_PRO_ORCHESTRATOR_MODEL, "Reasoning Gemini orchestrator"),
        (CHEAP_MODEL, "Economy Gemini model"),
    ):
        if any(item["id"] == model_id for item in models):
            continue
        models.append(
            {
                "id": model_id,
                "name": label,
                "description": "Configured by the Silkscreen service.",
                "input_token_limit": None,
                "output_token_limit": None,
                "thinking": None,
                "legacy": not meets_version_floor(model_id),
            }
        )
    return {
        "default": "auto",
        "auto_model": os.getenv("SILKSCREEN_ORCHESTRATOR_MODEL") or primary_model(),
        "source": "fallback",
        "models": models,
        **({"warning": reason} if reason else {}),
    }


def _clean_name(value: object) -> str:
    name = str(value or "")
    return name.removeprefix("models/")


def _claude_entry(model_id: str, name: str, description: str, **limits: Any) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": name,
        "description": description,
        "input_token_limit": limits.get("input_token_limit"),
        "output_token_limit": limits.get("output_token_limit"),
        "thinking": None,
        "legacy": False,
        "provider": "claude",
    }


def _claude_configured_entries() -> list[dict[str, Any]]:
    from silkscreen.agents.claude import CLAUDE_CHEAP_MODEL, claude_primary_model

    entries: list[dict[str, Any]] = []
    for model_id, label in (
        (claude_primary_model(), "Reasoning Claude model"),
        (CLAUDE_CHEAP_MODEL, "Economy Claude model"),
    ):
        if all(item["id"] != model_id for item in entries):
            entries.append(
                _claude_entry(model_id, label, "Configured by the Silkscreen service.")
            )
    return entries


def _claude_models() -> tuple[list[dict[str, Any]], str | None, str | None]:
    """``(entries, source, warning)``; no entries when Claude is not configured."""
    from silkscreen.agents.claude import API_KEY_ENV_VAR, claude_backend

    backend = claude_backend()
    if backend is None:
        return [], None, None
    if backend == "vertex":
        return _claude_configured_entries(), "claude-configured", None
    try:
        import anthropic

        client = anthropic.Anthropic(
            api_key=os.getenv(API_KEY_ENV_VAR, "").strip(), max_retries=0, timeout=15.0
        )
        entries = []
        for raw in client.models.list():
            model_id = str(getattr(raw, "id", "") or "")
            if not model_id.startswith("claude-"):
                continue
            entries.append(
                _claude_entry(
                    model_id,
                    str(getattr(raw, "display_name", None) or model_id),
                    "",
                    input_token_limit=getattr(raw, "max_input_tokens", None),
                    output_token_limit=getattr(raw, "max_tokens", None),
                )
            )
    except Exception as exc:  # discovery must never take the UI down
        return (
            _claude_configured_entries(),
            "claude-configured",
            f"Claude model discovery failed: {type(exc).__name__}: {exc}",
        )
    if not entries:
        return _claude_configured_entries(), "claude-configured", "Claude returned no models."
    entries.sort(key=lambda item: item["id"])
    return entries, "claude", None


def _auto_model() -> str:
    """``SILKSCREEN_ORCHESTRATOR_MODEL``, else the leading provider's primary."""
    explicit = os.getenv("SILKSCREEN_ORCHESTRATOR_MODEL")
    if explicit:
        return explicit
    from silkscreen.agents.claude import claude_primary_model
    from silkscreen.agents.providers import provider_order

    try:
        order = provider_order()
    except Exception:  # noqa: BLE001 - nothing configured: the old default
        return primary_model()
    return claude_primary_model() if order[0] == "claude" else primary_model()


def _live_catalog() -> dict[str, Any]:
    """Claude's models (when configured) ahead of Gemini's (when configured).

    With Claude unconfigured this is exactly the Gemini discovery it always
    was, fallback catalog included.
    """
    claude, claude_source, claude_warning = _claude_models()
    if not claude:
        return _gemini_catalog()
    has_gemini = bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))
    try:
        gemini = _gemini_catalog() if has_gemini else None
    except Exception as exc:  # noqa: BLE001 - Claude still answers
        gemini = _fallback(f"Model discovery failed: {type(exc).__name__}: {exc}")
    warnings = [w for w in (claude_warning, (gemini or {}).get("warning")) if w]
    return {
        "default": "auto",
        "auto_model": _auto_model(),
        "source": claude_source if gemini is None else f"{claude_source}+{gemini['source']}",
        "models": claude + list((gemini or {}).get("models", [])),
        **({"warning": " ".join(warnings)} if warnings else {}),
    }


def _gemini_catalog() -> dict[str, Any]:
    from google import genai

    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        return _fallback("Gemini model discovery needs GOOGLE_API_KEY.")

    client = genai.Client(api_key=key)
    try:
        models = []
        for raw in client.models.list():
            actions = list(getattr(raw, "supported_actions", None) or [])
            if "generateContent" not in actions:
                continue
            model_id = _clean_name(getattr(raw, "name", ""))
            if not model_id.startswith("gemini-") or not is_chat_model(model_id):
                continue
            models.append(
                {
                    "id": model_id,
                    "name": str(getattr(raw, "display_name", None) or model_id),
                    "description": str(getattr(raw, "description", None) or ""),
                    "input_token_limit": getattr(raw, "input_token_limit", None),
                    "output_token_limit": getattr(raw, "output_token_limit", None),
                    "thinking": getattr(raw, "thinking", None),
                    "legacy": not meets_version_floor(model_id),
                }
            )
    finally:
        client.close()

    models.sort(key=lambda item: item["id"])
    if not models:
        return _fallback("Gemini returned no generateContent models.")
    auto_model = os.getenv("SILKSCREEN_ORCHESTRATOR_MODEL") or primary_model()
    return {
        "default": "auto",
        "auto_model": auto_model,
        "source": "gemini",
        "models": models,
    }


def model_catalog(*, refresh: bool = False) -> dict[str, Any]:
    """Available text-generation models, cached briefly per service process."""
    global _cache, _cache_at

    now = time.monotonic()
    with _cache_lock:
        if not refresh and _cache is not None and now - _cache_at < _CACHE_TTL_S:
            return _cache
        try:
            catalog = _live_catalog()
        except Exception as exc:  # discovery must never take the UI down
            catalog = _fallback(f"Model discovery failed: {type(exc).__name__}: {exc}")
        _cache = catalog
        _cache_at = now
        return catalog


def select_model(requested: object, catalog: dict[str, Any]) -> str:
    """Resolve ``auto`` or require a model advertised by this server."""
    choice = str(requested or "auto").strip()
    if not choice or choice == "auto":
        return str(catalog.get("auto_model") or primary_model())
    allowed = {str(item.get("id") or "") for item in catalog.get("models", [])}
    if choice not in allowed:
        raise ValueError(
            "'model' must be 'auto' or an available Gemini model or Claude model"
        )
    if not meets_version_floor(choice) and not _legacy_allowed():
        raise ValueError(
            f"'model' {choice} is below the Gemini 3.5 floor; choose 'auto' or a "
            f"newer model, or set {_LEGACY_OPT_IN}=1 on the service to allow it"
        )
    return choice


def select_thinking_level(requested: object) -> str | None:
    """Resolve the web control to a Gemini 3 thinking level.

    Gemini 3.1 Pro and 3.7 Flash cannot turn thinking fully off.  ``auto``
    therefore means the selected model's native default, while explicit
    choices use the three levels both models support.
    """
    choice = str(requested or "auto").strip().lower()
    if not choice or choice == "auto":
        return None
    if choice not in {"low", "medium", "high"}:
        raise ValueError("'thinking_level' must be 'auto', 'low', 'medium', or 'high'")
    return choice


def select_quota_rpm(requested: object) -> int | None:
    """Return the selected app-side request pace; ``None`` means no pacing."""
    if requested is None or requested == "":
        return None
    if isinstance(requested, str) and requested.strip().lower() == "auto":
        return None
    if isinstance(requested, bool):
        raise ValueError("'quota_rpm' must be 'auto', 3, 6, or 15")
    try:
        rpm = int(requested)
    except (TypeError, ValueError) as exc:
        raise ValueError("'quota_rpm' must be 'auto', 3, 6, or 15") from exc
    if str(requested).strip() != str(rpm) or rpm not in QUOTA_RPM_OPTIONS:
        raise ValueError("'quota_rpm' must be 'auto', 3, 6, or 15")
    return rpm
