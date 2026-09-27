"""MCP: the protocol itself, not just the tools behind it.

The conformance tests at the bottom are one per MUST in the 2025-11-25
lifecycle, base-protocol, stdio and tools pages that applies to this server,
each named for its rule; the HTTP transport's are in test_mcp_http.py.
"""

import io
import json
import os
import re
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import silkscreen.mcp.server as mcp_server
from silkscreen.mcp.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SCHEMA_KEYWORDS,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOLS,
    RateLimiter,
    Server,
    handle,
    schema_errors,
)
from silkscreen.spice.simulators import NgspiceSimulator

CIRCUIT = {
    # ``pins`` maps a pin *name* to its pad number (netlist.Device). This
    # fixture once had the map backwards -- names "1".."3" on pads "GND",
    # "VOUT", "VIN" -- and the board drew U1 with no net on any pad until
    # board._pad_errors began refusing a pin with no pad to land on.
    "devices": {"U1": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "C1": {"type": "capacitor", "value": "10uF"},
        "C2": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VIN": ["U1.VIN", "C1.1"],
        "GND": ["U1.GND", "C1.2", "C2.2"],
        "VOUT": ["U1.VOUT", "C2.1"],
    },
}


@pytest.fixture(autouse=True)
def _unlimited_tool_calls(monkeypatch):
    """This file makes more tool calls a minute than the default limit allows.
    The limiter has its own test, which installs its own."""
    monkeypatch.setattr(mcp_server, "LIMITER", RateLimiter(1_000_000))


def rpc(method, params=None, req_id=1):
    return handle(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    )


def call(name, arguments=None):
    return rpc("tools/call", {"name": name, "arguments": arguments or {}})


def payload(response):
    """The JSON a successful tool call carried back.

    A tool error is plain text, not JSON; say so with the text rather than
    surfacing it as a JSONDecodeError that names nothing. When the tool also
    returned ``structuredContent`` it must be the same object as the text
    (``server/tools.mdx:322``), which every successful call here re-checks.
    """
    result = response["result"]
    content = result["content"][0]["text"]
    assert not result.get("isError"), f"tool call failed: {content}"
    body = json.loads(content)
    if "structuredContent" in result:
        assert result["structuredContent"] == body
    return body


def error_text(response):
    """The text of a tool execution error: a result with ``isError`` set."""
    result = response["result"]
    assert result["isError"] is True, f"expected a tool error: {result}"
    assert "structuredContent" not in result, "a failure has no structured output"
    return result["content"][0]["text"]


def error_payload(response):
    """The JSON a failed tool call carried back (simulate_circuit's stages)."""
    return json.loads(error_text(response))


def assert_error(response, code):
    """A JSON-RPC error with this code. Error codes MUST be integers
    (``basic/index.mdx:93``), so every error test checks that too."""
    error = response["error"]
    assert isinstance(error["code"], int) and not isinstance(error["code"], bool)
    assert error["code"] == code, error
    assert isinstance(error["message"], str) and error["message"]
    return error


def rpc_line(method, req_id, params):
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def stdio(*lines):
    """Run the stdio server over ``lines`` and return what it wrote, by line."""
    raw = b"\n".join(
        line if isinstance(line, bytes) else line.encode() for line in lines
    )
    out = io.StringIO()
    Server(stdin=io.BytesIO(raw), stdout=out).serve_forever()
    return out.getvalue().splitlines()


def test_initialize_reports_protocol_and_server():
    result = rpc("initialize")["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "silkscreen"
    assert "tools" in result["capabilities"]


def test_initialized_notification_gets_no_reply():
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping():
    assert rpc("ping")["result"] == {}


def test_tools_list_is_well_formed():
    tools = rpc("tools/list")["result"]["tools"]
    assert len(tools) == len(TOOLS)
    for tool in tools:
        assert tool["name"] and tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_unknown_method_is_a_jsonrpc_error():
    err = rpc("does/not/exist")["error"]
    assert err["code"] == METHOD_NOT_FOUND


def test_wrong_jsonrpc_version_is_rejected():
    assert "error" in handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})


def test_unknown_tool_is_a_jsonrpc_error():
    assert "error" in call("no_such_tool")


def test_response_id_matches_the_request():
    assert rpc("ping", req_id="abc-123")["id"] == "abc-123"


def test_validate_circuit_accepts_a_good_circuit():
    body = payload(call("validate_circuit", CIRCUIT))
    assert body == {"valid": True, "devices": 1, "passives": 2, "nets": 3}


def test_validate_circuit_reports_every_error_at_once():
    bad = {
        "devices": {"U1": {"pins": {"1": "GND"}}},
        "nets": {"GND": ["U1.1", "U1.99"], "FLOAT": ["U1.1"]},
    }
    body = payload(call("validate_circuit", bad))
    assert body["valid"] is False
    assert len(body["errors"]) >= 2, "all failures, not just the first"


def test_validate_circuit_refuses_a_pin_with_no_pad_like_build_board_does():
    """Pins keyed number-to-name (the old fixture's mistake) parse as names
    "1".."3" on pads "GND"... which a SOT-223 does not have. validate_circuit
    and build_board must give the same answer about that circuit."""
    backwards = {
        "devices": {"U1": {"pins": {"1": "GND", "2": "VOUT", "3": "VIN"}}},
        "passives": {"C1": {"type": "capacitor", "value": "10uF"}},
        "nets": {"VIN": ["U1.3", "C1.1"], "GND": ["U1.1", "C1.2"]},
    }
    body = payload(call("validate_circuit", backwards))
    assert body["valid"] is False
    assert any("has no pad for pin(s)" in e for e in body["errors"])
    built = call("build_board", {"circuit": backwards, "time_limit_s": 2})
    assert built["result"]["isError"] is True
    assert "has no pad for pin(s)" in built["result"]["content"][0]["text"]


