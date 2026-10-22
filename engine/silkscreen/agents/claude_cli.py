"""Claude through a logged-in Claude Code: the ``cli`` backend of ``ClaudeModel``.

The ``cli`` backend. The request is the same Messages shape the ``api``
backend sends; the transport is the Claude Agent SDK, which drives the
``claude`` binary over ``stream-json`` and bills whatever that binary is
signed in as -- a claude.ai subscription after ``claude auth login``, or an
API key when ``ANTHROPIC_API_KEY`` is exported for it. That is what makes this
the developer's backend: a laptop with a subscription and no key runs the
pipeline without opening an API org. It is chosen only when
``SILKSCREEN_CLAUDE_BACKEND=cli`` names it, never inferred from a login on
disk, for the same reason Bedrock is never inferred from AWS credentials: a
paid run must not quietly change who pays.

Prior art, read at source:

* **Spawning the CLI.** ``claude_agent_sdk/_internal/transport/
  subprocess_cli.py::SubprocessCLITransport._build_command`` (0.2.171) is the
  only supported way to drive Claude Code from a program, so this module uses
  the SDK's :func:`~claude_agent_sdk.query` rather than re-implementing the
  argv. The option-to-flag mapping used here is the SDK's own:
  ``system_prompt`` -> ``--system-prompt`` (a plain string *replaces* Claude
  Code's prompt; ``None`` would send ``--system-prompt ""``), ``tools=[]`` ->
  ``--tools ""``, ``thinking={"type": "adaptive"}`` -> ``--thinking adaptive``,
  ``effort`` -> ``--effort``, ``setting_sources=[]`` so no ``CLAUDE.md`` or
  ``settings.json`` on the machine leaks into a board design (the SDK's
  ``None`` means ``["user", "project"]``, ``_configure_sdk_defaults``).
* **Content blocks.** The streaming-input form of ``query`` takes a user
  message whose ``content`` is a Messages API block list, so the ``document``
  and ``image`` blocks :meth:`ClaudeModel._content` already builds go through
  unchanged (measured 2026-10-21: a base64 PDF block was read correctly).
* **Sync over async.** :meth:`ClaudeModel.generate` is synchronous and may be
  called from inside a running event loop (the service), where
  ``asyncio.run`` raises. Running the coroutine on a one-thread executor with
  its own loop is what LiteLLM does (``litellm/litellm_core_utils/
  asyncify.py::run_async_function``) and what ``anyio.from_thread`` wraps.
* **Error words.** The result is folded into the repo's one
  :class:`~silkscreen.agents.model.ModelError` with the Anthropic API's own
  type word in the text, so :mod:`silkscreen.agents.resilience` parks the
  rung on ``rate_limit_error`` exactly as it does for the ``api`` backend.

The CLI has no ``max_tokens`` argument; the SDK's ``env`` carries
``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` instead, which is the knob Claude Code itself
reads for the same number.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import tempfile
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any

from .model import ModelError

__all__ = [
    "AgentSdkClient",
    "CREDENTIAL_ENV_VARS",
    "MAX_OUTPUT_TOKENS_ENV_VAR",
    "agent_sdk_available",
]

#: Claude Code's own ceiling on one answer; the SDK passes it through ``env``.
MAX_OUTPUT_TOKENS_ENV_VAR = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"

#: Blanked in the child's environment. The ``claude`` binary prefers an API
#: key or auth token in its environment over its own login, so a stale
#: ``ANTHROPIC_API_KEY`` in the parent (``.env`` is loaded wholesale) would
#: silently move the bill off the subscription this backend was chosen for,
#: or fail with "Invalid API key" (measured 2026-10-21). The SDK merges
#: ``options.env`` over ``os.environ`` (``subprocess_cli.py::connect``) and
#: cannot drop a key, so an empty value is the lever; Claude Code treats an
#: empty variable as unset.
CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

_INSTALL_HINT = (
    "pip install -e '.[claude-cli]' and sign in with `claude auth login` "
    "(or export ANTHROPIC_API_KEY for the claude binary)"
)


def agent_sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def _run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run ``coro`` to completion from synchronous code, loop or no loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class _Stream:
    """The ``with messages.stream(**kwargs) as s: s.get_final_message()`` shape."""

    def __init__(self, run: Callable[[], Any]):
        self._run = run

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._run()


class _Messages:
    def __init__(self, client: AgentSdkClient):
        self._client = client

    def stream(self, **kwargs: Any) -> _Stream:
        return _Stream(lambda: self._client.complete(kwargs))


class AgentSdkClient:
    """Quacks like ``anthropic.Anthropic`` for the one call ``ClaudeModel`` makes.

    ``query`` is the test seam: anything with the Agent SDK's
    ``query(prompt=..., options=...)`` signature yielding its message types.
    """

    def __init__(
        self,
        *,
        timeout_s: float,
        query: Callable[..., Any] | None = None,
        cwd: str | None = None,
    ):
        if query is None:
            try:
                from claude_agent_sdk import query as sdk_query
            except ImportError as exc:  # pragma: no cover - import guard
                raise ModelError(
                    "the claude-agent-sdk package is not installed; " + _INSTALL_HINT
                ) from exc
            query = sdk_query
        self._query = query
        self.timeout_s = timeout_s
        # Not the repository: with no setting sources the CLI reads no
        # CLAUDE.md anyway, but a neutral cwd keeps that true if that default
        # ever changes.
        self.cwd = cwd or tempfile.gettempdir()
        self.messages = _Messages(self)
        # ``request_kwargs`` never sets ``betas`` on this backend; the alias
        # keeps ``generate`` total if that changes.
        self.beta = SimpleNamespace(messages=self.messages)

    # ------------------------------------------------------------ request

    def options(self, kwargs: dict[str, Any]) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        thinking = kwargs.get("thinking")
        effort = (kwargs.get("output_config") or {}).get("effort")
        return ClaudeAgentOptions(
            model=kwargs["model"],
            system_prompt=kwargs.get("system") or "",
            tools=[],
            max_turns=1,
            setting_sources=[],
            cwd=self.cwd,
            thinking={"type": "adaptive"} if thinking else {"type": "disabled"},
            effort=effort,
            env={
                MAX_OUTPUT_TOKENS_ENV_VAR: str(kwargs["max_tokens"]),
                **{name: "" for name in CREDENTIAL_ENV_VARS},
            },
        )

    def complete(self, kwargs: dict[str, Any]) -> Any:
        model = kwargs["model"]
        try:
            return _run_sync(
                asyncio.wait_for(self._complete(kwargs), timeout=self.timeout_s)
            )
        except ModelError:
            raise
        except TimeoutError as exc:
            raise ModelError(
                f"{model}: request to Claude Code timed out after "
                f"{self.timeout_s:.0f} s"
            ) from exc
        except Exception as exc:
            raise ModelError(
                f"{model}: Claude Code could not be run ({type(exc).__name__}: "
                f"{str(exc)[:300]}); {_INSTALL_HINT}"
            ) from exc

    async def _complete(self, kwargs: dict[str, Any]) -> Any:
        from claude_agent_sdk import AssistantMessage, ResultMessage

        model = kwargs["model"]
        messages = kwargs["messages"]

        async def prompt() -> Any:
            for message in messages:
                yield {"type": "user", "message": message}

        content: list[Any] = []
        stop_reason: str | None = None
        result: Any = None
        failure: ModelError | None = None
        # Drain the stream even after an error: breaking out of the generator
        # early leaves the CLI subprocess to be reaped by garbage collection
        # after the loop has closed ("Event loop is closed" from
        # ``BaseSubprocessTransport.__del__``, seen 2026-10-21 on an auth
        # failure). Only the first error is kept.
        async for message in self._query(prompt=prompt(), options=self.options(kwargs)):
            if isinstance(message, AssistantMessage):
                content = list(message.content)
                stop_reason = message.stop_reason or stop_reason
                if message.error and failure is None:
                    failure = self._error(model, str(message.error), message.content)
            elif isinstance(message, ResultMessage):
                result = message
                stop_reason = message.stop_reason or stop_reason
        if failure is not None:
            raise failure
        if result is None:
            raise ModelError(f"{model}: Claude Code ended without a result message")
        if result.is_error:
            raise self._error(model, result.subtype, [result.result or ""])
        blocks = [
            SimpleNamespace(type="text", text=block.text)
            for block in content
            if hasattr(block, "text")
        ]
        if not blocks and result.result:
            blocks = [SimpleNamespace(type="text", text=result.result)]
        return SimpleNamespace(
            content=blocks, stop_reason=stop_reason, usage=result.usage
        )

    @staticmethod
    def _error(model: str, kind: str, content: Any) -> ModelError:
        """One ``ModelError`` carrying the API's type word when the text has it."""
        detail = " ".join(
            (b if isinstance(b, str) else getattr(b, "text", "")) for b in content
        ).strip()[:300]
        lowered = f"{kind} {detail}".lower()
        if any(w in lowered for w in ("rate limit", "rate_limit", "usage limit")):
            word = "rate_limit_error"
        elif "overloaded" in lowered:
            word = "overloaded_error"
        elif any(w in lowered for w in ("authentication", "not logged in", "login")):
            word = "authentication_error"
        elif kind == "error_max_turns":
            word = "max_turns"
        else:
            word = kind or "error"
        return ModelError(f"{model}: {word} from Claude Code -- {detail or kind}")
