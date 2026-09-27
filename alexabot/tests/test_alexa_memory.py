"""Memory's pure parts (``alexabot/memory.py``): who, what a turn writes, what
comes back, what Ada asks, and the guard on model text that claims a memory.
No AWS, no Strands."""

import datetime
import json
import re

import pytest

from alexabot import agent, memory, speech
from alexabot.scripted_agent import decide

ACTOR_ID = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*"
                      r"[a-zA-Z0-9-_/]*\Z")  # botocore's ActorId pattern
USB = memory.preference("USB-C power input")
V33 = memory.preference("3.3 V logic")
NS = memory.namespace_for("acct-1")


def test_actor_id_is_stable_hashed_and_fits_botocore():
    one = memory.actor_id("a.b")
    assert one == memory.actor_id("a.b") != memory.actor_id("a_b")
    assert ACTOR_ID.match(one) and "/" not in one and "." not in one
    assert "a.b" not in one and len(one) == len("acct-") + 32
    assert memory.namespace_for(one).endswith("/preferences/")
    assert memory.NAMESPACE_TEMPLATE == "/users/{actorId}/preferences/"


def test_turn_record_keeps_a_question_only_and_skips_what_is_not_the_person():
    rec = memory.turn_record("conv_1", "t_2", "  Up to  one amp. ", source="typed",
                             question="How much current should the rail supply?")
    assert (rec.words, rec.question, rec.skip) == (
        "Up to one amp.", "How much current should the rail supply?", None)
    assert rec.client_token == "conv_1-t_2"
    said = memory.turn_record("conv_1", "t_3", "hi", source="voice",
                              question="Your board is done.")
    assert said.question is None
    assert memory.turn_record("c", "t", "Place and route", source="chip",
                              question=None).skip == "chip"
    assert memory.turn_record("c", "t", "   ", source="typed", question=None).skip \
        == "empty"
    chose = [{"name": "answer_design_questions",
              "input": {"session_id": "brd_1", "you_choose": True}}]
    assert memory.turn_record("c", "t", "you choose", source="voice", question="LED?",
                              tool_calls=chose).skip == "you_choose"


def _summary(text, *, space=NS, score=0.9, rid="r1"):
    return {"memoryRecordId": rid, "content": {"text": text}, "memoryStrategyId": "s",
            "namespaces": [space], "createdAt": datetime.datetime(2026, 9, 25, 3, 0),
            "score": score}


def test_parse_records_reads_the_documented_json_and_plain_text():
    doc = json.dumps({"context": "c", "preference": "Prefers USB-C power input",
                      "categories": ["power"]})
    got = memory.parse_records([
        _summary(doc), _summary("The user prefers 3.3 V logic.", rid="r2"),
        _summary("someone else's", space=memory.namespace_for("acct-12")),
        _summary("too weak", score=0.19), _summary("prefers usb-c power input",
                                                   rid="dup"),
        _summary("no score at all", score=None, rid="r3"),
    ], NS)
    assert [p.text for p in got] == ["Prefers USB-C power input",
                                     "The user prefers 3.3 V logic.", "no score at all"]
    assert got[1].short == "3.3 V logic" and got[1].category == "logic voltage"
    assert got[0].created_at == "2026-09-25" and got[0].record_id == "r1"
    assert memory.parse_records(None, NS) == ()


def test_short_text_and_the_ask_are_spoken_units_and_name_at_most_two():
    assert memory.short_text("The user prefers USB-C as the power input.") == \
        "USB-C as the power input"
    assert len(memory.short_text("x " * 80)) <= memory.SHORT_MAX
    ask = memory.ask_sentence([USB, V33, memory.preference("no indicator LED")])
    assert ask == ("Last time you chose: USB-C power input and 3.3 volt logic. "
                   "Same again?")
    assert speech.speech_problems(ask) == []


@pytest.mark.parametrize("state,prefs,kind,label", [
    ("off", (), "off", "Memory off"),
    ("checking", (), "agentcore", "Memory: checking"),
    ("ok", (USB, V33), "agentcore", "Remembering: USB-C power input, 3.3 V logic"),
    ("ok", (), "agentcore", "Memory on: nothing saved yet"),
    ("unavailable", (), "agentcore", "Memory unavailable"),
    ("ok", (USB,), "scripted", "Remembering (scripted): USB-C power input"),
    ("ok", (), "scripted", "Scripted memory: nothing saved yet"),
])
def test_chip_label_every_row(state, prefs, kind, label):
    assert memory.chip_label(state, prefs, kind) == label


def test_the_ask_reads_as_english_with_agentcores_own_phrasing():
    # The two records AgentCore's user-preference strategy extracted in the
    # live check on 2026-09-26, verbatim: a participle, and a subject-less
    # "Always prefers". "you mentioned powered from USB-C" is what shipped.
    live = [memory.preference("Powered from USB-C"),
            memory.preference("Always prefers 3.3 V logic")]
    assert [p.short for p in live] == ["Powered from USB-C", "3.3 V logic"]
    ask = memory.ask_sentence(live)
    assert ask == ("Last time you chose: powered from USB-C and 3.3 volt logic. "
                   "Same again?")
    assert speech.speech_problems(ask) == []


def test_chip_label_names_three_then_counts():
    many = [memory.preference(t) for t in ("USB-C power input", "3.3 V logic",
                                           "no indicator LED", "JST connectors",
                                           "a small board")]
    assert memory.chip_label("ok", many, "agentcore").endswith(
        "no indicator LED and 2 more")


