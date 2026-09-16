"""Claude behind the :class:`~silkscreen.agents.model.Model` seam.

Two backends, one class, because the request is the same Messages API either
way and only the client differs:

``api``
    The Anthropic API, ``anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)``.
``vertex``
    Claude on Google Cloud Vertex AI, ``anthropic.AnthropicVertex(project_id=
    ANTHROPIC_VERTEX_PROJECT_ID, region=CLOUD_ML_REGION)``, authenticated with
    Application Default Credentials so GCP credits pay. Those two variable
    names are the SDK's own (``anthropic/lib/vertex/_client.py``: ``project_id``
    falls back to ``ANTHROPIC_VERTEX_PROJECT_ID``, ``region`` to
    ``CLOUD_ML_REGION``), not names invented here.

Prior art this follows, read at source rather than from docs:

* **Document blocks.** A PDF goes in as ``{"type": "document", "source":
  {"type": "base64", "media_type": "application/pdf", ...}}`` *before* the
  text block -- the shape google-adk's own adapter emits
  (``google/adk/models/anthropic_llm.py::_part_to_message_block``, the
  ``_is_pdf_part`` branch) and pydantic-ai's
  (``pydantic_ai_slim/pydantic_ai/models/anthropic.py::_map_binary_data``).
  Images and plain text follow the same two files.
* **Error taxonomy.** pydantic-ai maps ``APIStatusError`` by status and
  ``APIConnectionError`` separately (``models/anthropic.py::_map_api_errors``);
  this module does the same, most-specific class first, but ends every branch
  in the repo's one :class:`ModelError` and puts the API's own error *type*
  word (``rate_limit_error``, ``overloaded_error``...) in the message, because
  :mod:`silkscreen.agents.resilience` decides failover from the words.
* **Sampling parameters.** google-adk drops ``temperature``/``top_p``/``top_k``
  whenever thinking or effort is set (``anthropic_llm.py::
  _build_anthropic_kwargs``, ``exclude_sampling``). The ``anthropic`` 1.x SDK
  has removed ``temperature`` from ``messages.stream`` altogether, and Claude
  Opus 5 / Sonnet 5 reject it with a 400, so the ``temperature`` argument of
  :meth:`ClaudeModel.generate` is accepted for protocol compatibility and not
  sent. Stated here so nobody concludes the pipeline is deterministic on
  Claude because it passes ``0.0``.

Retries belong to :class:`~silkscreen.agents.resilience.FallbackModel`, so the
SDK's own retry loop is switched off (``max_retries=0``). Two owners of one
retry policy multiply: the SDK's default of two retries under a chain that
already makes two attempts per rung is six requests before failover, each one
potentially billed.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from typing import Any

from .model import CHEAP_MODEL, Document, ModelError, request_timeout_ms

__all__ = [
    "CLAUDE_MODEL",
    "CLAUDE_CHEAP_MODEL",
    "CLAUDE_MODEL_ENV_VAR",
    "CLAUDE_EFFORT_ENV_VAR",
    "CLAUDE_BACKEND_ENV_VAR",
    "API_KEY_ENV_VAR",
    "VERTEX_PROJECT_ENV_VAR",
    "VERTEX_REGION_ENV_VAR",
    "THINKING_HEADROOM_TOKENS",
    "ClaudeModel",
    "claude_backend",
    "claude_configured",
    "claude_missing",
    "claude_primary_model",
    "is_claude_model",
    "supports_effort",
]

#: The reasoning tier. Claude Opus 5 thinks adaptively by default, reads PDFs
#: natively (a datasheet's pinout table is a picture, the reason
#: ``DEFAULT_MODEL`` was chosen for vision in the first place), and has a 1M
#: context window.
CLAUDE_MODEL = "claude-opus-5"

#: The cheap tier, where :func:`silkscreen.agents.effort.cheap_sibling` moves
#: the optional lanes. Sonnet 5 rather than Haiku 4.5: it keeps adaptive
#: thinking and effort (Haiku 4.5 takes neither), so the same request shape
#: serves both tiers.
CLAUDE_CHEAP_MODEL = "claude-sonnet-5"

#: Environment override for the Claude reasoning tier. Separate from
#: ``SILKSCREEN_MODEL`` on purpose: that one names a *Gemini* id and is read by
#: ``GeminiModel()`` directly, so a Claude id there would be sent to the Gemini
#: API.
CLAUDE_MODEL_ENV_VAR = "SILKSCREEN_CLAUDE_MODEL"

#: ``low`` / ``medium`` / ``high`` / ``xhigh`` / ``max``. Unset means the
#: per-tier default in :data:`_DEFAULT_EFFORT`.
CLAUDE_EFFORT_ENV_VAR = "SILKSCREEN_CLAUDE_EFFORT"

#: ``api`` or ``vertex``; unset chooses (see :func:`claude_backend`).
CLAUDE_BACKEND_ENV_VAR = "SILKSCREEN_CLAUDE_BACKEND"
#: Claude on Amazon Bedrock (``anthropic.AnthropicBedrock``): credentials come
#: from botocore's own chain (environment, ``~/.aws``, an instance role), the
#: region from ``AWS_REGION``. Chosen only when :data:`CLAUDE_BACKEND_ENV_VAR`
#: says ``bedrock``, never inferred: AWS credentials are present on most
#: developer machines for unrelated reasons, and a paid run must not move its
#: bill to a different account because a profile happened to exist.
BEDROCK_REGION_ENV_VAR = "AWS_REGION"
#: Overrides the Bedrock model id sent on the wire for the primary tier; the
#: default is the cross-region inference profile ``global.anthropic.<id>``,
#: which is how Bedrock names ``claude-opus-5``/``claude-sonnet-5`` (listed by
#: ``aws bedrock list-inference-profiles``, 2026-09-15).
BEDROCK_MODEL_ENV_VAR = "AWS_BEDROCK_CLAUDE_MODEL"
BEDROCK_PROFILE_PREFIX = "global.anthropic."

API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
VERTEX_PROJECT_ENV_VAR = "ANTHROPIC_VERTEX_PROJECT_ID"
VERTEX_REGION_ENV_VAR = "CLOUD_ML_REGION"

_EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: Effort per tier when ``SILKSCREEN_CLAUDE_EFFORT`` is blank. ``medium`` on
#: the reasoning tier, not the API's default of ``high``: every answer this
#: package asks for is gated by a validator (``parse_circuit_spec``,
#: ``verify.circuit.electrical_completeness``, ERC), so a cheaper answer is
#: caught and repaired rather than shipped, and ``high`` was measured at
#: 670 s for one propose call on 2026-09-16 -- past every client's ceiling.
#: The cheap tier one step down again, since the stages moved there are the
#: mechanical ones.
_DEFAULT_EFFORT = {CLAUDE_MODEL: "medium", CLAUDE_CHEAP_MODEL: "low"}

#: Tokens added to the caller's ``max_output_tokens`` for thinking.
#:
#: Claude counts thinking tokens inside ``max_tokens`` -- the same trap
#: ``GeminiModel._reject_truncation`` documents for Gemini 3 -- and every
#: budget in this package was sized for the *answer*. Without headroom a hard
#: prompt spends an 8192-token budget thinking and returns half a JSON object.
#: Streaming (below) is what makes the larger number safe: the SDK refuses a
#: non-streaming request whose ``max_tokens`` implies more than ten minutes.
THINKING_HEADROOM_TOKENS = 16_000

#: Ceiling on ``max_tokens`` after headroom. This is the only wall clock a
#: streaming call has -- the SDK timeout resets on every chunk and the API
#: pings during thinking -- so it is sized for the answer, not the model's
#: 128K: a validated netlist JSON is a few thousand tokens and the largest
#: plan a few more, and the headroom above covers the thinking. 64 000 let
#: one propose call run 670 s (2026-09-16); at 24 000 the worst case is a
#: few minutes, and a truncated answer still raises ``max_tokens`` loudly.
_MAX_TOKENS_CEILING = 24_000

#: Server-side refusal fallback, first-party API only (not on Vertex). On a
#: policy decline the API re-runs the same request on a model chosen by the
#: refusal category, inside the same call, instead of stopping with
#: ``stop_reason: "refusal"``. Anthropic's guidance is to enable it for Claude
#: Opus 5; a refusal that survives it still raises :class:`ModelError` below
#: and the chain moves on to Gemini.
_REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_REFUSAL_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-")


def _text(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def is_claude_model(model_id: object) -> bool:
    """True for an Anthropic model id (``claude-opus-5``, ``claude-sonnet-5``)."""
    return isinstance(model_id, str) and model_id.strip().lower().startswith("claude-")


def claude_primary_model(env: Mapping[str, str] | None = None) -> str:
    """:data:`CLAUDE_MODEL_ENV_VAR` when set, else :data:`CLAUDE_MODEL`."""
    env = os.environ if env is None else env
    return _text(env, CLAUDE_MODEL_ENV_VAR) or CLAUDE_MODEL


def claude_backend(env: Mapping[str, str] | None = None) -> str | None:
    """``"api"``, ``"vertex"``, or ``None`` when Claude is not configured.

    An explicit :data:`CLAUDE_BACKEND_ENV_VAR` wins. Otherwise a *complete*
    Vertex configuration (project **and** region) wins over an API key: naming
    a GCP project and region is a deliberate act for exactly this purpose,
    whereas ``ANTHROPIC_API_KEY`` is commonly exported for other tools and
    would otherwise silently move the bill off GCP credits.

    Only environment variables count. The SDK can also find an ``ant auth
    login`` profile on disk (google-adk's ``AnthropicLlm._anthropic_client``
    defers to that resolution); this deliberately does not, because a stray
    profile must not switch which provider a paid run bills.
    """
    env = os.environ if env is None else env
    forced = _text(env, CLAUDE_BACKEND_ENV_VAR).lower()
    has_vertex = bool(_text(env, VERTEX_PROJECT_ENV_VAR) and _text(env, VERTEX_REGION_ENV_VAR))
    has_key = bool(_text(env, API_KEY_ENV_VAR))
    if forced == "vertex":
        return "vertex" if has_vertex else None
    if forced == "api":
        return "api" if has_key else None
    if forced == "bedrock":
        return "bedrock" if _text(env, BEDROCK_REGION_ENV_VAR) else None
    if has_vertex:
        return "vertex"
    if has_key:
        return "api"
    return None


def claude_configured(env: Mapping[str, str] | None = None) -> bool:
    return claude_backend(env) is not None


def claude_missing(env: Mapping[str, str] | None = None) -> str:
    """One sentence naming exactly what to set, for a refusal in words."""
    env = os.environ if env is None else env
    forced = _text(env, CLAUDE_BACKEND_ENV_VAR).lower()
    if forced == "vertex" or (
        forced != "api"
        and bool(_text(env, VERTEX_PROJECT_ENV_VAR)) != bool(_text(env, VERTEX_REGION_ENV_VAR))
    ):
        return (
            f"Claude on Vertex AI needs both {VERTEX_PROJECT_ENV_VAR} and "
            f"{VERTEX_REGION_ENV_VAR} (e.g. 'global'), plus Application Default "
            "Credentials (gcloud auth application-default login)"
        )
    if forced == "api":
        return f"{CLAUDE_BACKEND_ENV_VAR}=api needs {API_KEY_ENV_VAR}"
    if forced == "bedrock":
        return (
            f"{CLAUDE_BACKEND_ENV_VAR}=bedrock needs {BEDROCK_REGION_ENV_VAR} and AWS "
            "credentials botocore can find (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
            "~/.aws/credentials, or an IAM role)"
        )
    return (
        f"set {API_KEY_ENV_VAR} for the Anthropic API, or {VERTEX_PROJECT_ENV_VAR} "
        f"and {VERTEX_REGION_ENV_VAR} for Claude on Vertex AI"
    )


def supports_effort(model_id: str) -> bool:
    """Adaptive thinking and ``effort`` exist on the 4.6+ families; Haiku 4.5
    and the Claude 3 line take neither and 400 on ``effort``."""
    lowered = model_id.lower()
    return not (lowered.startswith("claude-3") or "haiku" in lowered)


def _effort_for(model_id: str, env: Mapping[str, str]) -> str:
    raw = _text(env, CLAUDE_EFFORT_ENV_VAR).lower()
    if raw:
        if raw not in _EFFORTS:
            raise ModelError(
                f"{CLAUDE_EFFORT_ENV_VAR}={raw!r} must be one of {', '.join(_EFFORTS)}"
            )
        return raw
    return _DEFAULT_EFFORT.get(model_id, "high")


class ClaudeModel:
    """The live Claude path. Needs ``ANTHROPIC_API_KEY`` or a Vertex project.

    ``client`` is the test seam (the ``genai.Client`` monkeypatch in
    ``test_live_model.py`` does the same job for Gemini): anything with a
    ``messages.stream(**kwargs)`` context manager, and ``beta.messages.stream``
    when the refusal fallback applies.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        backend: str | None = None,
        api_key: str | None = None,
        project_id: str | None = None,
        region: str | None = None,
        timeout_s: float | None = None,
        effort: str | None = None,
        client: Any = None,
        env: Mapping[str, str] | None = None,
    ):
        env = os.environ if env is None else env
        self.model = model or claude_primary_model(env)
        if not is_claude_model(self.model):
            raise ModelError(f"ClaudeModel was given a non-Claude model id {self.model!r}")
        timeout_ms = request_timeout_ms(timeout_s)
        self.timeout_s = timeout_ms / 1000.0
        if effort is not None and effort not in _EFFORTS:
            raise ModelError(f"effort {effort!r} must be one of {', '.join(_EFFORTS)}")
        self.effort = (
            (effort or _effort_for(self.model, env)) if supports_effort(self.model) else None
        )

        if backend is None:
            if api_key:
                backend = "api"
            elif project_id and region:
                backend = "vertex"
            else:
                backend = claude_backend(env)
        if backend not in ("api", "vertex", "bedrock"):
            raise ModelError(f"Claude is not configured: {claude_missing(env)}.")
        self.backend = backend
        # Kept for :meth:`for_tier`, which must build a sibling without
        # re-reading an environment the caller may never have populated.
        self._api_key = api_key or _text(env, API_KEY_ENV_VAR)
        self._project_id = project_id or _text(env, VERTEX_PROJECT_ENV_VAR)
        self._region = region or _text(env, VERTEX_REGION_ENV_VAR)
        self._aws_region = _text(env, BEDROCK_REGION_ENV_VAR)
        self._bedrock_override = _text(env, BEDROCK_MODEL_ENV_VAR)
        self._env = env
        self._client = client if client is not None else self._build_client()

    def _build_client(self) -> Any:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - import guard
            raise ModelError(
                "the anthropic package is not installed; "
                "pip install -e '.[anthropic]'"
            ) from exc
        if self.backend == "api":
            if not self._api_key:
                raise ModelError(f"Claude is not configured: {claude_missing(self._env)}.")
            return anthropic.Anthropic(
                api_key=self._api_key, timeout=self.timeout_s, max_retries=0
            )
        if self.backend == "bedrock":
            if not self._aws_region:
                raise ModelError(
                    f"Claude is not configured: {claude_missing(self._env)}."
                )
            try:
                return anthropic.AnthropicBedrock(
                    aws_region=self._aws_region, timeout=self.timeout_s, max_retries=0
                )
            except Exception as exc:  # botocore missing or no credentials
                raise ModelError(
                    f"Claude on Bedrock could not be set up ({type(exc).__name__}: "
                    f"{exc}); pip install 'anthropic[bedrock]' and configure AWS "
                    "credentials (aws configure)"
                ) from exc
        if not (self._project_id and self._region):
            raise ModelError(f"Claude is not configured: {claude_missing(self._env)}.")
        try:
            return anthropic.AnthropicVertex(
                project_id=self._project_id,
                region=self._region,
                timeout=self.timeout_s,
                max_retries=0,
            )
        except Exception as exc:  # google-auth missing or no credentials
            raise ModelError(
                f"Claude on Vertex AI could not be set up ({type(exc).__name__}: "
                f"{exc}); install the extra (pip install -e '.[anthropic]') and "
                "run gcloud auth application-default login"
            ) from exc

    @property
    def wire_model(self) -> str:
        """The model id the backend is actually sent.

        The API and Vertex take the logical id (``claude-opus-5``). Bedrock
        names the same model by an inference profile, ``global.anthropic.
        claude-opus-5``, so the logical id stays the one every table in this
        package keys on (effort, tiers, ``is_claude_model``) and the mapping
        happens here alone. :data:`BEDROCK_MODEL_ENV_VAR` overrides the
        primary tier's profile (a regional ``us.`` profile, a provisioned one).
        """
        if self.backend != "bedrock":
            return self.model
        if self._bedrock_override and self.model == claude_primary_model(self._env):
            return self._bedrock_override
        return f"{BEDROCK_PROFILE_PREFIX}{self.model}"

    def for_tier(self, model: str) -> ClaudeModel | None:
        """The same backend on another tier, or ``None`` if nothing moves.

        :func:`silkscreen.agents.effort.cheap_sibling` asks every rung for
        ``CHEAP_MODEL``, which is a *Gemini* id. On this rung that request
        means "the cheap tier", so it maps to :data:`CLAUDE_CHEAP_MODEL`; any
        other non-Claude id answers ``None`` rather than sending a Gemini id
        to Anthropic.
        """
        target = model if is_claude_model(model) else (
            CLAUDE_CHEAP_MODEL if model == CHEAP_MODEL else None
        )
        if target is None or target == self.model:
            return None
        return ClaudeModel(
            target,
            backend=self.backend,
            api_key=self._api_key or None,
            project_id=self._project_id or None,
            region=self._region or None,
            timeout_s=self.timeout_s,
            env=self._env,
        )

    # ------------------------------------------------------------ request

    def _content(self, prompt: str, documents: list[Document] | None) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for doc in documents or []:
            blocks.append(self._document_block(doc))
        if blocks:
            # One breakpoint after the documents: a datasheet is thousands of
            # tokens and the propose/repair loop asks about it more than once
            # with a different question each time. Marking the *end* of the
            # prompt instead would cache a prefix no later request shares.
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        blocks.append({"type": "text", "text": prompt})
        return blocks

    def _document_block(self, doc: Document) -> dict[str, Any]:
        mime = (doc.mime_type or "application/pdf").split(";", 1)[0].strip().lower()
        if doc.data is None:
            # ``Document.url`` is a provider-fetched link. The Anthropic API
            # accepts a ``url`` source; Vertex does not document one, and the
            # repo's own path downloads datasheets and sends bytes anyway.
            if self.backend == "vertex":
                raise ModelError(
                    f"{self.model} on Vertex AI cannot fetch a document URL; "
                    "download it and pass Document(data=...) "
                    "(agents.datasheet.read_datasheet does)"
                )
            if mime != "application/pdf":
                raise ModelError(f"{self.model} cannot read a {mime} document by URL")
            return {"type": "document", "source": {"type": "url", "url": doc.url}}
        data = base64.standard_b64encode(doc.data).decode("ascii")
        if mime == "application/pdf":
            return {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            }
        if mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data},
            }
        if mime == "text/plain":
            return {
                "type": "document",
                "source": {
                    "type": "text",
                    "media_type": "text/plain",
                    "data": doc.data.decode("utf-8", errors="replace"),
                },
            }
        raise ModelError(f"{self.model} cannot read a {mime} document")

    def request_kwargs(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        max_output_tokens: int = 8192,
    ) -> dict[str, Any]:
        """The exact keyword arguments sent to ``messages.stream``.

        Public so a test can pin the request shape without a network.
        """
        kwargs: dict[str, Any] = {
            "model": self.wire_model,
            "messages": [{"role": "user", "content": self._content(prompt, documents)}],
        }
        if system:
            kwargs["system"] = system
        if self.effort is not None:
            kwargs["max_tokens"] = min(
                max_output_tokens + THINKING_HEADROOM_TOKENS, _MAX_TOKENS_CEILING
            )
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self.effort}
        else:
            kwargs["max_tokens"] = min(max_output_tokens, _MAX_TOKENS_CEILING)
        if self.backend == "api" and self.model.startswith(_REFUSAL_FALLBACK_MODELS):
            kwargs["betas"] = [_REFUSAL_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def generate(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        del temperature  # not sent; see the module docstring
        kwargs = self.request_kwargs(
            prompt,
            documents=documents,
            system=system,
            max_output_tokens=max_output_tokens,
        )
        messages = (
            self._client.beta.messages if "betas" in kwargs else self._client.messages
        )
        try:
            # Streaming for the timeout's sake, not for tokens: the deadline
            # becomes a stall detector between chunks, so a long thinking turn
            # that keeps producing is not cut off at 60 s, while a hung
            # upstream still raises and lets the chain fail over.
            with messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except ModelError:
            raise
        except Exception as exc:
            raise _map_error(self.model, self.backend, exc) from exc
        return self._text_of(message, kwargs["max_tokens"])

    def _text_of(self, message: Any, budget: int) -> str:
        stop = getattr(message, "stop_reason", None)
        if stop == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None)
            raise ModelError(
                f"{self.model} declined the request (stop_reason refusal"
                f"{', category ' + str(category) if category else ''}); "
                "no answer was produced"
            )
        text = "".join(
            getattr(block, "text", "")
            for block in getattr(message, "content", None) or []
            if getattr(block, "type", None) == "text"
        )
        if stop == "max_tokens":
            # The GeminiModel._reject_truncation rule: a fragment is not an
            # answer, and every caller parses this string as JSON.
            raise ModelError(
                f"{self.model} hit its {budget}-token output budget and the "
                f"answer is truncated ({len(text)} characters, cut mid-answer). "
                "Thinking tokens count against the same budget; raise "
                "max_output_tokens or lower "
                f"{CLAUDE_EFFORT_ENV_VAR}."
            )
        if not text.strip():
            raise ModelError(f"{self.model} returned an empty response")
        return text


def _map_error(model: str, backend: str, exc: Exception) -> ModelError:
    """One :class:`ModelError` per failure, carrying the API's own type word.

    The words are the contract with :mod:`silkscreen.agents.resilience`:
    ``rate_limit_error`` is in its ``PROVIDER_DOWN_MARKERS`` (stop spending
    attempts, park the rung, fail over now) and ``retry in Ns`` is what its
    ``retry_delay_s`` reads. ``overloaded_error`` and 5xx are deliberately not
    markers: like Gemini's ``UNAVAILABLE``, transient load is worth the in-rung
    retry.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover - a client could not exist either
        return ModelError(f"{model} call failed: {type(exc).__name__}: {exc}")

    where = "Claude on Vertex AI" if backend == "vertex" else "the Anthropic API"
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        kind = None
        detail = str(getattr(exc, "message", "") or exc)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                kind = error.get("type")
                detail = str(error.get("message") or detail)
        detail = detail[:300]
        if isinstance(exc, anthropic.AuthenticationError):
            credential = (
                "Application Default Credentials"
                if backend == "vertex"
                else API_KEY_ENV_VAR
            )
            return ModelError(
                f"{model}: authentication_error (HTTP 401) from {where}: "
                f"{credential} was rejected -- {detail}"
            )
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ModelError(
                f"{model}: permission_error (HTTP 403) from {where}: {detail}"
            )
        if isinstance(exc, anthropic.NotFoundError):
            hint = (
                "enable the model in Vertex AI Model Garden for this project and region"
                if backend == "vertex"
                else "the key's organization cannot use this model id"
            )
            return ModelError(
                f"{model}: not_found_error (HTTP 404) from {where}: {hint} -- {detail}"
            )
        if isinstance(exc, anthropic.RateLimitError):
            retry_after = None
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", None)
            if headers is not None:
                retry_after = headers.get("retry-after")
            wait = ""
            try:
                if retry_after is not None and float(retry_after) > 0:
                    wait = f"; retry in {float(retry_after):g}s"
            except ValueError:
                wait = ""
            return ModelError(
                f"{model}: rate_limit_error (HTTP 429) from {where}{wait} -- {detail}"
            )
        word = kind or {
            400: "invalid_request_error",
            402: "billing_error",
            413: "request_too_large",
            500: "api_error",
            529: "overloaded_error",
        }.get(status or 0, "api_error")
        return ModelError(f"{model}: {word} (HTTP {status}) from {where} -- {detail}")
    if isinstance(exc, anthropic.APITimeoutError):
        return ModelError(f"{model}: request to {where} timed out -- {exc}")
    if isinstance(exc, anthropic.APIConnectionError):
        return ModelError(f"{model}: could not reach {where} -- {exc}")
    return ModelError(f"{model} call failed: {type(exc).__name__}: {exc}")
