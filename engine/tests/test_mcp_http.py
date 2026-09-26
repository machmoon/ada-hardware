"""The Streamable HTTP transport: the spec's rules, checked over a real socket.

The rules are MCP 2025-11-25's (``basic/transports.mdx`` at the 2025-11-25 tag
of modelcontextprotocol/modelcontextprotocol); each conformance test below is
named for the one it checks.
"""

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest
import silkscreen.mcp.http as mcp_http
import silkscreen.mcp.server as mcp_server
from silkscreen.mcp.http import ENDPOINT, make_server
from silkscreen.mcp.server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    RateLimiter,
)

ACCEPT_BOTH = "application/json, text/event-stream"


@pytest.fixture(autouse=True)
def _unlimited_tool_calls(monkeypatch):
    monkeypatch.setattr(mcp_server, "LIMITER", RateLimiter(1_000_000))


@pytest.fixture
def served():
    """Yield a base URL for a server on a free loopback port, per test."""
    server = make_server("127.0.0.1", 0)
    # A short poll keeps shutdown() from costing half a second per test.
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()


def _req(url, method="POST", body=None, headers=None, *, raw=None, accept=ACCEPT_BOTH):
    data = raw
    if data is None and body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if accept is not None:
        req.add_header("Accept", accept)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def rpc(method, req_id=1, params=None):
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


def notification(method, params=None):
    return {"jsonrpc": "2.0", "method": method, "params": params or {}}


def refusal(body, code=INVALID_REQUEST):
    """A JSON-RPC error with no id: what the spec lets an HTTP refusal carry
    (``transports.mdx:81,100-102``), since no id could be answered."""
    message = json.loads(body)
    assert "id" not in message, message
    error = message["error"]
    assert isinstance(error["code"], int) and error["code"] == code, error
    return error


PING = json.dumps(rpc("ping", 1)).encode()


def _post_raw(body, *headers, length=True, version="HTTP/1.1"):
    """One POST as bytes, with framing headers exactly as given: what urllib
    cannot be made to send (padding, chunked, both lengths at once)."""
    lines = [
        f"POST {ENDPOINT} {version}",
        "Host: 127.0.0.1",
        "Content-Type: application/json",
        "Accept: application/json",
        *headers,
    ]
    if length:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def _exchange(server, data, count=1):
    """Send ``data`` on one connection and read ``count`` responses.

    Returns ``(responses, closed)``: each response as ``(status, headers,
    body)``, and whether the server closed the connection after them.
    """
    port = server.server_address[1]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(data)
        reader = sock.makefile("rb")
        responses = []
        for _ in range(count):
            status_line = reader.readline()
            assert status_line, "the server closed without answering"
            headers = {}
            while (line := reader.readline()) not in (b"\r\n", b""):
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            body = reader.read(int(headers.get("content-length", "0")))
            responses.append((int(status_line.split()[1]), headers, body))
        sock.settimeout(0.5)
        try:
            closed = reader.read(1) == b""
        except TimeoutError:
            closed = False
        return responses, closed


# transports.mdx:103-105, lifecycle.mdx:42 -- a stateless server is born ready:
# a request needs no initialize first, and is answered as application/json.
def test_a_request_needs_no_prior_initialize_and_is_answered_as_json(served):
    base, _ = served
    status, headers, body = _req(base + ENDPOINT, body=rpc("tools/list"))
    assert status == 200 and headers["Content-Type"] == "application/json"
    names = [t["name"] for t in json.loads(body)["result"]["tools"]]
    assert "generate_board" in names


def test_a_batch_is_answered_as_a_batch(served):
    """With no version header a request is taken to speak 2025-03-26, whose
    servers MUST accept batches. ``initialize`` is never batched (it was in
    this test until 2025-03-26's lifecycle rule was read), so tools/list."""
    base, _ = served
    status, _, body = _req(base + ENDPOINT, body=[rpc("ping", 1), rpc("tools/list", 2)])
    assert status == 200
    assert [r["id"] for r in json.loads(body)] == [1, 2]


