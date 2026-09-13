"""The ADK LLM root chooses between clarification and the board tool."""

from __future__ import annotations

from typing import Any

from google.adk.models import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from silkscreen.agents.adk.orchestrator import run_orchestrator


class FakeLlm(BaseLlm):
    responses: list[types.Content]
    requests: list[Any] = []

    async def generate_content_async(self, llm_request, stream=False):
        self.requests.append(llm_request)
        yield LlmResponse(content=self.responses.pop(0))


def text_response(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part.from_text(text=text)])


def tool_response() -> types.Content:
    return types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="generate_board", args={})],
    )


def test_the_orchestrator_can_ask_one_clarification_without_running_the_board():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[text_response("What input voltage should the regulator accept?")],
    )
    events = []
    generated = []

    outcome = run_orchestrator(
        message="make a regulator",
        model=model,
        session_id="s1",
        generate=lambda: generated.append(True),
        emit=events.append,
        debug=True,
    )

    assert outcome.needs_clarification is True
    assert outcome.result is None
    assert generated == []
    assert "input voltage" in outcome.assistant.lower()
    assert [event["event"] for event in events] == [
        "model.request",
        "model.call",
        "model.response",
        "assistant.message",
    ]
    request = events[0]
    assert request["layer"] == "orchestrator"
    assert request["system"]
    assert "make a regulator" in str(request["contents"])


def test_the_orchestrator_calls_the_validated_generator_and_summarizes_it():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[tool_response(), text_response("The board is ready for review.")],
    )
    events = []
    result = {
        "status": "FEASIBLE",
        "parts": [{"ref": "U1"}],
        "nets": ["VIN", "GND", "VOUT"],
        "findings": [],
        "blockers": [],
        "warnings": [],
        "duration_s": 1.2,
    }

    outcome = run_orchestrator(
        message="make a regulator",
        clarification="5 V input",
        model=model,
        session_id="s2",
        generate=lambda: result,
        emit=events.append,
        debug=True,
    )

    assert outcome.needs_clarification is False
    assert outcome.result is result
    assert outcome.assistant == "The board is ready for review."
    names = [event["event"] for event in events]
    assert names.count("model.request") == 2
    assert names.count("model.response") == 2
    assert "tool.start" in names
    assert "tool.done" in names
    assert names[-1] == "assistant.message"
    assert events[names.index("tool.done")]["result"] == {
        "status": "FEASIBLE",
        "parts": 1,
        "nets": 3,
        "findings": 0,
        "blockers": 0,
        "warnings": 0,
        "routed_nets": 0,
        "unrouted": {},
        "duration_s": 1.2,
        "served_by": None,
    }


def test_constraint_receipt_facts_reach_the_orchestrator_tool_summary():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[
            tool_response(),
            text_response("The artifact exists, but one constraint blocks promotion."),
        ],
    )
    events = []
    result = {
        "status": "FEASIBLE",
        "parts": [{"ref": "U1"}],
        "nets": ["VIN"],
        "findings": [],
        "blockers": ["constraint Power/routing: VIN is unrouted"],
        "warnings": [],
        "promotion_status": "constraint_blocked",
        "constraint_manifest": {"version": 2},
        "constraint_receipt": {
            "hard_gate": "blocked",
            "promotable": False,
            "net_classes": [
                {
                    "net_class": "Power",
                    "checks": [
                        {
                            "name": "routing",
                            "status": "violated",
                            "detail": "VIN is unrouted",
                        },
                        {
                            "name": "allowed_layers",
                            "status": "verified",
                            "detail": "Layers match",
                        },
                    ],
                }
            ],
            "mechanical": [
                {
                    "name": "component_height",
                    "status": "unresolved",
                    "detail": "No height metadata",
                }
            ],
            "blockers": [
                {
                    "scope": "Power",
                    "name": "routing",
                    "status": "violated",
                    "detail": "VIN is unrouted",
                    "evidence": {"unrouted": ["VIN"]},
                }
            ],
        },
    }

    run_orchestrator(
        message="make a regulator",
        clarification="5 V input",
        model=model,
        session_id="constraint-summary",
        generate=lambda: result,
        emit=events.append,
        debug=False,
    )

    tool_summary = next(
        event["result"] for event in events if event["event"] == "tool.done"
    )
    assert tool_summary["promotion_status"] == "constraint_blocked"
    assert tool_summary["constraint_manifest_version"] == 2
    assert tool_summary["constraint_receipt"] == {
        "hard_gate": "blocked",
        "promotable": False,
        "verified": 1,
        "violated": 1,
        "unresolved": 1,
        "blockers": [
            {
                "scope": "Power",
                "name": "routing",
                "status": "violated",
                "detail": "VIN is unrouted",
            }
        ],
    }


def test_reasoning_effort_reaches_adk_without_obsolete_sampling_controls():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[text_response("Which input voltage?")],
    )
    before = len(model.requests)

    run_orchestrator(
        message="make a regulator",
        model=model,
        thinking_level="high",
        session_id="thinking-session",
        generate=lambda: None,
        emit=lambda event: None,
        debug=False,
    )

    request = model.requests[before]
    assert request.config.thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert request.config.temperature is None


def test_each_adk_model_round_trip_passes_through_the_pre_call_hook():
    model = FakeLlm(
        model="fake-orchestrator",
        responses=[tool_response(), text_response("Done.")],
    )
    calls = []

    run_orchestrator(
        message="make a regulator",
        clarification="5 V input",
        model=model,
        session_id="paced-session",
        generate=lambda: {"status": "FEASIBLE"},
        emit=lambda event: None,
        before_model_call=lambda: calls.append("called"),
    )

    assert calls == ["called", "called"]


