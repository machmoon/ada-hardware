"""Memory in the simulated Alexa+ agent, end to end and offline: a real Strands
agent with the scripted voice model, a real Strands ``MCPClient`` over
Streamable HTTP to an in-process alexabot, and ``ScriptedMemory`` standing in
for AgentCore. Only the language model and the extractor are replaced.

Skips without the ``alexa`` extra through a ``skipif`` mark (the
``test_alexa_sim_agent.py`` rule, so check_docs counts the same everywhere).
``boto3.client`` is made to raise, so no AWS client can be built.
"""

import importlib.util
import time

import pytest

from alexabot import agent, memory, scripted_agent, sim, tools
from alexabot.tests.fakes import wait_for

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("strands") is None,
    reason='the alexa extra is not installed: pip install -e ".[alexa]"',
)
ASK = "Last time you chose: USB-C power input and 3.3 volt logic. Same again?"


@pytest.fixture
def no_aws(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    def refuse(*args, **kwargs):
        raise AssertionError("an offline test built an AWS client")

    monkeypatch.setattr(boto3, "client", refuse)


def _build(tmp_path, *extra):
    config = sim.load_sim_config(
        ["--scripted", "--port", "0", "--mcp-port", "0",
         "--db", str(tmp_path / "boards.sqlite3"), "--scripted-delay", "0.2", *extra],
        env={})
    return sim.build(config, env={}, poll_min_s=0.05, poll_cap_s=0.15)


@pytest.fixture
def running(tmp_path, steps_dir, loopback_only, no_aws):
    built = _build(tmp_path)
    yield built
    built.close()
    assert loopback_only == []


def session_of(built, conv):
    return next(s for s in built.factory.sessions if s.conv is conv)


def settle(built, conv, timeout=60.0):
    session = session_of(built, conv)
    wait_for(lambda: not conv.busy and not session.poller.running(), timeout=timeout,
             interval=0.02)
    assert session.memory.flush(5)


def talk(built, conv, text):
    after = conv.log.next_seq - 1
    built.host.turn(conv.id, text, "typed")
    time.sleep(0.05)
    settle(built, conv)
    return conv.log.since(after)


def said(events):
    return [e["text"] for e in events if e["kind"] == "ada"]


def opened(built):
    conv = built.host.create("en-US")
    assert session_of(built, conv).memory.wait_ready(5)
    return conv


def prompts(built, conv):
    """Every user message text the scripted model was shown."""
    model = session_of(built, conv).agent.model
    return [b["text"] for call in model.calls for m in call if m["role"] == "user"
            for b in m["content"] if "text" in b]


def started(events):
    return [e["arguments"]["intent"] for e in events if e["kind"] == "trace"
            and e.get("step") == "tool" and e.get("tool") == "start_board_design"]


def test_a_preference_said_once_is_offered_in_the_next_conversation(running):
    a = opened(running)
    assert a.memory["label"] == "Scripted memory: nothing saved yet"
    talk(running, a, "Ask Ada for a three point three volt regulator board powered "
         "from USB-C.")
    b = opened(running)
    firsts = [e["memory"] for e in b.log.since(0) if e["kind"] == "memory"]
    assert firsts[0]["state"] == "checking" or firsts[0]["state"] == "ok"
    assert b.memory["label"] == "Remembering (scripted): USB-C power input, 3.3 V logic"
    assert b.snapshot()["memory"]["preferences"][0]["short"] == "USB-C power input"

    events = talk(running, b, "Ask Ada for a sensor board")
    assert said(events) == [ASK]
    assert started(events) == []
    assert any("[Memory: from earlier conversations" in t for t in prompts(running, b))
    events = talk(running, b, "Yes")
    intent = started(events)[0]
    assert intent == "a sensor board, with USB-C power input and 3.3 V logic"


def test_the_request_s_own_words_win(running):
    a = opened(running)
    talk(running, a, "Ask Ada for a three point three volt regulator board powered "
         "from USB-C.")
    c = opened(running)
    events = talk(running, c, "Ask Ada for a 5 V board with a barrel jack")
    assert ASK not in said(events)
    assert not any("[Memory" in t for t in prompts(running, c))
    intent = started(events)[0]
    assert "USB" not in intent and "3.3" not in intent


def test_nothing_is_written_for_a_chip_or_you_choose(running):
    a = opened(running)
    talk(running, a, "Ask Ada for a regulator board")
    session = session_of(running, a)
    running.host.turn(a.id, "You choose", "chip",
                      {"tool": "answer_design_questions",
                       "arguments": {"session_id": a.current_session_id,
                                     "you_choose": True}})
    time.sleep(0.05)
    settle(running, a)
    writes = [e for e in a.log.since(0) if e["kind"] == "trace"
              and e.get("step") == "memory" and e.get("op") == "create_event"]
    assert len(writes) == 1, writes  # the typed request only
    assert session.memory.memory.recall(session.memory.actor).preferences == ()


def test_memory_off_says_so_and_a_claimed_memory_is_replaced(tmp_path, steps_dir,
                                                              loopback_only, no_aws,
                                                              monkeypatch):
    built = _build(tmp_path, "--memory", "off")
    try:
        view = built.app.config_view()["memory"]
        assert view["kind"] == "off" and "ADA_AGENTCORE_MEMORY_ID" in view["reason"]
        assert "memory off" in sim.banner(built)
        conv = opened(built)
        assert conv.memory["label"] == "Memory off"
        monkeypatch.setattr(scripted_agent, "decide", lambda messages: {
            "text": "Last time you wanted USB-C, so I'll use that again."})
        events = talk(built, conv, "Make it like last time")
        assert said(events) == [memory.OFF_SPEECH]
        assert any(e["kind"] == "trace" and e.get("rule") == "memory_claim"
                   for e in events)
        assert not any("[Memory" in t for t in prompts(built, conv))
        system = session_of(built, conv).agent.system_prompt
        assert "You keep nothing between conversations here." in system
    finally:
        built.close()


class WritesExplode(memory.ScriptedMemory):
    def record(self, actor, session, rec):
        raise RuntimeError("the network went away with an ARN in it")


def test_a_failing_write_is_one_notice_and_the_turn_is_still_spoken(running):
    factory = agent.VoiceAgentFactory(
        model_factory=scripted_agent.scripted_model, tools=running.factory.tools,
        server_name="Ada", server_instructions=tools.INSTRUCTIONS,
        budget=agent.Budget(0, 0), model_id=None, metered=False,
        memory=WritesExplode(), actor=memory.actor_id("local"))
    from alexabot.conversations import ConversationHost

    host = ConversationHost(factory, audio=False, scripted=True)
    conv = host.create("en-US")
    try:
        session = factory.sessions[0]
        for text in ("Are you Alexa?", "Are you Alexa?"):
            host.turn(conv.id, text, "typed")
            wait_for(lambda: not conv.busy)
            assert session.memory.flush(5)
        assert said(conv.log.since(0))[-1] == scripted_agent.IDENTITY_TEXT
        notices = [e["text"] for e in conv.log.since(0) if e["kind"] == "notice"]
        assert notices == [memory.WRITE_NOTICE]
        assert conv.memory["state"] == "unavailable"
        assert conv.memory["label"] == "Memory unavailable"
        assert conv.memory["detail"] == memory.REASONS["failed"]
        assert "ARN" not in str(conv.log.since(0))
    finally:
        host.close()
        for s in factory.sessions:
            s.close()


def test_config_and_resume_carry_the_memory_view_without_an_id(running):
    view = running.app.config_view()["memory"]
    assert view == {"kind": "scripted", "name": memory.MEMORY_NAME,
                    "strategy": "scripted rules",
                    "namespace": memory.NAMESPACE_TEMPLATE, "region": None,
                    "reason": memory.SCRIPTED_NOTE}
    assert "scripted memory" in sim.banner(running)
    conv = opened(running)
    snap = conv.snapshot()["memory"]
    assert snap["kind"] == "scripted" and snap["source"] == "scripted, on this machine"
    assert "memory_calls" in running.app.config_view()["budget"]