def test_notifications_alone_get_202_and_no_body(served):
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT, body={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert status == 202 and body == b""


# transports.mdx:142-144 -- GET MUST be an SSE stream or 405.
def test_get_is_405_because_no_sse_stream_is_offered(served):
    base, _ = served
    status, headers, body = _req(
        base + ENDPOINT, method="GET", accept="text/event-stream"
    )
    assert status == 405 and headers["Allow"] == "POST"
    assert "SSE" in refusal(body)["message"]


# transports.mdx:221-222 -- DELETE MAY be 405. It was 204, which claimed to end
# a session this server never assigned; 405 is what the spec names for a
# server that does not let clients end sessions, and python-sdk's answer when
# it has no session id (v1 streamable_http.py:797-806).
def test_delete_is_405_because_no_session_is_ever_assigned(served):
    base, _ = served
    status, headers, body = _req(base + ENDPOINT, method="DELETE")
    assert status == 405 and headers["Allow"] == "POST"
    assert "session" in refusal(body)["message"]


def test_only_the_mcp_endpoint_exists(served):
    base, _ = served
    status, _, _ = _req(base + "/other", body=rpc("ping"))
    assert status == 404


# transports.mdx:80-82 -- Origin MUST be validated on every connection, and a
# present, invalid one MUST be 403.
@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
def test_a_foreign_browser_origin_is_refused(served, method):
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT,
        method=method,
        body=rpc("ping") if method == "POST" else None,
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403
    assert "evil.example" in refusal(body)["message"]


@pytest.mark.parametrize("origin", ["https://claude.ai", "http://localhost:5173"])
def test_claude_and_loopback_origins_are_allowed(served, origin):
    base, _ = served
    status, _, _ = _req(base + ENDPOINT, body=rpc("ping"), headers={"Origin": origin})
    assert status == 200


# transports.mdx:100-102 -- input the server cannot accept MUST get an HTTP
# error. This was a 200 carrying the -32700, which answered unacceptable input
# as a success; it is now 400, python-sdk's status (v1 streamable_http.py:537).
def test_invalid_json_is_a_400_parse_error_with_no_id(served):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, raw=b"{nope", accept=None)
    assert status == 400
    assert refusal(body, PARSE_ERROR)["message"].startswith("invalid JSON")


