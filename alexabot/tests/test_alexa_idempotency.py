"""Reviewer finding M1: the key is the account plus an agent-minted request_id.

Never the JSON-RPC id -- python-sdk numbers those per connection from zero
(``src/mcp/shared/jsonrpc_dispatcher.py`` at ``f1b6589``), so every session
sends 1, 2, 3 and a key built from them collides across people.
"""

import threading

import pytest

from alexabot import tools
from alexabot.runner import Runner
from alexabot.store import BoardStore
from alexabot.tests.fakes import FakeSteps, as_account, call, error, ok, wait_for

START = {"intent": "a 3.3V regulator board", "request_id": "5d2c-0001-aaaa"}


def test_retry_same_request_id_returns_same_session_and_starts_once(
    toolset, fake_steps
):
    first = ok(call(toolset, "start_board_design", START))
    again = ok(call(toolset, "start_board_design", START))
    assert again["session_id"] == first["session_id"]
    assert (first["replayed"], again["replayed"]) == (False, True)
    assert again["speech"].startswith("I already started that one.")
    wait_for(lambda: fake_steps.names())
    assert fake_steps.names().count("start_once") == 1


def test_same_request_id_other_account_is_a_different_session(toolset, fake_steps):
    with as_account("alice"):
        alice = ok(call(toolset, "start_board_design", START))
    with as_account("bob"):
        bob = ok(call(toolset, "start_board_design", START))
        assert error(call(toolset, "board_status",
                          {"session_id": alice["session_id"]})).startswith(
            "I can't find that board")
    assert alice["session_id"] != bob["session_id"] and not bob["replayed"]
    wait_for(lambda: len(fake_steps.calls) == 2)
    assert sorted(c["key"] for c in fake_steps.calls) == [
        "alexa:alice:5d2c-0001-aaaa", "alexa:bob:5d2c-0001-aaaa"]


def test_same_request_id_different_intent_is_refused(toolset):
    ok(call(toolset, "start_board_design", START))
    # Whitespace and case are not a different board...
    same = {**START, "intent": "  A 3.3V   REGULATOR board "}
    assert ok(call(toolset, "start_board_design", same))["replayed"] is True
    # ...a different request is.
    text = error(call(toolset, "start_board_design",
                      {**START, "intent": "a USB hub"}))
    assert "already used for a different board" in text
    assert "mint a new request_id" in text


def test_different_jsonrpc_ids_same_request_id_one_session(toolset):
    first = ok(call(toolset, "start_board_design", START, req_id=1))
    again = ok(call(toolset, "start_board_design", START, req_id="other-99"))
    assert first["session_id"] == again["session_id"]


def test_same_jsonrpc_id_two_request_ids_two_sessions():
    fake = FakeSteps()
    runner = Runner(BoardStore(":memory:"), lambda: object(), object(),
                    steps=fake, max_active=8).warm()
    toolset = tools.toolset(runner)
    with as_account("alice"):
        one = ok(call(toolset, "start_board_design", START, req_id=1))
    with as_account("bob"):
        two = ok(call(toolset, "start_board_design",
                      {**START, "request_id": "5d2c-0002-bbbb"}, req_id=1))
    assert one["session_id"] != two["session_id"] and not two["replayed"]
    assert runner.join(10)


def test_start_once_gets_the_namespaced_key(toolset, fake_steps):
    with as_account("acme:team-7"):
        ok(call(toolset, "start_board_design", START))
    wait_for(lambda: fake_steps.calls)
    record = fake_steps.calls[0]
    assert record["key"] == "alexa:acme:team-7:5d2c-0001-aaaa"
    assert record["payload"] == {"intent": START["intent"], "plan_first": True,
                                 "prefetch": False}


def test_retry_after_restart_returns_the_failed_row_not_a_new_run(tmp_path):
    path = tmp_path / "boards.sqlite3"
    first_fake = FakeSteps()
    first_fake.gates["start_once"] = gate = threading.Event()
    first = Runner(BoardStore(path), lambda: object(), object(),
                   steps=first_fake).warm()
    started = ok(call(tools.toolset(first), "start_board_design", START))
    # The process dies mid-plan; the next one sweeps at startup.
    store = BoardStore(path)
    assert store.fail_unfinished(10.0) == 1
    fake = FakeSteps()
    second = Runner(store, lambda: object(), object(), steps=fake).warm()
    retried = ok(call(tools.toolset(second), "start_board_design", START))
    assert retried["session_id"] == started["session_id"]
    assert retried["replayed"] is True and retried["state"] == "failed"
    assert retried["failure"]["stage"] == "restart"
    assert fake.calls == []
    gate.set()
    first.join(10)


@pytest.mark.parametrize(
    "request_id", ["short", "x" * 65, "has space 123", "semi;colon12", "ünïcode-123"]
)
def test_bad_request_id_charset_and_length_are_refused(toolset, request_id):
    result = call(toolset, "start_board_design", {**START, "request_id": request_id})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "request_id" in text


@pytest.mark.parametrize("intent", ["  ab  ", "x" * 501])
def test_bad_intent_length_is_refused(toolset, intent):
    assert "intent" in error(call(toolset, "start_board_design",
                                  {**START, "intent": intent}))


def test_replay_is_answered_even_at_capacity():
    fake = FakeSteps()
    fake.gates["start_once"] = gate = threading.Event()
    runner = Runner(BoardStore(":memory:"), lambda: object(), object(),
                    steps=fake, max_active=1).warm()
    toolset = tools.toolset(runner)
    first = ok(call(toolset, "start_board_design", START))
    with as_account("bob"):
        assert "capacity" in error(call(toolset, "start_board_design",
                                        {**START, "request_id": "5d2c-0009-cccc"}))
    again = ok(call(toolset, "start_board_design", START))
    assert again["session_id"] == first["session_id"] and again["replayed"]
    gate.set()
    assert runner.join(10)
