"""Ada's agent harness: a thin tool loop whose gate is the verifiers.

Decided 2026-09-15 (``docs/agent-harness.md``): the loop is owned, small and
synchronous; its primitives are copied by name from the OpenAI Agents SDK
(``Agent``, ``Runner``, guardrail tripwires, ``needs_approval``), its
tool-failure recovery from Google ADK's ``ReflectAndRetryToolPlugin``, and
its model access stays behind this repo's seam extended with tool calls
(:mod:`.model`). Google ADK keeps the conversational root and the fixed
``Workflow``; the Claude Agent SDK is an executor to delegate file-shaped
work to, not the loop.

What makes the loop worth having is in :mod:`silkscreen.verify`: a required
verifier that is red stops a run from finishing, and the receipt names
which verifier proved which claim.
"""

from __future__ import annotations

from .guardrails import (
    GuardrailFunctionOutput,
    GuardrailTripwire,
    InputGuardrail,
    InputGuardrailTripwire,
    OutputGuardrail,
    OutputGuardrailTripwire,
)
from .loop import Agent, Budget, BudgetExceeded, PendingApproval, Runner, RunResult
from .model import (
    Message,
    ScriptedToolModel,
    ToolCall,
    ToolModel,
    ToolResult,
    ToolSpec,
    Turn,
    Usage,
)
from .receipt import Claim, Receipt, Summary, parse_summary, summary_guardrail, unproven
from .reflect import ReflectAndRetry, ToolFailureResponse
from .tools import (
    Tool,
    command_verifier,
    context_verifier,
    function_tool,
    verifier_tool,
)

__all__ = [
    "Agent",
    "Budget",
    "BudgetExceeded",
    "PendingApproval",
    "Runner",
    "RunResult",
    "Message",
    "ScriptedToolModel",
    "ToolCall",
    "ToolModel",
    "ToolResult",
    "ToolSpec",
    "Turn",
    "Usage",
    "Claim",
    "Receipt",
    "Summary",
    "parse_summary",
    "summary_guardrail",
    "unproven",
    "ReflectAndRetry",
    "ToolFailureResponse",
    "Tool",
    "function_tool",
    "verifier_tool",
    "command_verifier",
    "context_verifier",
    "GuardrailFunctionOutput",
    "GuardrailTripwire",
    "InputGuardrail",
    "InputGuardrailTripwire",
    "OutputGuardrail",
    "OutputGuardrailTripwire",
    "tool_model_for",
]


def tool_model_for(model: object) -> ToolModel:
    """The tool-calling face of a text ``Model``, or the model itself if it has one.

    ``GeminiModel`` and ``ClaudeModel`` are wrapped sharing their client;
    ``FallbackModel`` and the pipeline's event tap implement ``generate_turn``
    themselves by calling this on each rung.
    """
    if hasattr(model, "generate_turn"):
        return model  # type: ignore[return-value]
    from ..model import GeminiModel

    if isinstance(model, GeminiModel):
        from .gemini import GeminiToolModel

        return GeminiToolModel.from_model(model)
    try:
        from ..claude import ClaudeModel
    except Exception:  # pragma: no cover - anthropic not installed
        ClaudeModel = None  # type: ignore[assignment]
    if ClaudeModel is not None and isinstance(model, ClaudeModel):
        from .claude import ClaudeToolModel

        return ClaudeToolModel.from_model(model)
    raise TypeError(
        f"{type(model).__name__} has no generate_turn and is not a known provider model"
    )