def test_bearer_token_gates_every_verb_when_set():
    server = make_server("127.0.0.1", 0, token="s3cret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, headers, _ = _req(base + ENDPOINT, body=rpc("ping"))
        assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
        status, _, _ = _req(
            base + ENDPOINT, body=rpc("ping"), headers={"Authorization": "Bearer wrong"}
        )
        assert status == 401
        status, _, _ = _req(
            base + ENDPOINT,
            body=rpc("ping"),
            headers={"Authorization": "Bearer s3cret"},
        )
        assert status == 200
        # GET and DELETE are gated too, before their 405.
        for method in ("GET", "DELETE"):
            status, _, _ = _req(base + ENDPOINT, method=method)
            assert status == 401, method
            status, _, _ = _req(
                base + ENDPOINT,
                method=method,
                headers={"Authorization": "Bearer s3cret"},
            )
            assert status == 405, method
    finally:
        server.shutdown()
        server.server_close()


def test_a_refused_request_does_not_poison_a_keep_alive_connection():
    """The body of a 401 must not be read as the next request line."""
    import http.client

    server = make_server("127.0.0.1", 0, token="s3cret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
    try:
        body = json.dumps(rpc("ping"))
        conn.request(
            "POST", ENDPOINT, body=body, headers={"Content-Type": "application/json"}
        )
        first = conn.getresponse()
        first.read()
        assert first.status == 401
        conn.request(
            "POST",
            ENDPOINT,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer s3cret",
            },
        )
        second = conn.getresponse()
        assert second.status == 200
        assert json.loads(second.read())["id"] == 1
    finally:
        conn.close()
        server.shutdown()
        server.server_close()


def test_the_token_may_ride_in_the_path_and_never_reaches_the_log(capsys):
    server = make_server("127.0.0.1", 0, token="s3cret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, _, _ = _req(base + ENDPOINT + "/s3cret", body=rpc("ping"))
        assert status == 200
        status, _, _ = _req(base + ENDPOINT + "/wrong", body=rpc("ping"))
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()
    err = capsys.readouterr().err
    assert "s3cret" not in err and "<token>" in err


def test_a_path_secret_is_not_an_endpoint_without_a_token(served):
    base, _ = served
    status, _, _ = _req(base + ENDPOINT + "/anything", body=rpc("ping"))
    assert status == 404


# --------------------------------------------------------------------------
# MCP 2025-11-25 conformance over HTTP, one test per applicable MUST.
# --------------------------------------------------------------------------


# The conformance suite's dns-rebinding-protection pair
# (modelcontextprotocol/conformance src/scenarios/server/dns-rebinding.ts).
def test_dns_rebinding_protection(served):
    base, _ = served
    port = base.rsplit(":", 1)[1]
    status, _, _ = _req(
        base + ENDPOINT,
        body=rpc("initialize", params={"protocolVersion": "2025-11-25"}),
        headers={"Origin": "http://evil.example.com"},
    )
    assert 400 <= status < 500
    status, _, _ = _req(
        base + ENDPOINT,
        body=rpc("initialize", params={"protocolVersion": "2025-11-25"}),
        headers={"Origin": f"http://127.0.0.1:{port}"},
    )
    assert 200 <= status < 300


# transports.mdx:83 (SHOULD) -- bind only to localhost when running locally.
def test_the_server_binds_localhost_by_default(monkeypatch):
    import inspect

    assert inspect.signature(make_server).parameters["host"].default == "127.0.0.1"
    bound = {}

    class Stopped:
        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def fake_make_server(host, port, **kwargs):
        bound["host"] = host
        return Stopped()

    monkeypatch.delenv("MCP_HTTP_TOKEN", raising=False)
    monkeypatch.setattr(mcp_http, "make_server", fake_make_server)
    assert mcp_http.main([]) == 0
    assert bound["host"] == "127.0.0.1"


def test_a_bad_rate_limit_setting_refuses_to_start(monkeypatch, capsys):
    monkeypatch.setenv("MCP_TOOL_CALLS_PER_MINUTE", "0")
    monkeypatch.setattr(mcp_http, "make_server", lambda *a, **k: pytest.fail("bound"))
    assert mcp_http.main([]) == 2
    assert "MCP_TOOL_CALLS_PER_MINUTE" in capsys.readouterr().err


# basic/index.mdx:71 -- a result MUST carry the request's id.
@pytest.mark.parametrize("req_id", ["abc-123", 7])
def test_a_result_must_echo_the_request_id(served, req_id):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, body=rpc("ping", req_id))
    assert status == 200
    assert json.loads(body) == {"jsonrpc": "2.0", "id": req_id, "result": {}}


# basic/index.mdx:29,48-49 -- JSON-RPC 2.0, ids a string or an integer. A
# message that is neither a request, a notification nor a response is input
# the server cannot accept: 400 (transports.mdx:100-102). The error echoes the
# id when one could be read (basic/index.mdx:91) and omits it otherwise.
@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": None, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
        "ping",
        {"jsonrpc": "2.0", "result": {}},
    ],
    ids=["null-id", "bool-id", "float-id", "string", "result-without-id"],
)
def test_a_malformed_message_is_a_400_with_no_id(served, message):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, body=message)
    assert status == 400
    refusal(body)


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 8, "method": 5},
        {"jsonrpc": "1.0", "id": 8, "method": "ping"},
    ],
    ids=["method-not-a-string", "jsonrpc-1.0"],
)
def test_a_malformed_message_with_a_readable_id_is_a_400_echoing_it(served, message):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, body=message)
    assert status == 400
    answer = json.loads(body)
    assert answer["id"] == 8 and answer["error"]["code"] == INVALID_REQUEST


# basic/index.mdx:98 and transports.mdx:97-99 -- a notification gets no
# response: 202, no body, and nothing runs.
@pytest.mark.parametrize("method", ["ping", "tools/list", "tools/call", "initialize"])
def test_a_notification_must_not_get_a_response(served, monkeypatch, method):
    ran = []
    monkeypatch.setitem(mcp_server.DISPATCH, "generate_footprint", ran.append)
    base, _ = served
    params = {"name": "generate_footprint", "arguments": {"package": "0805"}}
    status, _, body = _req(base + ENDPOINT, body=notification(method, params))
    assert status == 202 and body == b""
    assert ran == []


# transports.mdx:97-99 -- a response the client sends is accepted with 202.
@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "declined"}},
    ],
    ids=["result", "error"],
)
def test_a_response_from_the_client_is_accepted_with_202(served, message):
    base, _ = served
    status, headers, body = _req(base + ENDPOINT, body=message)
    assert status == 202 and body == b""
    assert headers.get("Content-Type") is None


