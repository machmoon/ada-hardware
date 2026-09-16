"""The loop: an agent is data, a turn is a typed step, a verifier gate ends it.

Copied in shape and name from the OpenAI Agents SDK
(``openai/openai-agents-python@fbf59a4``, ``src/agents/agent.py``,
``run.py``, ``run_internal/run_steps.py``): :class:`Agent` is a dataclass of
instructions, tools, guardrails and an output type; :meth:`Runner.run` loops
up to a budget and every turn ends in one of ``NextStepFinalOutput``,
``NextStepRunAgain`` or ``NextStepInterruption`` (a tool with
``needs_approval``). Three deliberate differences, each stated:

* **synchronous.** Every stage body in ``agents/stages.py`` is a plain
  function and both drivers call them that way; an ``async`` loop would need
  the ``_run_coro_blocking`` bridge on every call.
* **a verifier gate.** ``Agent.required_verifiers`` names tools whose
  :class:`~silkscreen.verify.Verdict` must be ``ok`` on the *final* artifact.
  When the model offers a final output the runner runs them itself, and a
  red one becomes the next user turn (the proposer's batched repair, driven
  by the verifier rather than by a prompt) until the budget ends; an
  ``unverified`` one ends the run ``unverified``, since no repair fixes a
  missing ``kicad-cli``. No shipping loop the review could find refuses to
  finish this way -- aider and SWE-agent reflect failures back but let the
  model submit -- so this rule is Ada's own, and this sentence says so.
* **budgets in calls and seconds, not only turns**, because a run is billed
  in engine minutes (``billing/units.py``), and the receipt carries what was
  spent.

Events ride the existing ``on_event`` seam with the existing frame key
(``"event"``). A repair round is emitted as ``propose.round`` so the desktop's
"validate and repair" row ticks; the harness's own kinds are ``harness.turn``,
``harness.tool.start``, ``harness.tool.done``, ``harness.verdict`` and
``harness.blocked``, which both front ends drop when unknown
(``app/src/lib/silkscreen/describe.ts``, ``frontend/src/lib/stream.js``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ...verify import Verdict
from ..model import ModelError, parse_json
from .guardrails import (
    InputGuardrail,
    InputGuardrailTripwire,
    OutputGuardrail,
)
from .model import (
    Message,
    ToolCall,
    ToolModel,
    ToolResult,
    Turn,
    Usage,
    mint_call_id,
)
from .receipt import Receipt, Summary, parse_summary, summary_guardrail
from .reflect import ReflectAndRetry
from .tools import Tool

__all__ = [
    "Agent",
    "Budget",
    "Runner",
    "RunResult",
    "PendingApproval",
    "RunStatus",
    "BudgetExceeded",
]

RunStatus = Literal["ok", "blocked", "unverified", "interrupted", "budget"]
OnEvent = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class Agent:
    name: str
    instructions: str
    tools: tuple[Tool, ...] = ()
    #: Names of verifier tools that must be ``ok`` before a final output is
    #: accepted. Run by the runner itself at every final output.
    required_verifiers: tuple[str, ...] = ()
    #: ``text``: the final text as-is. ``json``: parsed. ``summary``: a
    #: :class:`Summary`, checked by the receipt guardrail.
    output_type: Literal["text", "json", "summary"] = "text"
    #: Where the runner stores a ``json`` final output in the context before
    #: running the required verifiers, so they check the offered artifact.
    artifact_key: str | None = "artifact"
    input_guardrails: tuple[InputGuardrail, ...] = ()
    output_guardrails: tuple[OutputGuardrail, ...] = ()
    max_output_tokens: int = 8192

    def tool(self, name: str) -> Tool | None:
        for t in self.tools:
            if t.name == name:
                return t
        return None


@dataclass(frozen=True)
class Budget:
    max_turns: int = 8
    max_model_calls: int = 8
    max_tool_seconds: float = 300.0
    max_seconds: float | None = None

    @classmethod
    def from_effort(cls, profile: Any) -> Budget:
        """Derive from an ``agents.effort.EffortProfile``: one slider, not two.

        A repair round is one model call; the tool turns between them are
        another, so ``max_repairs`` repairs need ``2 * (max_repairs + 1)``
        turns plus one for the summary. Tool seconds scale with the solver
        budget, since ERC on a drawn schematic is the expensive tool.
        """
        rounds = int(getattr(profile, "max_repairs", 1))
        turns = 2 * (rounds + 1) + 1
        return cls(
            max_turns=turns,
            max_model_calls=turns,
            max_tool_seconds=max(
                60.0, 4.0 * float(getattr(profile, "time_limit_s", 20.0))
            ),
        )


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingApproval:
    call: ToolCall
    tool: str
    turn: int


@dataclass
class RunResult:
    status: RunStatus
    reason: str = ""
    final_output: Any = None
    summary: Summary | None = None
    messages: list[Message] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    receipt: Receipt = field(default_factory=Receipt)
    usage: Usage = field(default_factory=Usage)
    interruption: PendingApproval | None = None
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def verdicts(self) -> dict[str, Verdict]:
        return self.receipt.verdicts

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "summary": self.summary.as_dict() if self.summary else None,
            "receipt": self.receipt.as_dict(),
            "turns": len(self.turns),
            "usage": self.usage.__dict__,
        }


class Runner:
    """Run one :class:`Agent` on one input; stateless between runs."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock

    def run(
        self,
        agent: Agent,
        input: str,
        *,
        model: ToolModel,
        budget: Budget | None = None,
        context: dict[str, Any] | None = None,
        on_event: OnEvent | None = None,
        reflect: ReflectAndRetry | None = None,
    ) -> RunResult:
        budget = budget or Budget()
        context = context if context is not None else {}
        reflect = reflect or ReflectAndRetry()
        emit = on_event or (lambda frame: None)
        receipt = Receipt()
        result = RunResult(status="budget", receipt=receipt, context=context)
        started = self._clock()
        tool_seconds = 0.0
        model_calls = 0
        rounds = 0

        for guard in agent.input_guardrails:
            out = guard.fn(input, context)
            if out.tripwire_triggered:
                raise InputGuardrailTripwire(guard.name, out)

        messages: list[Message] = [Message.user(input)]
        result.messages = messages
        specs = [t.spec for t in agent.tools]
        provider: str | None = None

        def repair(items: list[str], why: str) -> None:
            nonlocal rounds
            rounds += 1
            receipt.spent["rounds"] = rounds
            emit(
                {
                    "event": "propose.round",
                    "round": rounds,
                    "errors": len(items),
                    "first_error": items[0][:160] if items else why[:160],
                }
            )
            body = "\n".join(f"  - {i}" for i in items) or f"  - {why}"
            messages.append(
                Message.user(
                    f"Your answer was not accepted ({why}). Fix every item below and "
                    f"answer again in the same format:\n{body}"
                )
            )

        for turn_no in range(1, budget.max_turns + 1):
            if (
                budget.max_seconds is not None
                and self._clock() - started > budget.max_seconds
            ):
                return self._end(
                    result, "budget", f"{budget.max_seconds:.0f} s spent", emit
                )
            if model_calls >= budget.max_model_calls:
                return self._end(
                    result,
                    "budget",
                    f"{budget.max_model_calls} model calls spent",
                    emit,
                )
            model_calls += 1
            receipt.spent["model_calls"] = model_calls
            turn = model.generate_turn(
                messages,
                tools=specs,
                system=agent.instructions,
                max_output_tokens=agent.max_output_tokens,
            )
            if provider is None:
                provider = turn.provider
            elif turn.provider != provider:
                raise ModelError(
                    f"provider changed mid-conversation ({provider} -> "
                    f"{turn.provider}); "
                    f"a signed history cannot be replayed to another provider"
                )
            turn = self._with_ids(turn, turn_no)
            result.turns.append(turn)
            result.usage = result.usage + turn.usage
            messages.append(Message.from_turn(turn))
            emit(
                {
                    "event": "harness.turn",
                    "turn": turn_no,
                    "provider": turn.provider,
                    "tool_calls": len(turn.tool_calls),
                    "stop_reason": turn.stop_reason,
                    "input_tokens": turn.usage.input_tokens,
                    "output_tokens": turn.usage.output_tokens,
                }
            )

            if turn.stop_reason == "malformed":
                repair([], f"the tool call was malformed ({turn.raw_stop_reason})")
                continue
            if turn.stop_reason == "max_tokens":
                repair(
                    [],
                    "the answer was cut off at the token budget; answer more briefly",
                )
                continue
            if turn.stop_reason == "refused":
                return self._end(
                    result,
                    "blocked",
                    f"the model refused ({turn.raw_stop_reason})",
                    emit,
                )

            if turn.tool_calls:
                results: list[ToolResult] = []
                for call in turn.tool_calls:
                    tool = agent.tool(call.name)
                    if tool is None:
                        results.append(
                            ToolResult(
                                call.id,
                                call.name,
                                f"unknown tool {call.name!r}",
                                is_error=True,
                            )
                        )
                        continue
                    if tool.needs_approval:
                        result.interruption = PendingApproval(call, tool.name, turn_no)
                        return self._end(
                            result, "interrupted", f"{tool.name} needs approval", emit
                        )
                    emit(
                        {
                            "event": "harness.tool.start",
                            "tool": tool.name,
                            "turn": turn_no,
                        }
                    )
                    t0 = self._clock()
                    try:
                        output = tool(context, **call.arguments)
                    except Exception as exc:  # the tool's failure is the model's to fix
                        seconds = self._clock() - t0
                        tool_seconds += seconds
                        guidance = reflect.on_failure(tool.name, exc)
                        emit(
                            {
                                "event": "harness.tool.done",
                                "tool": tool.name,
                                "seconds": round(seconds, 3),
                                "ok": False,
                            }
                        )
                        if guidance is None:
                            return self._end(
                                result,
                                "blocked",
                                f"{tool.name} failed {reflect.max_retries + 1} times: "
                                f"{exc}",
                                emit,
                            )
                        results.append(
                            ToolResult(
                                call.id,
                                tool.name,
                                guidance.as_dict(),
                                is_error=True,
                                seconds=seconds,
                            )
                        )
                        continue
                    seconds = self._clock() - t0
                    tool_seconds += seconds
                    reflect.on_success(tool.name)
                    receipt.spent[tool.name] = receipt.spent.get(tool.name, 0) + 1
                    if tool.is_verifier:
                        output = self._record(receipt, output, emit)
                    emit(
                        {
                            "event": "harness.tool.done",
                            "tool": tool.name,
                            "seconds": round(seconds, 3),
                            "ok": True,
                        }
                    )
                    results.append(
                        ToolResult(call.id, tool.name, output, seconds=seconds)
                    )
                messages.append(Message.user(tool_results=tuple(results)))
                if tool_seconds > budget.max_tool_seconds:
                    return self._end(
                        result,
                        "budget",
                        f"{budget.max_tool_seconds:.0f} tool seconds spent",
                        emit,
                    )
                continue

            # ---- a final output was offered
            try:
                final = self._parse_final(agent, turn.text)
            except ValueError as exc:
                repair([str(exc)], "the answer did not parse")
                continue
            if agent.artifact_key is not None:
                context[agent.artifact_key] = final
            red: list[str] = []
            for name in agent.required_verifiers:
                tool = agent.tool(name)
                if tool is None:
                    raise ModelError(
                        f"required verifier {name!r} is not one of the agent's tools"
                    )
                verdict_dict = self._record(receipt, tool(context), emit)
                verdict = receipt.verdicts[name]
                if verdict.status == "unverified":
                    result.final_output = final
                    return self._end(
                        result,
                        "unverified",
                        f"{name}: {verdict.unverified_reason}",
                        emit,
                    )
                if not verdict.ok:
                    red.extend(f"{name}: {i}" for i in verdict.repair_items())
                del verdict_dict
            if red:
                repair(red, "a required check failed")
                continue
            guards = list(agent.output_guardrails)
            if agent.output_type == "summary":
                guards.append(summary_guardrail(receipt))
            tripped = None
            for guard in guards:
                out = guard.fn(final, context)
                if out.tripwire_triggered:
                    tripped = (guard.name, out)
                    break
            if tripped is not None:
                name, out = tripped
                info = out.output_info
                items = info if isinstance(info, list) else [str(info)]
                repair([f"{name}: {i}" for i in items], "an output check refused it")
                continue
            result.final_output = final
            if isinstance(final, Summary):
                result.summary = final
            return self._end(result, "ok", "", emit)

        return self._end(result, "budget", f"{budget.max_turns} turns spent", emit)

    # ---- helpers

    @staticmethod
    def _with_ids(turn: Turn, turn_no: int) -> Turn:
        calls = tuple(
            c
            if c.id
            else ToolCall(mint_call_id(turn_no, i), c.name, c.arguments, c.signature)
            for i, c in enumerate(turn.tool_calls)
        )
        return Turn(
            turn.provider,
            turn.text,
            calls,
            turn.stop_reason,
            turn.raw_stop_reason,
            turn.usage,
            turn.native,
        )

    @staticmethod
    def _parse_final(agent: Agent, text: str) -> Any:
        if agent.output_type == "text":
            return text
        if agent.output_type == "json":
            try:
                return parse_json(text)
            except ModelError as exc:
                raise ValueError(str(exc)) from exc
        return parse_summary(text)

    @staticmethod
    def _record(receipt: Receipt, output: Any, emit: OnEvent) -> dict[str, Any]:
        if not isinstance(output, Verdict):
            raise ModelError(
                f"a verifier tool must return a Verdict, got {type(output).__name__}"
            )
        n = receipt.spent.get(f"evidence:{output.verifier}", 0) + 1
        receipt.spent[f"evidence:{output.verifier}"] = n
        evidence_id = f"{output.verifier}#{n}"
        receipt.record(output, evidence_id)
        emit(
            {
                "event": "harness.verdict",
                "verifier": output.verifier,
                "status": output.status,
                "blocking": len(output.repair_items()),
                "first": output.failures[0].detail[:160] if output.failures else None,
                "evidence_id": evidence_id,
            }
        )
        return {"evidence_id": evidence_id, **output.as_dict()}

    @staticmethod
    def _end(
        result: RunResult, status: RunStatus, reason: str, emit: OnEvent
    ) -> RunResult:
        result.status = status
        result.reason = reason
        if status != "ok":
            emit({"event": "harness.blocked", "status": status, "reason": reason[:200]})
        return result


def json_tool_output(value: Any) -> str:
    """Render a tool output for a provider that wants text."""
    return value if isinstance(value, str) else json.dumps(value, default=str)
