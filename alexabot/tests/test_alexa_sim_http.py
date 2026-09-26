"""The simulated Alexa+ page's HTTP surface (``alexabot/sim.py``), with a fake
agent behind ``ConversationHost``: no Strands, no model, loopback only."""

import http.client
import io
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alexabot import agent, conversations, polly, sim
from alexabot.store import BoardStore
from alexabot.tests.fakes import wait_for

FIXTURE = Path(__file__).resolve().parents[2] / "engine" / "tests" / "fixtures" \
    / "ref.kicad_pcb"


class FakeSession:
    def __init__(self, conv, gate):
        self.conv = conv
        self.gate = gate
        self.turns = []

    def run_turn(self, turn_id, text, hint):
        self.turns.append((turn_id, text, hint))
        if self.gate is not None:
            assert self.gate.wait(10)
        self.conv.upsert_card({"id": "boards", "type": "boards", "listItems": []})
        self.conv.say(f"You said {text}.", origin="model", turn_id=turn_id)

    def close(self):
        pass


class FakeFactory:
    def __init__(self, gate=None):
        self.gate = gate
        self.sessions = []

    def open(self, conv):
        session = FakeSession(conv, self.gate)
        self.sessions.append(session)
        return session


class FakePolly:
    def __init__(self):
        self.calls = 0

    def synthesize_speech(self, **kwargs):
        self.calls += 1
        return {"AudioStream": io.BytesIO(b"ID3" + kwargs["Text"].encode()),
                "ContentType": "audio/mpeg",
                "ResponseMetadata": {"HTTPHeaders": {"x-amzn-requestcharacters": "5"}}}


CONFIG = {
    "label": sim.LABEL, "mode": "scripted",
    "agent": {"kind": "scripted", "framework": "Strands Agents", "model_id": None},
    "workers": "scripted", "tts": {"kind": "browser"},
    "mcp": {"url": "http://127.0.0.1:1/mcp", "server": "Ada", "spec": "2025-11-25",
            "tools": sorted(agent.ADA_TOOLS)},
    "board_images": True,
}