# transports.mdx:9 -- messages MUST be UTF-8; NaN is not JSON.
@pytest.mark.parametrize(
    "raw, says",
    [
        (json.dumps(rpc("ping")).encode("utf-16"), "UTF-8"),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}', "NaN"),
    ],
    ids=["utf-16", "nan"],
)
def test_messages_must_be_utf8_json(served, raw, says):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, raw=raw)
    assert status == 400
    assert says in refusal(body, PARSE_ERROR)["message"]


# transports.mdx:276-279 -- no header: assume 2025-03-26. And a supported one.
@pytest.mark.parametrize("version", [None, *SUPPORTED_PROTOCOL_VERSIONS])
def test_an_absent_or_supported_version_header_is_accepted(served, version):
    base, _ = served
    headers = {} if version is None else {"MCP-Protocol-Version": version}
    status, _, body = _req(base + ENDPOINT, body=rpc("tools/list"), headers=headers)
    assert status == 200
    assert json.loads(body)["result"]["tools"]


# transports.mdx:281-282 -- an invalid or unsupported MCP-Protocol-Version
# MUST be 400. 2026-07-28 included: that 400 is what sends python-sdk 2.2.0's
# client back to initialize.
@pytest.mark.parametrize("version", ["2026-07-28", "garbage", ""])
def test_an_unsupported_version_header_is_400(served, version):
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT,
        body=rpc("tools/list"),
        headers={"MCP-Protocol-Version": version},
    )
    assert status == 400
    error = refusal(body)
    assert error["data"] == {
        "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
        "requested": version,
    }


def test_the_version_header_is_checked_on_notifications_get_and_delete(served):
    base, _ = served
    bad = {"MCP-Protocol-Version": "2026-07-28"}
    status, _, body = _req(
        base + ENDPOINT, body=notification("notifications/initialized"), headers=bad
    )
    assert status == 400
    refusal(body)
    for method in ("GET", "DELETE"):
        status, _, body = _req(base + ENDPOINT, method=method, headers=bad)
        assert status == 400, method
        refusal(body)


def test_initialize_ignores_the_version_header_and_negotiates_from_its_body(served):
    """The header is for requests after initialization (transports.mdx:267-268);
    python-sdk v1 skips it on initialize, and the conformance suite says
    initialize carries none."""
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT,
        body=rpc("initialize", params={"protocolVersion": "2025-06-18"}),
        headers={"MCP-Protocol-Version": "2026-07-28"},
    )
    assert status == 200
    assert json.loads(body)["result"]["protocolVersion"] == "2025-06-18"


# lifecycle.mdx:172-174 -- version negotiation, over the transport.
@pytest.mark.parametrize(
    "requested, answered",
    [
        ("2025-11-25", "2025-11-25"),
        ("2025-03-26", "2025-03-26"),
        ("2024-11-05", "2024-11-05"),
        ("2099-01-01", LATEST_PROTOCOL_VERSION),
    ],
    ids=["latest", "older-supported", "oldest-supported", "unknown"],
)
def test_initialize_negotiates_the_protocol_version(served, requested, answered):
    base, _ = served
    status, headers, body = _req(
        base + ENDPOINT, body=rpc("initialize", params={"protocolVersion": requested})
    )
    assert status == 200
    result = json.loads(body)["result"]
    assert result["protocolVersion"] == answered
    assert result["capabilities"]["tools"] == {"listChanged": False}
    # transports.mdx:201-203: a session id MAY be assigned here. None is.
    assert headers.get("Mcp-Session-Id") is None


# transports.mdx:96 -- the POST body MUST be a single message. Batches exist
# only for 2025-03-26 (its basic/index.mdx:97-99); see http.py's deviations.
@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2024-11-05"])
def test_a_batch_is_refused_outside_2025_03_26(served, version):
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT,
        body=[rpc("ping", 1), rpc("ping", 2)],
        headers={"MCP-Protocol-Version": version},
    )
    assert status == 400
    assert "one message per POST" in refusal(body)["message"]


def test_a_2025_03_26_batch_is_answered_as_a_batch(served):
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT,
        body=[
            rpc("ping", 1),
            notification("notifications/initialized"),
            rpc("ping", 2),
        ],
        headers={"MCP-Protocol-Version": "2025-03-26"},
    )
    assert status == 200
    assert [r["id"] for r in json.loads(body)] == [1, 2]


