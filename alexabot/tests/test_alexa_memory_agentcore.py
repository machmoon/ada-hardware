"""The AgentCore Memory adapter against botocore's ``Stubber``, which checks
every request against the service model offline. No network: the socket guard
is on, and the clients are made with dummy keys and never send.

Skips without botocore (the ``alexa`` extra) through a ``skipif`` mark, not a
module-level ``importorskip``, so ``scripts/check_docs.py`` counts the same
tests on a CI runner without the extra (the ``test_alexa_sim_agent.py`` rule).
"""

import datetime
import importlib.util

import pytest

from alexabot import memory

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("botocore") is None,
    reason='botocore is not installed: pip install -e ".[alexa]"')


def Stubber(c):  # noqa: N802 -- botocore's class, imported when a test runs
    from botocore.stub import Stubber as real

    return real(c)


def EndpointConnectionError(**kwargs):  # noqa: N802
    from botocore.exceptions import EndpointConnectionError as real

    return real(**kwargs)


MEMORY_ID = "AdaDesignPreferences-a1B2c3D4e5"
ACTOR = memory.actor_id("local")
NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC)


def client(service):
    import botocore.session

    session = botocore.session.get_session()
    return session.create_client(service, region_name="us-east-1",
                                 aws_access_key_id="testing",
                                 aws_secret_access_key="testing")


@pytest.fixture
def data(loopback_only):
    c = client("bedrock-agentcore")
    with Stubber(c) as stub:
        yield c, stub
    assert loopback_only == []


def adapter(c, take=None):
    return memory.AgentCoreMemory(c, MEMORY_ID, region="us-east-1", take=take,
                                  now=lambda: NOW)


def test_create_event_sends_the_question_then_the_words_once_per_turn(data):
    c, stub = data
    rec = memory.turn_record("conv_0123456789abcdef", "t_2", "Up to one amp.",
                             source="voice", question="How much current?")
    stub.add_response("create_event", {"event": {
        "memoryId": MEMORY_ID, "actorId": ACTOR, "sessionId": "conv_0123456789abcdef",
        "eventId": "0000001758888000000#abc", "eventTimestamp": NOW, "payload": []}},
        {"memoryId": MEMORY_ID, "actorId": ACTOR, "sessionId": "conv_0123456789abcdef",
         "eventTimestamp": NOW, "clientToken": "conv_0123456789abcdef-t_2",
         "payload": [
             {"conversational": {"role": "ASSISTANT",
                                 "content": {"text": "How much current?"}}},
             {"conversational": {"role": "USER",
                                 "content": {"text": "Up to one amp."}}},
         ]})
    assert adapter(c).record(ACTOR, "conv_0123456789abcdef", rec).state == "written"
    stub.assert_no_pending_responses()


def test_the_default_timestamp_is_utc_aware():
    stamp = memory.AgentCoreMemory(object(), MEMORY_ID, region="us-east-1")._now()
    assert stamp.tzinfo is not None and stamp.utcoffset() == datetime.timedelta(0)


def test_retrieve_reads_our_namespace_once_and_parses(data):
    c, stub = data
    space = memory.namespace_for(ACTOR)
    stub.add_response("retrieve_memory_records", {"memoryRecordSummaries": [
        {"memoryRecordId": "mem-" + "a1" * 20,
         "memoryStrategyId": "DesignPreferences-x",
         "content": {"text": '{"context":"c","preference":"USB-C power input",'
                             '"categories":["power"]}'},
         "namespaces": [space], "createdAt": NOW, "score": 0.61},
    ]}, {"memoryId": MEMORY_ID, "namespace": space, "maxResults": memory.TOP_K,
         "searchCriteria": {"searchQuery": memory.PREFERENCE_QUERY,
                            "topK": memory.TOP_K}})
    got = adapter(c).recall(ACTOR)
    assert got.state == "ok"
    assert [(p.text, p.created_at) for p in got.preferences] == [
        ("USB-C power input", "2026-09-26")]


@pytest.mark.parametrize("code,reason", [
    ("AccessDeniedException", "access_denied"),
    ("ResourceNotFoundException", "not_found"),
    ("ThrottledException", "throttled"),
    ("ValidationException", "invalid"),
    ("ServiceException", "failed"),
])
def test_a_refusal_is_unavailable_in_words_never_the_message(data, code, reason):
    c, stub = data
    arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/" + MEMORY_ID
    stub.add_client_error("retrieve_memory_records", code, f"denied on {arn}")
    got = adapter(c).recall(ACTOR)
    assert (got.state, got.detail) == ("unavailable", memory.REASONS[reason])
    stub.add_client_error("create_event", code, f"denied on {arn}")
    wrote = adapter(c).record(ACTOR, "conv_1", memory.turn_record(
        "conv_1", "t_1", "USB-C please", source="voice", question=None))
    assert (wrote.state, wrote.detail) == ("failed", memory.REASONS[reason])
    assert "123456789012" not in f"{got}{wrote}"


class Unreachable:
    def retrieve_memory_records(self, **_):
        raise EndpointConnectionError(endpoint_url="https://bedrock-agentcore.example")

    create_event = retrieve_memory_records


def test_a_connect_error_is_unreachable():
    got = adapter(Unreachable()).recall(ACTOR)
    assert (got.state, got.detail) == ("unavailable", memory.REASONS["unreachable"])


def test_a_spent_budget_makes_no_call(data):
    c, stub = data
    mem = adapter(c, take=lambda: False)
    assert mem.recall(ACTOR).detail == memory.REASONS["budget"]
    assert mem.record(ACTOR, "conv_1", memory.turn_record(
        "conv_1", "t_1", "USB-C", source="voice", question=None)).state == "failed"
    stub.assert_no_pending_responses()


def _memory(status="ACTIVE", spaces=(memory.NAMESPACE_TEMPLATE,),
            kind="USER_PREFERENCE"):
    return {"memory": {
        "arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:memory/" + MEMORY_ID,
        "id": MEMORY_ID, "name": memory.MEMORY_NAME, "eventExpiryDuration": 7,
        "status": status, "createdAt": NOW, "updatedAt": NOW,
        "strategies": [{"strategyId": "DesignPreferences-x",
                        "name": "DesignPreferences",
                        "type": kind, "namespaces": list(spaces),
                        "namespaceTemplates": list(spaces)}]}}


@pytest.mark.parametrize("answer,words", [
    (_memory(), None),
    (_memory(status="CREATING"), "not ACTIVE"),
    (_memory(kind="SEMANTIC"), "no user-preference strategy"),
    (_memory(spaces=("/strategies/{memoryStrategyId}/actors/{actorId}/",)),
     "another namespace"),
])
def test_probe(loopback_only, answer, words):
    control = client("bedrock-agentcore-control")
    with Stubber(control) as stub:
        stub.add_response("get_memory", answer, {"memoryId": MEMORY_ID})
        problems = memory.probe(control, MEMORY_ID)
    assert (problems == []) if words is None else any(words in p for p in problems)
    assert all("123456789012" not in p for p in problems)
