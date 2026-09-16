"""Guardrails: tripwires around the run, the OpenAI Agents SDK shape.

``src/agents/guardrail.py``: a guardrail function returns
``GuardrailFunctionOutput(output_info, tripwire_triggered)``; a tripped wire
halts the run with a typed exception. Input guardrails run once, before the
first model call (a request that is not a board should not spend one);
output guardrails run on the final output, and the receipt's guardrail
(:func:`silkscreen.agents.harness.receipt.summary_guardrail`) is one of them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "GuardrailFunctionOutput",
    "InputGuardrail",
    "OutputGuardrail",
    "GuardrailTripwire",
    "InputGuardrailTripwire",
    "OutputGuardrailTripwire",
]


@dataclass(frozen=True)
class GuardrailFunctionOutput:
    tripwire_triggered: bool
    output_info: Any = None


@dataclass(frozen=True)
class InputGuardrail:
    name: str
    fn: Callable[[str, dict[str, Any]], GuardrailFunctionOutput]


@dataclass(frozen=True)
class OutputGuardrail:
    name: str
    fn: Callable[[Any, dict[str, Any]], GuardrailFunctionOutput]


class GuardrailTripwire(RuntimeError):
    def __init__(self, guardrail: str, output: GuardrailFunctionOutput) -> None:
        self.guardrail = guardrail
        self.output = output
        super().__init__(f"guardrail {guardrail!r} tripped: {output.output_info}")


class InputGuardrailTripwire(GuardrailTripwire):
    """Raised before any model call is spent."""


class OutputGuardrailTripwire(GuardrailTripwire):
    """The final output was refused; the run ends ``blocked``."""