@pytest.mark.parametrize(
    "circuit, problem",
    [
        (
            {"passives": {"R1": {"type": "resistor"}}},
            "circuit: missing required property 'nets'",
        ),
        ({"nets": []}, "circuit.nets: [] is not of type object"),
        # The IR alone accepts this one. build_board refuses it for the
        # missing ``nets``, so the verdict has to say so too.
        (
            {"devices": {"U1": {"pins": {}}}},
            "circuit: missing required property 'nets'",
        ),
    ],
    ids=["no-nets", "nets-a-list", "ir-accepts-schema-refuses"],
)
def test_validate_circuit_answers_a_malformed_shape_with_a_verdict(circuit, problem):
    """validate_circuit's schema is enforced like every tool's, but a failure
    is its answer (``valid: false``, isError false) -- a caller asking whether
    a circuit is right gets JSON for exactly the circuits that are wrong. Every
    other tool answers the same failure as an "Input validation error"."""
    body = payload(call("validate_circuit", circuit))
    assert body["valid"] is False
    assert body["errors"][0] == problem, body["errors"]
    built = call("build_board", {"circuit": circuit, "time_limit_s": 2})
    assert error_text(built).startswith("Input validation error")


def test_generate_footprint_returns_two_pads_for_a_chip_passive():
    body = payload(call("generate_footprint", {"package": "0805"}))
    assert body["name"] == "C_0805"
    assert len(body["pads"]) == 2
    assert body["pads"][0]["x_mm"] == -body["pads"][1]["x_mm"], "symmetric"
    assert body["courtyard_mm"][0] > 0


def test_generate_footprint_refuses_an_unknown_package():
    response = call("generate_footprint", {"package": "0201"})
    assert response["result"]["isError"] is True


def test_build_board_places_every_part():
    body = payload(call("build_board", {"circuit": CIRCUIT, "time_limit_s": 5}))
    assert [p["ref"] for p in body["parts"]] == ["U1", "C1", "C2"]
    assert body["board_mm"][0] > 0 and body["board_mm"][1] > 0


def test_emit_kicad_pcb_returns_a_parseable_board():
    body = payload(call("emit_kicad_pcb", {"circuit": CIRCUIT, "time_limit_s": 5}))
    text = body["kicad_pcb"]
    assert text.startswith("(kicad_pcb")
    assert text.count("(footprint") == 3
    assert body["bytes"] == len(text.encode())


def test_place_parts_returns_a_placement_per_part():
    body = payload(
        call(
            "place_parts",
            {
                "parts": [
                    {"ref": "U1", "width_mm": 10, "height_mm": 10},
                    {"ref": "C1", "width_mm": 2, "height_mm": 1.2},
                ],
                "time_limit_s": 3,
            },
        )
    )
    assert {p["ref"] for p in body["placements"]} == {"U1", "C1"}


def test_place_parts_rejects_an_empty_list():
    assert call("place_parts", {"parts": []})["result"]["isError"] is True


def test_a_bad_circuit_is_an_error_result_not_a_crash():
    response = call("build_board", {"circuit": {"nets": {"N": ["U9.1"]}}})
    assert response["result"]["isError"] is True


@pytest.mark.parametrize("name", [t["name"] for t in TOOLS])
def test_every_advertised_tool_is_dispatchable(name):
    assert "error" not in call(name, {}) or "unknown tool" not in str(call(name, {}))


def test_stdio_round_trip():
    """The transport, end to end, over real streams."""
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        "",
        "not json at all",
    ]
    out = io.StringIO()
    Server(stdin=io.StringIO("\n".join(lines)), stdout=out).serve_forever()
    replies = [json.loads(line) for line in out.getvalue().splitlines()]

    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[1]["result"]["tools"]
    assert replies[2]["error"]["message"].startswith("invalid JSON")


# --------------------------------------------------------------------------
# simulation tools
# --------------------------------------------------------------------------

#: A resistive divider, the simplest circuit with a checkable DC answer.
SIM_CIRCUIT = {
    "passives": {
        "Rtop": {"type": "resistor", "value": "10k"},
        "Rbot": {"type": "resistor", "value": "10k"},
        "Cbyp": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VIN": ["Rtop.1", "Cbyp.1"],
        "VMID": ["Rtop.2", "Rbot.1"],
        "GND": ["Rbot.2", "Cbyp.2"],
    },
}

SIM_BENCH = {
    "analysis": {"kind": "op"},
    "sources": [
        {"name": "V1", "positive": "VIN", "negative": "GND", "dc": 5.0}
    ],
}

needs_ngspice = pytest.mark.skipif(
    not NgspiceSimulator().is_available(),
    reason="ngspice is not installed on this machine",
)


def test_spice_capabilities_lists_measurement_kinds_without_local_paths(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "available_simulators",
        lambda: [SimpleNamespace(name="ngspice", executable="C:/private/ngspice")],
    )
    body = payload(call("spice_capabilities"))
    assert "rise_time" in body["measurement_kinds"]
    assert "within" in body["operators"]
    assert body["simulators"] == ["ngspice"]
    assert "private" not in json.dumps(body)


