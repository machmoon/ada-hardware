"""The Streamable HTTP transport: the spec's rules, checked over a real socket."""

import json
import threading
import urllib.error
import urllib.request

import pytest
from silkscreen.mcp.http import ENDPOINT, make_server


@pytest.fixture
def served():
    """Yield a base URL for a server on a free loopback port, per test."""
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server
    finally:
        server.shutdown()
        server.server_close()


def _req(url, method="POST", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json, text/event-stream")
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


def test_a_request_is_answered_as_json(served):
    base, _ = served
    status, headers, body = _req(base + ENDPOINT, body=rpc("tools/list"))
    assert status == 200 and headers["Content-Type"] == "application/json"
    names = [t["name"] for t in json.loads(body)["result"]["tools"]]
    assert "generate_board" in names


def test_a_batch_is_answered_as_a_batch(served):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, body=[rpc("ping", 1), rpc("initialize", 2)])
    assert status == 200
    assert [r["id"] for r in json.loads(body)] == [1, 2]


def test_notifications_alone_get_202_and_no_body(served):
    base, _ = served
    status, _, body = _req(
        base + ENDPOINT, body={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert status == 202 and body == b""


def test_get_is_405_because_no_sse_stream_is_offered(served):
    base, _ = served
    status, headers, _ = _req(base + ENDPOINT, method="GET")
    assert status == 405 and "POST" in headers["Allow"]


def test_delete_ends_nothing_and_says_so_with_204(served):
    base, _ = served
    status, _, body = _req(base + ENDPOINT, method="DELETE")
    assert status == 204 and body == b""


def test_only_the_mcp_endpoint_exists(served):
    base, _ = served
    status, _, _ = _req(base + "/other", body=rpc("ping"))
    assert status == 404


def test_a_foreign_browser_origin_is_refused(served):
    base, _ = served
    status, _, _ = _req(
        base + ENDPOINT, body=rpc("ping"), headers={"Origin": "https://evil.example"}
    )
    assert status == 403


@pytest.mark.parametrize("origin", ["https://claude.ai", "http://localhost:5173"])
def test_claude_and_loopback_origins_are_allowed(served, origin):
    base, _ = served
    status, _, _ = _req(base + ENDPOINT, body=rpc("ping"), headers={"Origin": origin})
    assert status == 200


def test_invalid_json_is_a_jsonrpc_parse_error_not_a_crash(served):
    base, _ = served
    req = urllib.request.Request(base + ENDPOINT, data=b"{nope", method="POST")
    with urllib.request.urlopen(req) as resp:
        assert json.loads(resp.read())["error"]["code"] == -32700


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
