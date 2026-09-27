"""Reviewer finding B1: no tool call waits on a model or on the solver.

Real HTTP, the real :class:`~alexabot.runner.Runner` and the real
``service/steps.py``, with the scripted model answering a second and a half
late. Every call, the first included, must return in under a second -- and
the status polls must *see* the slow states, which is what proves the call
came back while the model was still working rather than after it.
"""

import json
import threading
import time
import urllib.request

import pytest
from silkscreen.mcp.http import ENDPOINT

from alexabot import app, scripted
from alexabot.config import Config
from alexabot.runner import Runner
from alexabot.store import BoardStore

LIMIT_S = 1.0
MODEL_DELAY_S = 1.5


class Timed:
    def __init__(self, server):
        self.url = f"http://127.0.0.1:{server.server_address[1]}{ENDPOINT}"
        self.latencies: list[tuple[str, float]] = []

    def tool(self, name, **arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())["result"]
        self.latencies.append((name, time.perf_counter() - started))
        assert result["isError"] is False, result["content"][0]["text"]
        return result["structuredContent"]

    def poll(self, sid, *until, timeout=60):
        seen = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.tool("board_status", session_id=sid)
            if not seen or seen[-1] != status["state"]:
                seen.append(status["state"])
            if status["state"] in until:
                return status, seen
            time.sleep(0.05)
        raise AssertionError(f"never reached {until}; saw {seen}")

    def worst(self):
        return max(self.latencies, key=lambda item: item[1])


@pytest.fixture
def serve(tmp_path, steps_dir):
    started = []

    def start(runner):
        config = Config(port=0, db=":memory:", scripted=True)
        server = app.make_server(config, runner=runner)
        threading.Thread(target=server.serve_forever, args=(0.05,),
                         daemon=True).start()
        started.append((server, runner))
        return Timed(server)

    yield start
    for server, runner in started:
        server.shutdown()
        server.server_close()
        runner.join(30)


def _runner(factory):
    return Runner(BoardStore(":memory:"), factory, None, scripted=True).warm()


def test_every_tool_answers_under_a_second_while_the_model_is_slow(serve):
    client = serve(_runner(scripted.model_factory(MODEL_DELAY_S)))
    sid = client.tool("start_board_design", intent="a 3.3V regulator board",
                      request_id="lat-00000001")["session_id"]
    _, seen = client.poll(sid, "questions")
    assert "reading" in seen
    client.tool("answer_design_questions", session_id=sid,
                answers=[{"index": 0, "answer": "one amp"}])
    client.tool("answer_design_questions", session_id=sid, you_choose=True)
    client.tool("continue_design", session_id=sid)  # queued while proposing
    done, seen_after = client.poll(sid, "done", "failed")
    assert done["state"] == "done", done["failure"]
    assert "proposing" in seen_after and "reviewing" in seen_after
    client.tool("explain_finding", session_id=sid)
    client.tool("recall_my_boards")
    name, worst = client.worst()
    print(f"\nB1: {len(client.latencies)} tool calls, slowest {name} "
          f"at {worst * 1000:.1f} ms (model delay {MODEL_DELAY_S} s per call)")
    assert worst < LIMIT_S, client.latencies
    assert client.latencies[0][1] < LIMIT_S  # the first call pays no import


def test_status_answers_under_a_second_during_routing(serve, monkeypatch):
    """A poll while the pure-Python A* router holds the GIL."""
    from service import steps

    real = steps.route_stage
    routing = threading.Event()

    def busy_route(*args, **kwargs):
        routing.set()
        stop = time.perf_counter() + 1.5
        n = 0
        while time.perf_counter() < stop:  # pure Python, like the router
            n += sum(i * i for i in range(200))
        return real(*args, **kwargs)

    monkeypatch.setattr(steps, "route_stage", busy_route)
    client = serve(_runner(scripted.model_factory(0.0)))
    sid = client.tool("start_board_design", intent="a board",
                      request_id="lat-00000002")["session_id"]
    client.poll(sid, "questions")
    client.tool("answer_design_questions", session_id=sid, you_choose=True)
    client.poll(sid, "drafted")
    client.tool("continue_design", session_id=sid)
    assert routing.wait(30)
    polls_in_routing = 0
    while True:
        status = client.tool("board_status", session_id=sid)
        if status["state"] == "routing":
            polls_in_routing += 1
        if status["state"] in ("reviewing", "done", "failed"):
            break
        time.sleep(0.05)
    assert polls_in_routing >= 3
    assert client.worst()[1] < LIMIT_S, client.latencies


def test_a_gated_model_leaves_start_returning_in_reading(serve):
    gate = threading.Event()
    inner = scripted.model_factory(0.0)

    class Gated:
        def __init__(self):
            self._inner = inner()

        def generate(self, prompt, **kwargs):
            assert gate.wait(10), "the test never opened the gate"
            return self._inner.generate(prompt, **kwargs)

    client = serve(_runner(Gated))
    started = client.tool("start_board_design", intent="a board",
                          request_id="lat-00000003")
    assert started["state"] == "reading"
    assert client.tool("board_status", session_id=started["session_id"])[
        "state"] == "reading"
    gate.set()
    client.poll(started["session_id"], "questions")