@pytest.mark.parametrize(
    "testbench",
    [
        {**SIM_BENCH, "title": "demo\n.control"},
        {**SIM_BENCH, "options": ["reltol=1e-3\n.control"]},
        {
            **SIM_BENCH,
            "models": {
                "Rtop": {
                    "kind": "model",
                    "name": "R",
                    "text": ".control\nshell echo injected\n.endc",
                }
            },
        },
        {
            "analysis": {"kind": "tran", "step": 1e-6, "stop": 1e-3},
            "sources": [
                {
                    "name": "V1",
                    "positive": "VIN",
                    "negative": "GND",
                    "dc": 5.0,
                    "transient": "PULSE(0 5 0 1n 1n 1u 2u)\n.control",
                }
            ],
        },
    ],
    ids=["title", "options", "model-program", "raw-transient"],
)
def test_simulate_circuit_rejects_raw_spice_before_launch(
    monkeypatch, testbench
):
    """None of these fields is in the testbench schema, so the input check
    refuses them before ``testbench_from_dict`` ever sees them (it refused
    them itself, at the ``testbench`` stage, before inputs were validated)."""
    launched = False

    def unexpected_launch(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("the simulator must not be launched")

    monkeypatch.setattr(mcp_server, "simulate_deck", unexpected_launch)
    text = error_text(
        call(
            "simulate_circuit",
            {"circuit": SIM_CIRCUIT, "testbench": testbench},
        )
    )
    assert text.startswith("Input validation error")
    assert launched is False


@pytest.mark.parametrize(
    "extra, refused_by",
    [
        ({"unexpected": True}, "schema"),
        # NaN passes every numeric bound in JSON Schema (it compares false
        # both ways), so the tool's own controls are what stop it.
        ({"timeout_s": float("nan")}, "request"),
        ({"timeout_s": float("inf")}, "schema"),
        ({"timeout_s": 121}, "schema"),
        ({"max_points": 2001}, "schema"),
        ({"max_points": 1.5}, "schema"),
        ({"simulator": "hspice"}, "schema"),
    ],
    ids=[
        "unknown-field",
        "nan-timeout",
        "infinite-timeout",
        "long-timeout",
        "too-many-points",
        "fractional-points",
        "unknown-simulator",
    ],
)
def test_simulate_circuit_rejects_unsafe_controls_before_launch(
    monkeypatch, extra, refused_by
):
    launched = False

    def unexpected_launch(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("the simulator must not be launched")

    monkeypatch.setattr(mcp_server, "simulate_deck", unexpected_launch)
    response = call(
        "simulate_circuit",
        {"circuit": SIM_CIRCUIT, "testbench": SIM_BENCH, **extra},
    )
    if refused_by == "schema":
        assert error_text(response).startswith("Input validation error")
    else:
        body = error_payload(response)
        assert body["ok"] is False
        assert body["stage"] == "request"
    assert launched is False


def test_simulate_circuit_reports_an_invalid_circuit_without_simulating():
    body = error_payload(call("simulate_circuit", {"circuit": {"nets": {"N": ["U9.1"]}},
                                             "testbench": SIM_BENCH}))
    assert body["ok"] is False
    assert body["stage"] == "circuit"
    assert body["errors"]


def test_simulate_circuit_collects_testbench_problems():
    """Problems only the circuit can reveal -- a net it lacks, a source SPICE
    cannot read -- all come back together from the testbench stage."""
    body = error_payload(
        call(
            "simulate_circuit",
            {
                "circuit": SIM_CIRCUIT,
                "testbench": {
                    "analysis": {"kind": "op"},
                    "sources": [
                        {"name": "V1", "positive": "NOPE", "negative": "GND", "dc": 5},
                        {"name": "X2", "positive": "VIN", "negative": "GND", "dc": 1},
                    ],
                },
            },
        )
    )
    assert body["ok"] is False
    assert body["stage"] == "testbench"
    assert len(body["errors"]) >= 2


def test_simulate_circuit_names_every_missing_field_of_the_shape_meant():
    """``kind: tran`` without step and stop: the schema layer names both, on
    the tran shape the caller meant rather than the op shape it misses by
    one error."""
    text = error_text(
        call(
            "simulate_circuit",
            {"circuit": SIM_CIRCUIT, "testbench": {"analysis": {"kind": "tran"}}},
        )
    )
    assert "'step'" in text and "'stop'" in text


def test_simulate_circuit_rejects_an_unknown_measurement_kind():
    body = error_payload(
        call(
            "simulate_circuit",
            {
                "circuit": SIM_CIRCUIT,
                "testbench": SIM_BENCH,
                "assertions": [
                    {
                        "name": "x",
                        "measurement": {"kind": "vibes", "signal": "VMID"},
                        "op": "<",
                        "value": 1,
                    }
                ],
            },
        )
    )
    assert body["ok"] is False
    assert "vibes" in str(body["errors"])


@needs_ngspice
def test_simulate_circuit_returns_a_verdict_with_the_measured_number():
    body = payload(
        call(
            "simulate_circuit",
            {
                "circuit": SIM_CIRCUIT,
                "testbench": SIM_BENCH,
                "assertions": [
                    {
                        "name": "midpoint is half the supply",
                        "measurement": {"kind": "final", "signal": "VMID"},
                        "op": "within",
                        "value": 2.5,
                        "tolerance": 0.01,
                        "unit": "V",
                    },
                    {
                        "name": "midpoint is 3.3 V",
                        "measurement": {"kind": "final", "signal": "VMID"},
                        "op": "within",
                        "value": 3.3,
                        "tolerance": 0.01,
                        "unit": "V",
                    },
                ],
            },
        )
    )
    assert body["ok"] is True
    assert body["passed"] is False
    first, second = body["assertions"]
    assert first["passed"] is True
    assert first["measured"] == pytest.approx(2.5, rel=1e-6)
    assert second["passed"] is False
    assert "midpoint is 3.3 V" in body["summary"]


@needs_ngspice
def test_simulate_circuit_without_assertions_returns_waveform_summary():
    body = payload(
        call(
            "simulate_circuit",
            {"circuit": SIM_CIRCUIT, "testbench": SIM_BENCH},
        )
    )
    assert body["ok"] is True
    assert body["result"]["analysis"] == "op"
    assert "v(VMID)" in body["result"]["signals"]


def test_simulate_circuit_refuses_a_device_with_no_model():
    """An IC has no behaviour in the IR. Dropping it would simulate a different
    circuit and report success, so the tool must name it and stop."""
    body = error_payload(
        call(
            "simulate_circuit",
            {
                "circuit": {
                    "devices": {"U1": {"pins": {"A": "1", "B": "2"}}},
                    "passives": {"R1": {"type": "resistor", "value": "1k"}},
                    "nets": {"NA": ["U1.A", "R1.1"], "GND": ["U1.B", "R1.2"]},
                },
                "testbench": {
                    "analysis": {"kind": "op"},
                    "sources": [
                        {"name": "V1", "positive": "NA",
                         "negative": "GND", "dc": 5.0}
                    ],
                },
            },
        )
    )
    assert body["ok"] is False
    assert body["stage"] == "testbench"
    assert "U1" in str(body["errors"])


def test_generate_board_writes_a_project_and_reports_findings(tmp_path, monkeypatch):
    """The one tool that spends model calls, driven offline through the seam."""
    from test_agents import _scripted_pipeline_model

    monkeypatch.setattr(mcp_server, "build_model", _scripted_pipeline_model)
    out = tmp_path / "board.kicad_pcb"
    res = call(
        "generate_board", {"intent": "a 3.3V motor driver board", "output": str(out)}
    )
    assert res["result"]["isError"] is False
    body = json.loads(res["result"]["content"][0]["text"])
    assert assert_conforms(res, "generate_board") == body
    assert body["files"]["board"] == str(out) and out.exists()
    assert (tmp_path / "board.kicad_sch").exists()
    assert body["review"]["ran"] is True and body["review"]["blockers"] == 1
    assert body["findings"][0]["severity"] == "blocker"
    assert isinstance(body["unrouted"], dict)


def test_generate_board_refuses_an_empty_intent():
    res = call("generate_board", {"intent": "  "})
    assert res["result"]["isError"] is True


def test_generate_board_default_output_is_under_the_home_boards_dir():
    p = mcp_server._default_output("A 3.3V LDO board!")
    assert p.name == "board.kicad_pcb"
    assert p.parent.parent == mcp_server.DEFAULT_BOARDS_DIR.expanduser()
    assert p.parent.name.endswith("-a-3-3v-ldo-board")


# --------------------------------------------------------------------------
# MCP 2025-11-25 conformance, one test per applicable MUST, named for it.
# Paths are in modelcontextprotocol/modelcontextprotocol at the 2025-11-25 tag.
# --------------------------------------------------------------------------


def _tool_schema(name, key="outputSchema"):
    return next(t for t in TOOLS if t["name"] == name)[key]


def assert_conforms(response, name):
    """``structuredContent`` checked by jsonschema itself, independently of the
    server's own validator -- a check written in terms of the code under test
    would share its blind spot."""
    jsonschema = pytest.importorskip("jsonschema")
    result = response["result"]
    assert result["isError"] is False, result["content"][0]["text"]
    structured = result["structuredContent"]
    jsonschema.Draft202012Validator(_tool_schema(name)).validate(structured)
    assert json.loads(result["content"][0]["text"]) == structured
    return structured


# basic/index.mdx:29 -- every message MUST be JSON-RPC 2.0.
@pytest.mark.parametrize(
    "message, code",
    [
        ("ping", INVALID_REQUEST),
        (5, INVALID_REQUEST),
        ({"id": 1, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": 5}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "result": {}}, INVALID_REQUEST),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]}, INVALID_PARAMS),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": "x"}, INVALID_PARAMS),
    ],
    ids=[
        "string",
        "number",
        "no-jsonrpc",
        "jsonrpc-1.0",
        "method-not-a-string",
        "no-method-no-result",
        "result-and-error",
        "result-without-id",
        "params-a-list",
        "params-a-string",
    ],
)
def test_every_message_must_be_jsonrpc_2_0(message, code):
    assert_error(handle(message), code)


def test_a_malformed_message_does_not_stop_the_stdio_server():
    """``params: [1]`` used to raise AttributeError out of ``handle`` and end
    the stdio loop; now it is -32602 and the next line is still answered."""
    lines = stdio(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    )
    first, second = (json.loads(line) for line in lines)
    assert_error(first, INVALID_PARAMS)
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}


# basic/index.mdx:48-49 -- request ids MUST be a string or an integer, not null.
@pytest.mark.parametrize("bad_id", [None, True, 1.5, {}, []])
def test_request_ids_must_be_a_string_or_an_integer_never_null(bad_id):
    response = handle({"jsonrpc": "2.0", "id": bad_id, "method": "ping"})
    assert_error(response, INVALID_REQUEST)
    assert "id" not in response, "an id that is not a RequestId cannot be echoed"


# basic/index.mdx:71 -- a result MUST carry the request's id.
@pytest.mark.parametrize("req_id", ["abc-123", 7, 0, -3])
def test_a_result_must_echo_the_request_id(req_id):
    assert rpc("ping", req_id=req_id)["id"] == req_id


# basic/index.mdx:91 -- an error MUST carry the id, unless it could not be read.
def test_an_error_must_echo_the_id_unless_it_could_not_be_read():
    assert rpc("no/such/method", req_id=9)["id"] == 9
    assert rpc("tools/call", {"name": 5}, req_id="x")["id"] == "x"
    [line] = stdio("{not json")
    parse_error = json.loads(line)
    assert_error(parse_error, PARSE_ERROR)
    assert "id" not in parse_error, "null is not a RequestId (schema.ts:124)"


# basic/index.mdx:98 -- the receiver of a notification MUST NOT respond.
@pytest.mark.parametrize(
    "method",
    [
        "ping",
        "initialize",
        "tools/list",
        "tools/call",
        "notifications/initialized",
        "notifications/cancelled",
        "no/such/method",
    ],
)
def test_a_notification_must_not_get_a_response(method):
    message = {"jsonrpc": "2.0", "method": method, "params": {}}
    assert handle(message) is None
    assert stdio(json.dumps(message)) == []


def test_a_tools_call_notification_never_runs_the_tool(monkeypatch):
    """Without an id nobody can read the answer, and generate_board is paid."""
    ran = []
    monkeypatch.setitem(mcp_server.DISPATCH, "generate_board", ran.append)
    notification = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "generate_board", "arguments": {"intent": "an LDO"}},
    }
    assert handle(notification) is None
    assert ran == []


