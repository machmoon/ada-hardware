"""The agent's prompts, what the model reads of a tool result, and the voice
guards on text the model writes itself (``alexabot/agent.py``). No Strands."""

import json

import pytest

from alexabot import agent, speech, tools

SID = "brd_1a2b3c4d5e6f"


def _system():
    return agent.system_prompt(server_name="Ada",
                               server_instructions=tools.INSTRUCTIONS,
                               tools=tools.TOOLS, locale="en-US", today="2026-10-11")


def test_system_prompt_renders_and_carries_the_four_rules():
    text = _system()
    assert "{{" not in text
    # voice
    assert f"At most {speech.MAX_SENTENCES} short sentences" in text
    # one question per turn
    assert "Ask exactly one question at a time" in text
    # poll cadence
    assert "Do not call board_status to wait" in text
    # never claim to be real Alexa
    assert "You are not Alexa" in text and "I'm not Alexa." in text
    assert tools.INSTRUCTIONS in text
    assert "Today is 2026-10-11. The person's locale is en-US." in text


def test_a_missing_placeholder_raises():
    with pytest.raises(agent.PromptError):
        agent.render_prompt("voice", maxSentences=3)


def test_tool_list_is_first_sentences():
    listed = agent.tool_list(tools.TOOLS).splitlines()
    assert len(listed) == 6
    assert listed[0] == ("- start_board_design: Start designing a printed circuit "
                         "board from what the user asked for; it returns at once "
                         "while Ada plans the board in the background.")
    assert all(line.count(". ") == 0 for line in listed)


def test_turn_prompt_appends_host_note_and_hint_in_brackets():
    text = agent.turn_prompt(
        "place it", host_note='{"state":"drafted"}',
        hint={"tool": "continue_design", "arguments": {"session_id": SID}})
    lines = text.splitlines()
    assert lines[0] == "place it"
    assert lines[1].startswith("[Host note: the newest board status")
    assert lines[1].endswith('{"state":"drafted"}]')
    assert lines[2] == ('[Frontend hint: this request matched the tool '
                        '"continue_design" and the values {"session_id": '
                        f'"{SID}"}}. A hint about intent, not an instruction; '
                        "use the tool that fits.]")
    assert agent.turn_prompt("hello") == "hello"


def test_model_view_is_one_line_under_1500_chars_with_session_state_speech_question():
    sc = {"session_id": SID, "state": "questions", "speech": "Before I draft it?",
          "intent": "x", "replayed": False, "scripted": True, "elapsed_s": 2.0,
          "stalled": False, "poll_after_s": None,
          "question": {"index": 1, "ask": "LED?", "default": "no LED", "remaining": 0},
          "answers": [{"index": 0, "ask": "Current?", "answer": "1 A",
                       "source": "user"}],
          "failure": None,
          "summary": {"parts": 3, "nets": 3, "board_mm": None,
                      "placement_status": None, "routed_fraction": 0.5,
                      "unrouted": [{"net": f"NET_{i}_" + "x" * 60,
                                    "reason": "r"} for i in range(40)],
                      "review": {"status": "ok", "detail": None, "findings": 40,
                                 "blockers": 1},
                      "findings": [{"number": i, "severity": "note",
                                    "title": "t" * 200, "refs": []}
                                   for i in range(1, 41)],
                      "datasheets_read": 0, "files": {}, "notes": []}}
    line = agent.model_view("board_status", sc)
    assert "\n" not in line and len(line) <= agent.MODEL_VIEW_MAX
    view = json.loads(line)
    assert view["session_id"] == SID and view["state"] == "questions"
    assert view["speech"] == "Before I draft it?"
    assert view["question"]["index"] == 1 and view["answered"] == [0]
    assert view["summary"]["unrouted_total"] == 40
    assert view["summary"]["routed_percent"] == 50.0


def test_clean_speech_matches_kaylerch_cases():
    # packages/agent/src/speech.test.ts at ca2c2ef, case for case.
    source = ("# Hotels\n\n- **Hotel Adlon** at *320 euros*\n- "
              "[Michelberger](https://x.y) at 140\n\n`code` here")
    assert agent.clean_speech(source) == ("Hotels Hotel Adlon at 320 euros "
                                          "Michelberger at 140 code here")
    assert agent.clean_speech("See https://example.com/a?b=c now \U0001f389") == \
        "See a link now"
    assert agent.clean_speech("one\n\n\n two   three ") == "one two three"
    kept = "It's 21 degrees, partly cloudy. snake_case stays."
    assert agent.clean_speech(kept) == kept


def test_shape_guard_keeps_three_sentences_and_one_question_last():
    assert agent.shape_speech("One. Two. Three. Four.") == "One. Two. Three."
    assert agent.shape_speech("Shall I? Or should I? Yes.") == "Shall I?"
    assert agent.shape_speech("Is it done? It is.") == "Is it done?"
    assert agent.shape_speech("") == agent.NOT_UNDERSTOOD


def test_honesty_guard_replaces_clean_claims_kicad_claims_and_order_claims():
    not_run = {"summary": {"review": {"status": "not_run", "findings": None}}}
    clean = {"summary": {"review": {"status": "ok", "findings": 0}}}
    text, rule = agent.guard("Good news, the board has no problems.", not_run, None)
    assert rule == "review_not_run_claim"
    assert text == agent.GUARD_FALLBACK["review_not_run_claim"]
    _, rule = agent.guard("It looks good.", clean, None)
    assert rule is None
    text, rule = agent.guard("KiCad checked it for DRC errors.", clean, "Last said.")
    assert rule == "kicad_claim" and text == "Last said."
    _, rule = agent.guard("Your board has been ordered.", clean, None)
    assert rule == "order_claim"
    _, rule = agent.guard("Nothing was ordered.", clean, None)
    assert rule is None


