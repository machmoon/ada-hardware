"""The simulated Alexa+ agent end to end, offline: a real ``strands.Agent`` with
the scripted voice model, one real Strands ``MCPClient`` over Streamable HTTP
to an in-process alexabot with scripted workers, and the real
``service/steps.py`` pipeline. Only the language model is replaced.

Skips without the ``alexa`` extra. The gate is a ``skipif`` mark, the
``test_spice.py`` ``needs_ngspice`` convention, rather than a module-level
``importorskip``: the latter drops the file from ``pytest --collect-only``,
and ``scripts/check_docs.py`` would then count fewer tests on a CI runner
without the extra than the README quotes. A socket guard proves nothing leaves
the machine, and ``boto3.client`` is made to raise, so no Bedrock or Polly
client can be built.
"""

import importlib.util
import json
import re
import time
import uuid

import pytest

from alexabot import agent, cards, conversations, sim, tools
from alexabot.scripted_agent import IDENTITY_TEXT
from alexabot.tests.fakes import wait_for

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("strands") is None,
    reason='the alexa extra is not installed: pip install -e ".[alexa]"',
)

WORKING = agent.WORKING


@pytest.fixture
def no_aws(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    def refuse(*args, **kwargs):
        raise AssertionError("an offline test built an AWS client")

    monkeypatch.setattr(boto3, "client", refuse)


@pytest.fixture
def served_results(monkeypatch):
    """Every structuredContent the alexabot server produced, in order."""
    produced = []
    real = tools._result

    def spy(structured):
        result = real(structured)
        produced.append(json.loads(json.dumps(result["structuredContent"])))
        return result

    monkeypatch.setattr(tools, "_result", spy)
    return produced


@pytest.fixture
def running(tmp_path, steps_dir, loopback_only, no_aws, served_results):
    config = sim.load_sim_config(
        ["--scripted", "--port", "0", "--mcp-port", "0",
         "--db", str(tmp_path / "boards.sqlite3"), "--scripted-delay", "0.3"],
        env={})
    built = sim.build(config, env={}, poll_min_s=0.05, poll_cap_s=0.15)
    yield built
    built.close()
    assert loopback_only == []


def settle(built, conv, timeout=60.0):
    """Wait until the turn is over and no poller is still checking."""
    def quiet():
        return not conv.busy and not any(
            s.poller.running() for s in built.factory.sessions if s.conv is conv)

    wait_for(quiet, timeout=timeout, interval=0.02)


def talk(built, conv, text, hint=None):
    after = conv.log.next_seq - 1
    built.host.turn(conv.id, text, "typed", hint)
    time.sleep(0.05)
    settle(built, conv)
    return conv.log.since(after)


def spoken(events, kinds=("ada",)):
    return [e["text"] for e in events if e["kind"] in kinds]


def session_of(built, conv):
    return next(s for s in built.factory.sessions if s.conv is conv)


def test_mcp_client_negotiates_and_lists_the_six_tools_with_instructions(running):
    assert running.client.server_instructions == tools.INSTRUCTIONS
    names = sorted(t.tool_name for t in running.factory.tools)
    assert names == sorted(agent.ADA_TOOLS)
    assert running.app.config_view()["mcp"]["spec"] == "2025-11-25"


def test_a_whole_spoken_session_offline(running):
    conv = running.host.create("en-US")
    assert spoken(conv.log.since(0)) == [conversations.SCRIPTED_GREETING]

    events = talk(running, conv, "Ask Ada for a three point three volt regulator "
                  "board powered from USB-C.")
    said = spoken(events)
    assert said[0].startswith("Scripted mode, so you'll get the practice regulator")
    assert "up to 500 milliamps" in said[-1]
    assert said[-1].endswith("How much current should the 3.3 volt rail supply?")
    board = conv.cards[f"board:{conv.current_session_id}"]
    assert board["question"]["default"] == "up to 500 milliamps"

    events = talk(running, conv, "Up to one amp.")
    assert spoken(events) == ["Got it. Next, if you don't say, I'll assume no LED. "
                              "Do you want a power indicator LED?"]

    events = talk(running, conv, "You choose.")
    said = spoken(events)
    assert said[0].startswith("Okay, I'll go with my defaults.")
    assert said[-1] == ("The schematic is drafted: three parts on three nets. "
                        "Shall I place and route it?")

    events = talk(running, conv, "Yes, place and route it.")
    said = spoken(events)
    progress = [e for e in events if e["kind"] == "progress"]
    assert all(e["state"] in WORKING for e in progress)
    assert [e["state"] for e in progress] == sorted(
        (e["state"] for e in progress),
        key=["placing", "routing", "reviewing"].index)
    assert progress, "the host narrated nothing while the board worked"
    assert said[-1].startswith("Your board is done: three parts, fully routed.")
    assert "the first blocker: VOUT has no bulk capacitor" in said[-1]
    sid = conv.current_session_id
    done = conv.cards[f"board:{sid}"]
    assert done["state"] == "done" and done["image"]["src"].endswith("/board.svg")
    assert running.app.board_file(sid) is not None

    events = talk(running, conv, "Explain the first blocker.")
    finding = conv.cards[f"finding:{sid}:1"]
    assert finding["highlightRefs"] == ["U1", "C2"]
    assert conv.cards[f"board:{sid}"]["highlightRefs"] == ["U1", "C2"]
    assert spoken(events)[0].startswith("Finding 1 of 3 is a blocker")

    events = talk(running, conv, "Are you Alexa?")
    assert spoken(events) == [IDENTITY_TEXT]

    fresh = running.host.create("en-US")
    events = talk(running, fresh, "How did my regulator board go?")
    boards = fresh.cards["boards"]
    assert boards["listItems"][0]["sessionId"] == sid
    assert boards["query"] == "regulator"


def test_cards_equal_the_tool_structured_content(running, served_results):
    conv = running.host.create("en-US")
    seen = []
    session = session_of(running, conv)
    real = session.absorb

    def spy(tool, sc, **kw):
        seen.append((tool, json.loads(json.dumps(sc))))
        real(tool, sc, **kw)

    session.absorb = spy
    talk(running, conv, "Build me a regulator board")
    talk(running, conv, "you choose")
    assert seen
    for tool, sc in seen:
        assert sc in served_results, f"{tool} card was not built from served data"
        card = cards.from_tool(tool, sc, images=True)
        assert conv.cards[card["id"]]["sessionId"] == sc["session_id"]
    last_tool, last_sc = seen[-1]
    rebuilt = cards.from_tool(last_tool, last_sc, images=True)
    assert conv.cards[rebuilt["id"]] == rebuilt


def test_one_model_call_per_successful_turn(running):
    conv = running.host.create("en-US")
    talk(running, conv, "Build me a regulator board")
    talk(running, conv, "Up to one amp")
    talk(running, conv, "you choose")
    model_traces = [e for e in conv.log.since(0)
                    if e["kind"] == "trace" and e["step"] == "model"]
    per_turn = {}
    for e in model_traces:
        per_turn[e["turn_id"]] = per_turn.get(e["turn_id"], 0) + 1
    assert per_turn == {"t_1": 1, "t_2": 1, "t_3": 1}
    assert len(session_of(running, conv).agent.model.calls) == 3


def test_request_id_is_host_minted_uuid_and_reused_within_a_turn(running):
    conv = running.host.create("en-US")
    talk(running, conv, "Build me a regulator board")
    row = running.runner.store.get("local", conv.current_session_id)
    assert str(uuid.UUID(row.request_id)) == row.request_id
    assert row.request_id != "host-assigned"

    hooks = agent.VoiceHooks(session_of(running, conv))
    session = hooks.session

    class Event:
        def __init__(self, name, arguments):
            self.agent = session.agent
            self.tool_use = {"toolUseId": "x", "name": name, "input": arguments}
            self.cancel_tool = False

    session.turn = agent.TurnState("t_9")
    first = Event("start_board_design", {"intent": "A  Board", "request_id": "m1"})
    again = Event("start_board_design", {"intent": "a board", "request_id": "m2"})
    other = Event("start_board_design", {"intent": "another", "request_id": "m3"})
    for event in (first, again, other):
        hooks.before_tool(event)
    ids = [e.tool_use["input"]["request_id"] for e in (first, again, other)]
    assert ids[0] == ids[1] != ids[2]
    session.turn = agent.TurnState("t_10")
    later = Event("start_board_design", {"intent": "a board", "request_id": "m4"})
    hooks.before_tool(later)
    assert later.tool_use["input"]["request_id"] not in ids
    session.turn = None
    orphan = Event("start_board_design", {"intent": "a board", "request_id": "m5"})
    hooks.before_tool(orphan)
    assert orphan.cancel_tool == "Something went wrong on my side."


def test_missing_session_id_is_filled_and_a_given_one_is_kept(running):
    conv = running.host.create("en-US")
    session = session_of(running, conv)
    hooks = agent.VoiceHooks(session)
    conv.current_session_id = "brd_111111111111"
    session.turn = agent.TurnState("t_1")

    class Event:
        def __init__(self, name, arguments):
            self.agent = session.agent
            self.tool_use = {"toolUseId": "x", "name": name, "input": arguments}
            self.cancel_tool = False

    missing = Event("continue_design", {})
    empty = Event("explain_finding", {"session_id": "", "which": 1})
    given = Event("board_status", {"session_id": "brd_222222222222"})
    recall = Event("recall_my_boards", {})
    for event in (missing, empty, given, recall):
        hooks.before_tool(event)
    assert missing.tool_use["input"]["session_id"] == "brd_111111111111"
    assert empty.tool_use["input"]["session_id"] == "brd_111111111111"
    assert given.tool_use["input"]["session_id"] == "brd_222222222222"
    assert "session_id" not in recall.tool_use["input"]


def test_polls_are_host_calls_without_model_calls_or_history(running):
    conv = running.host.create("en-US")
    talk(running, conv, "Build me a regulator board")
    tool_traces = [e for e in conv.log.since(0)
                   if e["kind"] == "trace" and e["step"] == "tool"]
    polls = [e for e in tool_traces if e["origin"] == "host-poll"]
    assert polls and all(e["tool"] == "board_status" for e in polls)
    session = session_of(running, conv)
    uses = [b["toolUse"] for m in session.agent.messages for b in m["content"]
            if "toolUse" in b]
    assert [u["name"] for u in uses] == ["start_board_design"]
    assert session.poll_agent.messages == []
    assert len(session.agent.model.calls) == 1


def test_the_model_sees_one_compact_text_block(running):
    conv = running.host.create("en-US")
    talk(running, conv, "Build me a regulator board")
    talk(running, conv, "Up to one amp")
    session = session_of(running, conv)
    results = [b["toolResult"] for m in session.agent.messages for b in m["content"]
               if "toolResult" in b]
    assert len(results) == 2
    for result in results:
        assert len(result["content"]) == 1
        line = result["content"][0]["text"]
        assert "\n" not in line and len(line) <= agent.MODEL_VIEW_MAX
        view = json.loads(line)
        assert {"session_id", "state", "speech"} <= set(view)
        assert "structuredContent" not in result or result["structuredContent"]
    prompts = [b["text"] for m in session.agent.messages if m["role"] == "user"
               for b in m["content"] if "text" in b]
    assert prompts[0] == "Build me a regulator board"
    assert prompts[1].startswith("Up to one amp\n[Host note: the newest board status")


def _metered_host(running, *, budget, turn_limit=agent.TURN_LIMIT):
    from alexabot.scripted_agent import scripted_model

    factory = agent.VoiceAgentFactory(
        model_factory=scripted_model, tools=running.factory.tools,
        server_name="Ada", server_instructions=tools.INSTRUCTIONS, budget=budget,
        model_id="scripted-metered", metered=True, poll_min_s=0.05, poll_cap_s=0.15,
        turn_limit=turn_limit)
    return factory, conversations.ConversationHost(factory, audio=False,
                                                   scripted=True)


def test_model_budget_cancels_and_speaks_the_limit(running):
    budget = agent.Budget(1, 0)
    factory, host = _metered_host(running, budget=budget)
    conv = host.create("en-US")
    try:
        host.turn(conv.id, "Are you Alexa?", "typed")
        wait_for(lambda: not conv.busy)
        host.turn(conv.id, "Are you Alexa?", "typed")
        wait_for(lambda: not conv.busy)
        said = spoken(conv.log.since(1))
        assert said == [IDENTITY_TEXT, agent.BUDGET_SPEECH]
        assert budget.snapshot()["model_calls"] == {"used": 1, "max": 1}
    finally:
        host.close()


def test_turn_limit_is_spoken_as_stuck(running):
    factory, host = _metered_host(running, budget=agent.Budget(50, 0), turn_limit=1)
    conv = host.create("en-US")
    try:
        # A refused tool call has no speech to end the turn on, so the model
        # would be asked again; the limit stops it and says so.
        hint = {"tool": "explain_finding",
                "arguments": {"session_id": "brd_000000000000", "which": 1}}
        host.turn(conv.id, "Explain that", "chip", hint)
        wait_for(lambda: not conv.busy)
        assert spoken(conv.log.since(1)) == [agent.STUCK_SPEECH]
    finally:
        host.close()


def test_exception_text_never_reaches_an_event(running):
    conv = running.host.create("en-US")
    talk(running, conv, "Explain finding 2", {"tool": "explain_finding",
                                               "arguments": {
                                                   "session_id": "brd_000000000000",
                                                   "which": 2}})
    for event in conv.log.since(0):
        text = json.dumps(event)
        assert "Traceback" not in text
        assert not re.search(r"Tool execution failed", text)


def page_focus(events):
    """The card the page shows after ``events``: ``sim.js`` ``takeCard`` and
    the ``focus`` event, replayed the way a resumed page replays them."""
    cards_seen, focus = {}, None
    for event in events:
        if event["kind"] == "card":
            card = event["card"]
            before = cards_seen.get(card["id"])
            cards_seen[card["id"]] = card
            focused = cards_seen.get(focus) if focus else None
            only_highlight = (card["type"] == "board" and before is not None
                              and before["state"] == card["state"]
                              and focused is not None
                              and focused["type"] == "finding"
                              and focused["sessionId"] == card["sessionId"])
            if not only_highlight:
                focus = card["id"]
        elif event["kind"] == "focus" and event["card_id"] in cards_seen:
            focus = event["card_id"]
    return focus


def _done_board(built, conv):
    talk(built, conv, "Build me a regulator board")
    talk(built, conv, "you choose")
    talk(built, conv, "Yes, place and route it.")
    assert conv.cards[f"board:{conv.current_session_id}"]["state"] == "done"
    return conv.current_session_id


def test_a_model_call_to_an_unchanged_card_brings_it_back_into_focus(running):
    conv = running.host.create("en-US")
    sid = _done_board(running, conv)
    talk(running, conv, "Explain the first blocker.")
    assert page_focus(conv.log.since(0)) == f"finding:{sid}:1"

    # "Back to the board": the board card is unchanged, so no card event; the
    # focus event is what brings it back, live and on a resumed page.
    finding = conv.cards[f"finding:{sid}:1"]
    back = next(b for b in finding["buttons"] if b["text"] == "Back to the board")
    events = talk(running, conv, back["utterance"], back["hint"])
    assert [e for e in events if e["kind"] == "card"] == []
    assert [e["card_id"] for e in events if e["kind"] == "focus"] == [f"board:{sid}"]
    assert page_focus(conv.log.since(0)) == f"board:{sid}"
    assert conv.snapshot()["focus"] == f"board:{sid}"

    # The same finding again: unchanged too, and focused again.
    talk(running, conv, "Explain the first blocker.")
    assert page_focus(conv.log.since(0)) == f"finding:{sid}:1"

    # A plain status question about the unchanged board.
    talk(running, conv, "How's it going?")
    assert page_focus(conv.log.since(0)) == f"board:{sid}"

    # Host polls keep the dedupe: they never log a focus event.
    polls = [e for e in conv.log.since(0) if e["kind"] == "trace"
             and e["step"] == "tool" and e["origin"] == "host-poll"]
    assert polls
    focus_events = [e for e in conv.log.since(0) if e["kind"] == "focus"]
    assert len(focus_events) == 3