def test_a_response_from_the_client_gets_no_reply():
    error = {"code": -1, "message": "declined"}
    assert handle({"jsonrpc": "2.0", "id": 1, "result": {}}) is None
    assert handle({"jsonrpc": "2.0", "id": 1, "error": error}) is None
    # An error response may omit an id it could not read (schema.ts:163).
    assert handle({"jsonrpc": "2.0", "error": error}) is None


# basic/index.mdx:182,188 and server/tools.mdx:199 -- schemas MUST be valid
# JSON Schema under the default 2020-12 dialect, inputSchema an object.
def test_every_schema_must_be_valid_json_schema_2020_12():
    jsonschema = pytest.importorskip("jsonschema")
    for tool in TOOLS:
        for key in ("inputSchema", "outputSchema"):
            if key in tool:
                assert "$schema" not in tool[key], "the default dialect applies"
                jsonschema.Draft202012Validator.check_schema(tool[key])
        assert tool["inputSchema"]["type"] == "object"


# basic/index.mdx:198 -- MUST NOT make assumptions about reserved _meta values.
def test_reserved_meta_is_tolerated():
    meta = {"progressToken": 5, "io.modelcontextprotocol/related-task": {"x": 1}}
    assert rpc("ping", {"_meta": meta})["result"] == {}
    params = {"name": "generate_footprint", "arguments": {"package": "0603"}}
    body = payload(rpc("tools/call", {**params, "_meta": meta}))
    assert body["name"] == "C_0603"