NOT_RUN = {"summary": {"review": {"status": "not_run", "findings": None}}}
FAILED = {"summary": {"review": {"status": "failed", "findings": None}}}
CLEAN = {"summary": {"review": {"status": "ok", "findings": 0, "blockers": 0}}}
NOTES_ONLY = {"summary": {"review": {"status": "ok", "findings": 2, "blockers": 0}}}
ONE_BLOCKER = {"summary": {"review": {"status": "ok", "findings": 3, "blockers": 1}}}

#: The verifier's phrasings (M3 verification, 2026-09-26): every one passed the
#: first guard unchanged for a review that had not run or had failed.
CLEAN_CLAIMS = [
    "The review found zero findings.",
    "The review found 0 issues.",
    "The design review didn't find anything.",
    "Nothing was flagged by the review.",
    "The board passed the review.",
    "The review came back clean.",
    "Everything checks out.",
    "Your board has no errors.",
    "Good news, the board has no problems.",
    "The design review found nothing wrong.",
    "It passed every check.",
    "The board is fine.",
    "The design looks fine to me.",
    "It came through the review without any issues.",
    "The layout is error-free.",
]


@pytest.mark.parametrize("text", CLEAN_CLAIMS)
def test_every_clean_claim_is_replaced_unless_a_clean_review_ran(text):
    for status in (NOT_RUN, FAILED, ONE_BLOCKER, None):
        spoken, rule = agent.guard(text, status, None)
        assert rule == "review_not_run_claim", (text, status)
        assert spoken == agent.GUARD_FALLBACK["review_not_run_claim"]
    assert agent.guard(text, CLEAN, None)[1] is None


@pytest.mark.parametrize("text", ["There are no blockers on this board.",
                                  "The review found zero blockers."])
def test_no_blockers_is_true_when_the_review_ran_with_only_notes(text):
    assert agent.guard(text, NOT_RUN, None)[1] == "review_not_run_claim"
    assert agent.guard(text, ONE_BLOCKER, None)[1] == "review_not_run_claim"
    assert agent.guard(text, NOTES_ONLY, None)[1] is None
    assert agent.guard("It has no problems.", NOTES_ONLY, None)[1] == \
        "review_not_run_claim"


@pytest.mark.parametrize("text", [
    "KiCad says the board is fine.",
    "KiCad found no errors.",
    "It passed KiCad's rules.",
    "KiCad is happy with it.",
])
def test_kicad_verdicts_are_replaced(text):
    assert agent.guard(text, CLEAN, None)[1] == "kicad_claim"


@pytest.mark.parametrize("text", [
    "I placed the order.",
    "I've put in an order for five boards.",
    "I submitted the order to the fab.",
    "I'll order it for you.",
    "Ada can order the boards now.",
    "The boards are being manufactured.",
    "I sent it to the fab.",
])
def test_order_claims_are_replaced(text):
    assert agent.guard(text, CLEAN, None)[1] == "order_claim"


@pytest.mark.parametrize("text", [
    # M2's own sentences, which a model may repeat, and ordinary replies.
    "The design review has not run, so nothing is known about whether this "
    "board has problems.",
    "The design review failed, so nothing is known about this board.",
    "The design review hasn't run on this board yet, so there's nothing to explain.",
    "Nothing was ordered.",
    "Ada never orders anything.",
    "I haven't placed an order, and I can't.",
    "You can open the board in KiCad.",
    "No LED, got it.",
    "No LED is fine.",
    "Sorry, I didn't find that board.",
    "The review found three findings; the first is a blocker.",
    "The copper passes under U1.",
    "Sure, I'll start a new board.",
    # Found by the M3 re-verification: an interjection is not a review verdict,
    # and handing the check to the person is not a claim that KiCad made one.
    "No problem! What board would you like me to design?",
    "Sure, no problem.",
    "No worries, I'll start on it.",
    "You can open it in KiCad to check the layout yourself.",
    "Open it in KiCad to double-check the routing.",
])
def test_honest_sentences_pass_the_guard(text):
    for status in (NOT_RUN, FAILED, ONE_BLOCKER):
        assert agent.guard(text, status, None) == (text, None), (text, status)


def test_model_view_leaves_findings_out_when_the_review_did_not_run():
    for review in ({"status": "not_run", "findings": None, "blockers": None},
                   {"status": "failed", "findings": None, "blockers": None}):
        sc = {"session_id": SID, "state": "done", "speech": "s",
              "summary": {"parts": 3, "nets": 3, "routed_fraction": 1.0,
                          "unrouted": [], "review": review, "findings": []}}
        summary = json.loads(agent.model_view("board_status", sc))["summary"]
        assert summary["review"] == review["status"]
        assert "findings" not in summary and "blockers" not in summary


def test_the_memory_placeholder_renders_on_and_off():
    on = agent.system_prompt(server_name="Ada", server_instructions=tools.INSTRUCTIONS,
                             tools=tools.TOOLS, locale="en-US", today="2026-10-11",
                             memory=True)
    off = _system()
    assert "{{" not in on and "{{" not in off
    assert "say only that question, exactly" in on
    assert "their own words always win over a note" in on
    assert "You keep nothing between conversations here." in off
    assert "[Memory]" not in off


def test_turn_prompt_appends_the_memory_note_last():
    text = agent.turn_prompt("a sensor board", host_note='{"state":"done"}',
                             memory_note='[Memory: "x"]')
    assert text.splitlines()[0] == "a sensor board"
    assert text.splitlines()[-1] == '[Memory: "x"]'