def test_an_empty_batch_is_400(served):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, body=[])
    assert status == 400
    refusal(body)


def test_initialize_must_not_be_part_of_a_batch(served):
    """2025-03-26/basic/lifecycle.mdx:74-75."""
    base, _ = served
    status, _, body = _req(base + ENDPOINT, body=[rpc("initialize", 1), rpc("ping", 2)])
    assert status == 200
    first, second = json.loads(body)
    assert first["id"] == 1 and first["error"]["code"] == INVALID_REQUEST
    assert second == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_a_batch_of_only_notifications_is_202(served):
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT,
        body=[notification("notifications/initialized"), notification("ping")],
    )
    assert status == 202 and body == b""


# transports.mdx:103-105 -- a request is answered as application/json, and a
# JSON-RPC error is still an answer to it: 200, not an HTTP error.
def test_a_jsonrpc_error_to_a_request_is_still_200_json(served):
    base, _ = served
    status, headers, body = _req(
        base + ENDPOINT, body=rpc("tools/call", 4, {"name": "no_such_tool"})
    )
    assert status == 200 and headers["Content-Type"] == "application/json"
    answer = json.loads(body)
    assert answer["id"] == 4 and answer["error"]["code"] == INVALID_PARAMS


# No server rule for Accept (the client MUST send both types,
# transports.mdx:94-95); python-sdk v2 answers 406 when JSON is not
# admitted, and so does this, while an absent header means any type.
@pytest.mark.parametrize(
    "accept, status",
    [
        ("text/event-stream", 406),
        ("text/html", 406),
        (ACCEPT_BOTH, 200),
        ("application/json", 200),
        ("application/*", 200),
        ("*/*", 200),
        ("application/json;q=0.9", 200),
        (None, 200),
    ],
)
def test_accept_must_admit_json(served, accept, status):
    base, _ = served
    got, _, body = _req(base + ENDPOINT, body=rpc("ping"), accept=accept)
    assert got == status
    if status == 406:
        refusal(body)


# transports.mdx:198-212 -- sessions are optional. None is assigned, and a
# stray id from a client that expected one is not a reason to refuse.
def test_a_stray_session_id_is_ignored_and_none_is_ever_assigned(served):
    base, _ = served
    status, headers, body = _req(
        base + ENDPOINT,
        body=rpc("ping"),
        headers={"Mcp-Session-Id": "3f9a0c1e-made-up"},
    )
    assert status == 200 and json.loads(body)["result"] == {}
    assert headers.get("Mcp-Session-Id") is None


# basic/utilities/ping.mdx:31 (at 38c84e9) -- ping MUST be answered
# promptly, with an empty result.
def test_ping_must_get_an_empty_result(served):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, body=rpc("ping", "p"))
    assert status == 200 and json.loads(body)["result"] == {}


def test_ping_must_be_answered_promptly_while_a_tool_call_runs(served, monkeypatch):
    """Each connection has its own thread; test_mcp.py pins the same rule on
    stdio, where it once did not hold."""
    base, _ = served
    started, release = threading.Event(), threading.Event()
    original = mcp_server.DISPATCH["spice_capabilities"]

    def blocking(args):
        started.set()
        assert release.wait(10), "the test never released the call"
        return original(args)

    monkeypatch.setitem(mcp_server.DISPATCH, "spice_capabilities", blocking)
    answers = []
    call = rpc("tools/call", 1, {"name": "spice_capabilities", "arguments": {}})
    caller = threading.Thread(
        target=lambda: answers.append(_req(base + ENDPOINT, body=call)), daemon=True
    )
    caller.start()
    try:
        assert started.wait(5), "the tool call never started"
        status, _, body = _req(base + ENDPOINT, body=rpc("ping", 2))
        assert status == 200 and json.loads(body)["result"] == {}
        assert answers == [], "the call was still running"
    finally:
        release.set()
        caller.join(10)
    assert answers[0][0] == 200


# RFC 9110 section 5.5 -- whitespace around a field value is not part of it,
# and a parser MUST exclude it. http.server keeps the trailing part.
@pytest.mark.parametrize(
    "header",
    [
        "MCP-Protocol-Version: 2025-11-25 ",
        "MCP-Protocol-Version:\t2025-11-25\t",
        "Origin: https://claude.ai ",
    ],
    ids=["version-trailing-space", "version-tabs", "origin-trailing-space"],
)
def test_whitespace_around_a_header_value_is_not_part_of_it(served, header):
    _, server = served
    [(status, _, body)], _ = _exchange(server, _post_raw(PING, header))
    assert status == 200, body
    assert json.loads(body) == {"jsonrpc": "2.0", "id": 1, "result": {}}


