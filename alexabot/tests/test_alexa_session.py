"""The brief's whole voice session, offline, over HTTP, on the real pipeline.

start -> questions -> one answer -> "you choose" -> continue -> done ->
explain -> recall from a fresh store handle. The model is the ``--scripted``
one and ``service/steps.py`` is real, so this is the path a judge's laptop
runs; a guard on ``socket.connect`` proves nothing left the machine.
"""

import json
import socket
import threading
import urllib.request

import pytest
from silkscreen.mcp.http import ENDPOINT

from alexabot import app
from alexabot.config import Config
from alexabot.runner import Runner
from alexabot.store import BoardStore
from alexabot.tests.fakes import FakeSteps, wait_for

LOOPBACK = ("127.0.0.1", "::1", "localhost")


@pytest.fixture
def loopback_only(monkeypatch):
    """Refuse any connection that is not to this machine."""
    real = socket.socket.connect
    refused = []

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in LOOPBACK and "/" not in host:
            refused.append(address)
            raise OSError(f"test refused a connection to {address!r}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    return refused


class Client:
    """A minimal MCP client: POST one JSON-RPC message, read the result."""

    def __init__(self, server):
        self.url = f"http://127.0.0.1:{server.server_address[1]}{ENDPOINT}"
        self.next_id = 0

    def rpc(self, method, params=None):
        self.next_id += 1
        body = {"jsonrpc": "2.0", "id": self.next_id, "method": method,
                "params": params or {}}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("MCP-Protocol-Version", "2025-11-25")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())["result"]

    def tool(self, name, **arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        assert result["isError"] is False, result["content"][0]["text"]
        assert result["content"][0]["text"] == result["structuredContent"]["speech"]
        return result["structuredContent"]

    def until(self, sid, *states):
        return wait_for(
            lambda: (s := self.tool("board_status", session_id=sid))["state"]
            in states and s,
            timeout=60, interval=0.05,
        )


def _serve(config, runner):
    server = app.make_server(config, runner=runner)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return server


def _stop(server):
    server.shutdown()
    server.server_close()


def test_a_whole_voice_session_offline(tmp_path, steps_dir, loopback_only):
    config = Config(port=0, db=str(tmp_path / "boards.sqlite3"), scripted=True)
    runner = app.build_runner(config)
    server = _serve(config, runner)
    client = Client(server)
    try:
        init = client.rpc("initialize", {"protocolVersion": "2025-11-25",
                                         "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "0"}})
        assert init["serverInfo"]["name"] == "ada-voice"

        started = client.tool("start_board_design",
                              intent="a USB powered 3.3V regulator board",
                              request_id="0b1e-voice-0001")
        sid = started["session_id"]
        assert started["state"] == "reading" and started["scripted"] is True
        assert started["speech"].startswith("Scripted mode")

        asked = client.until(sid, "questions")
        assert asked["question"]["index"] == 0
        assert "up to 500 milliamps" in asked["speech"]
        assert asked["speech"].endswith(
            "How much current should the 3.3 volt rail supply?")

        second = client.tool("answer_design_questions", session_id=sid,
                             answers=[{"index": 0, "answer": "one amp"}])
        assert second["question"]["index"] == 1
        assert second["speech"].endswith("Do you want a power indicator LED?")

        chose = client.tool("answer_design_questions", session_id=sid,
                            you_choose=True)
        assert chose["state"] == "proposing"
        assert [a["source"] for a in chose["answers"]] == ["user", "default"]

        drafted = client.until(sid, "drafted", "failed")
        assert drafted["state"] == "drafted", drafted["failure"]
        assert drafted["summary"]["parts"] == 3
        assert drafted["speech"] == (
            "The schematic is drafted: three parts on three nets. "
            "Shall I place and route it?")

        client.tool("continue_design", session_id=sid)
        done = client.until(sid, "done", "failed")
        assert done["state"] == "done", done["failure"]
        summary = done["summary"]
        assert summary["parts"] == 3 and summary["routed_fraction"] is not None
        routed_nets = {u["net"] for u in summary["unrouted"]}
        assert routed_nets == set()  # the practice board routes completely
        assert summary["review"]["status"] == "ok"
        assert summary["review"]["blockers"] == 1
        assert summary["findings"][0]["title"] == "VOUT has no bulk capacitor"
        assert "the first blocker: VOUT has no bulk capacitor" in done["speech"]
        assert summary["datasheets_read"] == 0
        assert summary["files"]["board"].startswith(str(steps_dir))

        explained = client.tool("explain_finding", session_id=sid, which=1)
        assert explained["finding"]["refs"] == ["U1", "C2"]
        assert "U1 and C2" in explained["speech"]
    finally:
        _stop(server)
        assert runner.join(10)
        runner.store.close()

    # A fresh store handle and a fresh runner on the same file: the history,
    # not the process, is what remembers.
    fresh = Runner(BoardStore(config.db), lambda: pytest.fail("no model"),
                   object(), steps=FakeSteps(), scripted=True).warm()
    server = _serve(config, fresh)
    client = Client(server)
    try:
        recalled = client.tool("recall_my_boards")
        first = recalled["boards"][0]
        assert (first["session_id"], first["state"], first["blockers"]) == (
            sid, "done", 1)
        assert first["headline"] == "done: 3 parts, fully routed, 1 blocker"
        assert "3.3 volt regulator board" in recalled["speech"]
        again = client.tool("board_status", session_id=sid)
        assert again["state"] == "done" and again["summary"] == summary
        assert client.tool("explain_finding", session_id=sid)["finding"][
            "title"] == "VOUT has no bulk capacitor"
    finally:
        _stop(server)
        fresh.store.close()
    assert loopback_only == []


def test_recall_with_query_and_newest_first_across_two_sessions(tmp_path):
    config = Config(port=0, db=str(tmp_path / "boards.sqlite3"))
    runner = Runner(BoardStore(config.db), lambda: object(), object(),
                    steps=FakeSteps(questions=[])).warm()
    server = _serve(config, runner)
    client = Client(server)
    try:
        for n, intent in enumerate(["a USB-C charger", "an LED blinker"]):
            sid = client.tool("start_board_design", intent=intent,
                              request_id=f"req-0000000{n}")["session_id"]
            client.until(sid, "drafted")
        newest = client.tool("recall_my_boards")
        assert [b["intent"] for b in newest["boards"]] == [
            "an LED blinker", "a USB-C charger"]
        assert newest["speech"].endswith("I have one more; want to hear them?")
        matched = client.tool("recall_my_boards", query="usb-c")
        assert matched["total"] == 1 and matched["boards"][0]["intent"] == (
            "a USB-C charger")
        assert matched["speech"].startswith("Your latest matching board")
        none = client.tool("recall_my_boards", query="robot arm")
        assert none["total"] == 0 and "You have two other boards" in none["speech"]
    finally:
        _stop(server)
        assert runner.join(10)
        runner.store.close()