def _user(text, note=None):
    return {"role": "user", "content": [{"text": agent.turn_prompt(
        text, memory_note=note)}]}


def _asked(request, reply, prefs=(USB, V33)):
    ask = memory.ask_sentence(prefs)
    return [_user(request, memory.memory_note(prefs, ask=ask)),
            {"role": "assistant", "content": [{"text": ask}]},
            _user(reply, memory.memory_note(
                memory.unstated(prefs, f"{request} {reply}"), asked=True))]


def test_an_explicit_request_wins_over_a_remembered_preference():
    request = "Ask Ada for a 5 V board with a barrel jack"
    assert memory.unstated((USB, V33), request) == ()
    # No note is built, so the scripted agent starts with the request alone.
    step = decide([_user(request)])
    assert step["tool"] == "start_board_design"
    assert "USB" not in step["input"]["intent"] and "3.3" not in step["input"]["intent"]
    # Even if a note arrived, the agent recomputes and does not ask.
    stale = memory.memory_note((USB, V33))
    assert decide([_user(request, stale)])["tool"] == "start_board_design"


def test_a_request_that_settles_one_preference_is_asked_about_the_other():
    request = "Ask Ada for a sensor board with a USB-C port"
    left = memory.unstated((USB, V33), request)
    assert left == (V33,)
    step = decide([_user(request, memory.memory_note(left))])
    assert step == {"text": "Last time you chose: 3.3 volt logic. Same again?"}


def test_yes_to_the_ask_adds_the_preferences_and_no_leaves_them_out():
    step = decide(_asked("Ask Ada for a sensor board", "Yes"))
    assert step["input"]["intent"] == ("a sensor board, with USB-C power input and "
                                       "3.3 V logic")
    step = decide(_asked("Ask Ada for a sensor board", "No thanks"))
    assert step["input"]["intent"] == "a sensor board"
    step = decide(_asked("Ask Ada for a sensor board", "yes, but a barrel jack"))
    assert "USB" not in step["input"]["intent"]
    assert step["input"]["intent"].endswith("with 3.3 V logic")


def test_an_uncategorised_record_is_shown_never_offered():
    odd = memory.preference("likes blue solder mask")
    assert odd.category is None
    assert memory.unstated((odd, USB), "a sensor board") == (USB,)
    assert "blue" not in memory.memory_note(memory.unstated((odd,), "x") or (USB,))


def test_memory_note_forms():
    note = memory.memory_note((USB, V33))
    assert note.startswith('[Memory: from earlier conversations with this person: '
                           '"USB-C power input"; "3.3 V logic". Ask first, exactly: ')
    assert note.endswith('Same again?"]')
    assert "You already asked" in memory.memory_note((USB,), asked=True)
    assert "use their words alone" in memory.memory_note((), asked=True)
    text = agent.turn_prompt("hi", memory_note=note)
    assert text.splitlines() == ["hi", note]


@pytest.mark.parametrize("text,view,expected", [
    # No note this turn: every claimed memory is replaced by what is true.
    ("Last time you wanted USB-C.", memory.GuardView("off"), memory.OFF_SPEECH),
    ("I remember you like 3.3 V.", memory.GuardView("ok"), memory.NOTHING_SAVED),
    ("As before, USB-C then.", memory.GuardView("unavailable"),
     memory.UNAVAILABLE_SPEECH),
    # The request settled every preference (no note): go by the request.
    ("Last time you wanted USB-C. Shall I?", memory.GuardView("ok", (USB, V33)),
     memory.MEMORY_CLAIM_FALLBACK),
    # A note with something not in it: the ask instead.
    ("You usually want an LED.", memory.GuardView("ok", (USB,), note=(USB,)),
     memory.ask_sentence((USB,))),
])
def test_memory_claim_is_replaced(text, view, expected):
    said, rule = agent.guard(text, None, None, view)
    assert (said, rule) == (expected, "memory_claim")


def test_memory_claim_keeps_what_the_note_backs_and_tool_speech_is_never_guarded():
    view = memory.GuardView("ok", (USB, V33), note=(USB, V33))
    ask = memory.ask_sentence((USB, V33))
    assert agent.guard(ask, None, None, view) == (ask, None)
    assert agent.guard("Sure, what should it do?", None, None,
                       memory.GuardView("off")) == ("Sure, what should it do?", None)
    assert agent.GUARD_FALLBACK["memory_claim"] == "I'll go by what you just asked for."


def test_scripted_memory_reads_the_persons_words_never_the_question():
    fake = memory.ScriptedMemory()
    rec = memory.turn_record("conv_1", "t_1", "you choose", source="voice",
                             question="I'll assume no LED. Do you want an LED?")
    assert fake.record("acct-1", "conv_1", rec).state == "written"
    assert fake.recall("acct-1").preferences == ()
    said = memory.turn_record("conv_1", "t_2", "a three point three volt board from "
                              "USB-C, no LED", source="typed", question=None)
    fake.record("acct-1", "conv_1", said)
    assert [p.text for p in fake.recall("acct-1").preferences] == [
        "USB-C power input", "3.3 V logic", "no indicator LED"]
    fake.record("acct-1", "conv_1", memory.turn_record(
        "conv_1", "t_3", "with a green LED", source="typed", question=None))
    assert [p.text for p in fake.recall("acct-1").preferences][-1] == \
        "a power indicator LED"
    assert fake.recall("acct-2").preferences == ()
