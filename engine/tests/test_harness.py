"""The agent harness, offline: a scripted tool model drives every path.

Each test states which primitive it pins. None of them needs a key, a
network or KiCad; the one that mentions ERC fakes its absence to prove the
``unverified`` state is neither green nor red.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from silkscreen.agents.harness import (
    Agent,
    Budget,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwire,
    Message,
    ReflectAndRetry,
    Runner,
    ScriptedToolModel,
    ToolCall,
    Turn,
    function_tool,
    verifier_tool,
)
from silkscreen.agents.harness.design import DESIGN_MARKER, run_design
from silkscreen.agents.model import ModelError
from silkscreen.verify import Clause, Verdict

LDO = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "c_in": {"type": "capacitor", "value": "10uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
        "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "c_out.1"],
    },
}


def _forgotten_ground() -> dict:
    raw = copy.deepcopy(LDO)
    raw["nets"]["GND"].remove("AMS1117-3.3.GND")
    return raw


# ---- the design agent -------------------------------------------------------


def test_a_clean_proposal_finishes_ok_with_a_receipt():
    model = ScriptedToolModel({DESIGN_MARKER: [Turn.final(json.dumps(LDO))]})
    events = []
    result = run_design("an LDO", model=model, on_event=events.append, erc=False)
    assert result.status == "ok"
    assert set(result.verdicts) == {"validate_circuit", "electrical_completeness"}
    assert all(v.ok for v in result.verdicts.values())
    assert result.receipt.evidence_ids == {
        "validate_circuit#1",
        "electrical_completeness#1",
    }
    kinds = [e["event"] for e in events]
    assert kinds.count("harness.turn") == 1
    assert kinds.count("harness.verdict") == 2
    assert "propose.round" not in kinds
    assert result.context["spec"].part_count() == 3


def test_a_forgotten_ground_is_a_repair_round_then_green():
    """The headline path: the loop refuses to finish while a verifier is red."""
    model = ScriptedToolModel(
        {
            DESIGN_MARKER: [
                Turn.final(json.dumps(_forgotten_ground())),
                Turn.final(json.dumps(LDO)),
            ]
        }
    )
    events = []
    result = run_design("an LDO", model=model, on_event=events.append, erc=False)
    assert result.status == "ok"
    rounds = [e for e in events if e["event"] == "propose.round"]
    assert len(rounds) == 1
    assert "AMS1117-3.3.GND" in rounds[0]["first_error"]
    assert result.receipt.spent["rounds"] == 1
    assert result.receipt.spent["model_calls"] == 2
    # The repair message the model saw names the pin, not a prompt paragraph.
    repair = model.calls[1][-1]
    assert repair.role == "user" and "AMS1117-3.3.GND" in repair.text
    # The final receipt is the green verdict, not the red one.
    assert result.verdicts["electrical_completeness"].ok


def test_the_model_may_check_a_draft_with_a_tool_before_answering():
    model = ScriptedToolModel(
        {
            DESIGN_MARKER: [
                Turn.calls(
                    ToolCall(
                        "",
                        "check_circuit",
                        {"circuit_json": json.dumps(_forgotten_ground())},
                    )
                ),
                Turn.final(json.dumps(LDO)),
            ]
        }
    )
    result = run_design("an LDO", model=model, erc=False)
    assert result.status == "ok"
    tool_turn = model.calls[1]
    assert tool_turn[-1].role == "user"
    (tool_result,) = tool_turn[-1].tool_results
    assert tool_result.call_id == "h-1-0"  # minted, deterministic
    assert tool_result.name == "check_circuit" and not tool_result.is_error
    assert tool_result.output["electrical_completeness"]["status"] == "blocked"
    assert result.receipt.spent["check_circuit"] == 1


def test_a_proposal_that_never_passes_ends_on_the_budget():
    bad = json.dumps(_forgotten_ground())
    model = ScriptedToolModel({DESIGN_MARKER: [Turn.final(bad)] * 3})
    events = []
    result = run_design(
        "an LDO",
        model=model,
        on_event=events.append,
        erc=False,
        budget=Budget(max_turns=3),
    )
    assert result.status == "budget"
    assert "3 turns" in result.reason
    assert [e["round"] for e in events if e["event"] == "propose.round"] == [1, 2, 3]
    assert events[-1]["event"] == "harness.blocked"


def test_erc_without_kicad_ends_unverified_not_ok(monkeypatch):
    monkeypatch.setattr("silkscreen.verify.kicad.kicad_cli_path", lambda: None)
    model = ScriptedToolModel({DESIGN_MARKER: [Turn.final(json.dumps(LDO))]})
    result = run_design("an LDO", model=model, erc=True)
    assert result.status == "unverified"
    assert "kicad-cli" in result.reason
    assert result.final_output is not None
    assert result.verdicts["erc"].status == "unverified"


# ---- the loop's primitives ---------------------------------------------------


def _ok_verifier(name="always"):
    return verifier_tool(
        name, "always ok", lambda ctx: Verdict(name, (Clause("c", True, "fine"),))
    )


def test_a_tool_that_needs_approval_interrupts_the_run():
    @function_tool(needs_approval=True)
    def order_board(context, vendor: str) -> str:
        """Place a paid order."""
        raise AssertionError("must not run")

    agent = Agent("t", "MARK", tools=(order_board,))
    model = ScriptedToolModel(
        {"MARK": [Turn.calls(ToolCall("c1", "order_board", {"vendor": "x"}))]}
    )
    result = Runner().run(agent, "go", model=model)
    assert result.status == "interrupted"
    assert result.interruption.tool == "order_board"
    assert result.interruption.call.arguments == {"vendor": "x"}


def test_a_failing_tool_reflects_then_blocks():
    calls = {"n": 0}

    @function_tool
    def flaky(context, x: int) -> str:
        """Fails every time."""
        calls["n"] += 1
        raise RuntimeError("boom")

    agent = Agent("t", "MARK", tools=(flaky,))
    turns = [Turn.calls(ToolCall("", "flaky", {"x": 1}))] * 5
    model = ScriptedToolModel({"MARK": turns})
    result = Runner().run(
        agent, "go", model=model, reflect=ReflectAndRetry(max_retries=2)
    )
    assert result.status == "blocked"
    assert "failed 3 times" in result.reason
    first_result = model.calls[1][-1].tool_results[0]
    assert first_result.is_error
    assert first_result.output["response_type"] == "reflect_and_retry"
    assert first_result.output["retry_count"] == 1
    assert calls["n"] == 3


def test_an_unknown_tool_is_an_error_result_not_a_crash():
    agent = Agent("t", "MARK", tools=(_ok_verifier(),))
    model = ScriptedToolModel(
        {"MARK": [Turn.calls(ToolCall("", "nope", {})), Turn.final("done")]}
    )
    result = Runner().run(agent, "go", model=model)
    assert result.status == "ok"
    err = model.calls[1][-1].tool_results[0]
    assert err.is_error and "unknown tool" in err.output


def test_input_guardrails_run_before_any_model_call():
    guard = InputGuardrail(
        "no_boards", lambda text, ctx: GuardrailFunctionOutput("board" in text, "no")
    )
    agent = Agent("t", "MARK", input_guardrails=(guard,))
    model = ScriptedToolModel({"MARK": [Turn.final("x")]})
    with pytest.raises(InputGuardrailTripwire):
        Runner().run(agent, "a board", model=model)
    assert model.calls == []


def test_a_summary_claim_without_a_verifier_is_refused_then_repaired():
    agent = Agent(
        "t",
        "MARK",
        tools=(_ok_verifier("erc_like"),),
        required_verifiers=("erc_like",),
        output_type="summary",
        artifact_key=None,
    )
    bad = json.dumps(
        {"claims": [{"text": "DRC clean", "verifier": "drc"}], "notes": []}
    )
    good = json.dumps(
        {
            "claims": [
                {
                    "text": "ERC clean",
                    "verifier": "erc_like",
                    "evidence_id": "erc_like#1",
                }
            ],
            "notes": ["DRC was not run"],
        }
    )
    model = ScriptedToolModel({"MARK": [Turn.final(bad), Turn.final(good)]})
    events = []
    result = Runner().run(agent, "go", model=model, on_event=events.append)
    assert result.status == "ok"
    assert result.summary.claims[0].verifier == "erc_like"
    assert result.summary.notes == ("DRC was not run",)
    rounds = [e for e in events if e["event"] == "propose.round"]
    assert len(rounds) == 1 and "'drc', which did not run" in rounds[0]["first_error"]


def test_a_refusal_ends_blocked():
    agent = Agent("t", "MARK")
    model = ScriptedToolModel(
        {
            "MARK": [
                Turn("scripted", "", stop_reason="refused", raw_stop_reason="refusal")
            ]
        }
    )
    result = Runner().run(agent, "go", model=model)
    assert result.status == "blocked" and "refused" in result.reason


def test_a_provider_change_mid_conversation_is_an_error():
    agent = Agent("t", "MARK", tools=(_ok_verifier(),))
    model = ScriptedToolModel(
        {
            "MARK": [
                Turn.calls(ToolCall("", "always", {}), provider="gemini"),
                Turn.final("x", provider="claude"),
            ]
        }
    )
    with pytest.raises(ModelError, match="provider changed"):
        Runner().run(agent, "go", model=model)


def test_the_scripted_model_keeps_one_cursor_per_marker_and_runs_dry_loudly():
    model = ScriptedToolModel(
        {"A": [Turn.final("a1"), Turn.final("a2")], "B": [Turn.final("b1")]}
    )
    assert model.generate_turn([Message.user("A")], tools=[]).text == "a1"
    assert model.generate_turn([Message.user("B")], tools=[]).text == "b1"
    assert model.generate_turn([Message.user("A")], tools=[]).text == "a2"
    with pytest.raises(ModelError, match="ran out of turns for marker 'A'"):
        model.generate_turn([Message.user("A")], tools=[])
    with pytest.raises(ModelError, match="matched no marker"):
        model.generate_turn([Message.user("zzz")], tools=[])


def test_budget_derives_from_the_effort_profile():
    profile = SimpleNamespace(max_repairs=3, time_limit_s=20.0)
    budget = Budget.from_effort(profile)
    assert budget.max_turns == 9 and budget.max_model_calls == 9
    assert budget.max_tool_seconds == 80.0


# ---- the adapters, without a network ------------------------------------------


def test_gemini_adapter_reads_calls_with_signatures_and_replays_native():
    from silkscreen.agents.harness.gemini import GeminiToolModel

    part_call = SimpleNamespace(
        function_call=SimpleNamespace(id=None, name="erc", args={}),
        thought_signature=b"sig",
        text=None,
        thought=False,
    )
    content = SimpleNamespace(role="model", parts=[part_call])
    resp = SimpleNamespace(
        candidates=[
            SimpleNamespace(content=content, finish_reason=SimpleNamespace(name="STOP"))
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=5,
            thoughts_token_count=3,
            cached_content_token_count=0,
        ),
    )
    adapter = GeminiToolModel("gemini-x", client=SimpleNamespace())
    turn = adapter._turn_of(resp)
    assert turn.stop_reason == "tool_calls" and turn.raw_stop_reason == "STOP"
    assert turn.tool_calls[0].signature == b"sig" and turn.tool_calls[0].id == ""
    assert turn.usage.thinking_tokens == 3
    assert turn.native is content
    # Replay: the assistant message goes back as the very object; a minted id
    # is not sent as a function_response id.
    history = [
        Message.user("hi"),
        Message.from_turn(turn),
        Message.user(
            tool_results=(
                __import__(
                    "silkscreen.agents.harness.model", fromlist=["ToolResult"]
                ).ToolResult("h-1-0", "erc", {"ok": True}),
            )
        ),
    ]
    contents = adapter._contents(history)
    assert contents[1] is content
    fr = contents[2].parts[0].function_response
    assert fr.name == "erc" and fr.id is None and fr.response == {"ok": True}
    foreign = [
        Message.user("hi"),
        Message(role="assistant", provider="claude", native=[{}]),
    ]
    with pytest.raises(ModelError, match="cannot be replayed"):
        adapter._contents(foreign)


def test_claude_adapter_maps_tool_use_and_keeps_thinking_blocks():
    from silkscreen.agents.harness.claude import ClaudeToolModel

    base = SimpleNamespace(
        model="claude-x",
        wire_model="claude-x",
        effort="medium",
        backend="api",
        _client=None,
    )
    adapter = ClaudeToolModel(base)  # type: ignore[arg-type]
    blocks = [
        SimpleNamespace(
            type="thinking",
            model_dump=lambda exclude_none: {
                "type": "thinking",
                "thinking": "",
                "signature": "s",
            },
        ),
        SimpleNamespace(
            type="tool_use",
            id="toolu_1",
            name="erc",
            input={"a": 1},
            model_dump=lambda exclude_none: {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "erc",
                "input": {"a": 1},
            },
        ),
    ]
    message = SimpleNamespace(
        content=blocks,
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=7, output_tokens=2, cache_read_input_tokens=1
        ),
    )
    turn = adapter._turn_of(message)
    assert turn.stop_reason == "tool_calls" and turn.tool_calls[0].id == "toolu_1"
    assert turn.native[0]["type"] == "thinking" and turn.usage.cache_read_tokens == 1
    kwargs = adapter.request_kwargs(
        [
            Message.user("hi"),
            Message.from_turn(turn),
            Message.user(
                tool_results=(
                    __import__(
                        "silkscreen.agents.harness.model", fromlist=["ToolResult"]
                    ).ToolResult("toolu_1", "erc", "ok"),
                )
            ),
        ],
        tools=[
            SimpleNamespace(name="erc", description="d", parameters={"type": "object"})
        ],
        system="sys",
        max_output_tokens=100,
    )
    assert kwargs["tools"][0]["input_schema"] == {"type": "object"}
    assert kwargs["messages"][1] == {"role": "assistant", "content": turn.native}
    assert kwargs["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "ok",
    }
    assert kwargs["thinking"] == {"type": "adaptive"} and kwargs["output_config"] == {
        "effort": "medium"
    }
