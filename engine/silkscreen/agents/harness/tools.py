"""Tools: what the model may call, and the verifiers among them.

``Tool`` is the OpenAI Agents SDK's ``FunctionTool`` (``src/agents/tool.py``)
reduced to what this loop uses: a spec the model sees, a Python callable, and
``needs_approval``, which pauses the run rather than calling. The JSON schema
is derived from the signature the way ``function_schema.py`` does it, for the
plain types a verifier takes; anything richer passes an explicit schema.

A verifier tool is the one addition. Its callable returns a
:class:`~silkscreen.verify.Verdict`, never a string, and the runner records
the verdict under the tool's name, which is how ``Agent.required_verifiers``
can refuse to finish while one is red. Every tool takes the run's ``context``
first -- the mutable dict the loop hands around, where a verifier finds the
artifact it checks -- and the model's arguments as keywords.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

from ...verify import Verdict
from .model import ToolSpec

__all__ = [
    "Tool",
    "function_tool",
    "verifier_tool",
    "command_verifier",
    "context_verifier",
    "schema_for",
]

_JSON_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def schema_for(fn: Callable[..., Any], *, skip: int = 1) -> dict[str, Any]:
    """A JSON schema for ``fn``'s keyword parameters past the first ``skip``."""
    hints = get_type_hints(fn)
    props: dict[str, Any] = {}
    required: list[str] = []
    params = list(inspect.signature(fn).parameters.values())[skip:]
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        hint = hints.get(p.name, str)
        json_type = _JSON_TYPES.get(hint, "string")
        props[p.name] = {"type": json_type}
        if p.default is p.empty:
            required.append(p.name)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    fn: Callable[..., Any]
    #: Pause the run before calling (the OpenAI ``needs_approval`` shape). The
    #: caller resumes by answering the interruption; the loop never asks a
    #: human itself.
    needs_approval: bool = False
    #: The callable returns a :class:`Verdict`; the runner records it.
    is_verifier: bool = False

    @property
    def name(self) -> str:
        return self.spec.name

    def __call__(self, context: dict[str, Any], **arguments: Any) -> Any:
        return self.fn(context, **arguments)


def function_tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, Any] | None = None,
    needs_approval: bool = False,
) -> Any:
    """Make a :class:`Tool` from ``fn(context, **args)``; usable as a decorator."""

    def wrap(f: Callable[..., Any]) -> Tool:
        doc = (description or (inspect.getdoc(f) or "").split("\n", 1)[0]).strip()
        return Tool(
            spec=ToolSpec(
                name=name or f.__name__,
                description=doc,
                parameters=parameters or schema_for(f),
            ),
            fn=f,
            needs_approval=needs_approval,
        )

    return wrap if fn is None else wrap(fn)


def verifier_tool(
    name: str, description: str, fn: Callable[[dict[str, Any]], Verdict]
) -> Tool:
    """A tool with no arguments whose answer is a :class:`Verdict`."""
    return Tool(
        spec=ToolSpec(
            name=name,
            description=description,
            parameters={"type": "object", "properties": {}},
        ),
        fn=lambda context, **_: fn(context),
        is_verifier=True,
    )


def command_verifier(
    name: str,
    description: str,
    argv: list[str] | Callable[[dict[str, Any]], list[str]],
    parse: Any = "gnu",
    *,
    cwd_key: str | None = None,
    timeout_s: float = 600,
) -> Tool:
    """A verifier that runs a build or check command (``verify.ingest.run_check``).

    ``argv`` may be a function of the run's context, so a gate can compile the
    file the agent just wrote. ``cwd_key`` names the context entry holding the
    working directory. This is how a compiler, a linter or a cloud CLI becomes
    something ``Agent.required_verifiers`` can refuse to finish on.
    """
    from ...verify.ingest import run_check

    def check(context: dict[str, Any]) -> Verdict:
        args = argv(context) if callable(argv) else argv
        cwd = context.get(cwd_key) if cwd_key else None
        return run_check(name, args, parse, cwd=cwd, timeout_s=timeout_s)

    return verifier_tool(name, description, check)


def context_verifier(
    name: str,
    description: str,
    key: str,
    adapter: Callable[[Any], Verdict],
) -> Tool:
    """A verifier over a stage result already in the context.

    ``adapter`` is one of ``verify.ingest.from_simulation``, ``from_kernel``,
    ``from_mechanism`` or any callable returning a :class:`Verdict`. A missing
    key is ``unverified``, never ``ok``.
    """

    def check(context: dict[str, Any]) -> Verdict:
        if key not in context:
            reason = f"nothing at context[{key!r}] to check"
            return Verdict(name, unverified_reason=reason)
        verdict = adapter(context[key])
        if verdict.verifier != name:
            verdict = Verdict(
                name, verdict.clauses, verdict.evidence, verdict.unverified_reason
            )
        return verdict

    return verifier_tool(name, description, check)
