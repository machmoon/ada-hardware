"""One contract for every ``Memory``: the scripted stand-in, the AgentCore
adapter behind botocore's ``Stubber``, and ``MemoryOff`` are held to the same
assertions, so the fake cannot drift into promises the adapter does not keep.

The AgentCore cases skip without botocore (a ``skipif`` mark per case, so the
test count is the same everywhere; the ``check_docs.py`` rule)."""

import contextlib
import importlib.util
import json
import re

import pytest

from alexabot import memory

HAS_BOTO = importlib.util.find_spec("botocore") is not None
needs_botocore = pytest.mark.skipif(not HAS_BOTO, reason="botocore is not installed")
MEMORY_ID = "AdaDesignPreferences-a1B2c3D4e5"
ACTOR = memory.actor_id("local")
SECRETS = re.compile(r"\b\d{12}\b|AKIA[0-9A-Z]{16}|aws_secret|arn:aws|"
                     + re.escape(MEMORY_ID) + "|" + re.escape(ACTOR))
SAID = memory.turn_record("conv_1", "t_1", "a USB-C board at 3.3 V", source="voice",
                          question=None)
CHIP = memory.turn_record("conv_1", "t_2", "Place and route", source="chip",
                          question=None)


@contextlib.contextmanager
def stubbed(*, fail_code=None):
    """An AgentCoreMemory whose client answers from a Stubber, and the stub."""
    import botocore.session
    from botocore.stub import Stubber

    c = botocore.session.get_session().create_client(
        "bedrock-agentcore", region_name="us-east-1", aws_access_key_id="testing",
        aws_secret_access_key="testing")
    with Stubber(c) as stub:
        if fail_code:
            for op in ("retrieve_memory_records", "create_event"):
                stub.add_client_error(op, fail_code,
                                      "on arn:aws:bedrock-agentcore:us-east-1:"
                                      "123456789012:memory/" + MEMORY_ID)
        else:
            stub.add_response("retrieve_memory_records", {"memoryRecordSummaries": []})
            stub.add_response("create_event", {"event": {
                "memoryId": MEMORY_ID, "actorId": ACTOR, "sessionId": "conv_1",
                "eventId": "e-1", "eventTimestamp": 0, "payload": []}})
        yield memory.AgentCoreMemory(c, MEMORY_ID, region="us-east-1"), stub


def _ok(kind):
    if kind == "scripted":
        return contextlib.nullcontext((memory.ScriptedMemory(), None))
    if kind == "off":
        return contextlib.nullcontext((memory.MemoryOff(), None))
    return stubbed()


def _failing(kind):
    if kind == "scripted":
        return contextlib.nullcontext((memory.ScriptedMemory(fail="throttled"), None))
    return stubbed(fail_code="ThrottledException")


KINDS = [pytest.param("scripted"), pytest.param("off"),
         pytest.param("agentcore", marks=needs_botocore)]


@pytest.mark.parametrize("kind", KINDS)
def test_states_come_from_the_fixed_sets_and_describe_holds_no_secret(kind,
                                                                      loopback_only):
    with _ok(kind) as (mem, _stub):
        got = mem.recall(ACTOR)
        wrote = mem.record(ACTOR, "conv_1", SAID)
        assert got.state in memory.RECALL_STATES and wrote.state in memory.RECORD_STATES
        assert (got.state, wrote.state) == (
            ("off", "off") if kind == "off" else ("ok", "written"))
        assert mem.kind == kind
        assert not SECRETS.search(json.dumps(mem.describe()))


@pytest.mark.parametrize("kind", KINDS)
def test_a_skipped_turn_makes_no_call_anywhere(kind, loopback_only):
    with _ok(kind) as (mem, stub):
        assert mem.record(ACTOR, "conv_1", CHIP) == memory.Recorded("skipped", "chip")
        if stub is not None:
            # The create_event response is still queued: nothing was sent.
            with pytest.raises(AssertionError):
                stub.assert_no_pending_responses()


@pytest.mark.parametrize("kind", [pytest.param("scripted"),
                                  pytest.param("agentcore", marks=needs_botocore)])
def test_a_failure_is_never_an_empty_ok_and_says_why_in_words(kind, loopback_only):
    with _failing(kind) as (mem, _stub):
        got = mem.recall(ACTOR)
        wrote = mem.record(ACTOR, "conv_1", SAID)
    assert got.state == "unavailable" and got.preferences == ()
    assert wrote.state == "failed"
    assert got.detail in memory.REASONS.values()
    assert wrote.detail in memory.REASONS.values()
    assert not SECRETS.search(f"{got} {wrote}")


class Exploding:
    """A client whose every call raises something botocore never would."""

    def retrieve_memory_records(self, **_):
        raise RuntimeError("secret arn:aws:... 123456789012")

    create_event = retrieve_memory_records


def test_the_adapter_never_raises_even_on_an_unexpected_exception():
    mem = memory.AgentCoreMemory(Exploding(), MEMORY_ID, region="us-east-1")
    got, wrote = mem.recall(ACTOR), mem.record(ACTOR, "conv_1", SAID)
    assert (got.detail, wrote.detail) == (memory.REASONS["failed"],) * 2
    assert not SECRETS.search(f"{got} {wrote}")


def test_memory_off_says_how_to_turn_it_on():
    off = memory.MemoryOff()
    assert off.recall(ACTOR).detail == memory.OFF_FIX
    assert "ADA_AGENTCORE_MEMORY_ID" in memory.OFF_FIX
