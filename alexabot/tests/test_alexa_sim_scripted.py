"""The scripted voice agent's rules (``scripted_agent.decide``): pure, over
Strands-shaped message dicts. No Strands needed."""

import json

import pytest

from alexabot import agent
from alexabot.scripted_agent import HELP_TEXT, IDENTITY_TEXT, decide

SID = "brd_1a2b3c4d5e6f"


def ctx(state, question=None):
    return {"session_id": SID, "state": state, "speech": "s", "stalled": False,
            "scripted": True, "question": question, "answered": [], "summary": None}


def user(text, context=None, hint=None):
    note = json.dumps(context) if context else None
    return {"role": "user",
            "content": [{"text": agent.turn_prompt(text, host_note=note, hint=hint)}]}


Q0 = {"index": 0, "ask": "How much current?", "default": "500 mA", "remaining": 1}


@pytest.mark.parametrize("utterance,context,expected", [
    ("Ask Ada for a 3.3 volt regulator board powered from USB-C.", None,
     ("start_board_design",
      {"intent": "a 3.3 volt regulator board powered from USB-C.",
       "request_id": "host-assigned"})),
    ("Build me a regulator board", None,
     ("start_board_design", {"intent": "Build me a regulator board",
                             "request_id": "host-assigned"})),
    ("Ada, design a blinking LED board", None,
     ("start_board_design", {"intent": "a blinking LED board",
                             "request_id": "host-assigned"})),
    ("an adapter board for USB", None,
     ("start_board_design", {"intent": "an adapter board for USB",
                             "request_id": "host-assigned"})),
    ("How's it going?", ctx("routing"), ("board_status", {"session_id": SID})),
    ("status please", None, ("board_status", {})),
    ("Explain the second finding", ctx("done"),
     ("explain_finding", {"session_id": SID, "which": 2})),
    ("why is that a blocker", ctx("done"),
     ("explain_finding", {"session_id": SID, "which": 1})),
    ("Yes, place and route it.", ctx("drafted"),
     ("continue_design", {"session_id": SID})),
    ("How did my USB-C board go?", None, ("recall_my_boards", {"query": "USB-C"})),
    ("what boards have I made", None, ("recall_my_boards", {})),
])
def test_rule_table(utterance, context, expected):
    step = decide([user(utterance, context)])
    assert (step.get("tool"), step.get("input")) == expected


def test_a_hint_wins_over_keywords():
    hint = {"tool": "explain_finding", "arguments": {"session_id": SID, "which": 3}}
    step = decide([user("How's it going?", ctx("done"), hint)])
    assert step == {"tool": "explain_finding", "input": {"session_id": SID,
                                                         "which": 3}}


def test_free_text_answers_the_pending_question_index():
    q1 = {"index": 1, "ask": "LED?", "default": "no LED", "remaining": 0}
    step = decide([user("Yes, a green one", ctx("questions", q1))])
    assert step == {"tool": "answer_design_questions",
                    "input": {"session_id": SID,
                              "answers": [{"index": 1,
                                           "answer": "Yes, a green one"}]}}


def test_context_comes_from_the_newest_tool_result_when_there_is_no_note():
    result_text = agent.model_view("answer_design_questions", {
        "session_id": SID, "state": "questions", "speech": "Next?",
        "question": {"index": 1, "ask": "LED?", "default": "no LED",
                     "remaining": 0}, "answers": [], "summary": None})
    history = [
        user("Up to one amp", ctx("questions", Q0)),
        {"role": "assistant", "content": [{"toolUse": {
            "toolUseId": "scripted-1", "name": "answer_design_questions",
            "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {
            "toolUseId": "scripted-1", "status": "success",
            "content": [{"text": result_text}]}}]},
        {"role": "assistant", "content": [{"text": "Next?"}]},
        user("no LED please"),
    ]
    step = decide(history)
    assert step["input"]["answers"] == [{"index": 1, "answer": "no LED please"}]


def test_you_choose_only_while_questions_are_open():
    step = decide([user("you choose", ctx("questions", Q0))])
    assert step == {"tool": "answer_design_questions",
                    "input": {"session_id": SID, "you_choose": True}}
    step = decide([user("you choose", ctx("drafted"))])
    assert step == {"text": HELP_TEXT}


def test_identity_question_answers_not_alexa():
    assert decide([user("Are you Alexa?")]) == {"text": IDENTITY_TEXT}
    assert "I'm not Alexa." in IDENTITY_TEXT


def test_unknown_text_gets_the_help_sentence():
    assert decide([user("tell me a joke")]) == {"text": HELP_TEXT}


