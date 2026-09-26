"""The tool list itself: shapes, schemas, descriptions, result form."""

import re

import pytest
from silkscreen.mcp.server import SCHEMA_KEYWORDS, handle

from alexabot import tools
from alexabot.tests.fakes import call, error, ok, wait_for

NAMES = ["start_board_design", "answer_design_questions", "continue_design",
         "board_status", "explain_finding", "recall_my_boards"]


def _walk(schema, where="#"):
    """Every keyword a schema uses, found structurally (test_mcp.py's walk)."""
    if not isinstance(schema, dict):
        return
    for key in schema:
        yield key, where
    for name, sub in schema.get("properties", {}).items():
        yield from _walk(sub, f"{where}/properties/{name}")
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            yield from _walk(schema[key], f"{where}/{key}")
    for key in ("anyOf", "oneOf"):
        for index, sub in enumerate(schema.get(key, ())):
            yield from _walk(sub, f"{where}/{key}/{index}")


def test_six_tools_with_title_annotations_and_both_schemas():
    assert [t["name"] for t in tools.TOOLS] == NAMES
    for tool in tools.TOOLS:
        assert tool["title"] and tool["annotations"]["title"] == tool["title"]
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] == "object"
        hints = tool["annotations"]
        if tool["name"] in ("board_status", "explain_finding", "recall_my_boards"):
            assert hints["readOnlyHint"] is True
        else:
            assert hints["readOnlyHint"] is False
            assert hints["destructiveHint"] is False
            assert hints["idempotentHint"] is True
    for tool in tools.TOOLS:
        assert "execution" not in tool  # no MCP tasks: python-sdk defers them


def test_every_schema_uses_only_validator_keywords():
    for tool in tools.TOOLS:
        for key in ("inputSchema", "outputSchema"):
            unknown = {k for k, _ in _walk(tool[key])} - SCHEMA_KEYWORDS
            assert not unknown, (tool["name"], key, unknown)


def test_schemas_agree_with_jsonschema(toolset, fake_steps):
    jsonschema = pytest.importorskip("jsonschema")
    for tool in tools.TOOLS:
        for key in ("inputSchema", "outputSchema"):
            jsonschema.Draft202012Validator.check_schema(tool[key])
    started = ok(call(toolset, "start_board_design",
                      {"intent": "a board", "request_id": "req-00000001"}))
    status = tools.TOOLS[3]["outputSchema"]
    jsonschema.Draft202012Validator(status).validate(started)
    sid = started["session_id"]
    wait_for(lambda: ok(call(toolset, "board_status", {"session_id": sid}))["state"]
             == "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    call(toolset, "continue_design", {"session_id": sid})
    done = wait_for(lambda: (s := ok(call(toolset, "board_status",
                                          {"session_id": sid})))["state"] == "done"
                    and s)
    jsonschema.Draft202012Validator(status).validate(done)
    explained = ok(call(toolset, "explain_finding", {"session_id": sid}))
    jsonschema.Draft202012Validator(tools.TOOLS[4]["outputSchema"]).validate(explained)
    recalled = ok(call(toolset, "recall_my_boards", {}))
    jsonschema.Draft202012Validator(tools.TOOLS[5]["outputSchema"]).validate(recalled)


def test_each_description_first_sentence_stands_alone():
    """The bridge lists tools by their first sentence (``prompt.ts``)."""
    for tool in tools.TOOLS:
        first = re.split(r"(?<=[.;])\s", tool["description"], maxsplit=1)[0]
        assert len(first.split()) >= 5, first
        assert not first.startswith(("It ", "This ", "Call ")), first


def test_success_results_carry_speech_first_then_json_and_structured_content(
    toolset,
):
    result = call(toolset, "start_board_design",
                  {"intent": "a board", "request_id": "req-00000001"})
    structured = ok(result)
    assert [block["type"] for block in result["content"]] == ["text", "text"]
    assert result["content"][0]["text"] == structured["speech"]


def test_initialize_carries_instructions(toolset):
    reply = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"}}, toolset=toolset)
    result = reply["result"]
    assert result["serverInfo"] == tools.SERVER_INFO
    assert result["instructions"] == tools.INSTRUCTIONS
    assert "never orders" in result["instructions"]
    assert "tasks" not in result["capabilities"]


def test_errors_carry_no_exception_text(toolset, runner, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("SECRET-INTERNAL /Users/me/.env")

    monkeypatch.setattr(runner, "board", boom)
    text = error(call(toolset, "board_status", {}))
    assert text == "Something went wrong on my side. Please try that again."
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "SECRET" not in err


def test_tools_never_receive_the_jsonrpc_id(runner):
    seen = []
    toolset = tools.toolset(runner)
    original = toolset.dispatch["recall_my_boards"]
    toolset.dispatch["recall_my_boards"] = lambda args: (seen.append(args),
                                                         original(args))[1]
    call(toolset, "recall_my_boards", {"query": "usb"}, req_id="rpc-123")
    call(toolset, "recall_my_boards", {}, req_id=77)
    assert seen == [{"query": "usb"}, {}]


def test_engine_tools_are_not_exposed(toolset):
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    toolset=toolset)["result"]["tools"]
    assert [t["name"] for t in listed] == NAMES
    refused = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "generate_board",
                                 "arguments": {"intent": "x"}}}, toolset=toolset)
    assert refused["error"]["code"] == -32602


def test_explain_bounds_and_review_states(toolset):
    started = ok(call(toolset, "start_board_design",
                      {"intent": "a board", "request_id": "req-00000001"}))
    sid = started["session_id"]
    not_run = ok(call(toolset, "explain_finding", {"session_id": sid}))
    assert (not_run["review_status"], not_run["count"], not_run["finding"]) == (
        "not_run", None, None)
    wait_for(lambda: ok(call(toolset, "board_status", {"session_id": sid}))["state"]
             == "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    call(toolset, "continue_design", {"session_id": sid})
    wait_for(lambda: ok(call(toolset, "board_status", {"session_id": sid}))["state"]
             == "done")
    first = ok(call(toolset, "explain_finding", {"session_id": sid}))
    # Numbered blocker first, whatever order the critic wrote them in.
    assert first["finding"]["severity"] == "blocker" and first["count"] == 3
    assert first["finding"]["refs"] == ["U1", "C2"]
    assert "U1 and C2" in first["speech"] and "haven't applied" in first["speech"]
    beyond = error(call(toolset, "explain_finding", {"session_id": sid, "which": 4}))
    assert beyond == "This board has 3 findings; which runs 1 to 3."