# ------------------------------------------------ root failover (2026-09-13)
# The root was on gemini-3.7-flash, got 503 UNAVAILABLE, and the chat run
# failed outright while the worker behind it had a three-tier ladder.

from silkscreen.agents.adk.failover import (  # noqa: E402
    FailoverLlm,
    orchestrator_ladder,
)
from silkscreen.agents.model import (  # noqa: E402
    CHEAP_MODEL,
    FALLBACK_MODEL,
    ModelError,
)


class DownLlm(BaseLlm):
    error: str
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        raise RuntimeError(self.error)
        yield  # pragma: no cover - makes this an async generator


UNAVAILABLE = "503 UNAVAILABLE. This model is currently experiencing high demand."
DAILY = (
    "429 RESOURCE_EXHAUSTED. 'quotaId': "
    "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'retryDelay': '34s'"
)


def _ladder(first, second, **kw):
    return FailoverLlm(
        model=first.model, tiers=[first, second], backoff_s=0.0, cooldowns={}, **kw
    )


def test_the_ladder_steps_down_through_flash_and_flash_lite_without_repeats():
    assert orchestrator_ladder("gemini-3.7-flash") == [
        "gemini-3.7-flash",
        FALLBACK_MODEL,
        CHEAP_MODEL,
    ]
    assert orchestrator_ladder(FALLBACK_MODEL) == [FALLBACK_MODEL, CHEAP_MODEL]


def test_a_503_on_the_root_fails_over_and_says_so():
    down = DownLlm(model="gemini-3.7-flash", error=UNAVAILABLE)
    backup = FakeLlm(model="gemini-3.5-flash", responses=[text_response("Which voltage?")])
    events: list[dict] = []
    model = _ladder(down, backup)

    outcome = run_orchestrator(
        message="make a regulator",
        model=model,
        session_id="failover-1",
        generate=lambda: None,
        emit=events.append,
    )

    assert outcome.assistant == "Which voltage?"
    assert down.calls == 2, "a 503 keeps its one in-tier retry"
    retries = [e for e in events if e["event"] == "model.retry"]
    assert [r["provider"] for r in retries] == ["gemini-3.7-flash"] * 2
    assert all(r["layer"] == "orchestrator" and "503" in r["error"] for r in retries)
    call = next(e for e in events if e["event"] == "model.call")
    assert call["model"] == "gemini-3.5-flash", "the event names the tier that answered"
    assert outcome.model == "gemini-3.5-flash"


def test_a_daily_quota_refusal_parks_the_root_tier_for_the_next_turn():
    table: dict[str, float] = {}
    down = DownLlm(model="gemini-3.7-flash", error=DAILY)
    backup = FakeLlm(
        model="gemini-3.5-flash",
        responses=[text_response("one?"), text_response("two?")],
    )
    for turn in ("a", "b"):
        events: list[dict] = []
        model = FailoverLlm(
            model=down.model, tiers=[down, backup], backoff_s=0.0, cooldowns=table
        )
        run_orchestrator(
            message="make a regulator",
            model=model,
            session_id=f"failover-{turn}",
            generate=lambda: None,
            emit=events.append,
        )
    assert down.calls == 1, "a quota wall is asked once, not once per attempt or turn"
    skipped = [e for e in events if e["event"] == "model.retry"]
    assert skipped and "skipped" in skipped[0]["error"]


def test_every_root_tier_failing_names_every_tier():
    import pytest

    a = DownLlm(model="gemini-3.7-flash", error=UNAVAILABLE)
    b = DownLlm(model="gemini-3.5-flash", error=UNAVAILABLE)
    with pytest.raises(ModelError) as caught:
        run_orchestrator(
            message="make a regulator",
            model=_ladder(a, b, attempts=[1, 1]),
            session_id="failover-dead",
            generate=lambda: None,
            emit=lambda e: None,
        )
    assert "gemini-3.7-flash" in str(caught.value)
    assert "gemini-3.5-flash" in str(caught.value)


def test_a_bare_model_id_gets_the_ladder(monkeypatch):
    """The service passes a string; the root must not run it without failover."""
    from silkscreen.agents.adk import failover

    built: list[str] = []

    def spy(model, **kwargs):
        built.append(model)
        return FailoverLlm(
            model=model,
            tiers=[FakeLlm(model=model, responses=[text_response("ok?")])],
            cooldowns={},
        )

    monkeypatch.setattr(failover, "build_failover_llm", spy)
    outcome = run_orchestrator(
        message="make a regulator",
        model="gemini-3.7-flash",
        session_id="failover-str",
        generate=lambda: None,
        emit=lambda e: None,
    )
    assert built == ["gemini-3.7-flash"]
    assert outcome.assistant == "ok?"


def test_a_proposal_error_from_the_board_tool_reaches_the_caller_unwrapped():
    """The service can only name the failure if the root does not rewrap it."""
    import pytest
    from silkscreen.agents.propose import ProposalAttempt, ProposalError

    model = FakeLlm(model="fake-orchestrator", responses=[tool_response()])

    def generate():
        raise ProposalError(
            "No valid circuit after 2 attempts.",
            [ProposalAttempt(round=0, raw="{}", errors=["bad"])],
        )

    with pytest.raises(ProposalError):
        run_orchestrator(
            message="a robotic arm",
            model=model,
            session_id="proposal-error",
            generate=generate,
            emit=lambda e: None,
        )