def test_a_tool_error_is_spoken_in_words_never_raw():
    def failed(text):
        return [{"role": "user", "content": [{"toolResult": {
            "toolUseId": "x", "status": "error", "content": [{"text": text}]}}]}]

    assert decide(failed("I can't find that board.")) == \
        {"text": "I can't find that board."}
    assert "request_id" not in decide(failed("request_id must be 8 to 64 {x}"))["text"]


def _tool_turn(n, tool, view_text, reply):
    return [
        {"role": "assistant", "content": [{"toolUse": {
            "toolUseId": f"scripted-{n}", "name": tool, "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {
            "toolUseId": f"scripted-{n}", "status": "success",
            "content": [{"text": view_text}]}}]},
        {"role": "assistant", "content": [{"text": reply}]},
    ]


def test_an_explain_result_does_not_hide_the_pending_question():
    # Verifier, M3: "Why?" at the questions stage called explain_finding, and
    # the next free-text answer got the help sentence, because the explain
    # view (no state, no question) had become the newest context.
    status = agent.model_view("start_board_design", {
        "session_id": SID, "state": "questions", "speech": "How much current?",
        "question": Q0, "answers": [], "summary": None})
    explain = agent.model_view("explain_finding", {
        "session_id": SID, "which": 1, "count": None, "review_status": "not_run",
        "speech": "The design review hasn't run on this board yet."})
    recall = agent.model_view("recall_my_boards", {
        "total": 1, "boards": [{"session_id": SID, "intent": "x",
                                "state": "questions", "headline": "h"}],
        "speech": "One board."})
    history = [user("design a sensor board")]
    history += _tool_turn(1, "start_board_design", status, "How much current?")
    history += [user("Why?")]
    history += _tool_turn(2, "explain_finding", explain, "Not run yet.")
    history += [user("What boards have I made?")]
    history += _tool_turn(3, "recall_my_boards", recall, "One board.")
    history += [user("Up to one amp.")]
    step = decide(history)
    assert step == {"tool": "answer_design_questions",
                    "input": {"session_id": SID,
                              "answers": [{"index": 0, "answer": "Up to one amp."}]}}


@pytest.mark.parametrize("context", [ctx("done"), ctx("questions", Q0)])
def test_a_question_about_the_review_asks_for_status_not_a_new_board(context):
    step = decide([user("Did the design review find any problems?", context)])
    assert step == {"tool": "board_status", "input": {"session_id": SID}}
    step = decide([user("Were there any issues?", context)])
    assert step == {"tool": "board_status", "input": {"session_id": SID}}


def test_yes_no_problem_still_answers_the_open_question():
    q1 = {"index": 1, "ask": "LED?", "default": "no LED", "remaining": 0}
    step = decide([user("Sure, no problem", ctx("questions", q1))])
    assert step["tool"] == "answer_design_questions"


def test_with_a_board_in_hand_a_new_board_needs_a_start_verb():
    step = decide([user("That board looks tiny", ctx("done"))])
    assert step == {"text": HELP_TEXT}
    step = decide([user("Now design a blinking LED board", ctx("done"))])
    assert step["tool"] == "start_board_design"
    assert step["input"]["intent"] == "Now design a blinking LED board"
    step = decide([user("I want another board with a USB-C port", ctx("done"))])
    assert step["tool"] == "start_board_design"
    # With no board yet, the noun alone is enough (the first request).
    assert decide([user("a blinking LED board")])["tool"] == "start_board_design"


@pytest.mark.parametrize("text", [
    "Is the design finished?",
    "Tell me about the design.",
    "What does the board look like?",
    "Does the board need a heatsink?",
    "I like this design.",
])
def test_with_a_board_in_hand_mentioning_the_design_is_not_a_new_board(text):
    assert decide([user(text, ctx("done"))]).get("tool") != "start_board_design"


@pytest.mark.parametrize("text", [
    "Design me a 5 volt boost board",
    "Ada, make a USB hub board",
    "Can you build a new sensor board?",
    "I need a board for a thermistor",
    "Let's do a different board with an ESP32",
])
def test_with_a_board_in_hand_asking_for_one_starts_it(text):
    assert decide([user(text, ctx("done"))])["tool"] == "start_board_design"


def test_how_did_that_board_go_is_status_with_a_board_and_recall_without():
    step = decide([user("How did that board go?", ctx("done"))])
    assert step == {"tool": "board_status", "input": {"session_id": SID}}
    assert decide([user("How did that board go?")])["tool"] == "recall_my_boards"
    step = decide([user("How did my USB-C board go?", ctx("done"))])
    assert step == {"tool": "recall_my_boards", "input": {"query": "USB-C"}}