def _chunk(data, extension=b""):
    return b"%x%s\r\n%s\r\n" % (len(data), extension, data)


# RFC 9112 section 7.1 -- a recipient MUST be able to parse the chunked
# transfer coding. http.server cannot: it read this body as empty, answered
# -32700, and then parsed the leftover chunks as the next request.
def test_a_chunked_body_is_decoded_and_the_connection_stays_usable(served):
    _, server = served
    chunked = (
        _chunk(PING[:10], b" ;name=value")
        + _chunk(PING[10:])
        + b"0\r\nX-Trailer: ignored\r\n\r\n"
    )
    second = json.dumps(rpc("ping", 2)).encode()
    data = _post_raw(chunked, "Transfer-Encoding: chunked", length=False)
    data += _post_raw(second)
    responses, closed = _exchange(server, data, count=2)
    assert [(status, json.loads(body)["id"]) for status, _, body in responses] == [
        (200, 1),
        (200, 2),
    ]
    assert not closed


# RFC 9112 sections 6.1 and 6.3 -- framing a server cannot trust is refused,
# and the connection closed, since what is left on it is not a request.
@pytest.mark.parametrize(
    "data, status",
    [
        (
            _post_raw(_chunk(PING) + b"0\r\n\r\n", "Transfer-Encoding: chunked"),
            400,
        ),
        (
            _post_raw(PING, "Transfer-Encoding: chunked", version="HTTP/1.0"),
            400,
        ),
        (_post_raw(PING, "Transfer-Encoding: gzip", length=False), 400),
        (_post_raw(PING, "Transfer-Encoding: chunked, chunked", length=False), 400),
        (_post_raw(PING, "Transfer-Encoding: gzip, chunked", length=False), 501),
        (
            _post_raw(b"zz\r\n", "Transfer-Encoding: chunked", length=False),
            400,
        ),
        (
            _post_raw(
                b"%x\r\n%sXX0\r\n\r\n" % (len(PING), PING),
                "Transfer-Encoding: chunked",
                length=False,
            ),
            400,
        ),
        (
            _post_raw(
                b"%x\r\n" % (1 << 40), "Transfer-Encoding: chunked", length=False
            ),
            413,
        ),
        (_post_raw(PING, f"Content-Length: +{len(PING)}", length=False), 400),
        (_post_raw(PING, "Content-Length: 1_0", length=False), 400),
        (
            _post_raw(
                PING,
                f"Content-Length: {len(PING)}",
                f"Content-Length: {len(PING) + 1}",
                length=False,
            ),
            400,
        ),
    ],
    ids=[
        "chunked-and-content-length",
        "chunked-on-http-1.0",
        "chunked-not-final",
        "chunked-twice",
        "unknown-coding",
        "bad-chunk-size",
        "unterminated-chunk",
        "chunk-over-the-body-limit",
        "content-length-plus-sign",
        "content-length-underscore",
        "content-lengths-disagree",
    ],
)
def test_untrustworthy_framing_is_refused_and_the_connection_closed(
    served, data, status
):
    _, server = served
    [(got, headers, body)], closed = _exchange(server, data)
    assert got == status, body
    refusal(body)
    assert headers["connection"] == "close" and closed


def test_equal_content_lengths_are_one_length(served):
    """RFC 9112 section 6.3 item 5: a list of equal valid values is that value."""
    _, server = served
    data = _post_raw(PING, f"Content-Length: {len(PING)}, {len(PING)}", length=False)
    [(status, _, body)], _ = _exchange(server, data)
    assert status == 200 and json.loads(body)["id"] == 1


# server/tools.mdx:329 -- a structured result over HTTP, as a client sees it.
def test_a_tool_result_carries_structured_content_and_the_same_text(served):
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT,
        body=rpc(
            "tools/call",
            params={"name": "generate_footprint", "arguments": {"package": "0805"}},
        ),
    )
    assert status == 200
    result = json.loads(body)["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["structuredContent"]["name"] == "C_0805"
