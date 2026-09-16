"""A conversational ADK root agent over Silkscreen's deterministic pipeline.

The LLM owns only the conversational decision: answer a message that is not a
board request, ask one necessary clarification, or call ``generate_board``.
The tool retains the existing validated pipeline, so introducing a chat surface
does not turn placement, repair, or review into free-form orchestration.

There is no intent classifier in front of the model. "Can you hear me" and
"make me a 3.3 V LDO board" reach the same agent, which decides whether a tool
is warranted -- the shape OpenClaw's voice wake uses
(``apps/macos/Sources/OpenClaw/VoiceWakeForwarder.swift`` forwards every
transcript to the one main session). ``confirm_before_build`` is the Hermes
Agent approval gate (``tools/approval.py``) applied to the one expensive tool:
the model may only *propose* a board, the proposal comes back on the result,
and the caller asks a human before anything is spent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from google.adk import Runner
from google.adk.agents import LlmAgent
from google.adk.sessions import InMemorySessionService
from google.genai import types

from silkscreen.agents.model import ModelError

__all__ = ["OrchestratorResult", "run_orchestrator"]

_APP_NAME = "silkscreen_chat"
_USER_ID = "silkscreen"

logging.getLogger("google_adk").setLevel(logging.CRITICAL)

_INSTRUCTION = """You are Ada, an AI hardware engineer and assistant at the
engineer's bench.

For each message, take exactly one of these paths:
1. If the message is not asking you to design a board -- a greeting, a check
   such as "can you hear me", a question about electronics, KiCad, this app, or
   anything else -- just answer it in one or two short, plain sentences that
   read well aloud. Do not call a tool.
2. If it asks for a board but an electrically essential constraint is genuinely
   missing, ask one short, concrete clarification question. Ask only when
   guessing could materially change or damage the design. Do not call a tool in
   that response.
3. Otherwise call generate_board exactly once. Never invent a board, component,
   validation result, or artifact yourself.

If the message includes a clarification answer, do not ask another question;
call generate_board. After the tool returns, summarize the outcome in friendly,
compact language and mention blockers, unrouted nets, and production-promotion
eligibility honestly. A blocked constraint receipt does not remove the generated
artifact; it means the board is not eligible for production promotion. Do not
reveal private chain-of-thought. The interface separately shows observable tool
calls, prompts, responses, validation, and retry events for debugging.
"""

_CONFIRM_INSTRUCTION = """You are Ada, an AI hardware engineer and assistant at
the engineer's bench. Messages may arrive by
voice through speech recognition, so some words can be misheard.

For each message, take exactly one of these paths:
1. If the message is not asking you to design a board -- a greeting, a check
   such as "can you hear me", a question about electronics, KiCad, this app, or
   anything else -- just answer it in one or two short, plain sentences that
   read well aloud. Do not call a tool.
2. If it clearly asks you to design or build a board, call propose_board
   exactly once with a one-sentence board request in your own words that keeps
   every constraint the engineer gave. Building costs real time and money, so
   you never build directly: the engineer confirms first. After the tool
   returns, ask in one short sentence whether to build it, naming the board.
3. If it is unclear whether they want a board, ask one short question. Do not
   call a tool.

Never claim a board was built. Do not reveal private chain-of-thought.
"""


@dataclass(frozen=True)
class OrchestratorResult:
    assistant: str
    result: dict[str, Any] | None
    needs_clarification: bool
    model: str
    #: With ``confirm_before_build``: the board request the model wants to
    #: build, awaiting a human yes. Never set when a board actually ran.
    proposal: str | None = None


def _dump(value: object) -> Any:
    """A JSON-safe trace payload without private reasoning or thought signatures."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json", exclude_none=True)
        except TypeError:
            value = value.model_dump(exclude_none=True)
    return _without_private_reasoning(json.loads(json.dumps(value, default=str)))


