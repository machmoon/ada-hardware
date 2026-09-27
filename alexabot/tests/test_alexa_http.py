"""The endpoint over a real socket: accounts, auth, origin, the handshake."""

import json
import threading
import urllib.error
import urllib.request

import pytest
from silkscreen.mcp.http import ENDPOINT

from alexabot import app, tools
from alexabot.config import Config, ConfigError, load_config
from alexabot.runner import Runner
from alexabot.store import BoardStore
from alexabot.tests.fakes import FakeSteps
from service.auth import MemoryKeyStore, create_key


@pytest.fixture
def serve():
    """Start a server for ``Config`` over fake steps; yield ``(url, runner)``."""
    servers = []

    def start(config: Config, key_store=None):
        fake = FakeSteps()
        runner = Runner(BoardStore(":memory:"), lambda: object(), object(),
                        steps=fake, max_active=8).warm()
        server = app.make_server(config, runner=runner, key_store=key_store)
        thread = threading.Thread(target=server.serve_forever, args=(0.05,),
                                  daemon=True)
        thread.start()
        servers.append((server, runner))
        return f"http://127.0.0.1:{server.server_address[1]}", runner

    yield start
    for server, runner in servers:
        server.shutdown()
        server.server_close()
        runner.join(10)


def _post(url, body, headers=None, path=ENDPOINT):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


def _tool(url, name, arguments, headers=None, path=ENDPOINT):
    status, body = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}},
                         headers, path)
    assert status == 200, (status, body)
    return body["result"]


def _config(**over):
    return Config(port=0, db=":memory:", **over)


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def test_ada_key_selects_the_account(serve):
    keys = MemoryKeyStore()
    key, _ = create_key(keys, "acme")
    url, runner = serve(_config(keys_db="unused"), key_store=keys)
    result = _tool(url, "start_board_design",
                   {"intent": "a board", "request_id": "req-00000001"}, _bearer(key))
    sid = result["structuredContent"]["session_id"]
    assert runner.store.get("acme", sid) is not None
    assert runner.store.get("local", sid) is None


def test_two_keys_two_histories(serve):
    keys = MemoryKeyStore()
    alice, _ = create_key(keys, "alice")
    bob, _ = create_key(keys, "bob")
    url, _ = serve(_config(keys_db="unused"), key_store=keys)
    _tool(url, "start_board_design",
          {"intent": "alice's usb board", "request_id": "req-00000001"},
          _bearer(alice))
    mine = _tool(url, "recall_my_boards", {}, _bearer(alice))["structuredContent"]
    theirs = _tool(url, "recall_my_boards", {}, _bearer(bob))["structuredContent"]
    assert mine["total"] == 1 and mine["boards"][0]["intent"] == "alice's usb board"
    assert theirs == {"query": None, "total": 0, "boards": [],
                      "speech": "You don't have any boards with me yet."}


def test_shared_token_is_the_local_account(serve):
    url, runner = serve(_config(token="s3cret-token"))
    result = _tool(url, "start_board_design",
                   {"intent": "a board", "request_id": "req-00000001"},
                   _bearer("s3cret-token"))
    assert runner.store.get("local", result["structuredContent"]["session_id"])


@pytest.mark.parametrize(
    "headers",
    [{}, _bearer("ada_nope.nope"), _bearer("wrong-token"),
     _bearer("ada_AAAAAAAA." + "x" * 32)],
)
def test_bad_key_is_401(serve, headers):
    keys = MemoryKeyStore()
    create_key(keys, "acme")
    url, _ = serve(_config(keys_db="unused", token="s3cret-token"), key_store=keys)
    status, _ = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers)
    assert status == 401


def test_the_path_form_key_works_and_is_masked_in_the_log(serve, capsys):
    keys = MemoryKeyStore()
    key, _ = create_key(keys, "acme")
    url, runner = serve(_config(keys_db="unused"), key_store=keys)
    result = _tool(url, "recall_my_boards", {}, path=f"{ENDPOINT}/{key}")
    assert result["isError"] is False
    status, _ = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                      path=f"{ENDPOINT}/ada_wrong.key")
    assert status == 401
    err = capsys.readouterr().err
    secret = key.partition(".")[2]
    assert secret not in err and "ada_wrong.key" not in err and "<token>" in err


def test_foreign_origin_is_403(serve):
    url, _ = serve(_config())
    status, body = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         {"Origin": "https://evil.example"})
    assert status == 403 and "id" not in body


def test_initialize_negotiates_2025_11_25_with_instructions(serve):
    url, _ = serve(_config())
    status, body = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-11-25",
                                          "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "0"}}})
    assert status == 200
    result = body["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"]["name"] == "ada-voice"
    assert result["instructions"] == tools.INSTRUCTIONS


def test_tools_list_is_the_six(serve):
    url, _ = serve(_config())
    status, body = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         {"MCP-Protocol-Version": "2025-11-25"})
    assert status == 200
    assert [t["name"] for t in body["result"]["tools"]] == [
        t["name"] for t in tools.TOOLS]


def test_main_refuses_non_loopback_without_auth(monkeypatch, capsys):
    monkeypatch.delenv("MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("SILKSCREEN_API_KEYS_DB", raising=False)
    monkeypatch.setattr(app, "_load_dotenv", lambda: None)
    monkeypatch.setattr(app, "make_server", lambda *a, **k: pytest.fail("bound"))
    assert app.main(["--host", "0.0.0.0", "--scripted"]) == 2
    assert "not loopback" in capsys.readouterr().err
    with pytest.raises(ConfigError) as caught:
        load_config(["--host", "0.0.0.0", "--port", "x", "--scripted-delay", "2"],
                    env={})
    # Every problem at once, the zoombot rule.
    assert len(caught.value.errors) == 3


def test_main_refuses_live_mode_without_a_provider_naming_scripted(
    monkeypatch, capsys, tmp_path
):
    from silkscreen.agents import claude

    for name in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                 "SILKSCREEN_PROVIDER", claude.VERTEX_PROJECT_ENV_VAR,
                 claude.VERTEX_REGION_ENV_VAR, claude.CLAUDE_BACKEND_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app, "_load_dotenv", lambda: None)
    monkeypatch.setattr(app, "build_runner", lambda *a, **k: pytest.fail("built"))
    assert app.main(["--db", str(tmp_path / "b.sqlite3")]) == 2
    err = capsys.readouterr().err
    assert "no model provider is configured" in err and "--scripted" in err