class Served:
    def __init__(self, app):
        self.app = app
        self.server = sim.make_sim_server(app, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.05,),
                         daemon=True).start()

    def request(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = raw if raw is not None else (
            None if body is None else json.dumps(body).encode())
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, payload

    def json(self, method, path, body=None, headers=None):
        resp, payload = self.request(method, path, body, headers)
        return resp.status, (json.loads(payload) if payload else None), resp

    def close(self):
        self.app.closing.set()
        self.app.host.close()
        self.server.shutdown()
        self.server.server_close()


def build(*, gate=None, speaker=None, board_file=None, budget=None, audio=False):
    host = conversations.ConversationHost(FakeFactory(gate), audio=audio,
                                          scripted=True)
    app = sim.SimApp(host=host, config=dict(CONFIG), speaker=speaker,
                     board_file=board_file, budget=budget, keepalive_s=0.2)
    return Served(app)


@pytest.fixture
def served(loopback_only):
    s = build()
    yield s
    s.close()


def _events(served, cid, after=0):
    path = f"/api/conversations/{cid}/events?after={after}"
    status, body, _ = served.json("GET", path)
    assert status == 200
    return body


def test_page_carries_the_simulated_label_and_csp(served):
    resp, payload = served.request("GET", "/")
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "text/html; charset=utf-8"
    csp = resp.getheader("Content-Security-Policy")
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    page = payload.decode()
    assert "Simulated Alexa+ experience" in page
    assert "It is not Alexa, not an Alexa skill, and not made by Amazon." in page


def test_static_and_lib_allowlists_and_no_traversal(served):
    for path, kind in (("/static/sim.js", "text/javascript"),
                       ("/static/sim.css", "text/css"),
                       ("/static/fonts/libre-baskerville-latin-wght-normal.woff2",
                        "font/woff2"),
                       ("/lib/voice.js", "text/javascript"),
                       ("/lib/severity.js", "text/javascript")):
        resp, _ = served.request("GET", path)
        assert resp.status == 200, path
        assert resp.getheader("Content-Type").startswith(kind)
    for path in ("/static/../sim.py", "/static/index.html", "/lib/api.js",
                 "/lib/../../../pyproject.toml", "/static/%2e%2e/sim.py",
                 "/api/boards/../../etc/passwd/board.svg"):
        resp, _ = served.request("GET", path)
        assert resp.status == 404, path


def test_new_conversation_logs_the_greeting_without_a_model_call(served):
    status, body, _ = served.json("POST", "/api/conversations", {"locale": "en-GB"})
    assert status == 201
    cid = body["conversation_id"]
    assert cid.startswith("conv_") and len(cid) == 21
    assert body["label"] == "Simulated Alexa+ experience" and body["mode"] == "scripted"
    events = _events(served, cid)["events"]
    assert [e["kind"] for e in events] == ["ada"]
    assert events[0]["text"] == conversations.SCRIPTED_GREETING
    assert events[0]["origin"] == "host" and events[0]["utterance_id"] == "u_1"
    assert served.app.host.get(cid).locale == "en-GB"


def test_turn_is_202_then_events_arrive_in_order(served):
    cid = served.json("POST", "/api/conversations", {})[1]["conversation_id"]
    status, body, _ = served.json("POST", f"/api/conversations/{cid}/turns",
                                  {"text": "  How's   it going?  ", "source": "voice"})
    assert status == 202 and body["turn_id"] == "t_1"
    wait_for(lambda: not served.app.host.get(cid).busy and
             _events(served, cid)["events"][-1]["kind"] == "agent_status")
    events = _events(served, cid)["events"]
    kinds = [e["kind"] for e in events]
    assert kinds == ["ada", "user", "agent_status", "card", "ada", "agent_status"]
    assert events[1]["text"] == "How's it going?"
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert events[-1]["state"] == "idle"
    snapshot = served.json("GET", f"/api/conversations/{cid}")[1]
    assert snapshot["cards"]["boards"]["type"] == "boards"
    assert snapshot["next_seq"] == events[-1]["seq"] + 1


def _read_frames(port, cid, last_event_id, count):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", f"/api/conversations/{cid}/events",
                 headers={"Accept": "text/event-stream",
                          "Last-Event-ID": str(last_event_id)})
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/event-stream")
    frames, current = [], {}
    first = resp.fp.readline().decode()
    assert first == "retry: 2000\n"
    while len(frames) < count:
        line = resp.fp.readline().decode()
        if line == "\n":
            if current:
                frames.append(current)
            current = {}
        elif line.startswith(":"):
            continue
        else:
            key, _, value = line.rstrip("\n").partition(": ")
            current[key] = value
    conn.close()
    return frames


def test_sse_frames_have_id_event_data_and_resume_from_last_event_id(served):
    cid = served.json("POST", "/api/conversations", {})[1]["conversation_id"]
    served.json("POST", f"/api/conversations/{cid}/turns", {"text": "hi"})
    wait_for(lambda: not served.app.host.get(cid).busy)
    everything = _events(served, cid)["events"]
    frames = _read_frames(served.port, cid, 2, len(everything) - 2)
    assert [int(f["id"]) for f in frames] == [e["seq"] for e in everything[2:]]
    for frame in frames:
        data = json.loads(frame["data"])
        assert frame["event"] == data["kind"] and int(frame["id"]) == data["seq"]


def test_busy_conversation_is_409(loopback_only):
    gate = threading.Event()
    s = build(gate=gate)
    try:
        cid = s.json("POST", "/api/conversations", {})[1]["conversation_id"]
        assert s.json("POST", f"/api/conversations/{cid}/turns",
                      {"text": "one"})[0] == 202
        status, body, _ = s.json("POST", f"/api/conversations/{cid}/turns",
                                 {"text": "two"})
        assert status == 409
        assert body == {"error": "busy", "speech": "One moment, I'm still answering."}
    finally:
        gate.set()
        s.app.host.join(5)
        s.close()


def test_foreign_origin_is_403_and_foreign_host_is_421(served):
    status, body, _ = served.json("POST", "/api/conversations", {},
                                  headers={"Origin": "https://evil.example"})
    assert status == 403 and body["error"] == "forbidden_origin"
    ok = served.json("POST", "/api/conversations", {},
                     headers={"Origin": f"http://127.0.0.1:{served.port}"})
    assert ok[0] == 201
    status, _, _ = served.json("GET", "/api/config",
                               headers={"Host": "attacker.example:80"})
    assert status == 421
    status, _, _ = served.json("GET", "/api/config",
                               headers={"Host": f"localhost:{served.port}"})
    assert status == 200