def _without_private_reasoning(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_private_reasoning(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("thought") is True:
        return {"thought": True, "omitted": True}
    return {
        key: _without_private_reasoning(item)
        for key, item in value.items()
        if key != "thought_signature"
    }


def _text(content: object) -> str:
    parts = getattr(content, "parts", None) or []
    return "\n".join(
        str(part.text) for part in parts if getattr(part, "text", None)
    ).strip()


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    route = result.get("routing") if isinstance(result.get("routing"), dict) else {}
    summary = {
        "status": result.get("status"),
        "parts": len(result.get("parts") or []),
        "nets": len(result.get("nets") or []),
        "findings": len(result.get("findings") or []),
        "blockers": len(result.get("blockers") or []),
        "warnings": len(result.get("warnings") or []),
        "routed_nets": len(route.get("routed") or []),
        "unrouted": dict(route.get("unrouted") or {}),
        "duration_s": result.get("duration_s"),
        "served_by": result.get("served_by"),
    }
    receipt = result.get("constraint_receipt")
    manifest = result.get("constraint_manifest")
    if isinstance(receipt, dict):
        checks = [
            check
            for group in receipt.get("net_classes", [])
            if isinstance(group, dict)
            for check in group.get("checks", [])
            if isinstance(check, dict)
        ]
        checks.extend(
            check
            for check in receipt.get("mechanical", [])
            if isinstance(check, dict)
        )
        blockers = [
            {
                key: blocker.get(key)
                for key in ("scope", "name", "status", "detail")
            }
            for blocker in receipt.get("blockers", [])
            if isinstance(blocker, dict)
        ]
        summary.update(
            {
                "promotion_status": result.get("promotion_status"),
                "constraint_manifest_version": (
                    manifest.get("version") if isinstance(manifest, dict) else None
                ),
                "constraint_receipt": {
                    "hard_gate": receipt.get("hard_gate"),
                    "promotable": receipt.get("promotable"),
                    "verified": sum(
                        check.get("status") == "verified" for check in checks
                    ),
                    "violated": sum(
                        check.get("status") == "violated" for check in checks
                    ),
                    "unresolved": sum(
                        check.get("status") == "unresolved" for check in checks
                    ),
                    "blockers": blockers,
                },
            }
        )
    return summary


async def _run(
    *,
    message: str,
    clarification: str,
    model: str | object,
    thinking_level: str | None,
    session_id: str,
    generate: Callable[[], dict[str, Any]],
    emit: Callable[[dict[str, Any]], None],
    debug: bool,
    before_model_call: Callable[[], None] | None,
    confirm_before_build: bool = False,
) -> OrchestratorResult:
    model_name = str(model if isinstance(model, str) else getattr(model, "model", ""))
    if isinstance(model, str):
        # A bare id gets the tiered ladder (failover.py); an injected BaseLlm
        # is used as given, which is how the offline tests drive the root.
        from .failover import build_failover_llm

        model = build_failover_llm(
            model, on_retry=lambda event: emit(event), before_attempt=before_model_call
        )
    else:
        from .failover import FailoverLlm

        if isinstance(model, FailoverLlm):
            # An injected ladder still reports on this turn's stream.
            if model.on_retry is None:
                model.on_retry = emit
            if model.before_attempt is None:
                model.before_attempt = before_model_call

    def served() -> str:
        """The tier that actually answered, when the model can say."""
        return str(getattr(model, "served_model", None) or model_name)
    call_seq = 0
    tool_seq = 0
    tool_failed = False
    pending: list[tuple[str, float]] = []
    full_result: dict[str, Any] | None = None
    proposal: str | None = None

    def before_model(callback_context, llm_request):
        del callback_context
        nonlocal call_seq
        if before_model_call is not None:
            before_model_call()
        call_seq += 1
        call_id = f"orchestrator-{call_seq}"
        pending.append((call_id, time.monotonic()))
        if debug:
            config = getattr(llm_request, "config", None)
            emit(
                {
                    "event": "model.request",
                    "layer": "orchestrator",
                    "call_id": call_id,
                    "model": model_name,
                    "thinking_level": _dump(
                        getattr(
                            getattr(config, "thinking_config", None),
                            "thinking_level",
                            None,
                        )
                    ),
                    "system": _dump(getattr(config, "system_instruction", None)),
                    "contents": _dump(getattr(llm_request, "contents", None)),
                    "tools": _dump(getattr(config, "tools", None)),
                }
            )
        return None

    def after_model(callback_context, llm_response):
        del callback_context
        call_id, started = (
            pending.pop(0)
            if pending
            else ("orchestrator-unknown", time.monotonic())
        )
        content = getattr(llm_response, "content", None)
        text = _text(content)
        emit(
            {
                "event": "model.call",
                "layer": "orchestrator",
                "call_id": call_id,
                "model": served(),
                "elapsed_s": round(time.monotonic() - started, 3),
                "ok": True,
                "chars": len(text),
            }
        )
        if debug:
            emit(
                {
                    "event": "model.response",
                    "layer": "orchestrator",
                    "call_id": call_id,
                    "model": served(),
                    "chars": len(text),
                    "text": text,
                    "response": _dump(content),
                }
            )
        return None

    def model_error(callback_context, llm_request, error):
        del callback_context, llm_request
        call_id, started = (
            pending.pop(0)
            if pending
            else ("orchestrator-unknown", time.monotonic())
        )
        emit(
            {
                "event": "model.call",
                "layer": "orchestrator",
                "call_id": call_id,
                "model": model_name,
                "elapsed_s": round(time.monotonic() - started, 3),
                "ok": False,
                "chars": 0,
                "error": f"{type(error).__name__}: {error}",
            }
        )
        return None

    def generate_board() -> dict[str, Any]:
        """Generate, validate, place, route, and review the requested PCB."""
        nonlocal full_result
        # "Call generate_board exactly once" lived only in the prompt, and a
        # prompt is not a guard. A model that emits two function calls in one
        # turn -- or retries after a tool error -- ran the whole pipeline
        # twice: two full sets of paid model calls for one request, with only
        # the second result kept. The engineer asked for one board.
        if full_result is not None:
            return {
                **_summary(full_result),
                "note": (
                    "the board was already generated in this turn; returning "
                    "it rather than running the pipeline again"
                ),
            }
        full_result = generate()
        return _summary(full_result)

    def propose_board(board_request: str) -> dict[str, Any]:
        """Propose a PCB to build; it is built only after the engineer confirms.

        Args:
            board_request: One sentence describing the board, keeping every
                constraint the engineer stated.
        """
        nonlocal proposal
        text = str(board_request or "").strip()
        if not text:
            return {"status": "refused", "reason": "board_request was empty"}
        # First proposal wins, for the reason generate_board keeps its first
        # result: a second call in one turn is a model retry, not a new ask.
        if proposal is None:
            proposal = text
        return {
            "status": "awaiting_confirmation",
            "board_request": proposal,
            "note": "nothing has been built; ask the engineer to confirm",
        }

    def before_tool(tool, args, tool_context):
        del tool_context
        nonlocal tool_seq
        tool_seq += 1
        emit(
            {
                "event": "tool.start",
                "layer": "orchestrator",
                "tool_call_id": f"tool-{tool_seq}",
                "tool": getattr(tool, "name", None) or "generate_board",
                "args": _dump(args),
            }
        )
        return None

    def after_tool(tool, args, tool_context, tool_response):
        del args, tool_context
        emit(
            {
                "event": "tool.done",
                "layer": "orchestrator",
                "tool_call_id": f"tool-{tool_seq}",
                "tool": getattr(tool, "name", None) or "generate_board",
                "result": _dump(tool_response),
            }
        )
        return None

    def tool_error(tool, args, tool_context, error):
        nonlocal tool_failed
        tool_failed = True
        del args, tool_context
        emit(
            {
                "event": "tool.error",
                "layer": "orchestrator",
                "tool_call_id": f"tool-{tool_seq}",
                "tool": getattr(tool, "name", None) or "generate_board",
                "error": f"{type(error).__name__}: {error}",
            }
        )
        return None

    thinking_config = (
        types.ThinkingConfig(
            thinking_level=types.ThinkingLevel(thinking_level.upper())
        )
        if thinking_level
        else None
    )
    agent = LlmAgent(
        name="orchestrator",
        description=(
            "Clarifies a PCB request and invokes Silkscreen's validated generator."
        ),
        model=model,
        instruction=_CONFIRM_INSTRUCTION if confirm_before_build else _INSTRUCTION,
        tools=[propose_board] if confirm_before_build else [generate_board],
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=2048,
            thinking_config=thinking_config,
        ),
        before_model_callback=before_model,
        after_model_callback=after_model,
        on_model_error_callback=model_error,
        before_tool_callback=before_tool,
        after_tool_callback=after_tool,
        on_tool_error_callback=tool_error,
    )

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=_APP_NAME,
        user_id=_USER_ID,
        session_id=session_id,
    )
    runner = Runner(
        app_name=_APP_NAME,
        agent=agent,
        session_service=session_service,
    )

    prompt = f"Engineer's message:\n{message.strip()}"
    if clarification.strip():
        prompt += f"\n\nClarification answer:\n{clarification.strip()}"
    assistant = ""
    try:
        async for event in runner.run_async(
            user_id=_USER_ID,
            session_id=session_id,
            new_message=types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt)],
            ),
        ):
            if event.is_final_response() and event.content is not None:
                assistant = _text(event.content) or assistant
    except Exception as exc:
        if tool_failed:
            raise
        raise ModelError(f"{model_name} orchestrator call failed: {exc}") from exc

    if full_result is not None and not assistant:
        summary = _summary(full_result)
        assistant = (
            f"The board finished with {summary['parts']} parts and "
            f"{summary['findings']} review findings."
        )
        receipt = summary.get("constraint_receipt")
        if isinstance(receipt, dict):
            if receipt.get("promotable"):
                assistant += " It is eligible for production promotion."
            else:
                assistant += (
                    " It is not eligible for production promotion because "
                    f"{len(receipt.get('blockers') or [])} constraint checks block it; "
                    "the generated artifact is still available."
                )
    if proposal is not None and not assistant:
        assistant = f"Want me to build this: {proposal}?"
    if not assistant:
        assistant = "I need one more detail before I can generate this board."

    if clarification.strip() and full_result is None:
        raise ModelError(
            f"{model_name} did not call generate_board after the clarification"
        )

    needs_clarification = full_result is None and proposal is None
    emit(
        {
            "event": "assistant.message",
            "layer": "orchestrator",
            "model": served(),
            "text": assistant,
            "needs_clarification": needs_clarification,
            "proposal": proposal,
        }
    )
    return OrchestratorResult(
        assistant=assistant,
        result=full_result,
        needs_clarification=needs_clarification,
        model=served(),
        proposal=proposal,
    )


def run_orchestrator(
    *,
    message: str,
    clarification: str = "",
    model: str | object,
    thinking_level: str | None = None,
    session_id: str,
    generate: Callable[[], dict[str, Any]],
    emit: Callable[[dict[str, Any]], None],
    debug: bool = False,
    before_model_call: Callable[[], None] | None = None,
    confirm_before_build: bool = False,
) -> OrchestratorResult:
    """Run one presentation turn from synchronous service code."""
    return asyncio.run(
        _run(
            message=message,
            clarification=clarification,
            model=model,
            thinking_level=thinking_level,
            session_id=session_id,
            generate=generate,
            emit=emit,
            debug=debug,
            before_model_call=before_model_call,
            confirm_before_build=confirm_before_build,
        )
    )