# basic/transports.mdx:9 -- messages MUST be UTF-8 (and JSON: no NaN).
def test_messages_must_be_utf8_on_stdio():
    ping = rpc_line("ping", 3, {"_meta": {"note": "µΩ"}})
    lines = stdio(
        '{"jsonrpc":"2.0","id":1,"method":"ping"}'.encode("utf-16"),
        b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{"x":NaN}}',
        json.dumps(ping, ensure_ascii=False).encode("utf-8"),
    )
    utf16, nan, good = (json.loads(line) for line in lines)
    for refused in (utf16, nan):
        assert_error(refused, PARSE_ERROR)
        assert "id" not in refused
    assert "UTF-8" in utf16["error"]["message"]
    assert "NaN" in nan["error"]["message"]
    assert good == {"jsonrpc": "2.0", "id": 3, "result": {}}


# basic/lifecycle.mdx:172-174 -- echo a supported version; otherwise answer
# another one it supports, which SHOULD be the latest.
@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
def test_initialize_must_echo_a_supported_version(version):
    result = rpc("initialize", {"protocolVersion": version})["result"]
    assert result["protocolVersion"] == version


@pytest.mark.parametrize(
    "params",
    [
        {"protocolVersion": "1.0.0"},
        {"protocolVersion": "2026-07-28"},
        {},
        {"protocolVersion": 5},
        {"protocolVersion": None},
    ],
    ids=["unknown", "newer-than-this-server", "absent", "not-a-string", "null"],
)
def test_initialize_answers_the_latest_version_it_supports_otherwise(params):
    result = rpc("initialize", params)["result"]
    assert result["protocolVersion"] == LATEST_PROTOCOL_VERSION == "2025-11-25"


def test_the_version_list_is_python_sdks():
    """v1.30.0 ``shared/version.py:3``; v2.2.0 ``HANDSHAKE_PROTOCOL_VERSIONS``."""
    assert SUPPORTED_PROTOCOL_VERSIONS == (
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    )
    assert PROTOCOL_VERSION == LATEST_PROTOCOL_VERSION


def test_server_discover_is_an_unknown_method():
    """2026-07-28's probe. -32601 is what sends python-sdk 2.2.0 back to
    ``initialize`` over stdio (``client/_probe.py``)."""
    assert_error(rpc("server/discover"), METHOD_NOT_FOUND)


# basic/transports.mdx:30 -- stdio messages MUST NOT contain embedded newlines.
def test_stdio_messages_must_not_contain_embedded_newlines():
    call_line = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "generate_footprint", "arguments": {"package": "0805"}},
    }
    raw = io.StringIO()
    stdin = io.BytesIO(
        (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            + "\n"
            + json.dumps(call_line)
        ).encode()
    )
    Server(stdin=stdin, stdout=raw).serve_forever()
    text = raw.getvalue()
    assert text.count("\n") == 2 and text.endswith("\n")
    replies = [json.loads(line) for line in text.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    # The tool's own text is indented JSON; its newlines travel escaped.
    assert "\n" in replies[1]["result"]["content"][0]["text"]


class _Lines:
    """A stdout for a server under test: whole lines, readable as they land."""

    def __init__(self):
        self._lines = []
        self._partial = ""
        self._landed = threading.Condition()

    def write(self, text):
        with self._landed:
            self._partial += text
            *done, self._partial = self._partial.split("\n")
            self._lines.extend(json.loads(line) for line in done)
            self._landed.notify_all()

    def flush(self):
        pass

    def wait_for(self, req_id, timeout):
        """The reply to ``req_id``, or None if none lands within ``timeout``."""
        def landed():
            return next((m for m in self._lines if m.get("id") == req_id), None)

        with self._landed:
            self._landed.wait_for(landed, timeout)
            return landed()


def _blocking_capabilities(monkeypatch):
    """Make spice_capabilities wait for a release: a tool call that stays in
    flight for as long as a test needs it to."""
    started, release = threading.Event(), threading.Event()
    original = mcp_server.DISPATCH["spice_capabilities"]

    def blocking(args):
        started.set()
        assert release.wait(10), "the test never released the call"
        return original(args)

    monkeypatch.setitem(mcp_server.DISPATCH, "spice_capabilities", blocking)
    return started, release


def _capabilities_call(req_id):
    params = {"name": "spice_capabilities", "arguments": {}}
    return rpc_line("tools/call", req_id, params)


# basic/utilities/ping.mdx:31 (at 38c84e9) -- the receiver MUST respond to a
# ping promptly. On stdio a ping read behind a running tools/call waited for
# the whole call: 8 s behind a place_parts, minutes behind generate_board.
def test_a_ping_must_be_answered_promptly_while_a_tool_call_runs(monkeypatch):
    started, release = _blocking_capabilities(monkeypatch)
    read_end, write_end = os.pipe()
    out = _Lines()
    with os.fdopen(read_end, "rb") as stdin, os.fdopen(write_end, "wb") as feed:
        server = threading.Thread(
            target=Server(stdin=stdin, stdout=out).serve_forever, daemon=True
        )
        server.start()
        try:
            feed.write((json.dumps(_capabilities_call(1)) + "\n").encode())
            feed.flush()
            assert started.wait(5), "the tool call never started"
            feed.write((json.dumps(rpc_line("ping", 2, {})) + "\n").encode())
            feed.flush()
            pong = out.wait_for(2, timeout=5)
            assert pong == {"jsonrpc": "2.0", "id": 2, "result": {}}
            assert out.wait_for(1, timeout=0) is None, "the call was still running"
        finally:
            release.set()
            feed.close()
            server.join(10)
    assert not server.is_alive()
    assert out.wait_for(1, timeout=0)["result"]["isError"] is False


def test_stdio_answers_the_calls_in_flight_when_input_ends(monkeypatch):
    """Closing stdin does not drop a call already read: ``printf ... |
    silkscreen-mcp`` waits on that answer, as it did when the loop ran each
    call inline."""
    started, release = _blocking_capabilities(monkeypatch)
    out = _Lines()
    stdin = io.BytesIO(json.dumps(_capabilities_call(1)).encode())
    server = threading.Thread(
        target=Server(stdin=stdin, stdout=out).serve_forever, daemon=True
    )
    server.start()
    assert started.wait(5)
    server.join(0.2)
    assert server.is_alive(), "the loop returned with a call still in flight"
    release.set()
    server.join(10)
    assert not server.is_alive()
    assert out.wait_for(1, timeout=0)["result"]["isError"] is False


# basic/transports.mdx:35 -- stdout MUST carry nothing but MCP messages.
def test_stdout_must_carry_only_mcp_messages(monkeypatch, capsys):
    original = mcp_server.DISPATCH["spice_capabilities"]

    def noisy(args):
        print("a library printing to stdout")
        return original(args)

    monkeypatch.setitem(mcp_server.DISPATCH, "spice_capabilities", noisy)
    line = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "spice_capabilities", "arguments": {}},
    }
    # The channel is the process's real stdout, which is what print() uses.
    stdin = io.BytesIO(json.dumps(line).encode())
    Server(stdin=stdin, stdout=sys.stdout).serve_forever()
    captured = capsys.readouterr()
    replies = [json.loads(out) for out in captured.out.splitlines()]
    assert [r["id"] for r in replies] == [1]
    assert replies[0]["result"]["isError"] is False
    assert "a library printing to stdout" in captured.err