def test_body_over_8k_is_413_and_bad_hint_is_400(served):
    cid = served.json("POST", "/api/conversations", {})[1]["conversation_id"]
    resp, _ = served.request("POST", f"/api/conversations/{cid}/turns",
                             raw=b"{" + b" " * 9000 + b"}")
    assert resp.status == 413
    for bad in ({"text": ""}, {"text": "x" * 501}, {"text": "hi", "source": "psychic"},
                {"text": "hi", "hint": {"tool": "order_board", "arguments": {}}},
                {"text": "hi", "hint": {"tool": "board_status", "arguments": []}},
                {"text": "hi", "hint": {"tool": "board_status",
                                        "arguments": {"a": {"b": {"c": 1}}}}}):
        status, body, _ = served.json("POST", f"/api/conversations/{cid}/turns", bad)
        assert status == 400, bad
    ok = {"text": "go", "source": "chip",
          "hint": {"tool": "answer_design_questions",
                   "arguments": {"session_id": "brd_1a2b3c4d5e6f",
                                 "answers": [{"index": 0, "answer": "one amp"}]}}}
    assert served.json("POST", f"/api/conversations/{cid}/turns", ok)[0] == 202
    status, body, _ = served.json("POST", "/api/conversations/conv_0000000000000000/"
                                  "turns", {"text": "hi"})
    assert status == 404 and body == {"error": "unknown_conversation"}


def test_speech_route_serves_only_logged_utterances(loopback_only):
    client = FakePolly()
    budget = agent.Budget(10, 1)
    speaker = polly.PollySpeaker(client, take=budget.take_polly)
    s = build(speaker=speaker, audio=True, budget=budget)
    try:
        cid = s.json("POST", "/api/conversations", {})[1]["conversation_id"]
        greeting = _events(s, cid)["events"][0]
        assert greeting["audio"] == f"/api/conversations/{cid}/speech/u_1.mp3"
        resp, data = s.request("GET", greeting["audio"])
        assert resp.status == 200 and resp.getheader("Content-Type") == "audio/mpeg"
        assert data.startswith(b"ID3Scripted mode")
        resp, _ = s.request("GET", f"/api/conversations/{cid}/speech/u_1.mp3")
        assert resp.status == 200 and client.calls == 1  # cached, no second call
        resp, body = s.request("GET", f"/api/conversations/{cid}/speech/u_99.mp3")
        assert resp.status == 404
        assert json.loads(body)["error"] == "unknown_utterance"
        s.json("POST", f"/api/conversations/{cid}/turns", {"text": "hello"})
        wait_for(lambda: not s.app.host.get(cid).busy)
        spoken = [e for e in _events(s, cid)["events"]
                  if e["kind"] == "ada" and e["seq"] > 1][0]
        resp, body = s.request("GET", spoken["audio"])
        assert resp.status == 404
        assert json.loads(body) == {"error": "no_audio", "fallback": "browser",
                                    "reason": "polly_budget"}
        notices = [e for e in _events(s, cid)["events"] if e["kind"] == "notice"]
        assert [n["text"] for n in notices] == [sim.POLLY_FALLBACK_NOTICE]
    finally:
        s.close()
    browser = build()
    try:
        cid = browser.json("POST", "/api/conversations", {})[1]["conversation_id"]
        assert _events(browser, cid)["events"][0]["audio"] is None
        resp, body = browser.request("GET", f"/api/conversations/{cid}/speech/u_1.mp3")
        assert resp.status == 404 and json.loads(body)["reason"] == "browser_voice"
    finally:
        browser.close()