def test_stdio_refuses_a_batch():
    """Messages on stdio are individual (basic/transports.mdx:29)."""
    [line] = stdio(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]))
    refused = json.loads(line)
    assert_error(refused, INVALID_REQUEST)
    assert "id" not in refused


# server/tools.mdx:215-220 (SHOULD) -- tool names.
def test_tool_names_follow_the_naming_rules():
    names = [tool["name"] for tool in TOOLS]
    assert len(set(names)) == len(names)
    for name in names:
        assert re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name), name


# schema.ts:1275 -- outputSchema is restricted to type: object at the root.
def test_every_output_schema_is_an_object_at_the_root():
    with_output = [t for t in TOOLS if "outputSchema" in t]
    assert {t["name"] for t in TOOLS} - {t["name"] for t in with_output} == {
        "emit_kicad_pcb"
    }, "only the file-emitting tool is text-only"
    for tool in with_output:
        assert tool["outputSchema"]["type"] == "object", tool["name"]


def test_tools_have_titles_and_honest_annotations():
    for tool in TOOLS:
        assert tool["title"] and tool["annotations"]["title"] == tool["title"]
        hints = tool["annotations"]
        if tool["name"] == "generate_board":
            assert hints["readOnlyHint"] is False
            assert hints["destructiveHint"] is True
            assert hints["idempotentHint"] is False
            assert hints["openWorldHint"] is True
        else:
            assert hints == {
                "title": tool["title"],
                "readOnlyHint": True,
                "openWorldHint": False,
            }


def test_the_finding_severities_are_the_critics():
    from silkscreen.agents.review import Severity

    assert [severity.value for severity in Severity] == mcp_server._SEVERITIES


# server/tools.mdx:329 -- structured results MUST conform to the outputSchema.
@pytest.mark.parametrize(
    "name, arguments",
    [
        ("validate_circuit", CIRCUIT),
        ("validate_circuit", {"nets": {"N": ["U9.1"]}}),
        ("validate_circuit", {"nets": [], "devices": "U1"}),
        ("build_board", {"circuit": CIRCUIT, "time_limit_s": 5}),
        ("place_parts", {"parts": [{"ref": "U1", "width_mm": 4, "height_mm": 3}]}),
        ("generate_footprint", {"package": "1206"}),
        ("spice_capabilities", {}),
    ],
    ids=[
        "validate-ok",
        "validate-invalid",
        "validate-malformed-shape",
        "build_board",
        "place_parts",
        "generate_footprint",
        "spice_capabilities",
    ],
)
def test_structured_content_must_conform_to_the_output_schema(name, arguments):
    assert_conforms(call(name, arguments), name)


@needs_ngspice
def test_simulation_structured_content_conforms_to_its_output_schema():
    response = call(
        "simulate_circuit",
        {
            "circuit": SIM_CIRCUIT,
            "testbench": SIM_BENCH,
            "assertions": [
                {
                    "name": "midpoint",
                    "measurement": {"kind": "final", "signal": "VMID"},
                    "op": "within",
                    "value": 2.5,
                    "tolerance": 0.01,
                    "unit": "V",
                }
            ],
        },
    )
    assert assert_conforms(response, "simulate_circuit")["passed"] is True


def test_structured_output_that_breaks_its_schema_is_an_error(monkeypatch):
    monkeypatch.setitem(
        mcp_server.DISPATCH,
        "spice_capabilities",
        lambda args: mcp_server._structured_result({"simulators": "ngspice"}),
    )
    assert error_text(call("spice_capabilities")).startswith("Output validation error")

    monkeypatch.setitem(
        mcp_server.DISPATCH,
        "spice_capabilities",
        lambda args: mcp_server._text_result({"simulators": []}),
    )
    assert "no structured output returned" in error_text(call("spice_capabilities"))


# server/tools.mdx:449-464 -- a malformed call is a protocol error; a failing
# tool is a result with isError.
def test_a_malformed_tools_call_is_invalid_params():
    assert_error(rpc("tools/call", {"arguments": {}}), INVALID_PARAMS)
    assert_error(rpc("tools/call", {"name": 5}), INVALID_PARAMS)
    assert_error(call("no_such_tool"), INVALID_PARAMS)
    malformed = rpc(
        "tools/call", {"name": "generate_footprint", "arguments": ["0805"]}
    )
    assert "must be an object" in assert_error(malformed, INVALID_PARAMS)["message"]


# server/tools.mdx:502 -- servers MUST validate all tool inputs.
def test_tool_inputs_must_be_validated_before_the_tool_runs(monkeypatch):
    """The 2026-09-25 probe: one undeclared argument went straight into a paid
    pipeline run. ``additionalProperties: false`` now stops it first."""
    called = []
    monkeypatch.setattr(mcp_server, "build_model", lambda: called.append(1))
    text = error_text(
        call("generate_board", {"intent": "a 3.3V LDO board", "colour": "red"})
    )
    assert text.startswith("Input validation error")
    assert "'colour'" in text
    assert called == []


def test_input_validation_reports_every_error_at_once():
    text = error_text(
        call(
            "place_parts",
            {"parts": [{"ref": 1, "width_mm": 2}], "clearance_mm": "wide"},
        )
    )
    assert "arguments.parts[0].ref: 1 is not of type string" in text
    assert "missing required property 'height_mm'" in text
    assert "arguments.clearance_mm" in text


def _walk(schema, where="#"):
    """Every (keyword, where) a schema uses, found structurally rather than by
    asking the validator -- which is the thing under test."""
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


def test_the_validator_implements_every_keyword_the_schemas_use():
    """A keyword the validator does not know would be a constraint that
    silently stops holding; this fails first."""
    used = {}
    for tool in TOOLS:
        for key in ("inputSchema", "outputSchema"):
            for keyword, where in _walk(tool.get(key)):
                used.setdefault(keyword, f"{tool['name']} {key}{where[1:]}")
    unknown = {k: w for k, w in used.items() if k not in SCHEMA_KEYWORDS}
    assert not unknown, f"the validator does not implement {unknown}"
    with pytest.raises(mcp_server.UnsupportedKeyword):
        schema_errors("x", {"type": "string", "pattern": "^x$"})


_PLACE = _tool_schema("place_parts", "inputSchema")
_SIM = _tool_schema("simulate_circuit", "inputSchema")
_BOARD = _tool_schema("generate_board", "inputSchema")
_SIM_OUT = _tool_schema("simulate_circuit")
_BUILT = _tool_schema("build_board")


def _part(**fields):
    return {"parts": [{"ref": "U1", "width_mm": 1, "height_mm": 2.5, **fields}]}


def _sim(analysis=None, sources=None, **controls):
    testbench = {"analysis": analysis or {"kind": "op"}}
    if sources is not None:
        testbench["sources"] = sources
    return {"circuit": {}, "testbench": testbench, **controls}


def _source(**fields):
    return {"name": "V1", "positive": "a", "negative": "b", **fields}


def _ac(points):
    return {"kind": "ac", "f_start": 1, "f_stop": 9, "points": points}


def _built(**fields):
    base = {"status": "optimal", "board_mm": [1, 2], "wirelength_mm": None}
    return {**base, "parts": [], "warnings": [], **fields}


def _clause(**fields):
    return {"name": "a", "passed": False, "measured": None, "margin": None, **fields}


#: Pairs on both sides of every keyword the validator implements, including
#: the JSON-specific edges: bool is not a number, 1.0 is an integer, a
#: discriminated oneOf, and a const of true.
_CORPUS = [
    (_PLACE, {"parts": []}),
    (_PLACE, _part()),
    (_PLACE, _part(width_mm=True)),
    (_PLACE, {"parts": [{"ref": "U1", "width_mm": 1}]}),
    (_PLACE, {"parts": {}}),
    (_PLACE, {"parts": [], "time_limit_s": "10"}),
    (_BOARD, {"intent": "x", "effort": "thorough"}),
    (_BOARD, {"intent": "x", "effort": "maximum"}),
    (_BOARD, {"intent": "x", "datasheets": {"U1": "https://x"}}),
    (_BOARD, {"intent": "x", "datasheets": {"U1": 5}}),
    (_BOARD, {"intent": "x", "route": 1}),
    (_BOARD, {"intent": None}),
    (_SIM, _sim()),
    (_SIM, _sim({"kind": "op", "x": 1})),
    (_SIM, _sim({"kind": "tran", "step": 1, "stop": 2})),
    (_SIM, _sim({"kind": "tran", "step": 0, "stop": 2})),
    (_SIM, _sim(_ac(10.0))),
    (_SIM, _sim(_ac(10.5))),
    (_SIM, _sim(sources=[_source(name="", dc=1)])),
    (_SIM, _sim(sources=[_source()])),
    (_SIM, _sim(sources=[_source(kind="sine", offset=0, amplitude=1, frequency=50)])),
    (_SIM, _sim(sources=[_source(dc=1, ac=2)])),
    (_SIM, _sim(timeout_s=0)),
    (_SIM, _sim(timeout_s=120)),
    (_SIM, _sim(max_points=2.0)),
    (_SIM_OUT, {"ok": True}),
    (_SIM_OUT, {"ok": 1}),
    (_SIM_OUT, {"ok": True, "assertions": [_clause()]}),
    (_SIM_OUT, {"ok": True, "assertions": [_clause(measured="1 V")]}),
    (_SIM_OUT, {"ok": True, "result": None}),
    (_SIM_OUT, {"ok": True, "result": []}),
    (_BUILT, _built()),
    (_BUILT, _built(board_mm=[1, 2, 3])),
    (_BUILT, _built(status="unknown", wirelength_mm=3)),
]


@pytest.mark.parametrize("schema, instance", _CORPUS, ids=range(len(_CORPUS)))
def test_the_validator_agrees_with_jsonschema(schema, instance):
    jsonschema = pytest.importorskip("jsonschema")
    expected = jsonschema.Draft202012Validator(schema).is_valid(instance)
    assert (schema_errors(instance, schema) == []) is expected


def test_the_corpus_exercises_both_verdicts():
    verdicts = {schema_errors(instance, schema) == [] for schema, instance in _CORPUS}
    assert verdicts == {True, False}