def test_board_routes_refuse_other_accounts_unrouted_and_paths_outside_steps_dir(
    tmp_path, loopback_only
):
    steps = tmp_path / "steps"
    (steps / "s1").mkdir(parents=True)
    inside = steps / "s1" / "board.kicad_pcb"
    shutil.copy(FIXTURE, inside)
    outside = tmp_path / "elsewhere.kicad_pcb"
    shutil.copy(FIXTURE, outside)
    store = BoardStore(tmp_path / "b.sqlite3")
    now = time.time()

    def board(account, sid, path):
        store.claim(account, f"req-{sid}", "a board", session_id=sid, now=now)
        store.set_state(account, sid, "done", now=now, summary={
            "files": {"board": None if path is None else str(path)}})

    board("local", "brd_aaaaaaaaaaaa", inside)
    board("someone", "brd_bbbbbbbbbbbb", inside)
    board("local", "brd_cccccccccccc", None)
    board("local", "brd_dddddddddddd", outside)
    s = build(board_file=sim.board_file_lookup(store, "local", steps))
    try:
        resp, svg = s.request("GET", "/api/boards/brd_aaaaaaaaaaaa/board.svg")
        assert resp.status == 200 and resp.getheader("Content-Type") == "image/svg+xml"
        assert b'data-ref="U1"' in svg
        resp, pcb = s.request("GET", "/api/boards/brd_aaaaaaaaaaaa/board.kicad_pcb")
        assert resp.status == 200 and pcb == inside.read_bytes()
        assert "attachment" in resp.getheader("Content-Disposition")
        for sid in ("brd_bbbbbbbbbbbb", "brd_cccccccccccc", "brd_dddddddddddd",
                    "brd_eeeeeeeeeeee", "brd_nothex"):
            resp, _ = s.request("GET", f"/api/boards/{sid}/board.svg")
            assert resp.status == 404, sid
    finally:
        s.close()
        store.close()
    remote = build(board_file=None)
    try:
        resp, _ = remote.request("GET", "/api/boards/brd_aaaaaaaaaaaa/board.svg")
        assert resp.status == 503
    finally:
        remote.close()


def test_config_reports_mode_agent_tts_and_budget_truthfully(loopback_only):
    budget = agent.Budget(20, 10)
    budget.take_model()
    s = build(budget=budget)
    try:
        status, body, _ = s.json("GET", "/api/config")
        assert status == 200
        assert body["label"] == "Simulated Alexa+ experience"
        assert body["mode"] == "scripted" and body["agent"]["kind"] == "scripted"
        assert body["tts"] == {"kind": "browser"}
        assert body["budget"] == {"model_calls": {"used": 1, "max": 20},
                                  "polly_calls": {"used": 0, "max": 10}}
    finally:
        s.close()


def test_an_unknown_conversation_is_404_everywhere(served):
    for path in ("/api/conversations/conv_1234567890abcdef",
                 "/api/conversations/conv_1234567890abcdef/events",
                 "/api/conversations/conv_1234567890abcdef/speech/u_1.mp3"):
        status, body, _ = served.json("GET", path)
        assert status == 404 and body == {"error": "unknown_conversation"}, path


def test_every_api_response_is_no_store(served):
    for path in ("/api/config", "/healthz"):
        resp, _ = served.request("GET", path)
        assert resp.getheader("Cache-Control") == "no-store"
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{served.port}/nope", timeout=5)


def test_a_card_is_logged_only_when_it_changed():
    conv = conversations.Conversation(id="conv_0123456789abcdef", locale="en-US",
                                      audio=False)
    card = {"id": "board:brd_1", "type": "board", "state": "routing",
            "elapsedS": 3.0}
    assert conv.upsert_card(card)["kind"] == "card"
    assert conv.upsert_card({**card, "elapsedS": 6.0}) is None
    assert conv.upsert_card({**card, "state": "reviewing"})["card"]["state"] == \
        "reviewing"
    assert [e["kind"] for e in conv.log.since(0)] == ["card", "card"]


def test_focus_brings_back_a_known_card_and_ignores_an_unknown_one():
    conv = conversations.Conversation(id="conv_0123456789abcdef", locale="en-US",
                                      audio=False)
    board = {"id": "board:brd_1", "type": "board", "state": "done"}
    finding = {"id": "finding:brd_1:1", "type": "finding", "sessionId": "brd_1"}
    conv.upsert_card(board)
    conv.upsert_card(finding)
    assert conv.snapshot()["focus"] == "finding:brd_1:1"
    assert conv.upsert_card(board) is None
    event = conv.focus_card("board:brd_1")
    assert event["kind"] == "focus" and event["card_id"] == "board:brd_1"
    assert conv.snapshot()["focus"] == "board:brd_1"
    assert conv.focus_card("board:brd_nope") is None
    assert [e["kind"] for e in conv.log.since(0)] == ["card", "card", "focus"]