# server/tools.mdx:504 -- servers MUST rate limit tool invocations.
def test_tool_calls_must_be_rate_limited(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(mcp_server, "LIMITER", RateLimiter(2, clock=lambda: now[0]))
    assert payload(call("spice_capabilities"))
    assert payload(call("spice_capabilities"))
    refused = error_text(call("spice_capabilities"))
    assert "at most 2 tool calls a minute" in refused
    assert "try again in 30.0 s" in refused
    now[0] += 30.0
    assert payload(call("spice_capabilities"))


def test_a_protocol_error_spends_no_rate_limit(monkeypatch):
    monkeypatch.setattr(mcp_server, "LIMITER", RateLimiter(1, clock=lambda: 0.0))
    assert_error(call("no_such_tool"), INVALID_PARAMS)
    assert payload(call("spice_capabilities"))


@pytest.mark.parametrize("raw", ["0", "-5", "sixty", "1.5"])
def test_a_bad_rate_limit_setting_refuses_to_start(monkeypatch, capsys, raw):
    with pytest.raises(ValueError, match="MCP_TOOL_CALLS_PER_MINUTE"):
        RateLimiter.from_env({"MCP_TOOL_CALLS_PER_MINUTE": raw})
    monkeypatch.setenv("MCP_TOOL_CALLS_PER_MINUTE", raw)
    assert mcp_server.main() == 2
    assert "MCP_TOOL_CALLS_PER_MINUTE" in capsys.readouterr().err


def test_the_rate_limit_setting_is_read_from_the_environment():
    assert RateLimiter.from_env({}).per_minute == 60
    assert RateLimiter.from_env({"MCP_TOOL_CALLS_PER_MINUTE": " 5 "}).per_minute == 5


def test_only_one_generate_board_runs_at_a_time(monkeypatch, tmp_path):
    import silkscreen.agents as agents

    started, release = threading.Event(), threading.Event()

    def slow_pipeline(*args, **kwargs):
        started.set()
        release.wait(10)
        raise RuntimeError("the first run ended")

    monkeypatch.setattr(mcp_server, "build_model", lambda: object())
    monkeypatch.setattr(agents, "generate_pcb", slow_pipeline)
    arguments = {"intent": "an LDO", "output": str(tmp_path / "board.kicad_pcb")}
    first = []
    runner = threading.Thread(
        target=lambda: first.append(call("generate_board", arguments))
    )
    runner.start()
    try:
        assert started.wait(10)
        busy = error_text(call("generate_board", arguments))
        assert busy.startswith("busy:") and "one at a time" in busy
    finally:
        release.set()
        runner.join(10)
    assert "the first run ended" in error_text(first[0])
    # The lock is released even though the run raised.
    release.set()
    assert "the first run ended" in error_text(call("generate_board", arguments))


def test_over_stdio_a_second_generate_board_is_refused_not_queued(monkeypatch):
    """Pipelined over stdio, two boards are one board and one busy error.

    Before tool calls ran on threads, stdio queued the second run behind the
    first; now it meets the same one-at-a-time guard HTTP does, so a client
    that fires two in parallel is told in words rather than billed twice."""

    def slow_board(args):
        time.sleep(0.5)
        return mcp_server._error_result("stub: the real run is not exercised here")

    monkeypatch.setattr(mcp_server, "_generate_board", slow_board)
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    ] + [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": n,
                "method": "tools/call",
                "params": {"name": "generate_board", "arguments": {"intent": "an LDO"}},
            }
        )
        for n in (2, 3)
    ]
    out = io.StringIO()
    Server(stdin=io.StringIO("\n".join(lines)), stdout=out).serve_forever()
    replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
    texts = sorted(replies[n]["result"]["content"][0]["text"] for n in (2, 3))
    assert texts[0].startswith("busy:") and "one at a time" in texts[0]
    assert texts[1].startswith("stub:")


# server/tools.mdx:505 -- servers MUST sanitize tool outputs. NaN is not JSON.
def test_tool_outputs_are_sanitized_of_non_finite_numbers(monkeypatch):
    monkeypatch.setitem(
        mcp_server.DISPATCH,
        "simulate_circuit",
        lambda args: mcp_server._structured_result(
            {
                "ok": True,
                "assertions": [
                    {
                        "name": "gain",
                        "passed": False,
                        "measured": float("nan"),
                        "margin": float("-inf"),
                    }
                ],
            }
        ),
    )
    line = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "simulate_circuit",
            "arguments": {"circuit": SIM_CIRCUIT, "testbench": SIM_BENCH},
        },
    }
    [out] = stdio(json.dumps(line))
    reply = mcp_server.decode(out)  # refuses NaN and Infinity, unlike json.loads
    clause = reply["result"]["structuredContent"]["assertions"][0]
    assert clause["measured"] is None and clause["margin"] is None
    assert json.loads(reply["result"]["content"][0]["text"]) == reply["result"][
        "structuredContent"
    ]


# basic/utilities/cancellation.mdx:79 -- invalid cancellations SHOULD be ignored.
def test_an_invalid_cancellation_is_ignored():
    cancel = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "never-sent", "reason": "x"},
    }
    assert handle(cancel) is None
    assert stdio(json.dumps(cancel)) == []


# server/utilities/pagination.mdx:99 -- an invalid cursor SHOULD be -32602.
def test_an_invalid_cursor_is_invalid_params():
    assert_error(rpc("tools/list", {"cursor": "page-2"}), INVALID_PARAMS)
    assert rpc("tools/list", {"cursor": None})["result"]["tools"]


# --------------------------------------------------------------------------
# Toolset: another tool list behind the same protocol code (alexabot/).
# --------------------------------------------------------------------------


def _echo_toolset(**over):
    tool = {
        "name": "echo",
        "title": "Echo",
        "description": "Say it back.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        "outputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
    }

    def echo(args):
        body = {"text": args["text"]} if args["text"] != "bad" else {"text": 7}
        return {
            "content": [{"type": "text", "text": json.dumps(body)}],
            "structuredContent": body,
            "isError": False,
        }

    fields = {
        "tools": [tool],
        "dispatch": {"echo": echo},
        "server_info": {"name": "echo-server", "version": "1"},
        "instructions": "Use echo.",
        **over,
    }
    return mcp_server.Toolset(**fields)


def _in(toolset, method, params=None, req_id=1):
    return handle(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
        toolset=toolset,
    )


def test_handle_with_a_toolset_lists_only_its_tools():
    listed = _in(_echo_toolset(), "tools/list")["result"]["tools"]
    assert [t["name"] for t in listed] == ["echo"]


def test_initialize_carries_the_toolset_instructions_and_server_info():
    result = _in(_echo_toolset(), "initialize")["result"]
    assert result["serverInfo"] == {"name": "echo-server", "version": "1"}
    assert result["instructions"] == "Use echo."


def test_engine_initialize_is_unchanged_without_instructions():
    result = rpc("initialize")["result"]
    assert "instructions" not in result
    assert result["serverInfo"] == mcp_server.SERVER_INFO
    assert mcp_server.ENGINE.tools is TOOLS
    assert mcp_server.ENGINE.dispatch is mcp_server.DISPATCH


def test_a_toolset_tool_is_validated_in_and_out_like_an_engine_tool():
    toolset = _echo_toolset()
    good = _in(toolset, "tools/call", {"name": "echo", "arguments": {"text": "hi"}})
    assert good["result"]["structuredContent"] == {"text": "hi"}
    missing = _in(toolset, "tools/call", {"name": "echo", "arguments": {}})
    assert error_text(missing).startswith("Input validation error")
    wrong = _in(toolset, "tools/call", {"name": "echo", "arguments": {"text": "bad"}})
    assert error_text(wrong).startswith("Output validation error")


def test_an_unknown_tool_in_a_toolset_is_invalid_params():
    toolset = _echo_toolset()
    assert_error(
        _in(toolset, "tools/call", {"name": "generate_board", "arguments": {}}),
        INVALID_PARAMS,
    )
    # And the engine's own list does not grow the toolset's tool.
    assert_error(call("echo", {"text": "hi"}), INVALID_PARAMS)
