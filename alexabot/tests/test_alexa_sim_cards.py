"""The simulated Alexa+ screen's cards, from the exact structuredContent the
tools return (``cards.from_tool``). Pure; no Strands, no network."""

import json

import pytest

from alexabot import cards, speech
from alexabot.store import STATES

SID = "brd_1a2b3c4d5e6f"
FINDINGS = [
    {"number": 1, "severity": "blocker", "title": "VOUT has no bulk capacitor",
     "refs": ["U1", "C2"]},
    {"number": 2, "severity": "marginal", "title": "Input capacitor is small",
     "refs": ["C1"]},
]


def _summary(**over):
    base = {"parts": None, "nets": None, "board_mm": None, "placement_status": None,
            "routed_fraction": None, "unrouted": None,
            "review": {"status": "not_run", "detail": None, "findings": None,
                       "blockers": None},
            "findings": [], "datasheets_read": 0,
            "files": {"schematic": None, "board": None, "project": None},
            "notes": []}
    base.update(over)
    return base


def status(state="reading", **over):
    sc = {"session_id": SID, "state": state, "speech": "Something to say.",
          "intent": "a 3.3 volt regulator powered from USB-C", "replayed": False,
          "scripted": False, "elapsed_s": 4.0, "stalled": False,
          "poll_after_s": 5 if state in ("reading", "proposing", "placing",
                                          "routing", "reviewing") else None,
          "question": None, "answers": [], "failure": None, "summary": None}
    sc.update(over)
    return sc


def done(**summary_over):
    summary = _summary(parts=3, nets=3, board_mm=[16.4, 8.8], routed_fraction=1.0,
                       unrouted=[],
                       review={"status": "ok", "detail": None, "findings": 2,
                               "blockers": 1},
                       findings=FINDINGS,
                       files={"schematic": None, "board": "/x/b.kicad_pcb",
                              "project": None})
    summary.update(summary_over)
    return status("done", summary=summary,
                  answers=[{"index": 0, "ask": "How much current?",
                            "answer": "one amp", "source": "user"}])


def always(_path):
    return True


def _all_text(card):
    return json.dumps(card)


@pytest.mark.parametrize("state", [s for s in STATES])
def test_board_card_for_every_state_has_a_label_rail_and_chips(state):
    extra = {}
    if state == "questions":
        extra["question"] = {"index": 0, "ask": "How much current?",
                             "default": "up to 500 milliamps", "remaining": 1}
    if state == "failed":
        extra["failure"] = {"stage": "proposing",
                            "reason": "ModelError: the AI model didn't answer"}
    if state == "done":
        card = cards.from_tool("board_status", done(), exists=always)
    else:
        card = cards.from_tool("board_status", status(state, **extra), exists=always)
    assert card["type"] == "board" and card["id"] == f"board:{SID}"
    assert card["stateLabel"] == cards.STATE_LABELS[state]
    assert [s["key"] for s in card["steps"]] == ["plan", "questions", "schematic",
                                                 "place", "route", "review"]
    assert card["buttons"], f"{state} has no chip"
    assert card["hintText"].startswith("Try “")
    assert card["headerAttributionText"] == "Simulated Alexa+ experience"


def test_the_question_default_is_always_visible_and_is_a_chip():
    q = {"index": 1, "ask": "Do you want a power indicator LED?", "default": "no LED",
         "remaining": 0}
    card = cards.from_tool("board_status", status("questions", question=q))
    assert card["question"] == q
    first = card["buttons"][0]
    assert first["text"] == "Use the default: no LED"
    assert first["hint"] == {"tool": "answer_design_questions",
                             "arguments": {"session_id": SID,
                                           "answers": [{"index": 1,
                                                        "answer": "no LED"}]}}
    assert card["buttons"][1]["hint"]["arguments"]["you_choose"] is True
    assert card["hintText"] == "Try “no LED”"


def test_unknown_values_are_omitted_never_zero():
    card = cards.from_tool("board_status", status("proposing", summary=_summary()))
    assert card["stats"] == []
    assert "primaryText" not in card
    assert card["unrouted"] is None and card["review"] is None
    drafted = cards.from_tool("board_status",
                              status("drafted", summary=_summary(parts=3, nets=3)))
    assert drafted["stats"] == [{"label": "Parts", "value": "3"},
                                {"label": "Nets", "value": "3"}]
    assert '"0"' not in _all_text(drafted)


def test_every_unrouted_net_is_named():
    nets = [{"net": f"N{i}", "reason": "blocked by U1"} for i in range(8)]
    sc = done(unrouted=nets, routed_fraction=0.875)
    card = cards.from_tool("board_status", sc, exists=always)
    assert [u["net"] for u in card["unrouted"]] == [f"N{i}" for i in range(8)]
    assert card["unroutedShown"] == 5
    assert "87% routed, 8 nets left" in card["primaryText"]
    stats = {s["label"]: s["value"] for s in card["stats"]}
    assert stats["Routed"] == "87%"


@pytest.mark.parametrize("review", [
    {"status": "not_run", "detail": None, "findings": None, "blockers": None},
    {"status": "failed", "detail": "ModelError: the AI model didn't answer",
     "findings": None, "blockers": None},
])
def test_review_not_run_and_failed_never_read_as_zero_or_no_problems(review):
    card = cards.from_tool("board_status", done(review=review, findings=[]),
                           exists=always)
    label = card["review"]["label"]
    assert "nothing is known" in label
    assert "0" not in label and "no problems" not in label.lower()
    assert card["listItems"] == []
    assert "findings" not in card["review"]
    if review["status"] == "failed":
        assert card["review"]["detail"] == review["detail"]


def test_a_clean_review_is_a_first_pass_not_a_sign_off():
    ok = {"status": "ok", "detail": None, "findings": 0, "blockers": 0}
    card = cards.from_tool("board_status", done(review=ok, findings=[]),
                           exists=always)
    assert card["review"]["label"] == ("Nothing flagged. First pass: no datasheets "
                                       "were read.")
    labels = [b["text"] for b in card["buttons"]]
    assert labels == ["My boards"]


def test_failure_shows_class_and_cause_words_and_nothing_was_ordered():
    sc = status("failed", failure={"stage": "routing",
                                   "reason": "ModelError: the AI model didn't answer"})
    card = cards.from_tool("board_status", sc)
    assert card["failure"] == {"stage": "routing",
                               "text": "Stopped while routing the copper",
                               "reason": "ModelError: the AI model didn't answer",
                               "ordered": "Nothing was ordered."}
    # No answers were given, so the plan asked nothing: questions is skipped.
    assert [s["status"] for s in card["steps"]] == ["done", "skipped", "done", "done",
                                                    "failed", "todo"]
    again = card["buttons"][0]
    assert again["hint"] == {"tool": "start_board_design",
                             "arguments": {"intent": sc["intent"]}}


def test_finding_card_highlights_board_refs():
    explain = {"session_id": SID, "which": 1, "count": 2, "review_status": "ok",
               "finding": {"number": 1, "severity": "blocker",
                           "title": "VOUT has no bulk capacitor",
                           "detail": "It oscillates.", "parts": ["AMS1117-3.3", "C2"],
                           "refs": ["U1", "C2"],
                           "suggested_fix": "Add a 22uF tantalum.", "citation": None},
               "speech": "Finding 1 of 2 is a blocker."}
    card = cards.from_tool("explain_finding", explain)
    assert card["id"] == f"finding:{SID}:1"
    assert card["highlightRefs"] == ["U1", "C2"]
    assert card["severityLabel"] == "Blocker: will not work as drawn"
    assert card["fixNote"] == "Not applied. Ada never changes a board on its own."
    assert card["buttons"][0]["hint"]["arguments"] == {"session_id": SID, "which": 2}
    board = cards.from_tool("board_status", done(), exists=always)
    lit = cards.highlighted(board, card["highlightRefs"])
    assert lit["highlightRefs"] == ["U1", "C2"] and board["highlightRefs"] == []


def test_a_finding_card_for_a_review_that_did_not_run_highlights_nothing():
    explain = {"session_id": SID, "which": 1, "count": None,
               "review_status": "not_run", "finding": None,
               "speech": "The design review hasn't run on this board yet."}
    card = cards.from_tool("explain_finding", explain)
    assert card["severity"] is None and card["highlightRefs"] == []
    assert card["primaryText"] == explain["speech"]


def test_recall_card_lists_boards_with_a_hint_per_item():
    # The headline is the one the tool really sends (speech.headline), so a
    # card that prefixes the state again shows up here: the M3 verifier saw
    # "done: done: 3 parts, fully routed, 1 blocker" on the live card.
    done_summary = {"parts": 3, "routed_fraction": 1.0, "unrouted": [],
                    "review": {"status": "ok", "blockers": 1}}
    boards = [
        {"session_id": SID, "intent": "a USB-C regulator", "state": "done",
         "created_at": "2026-10-10T17:40:12Z", "updated_at": "2026-10-10T17:41:00Z",
         "headline": speech.headline("done", done_summary, None),
         "routed_fraction": 1.0, "unrouted": 0, "blockers": 1,
         "review_status": "ok"},
        {"session_id": "brd_0f0f0f0f0f0f", "intent": "a motor driver",
         "state": "failed", "created_at": "2026-10-09T17:40:12Z",
         "updated_at": "2026-10-09T17:41:00Z",
         "headline": speech.headline("failed", None, "routing"),
         "routed_fraction": None, "unrouted": None, "blockers": None,
         "review_status": "not_run"},
    ]
    recall = {"query": "USB-C", "total": 3, "speech": "Your latest board.",
              "boards": boards}
    card = cards.from_tool("recall_my_boards", recall)
    assert card["headerSubtitle"] == "2 of 3 matching “USB-C”"
    item = card["listItems"][0]
    assert item["createdAt"] == "2026-10-10T17:40:12Z"
    assert item["hint"] == {"tool": "board_status", "arguments": {"session_id": SID}}
    assert item["secondaryText"] == "done: 3 parts, fully routed, 1 blocker"
    assert card["listItems"][1]["secondaryText"] == "failed while routing the copper"


def test_image_only_when_the_board_is_routed():
    routing = status("routing", summary=_summary(board_mm=[16.4, 8.8]))
    card = cards.from_tool("board_status", routing, exists=always)
    assert card["image"] is None
    assert card["imageNote"] == "The board is drawn once it is routed."
    finished = cards.from_tool("board_status", done(), exists=always)
    assert finished["image"]["src"] == f"/api/boards/{SID}/board.svg"
    assert finished["files"]["board"] == f"/api/boards/{SID}/board.kicad_pcb"
    # The page names the file, never the directory it sits in on the server.
    assert "/" not in finished["files"]["boardPath"]
    assert finished["files"]["boardPath"].endswith(".kicad_pcb")
    missing = cards.from_tool("board_status", done(), exists=lambda p: False)
    assert missing["image"] is None
    remote = cards.from_tool("board_status", done(), images=False, exists=always)
    assert remote["image"] is None
    assert remote["imageNote"] == "The board file is on the server that made it."


def test_field_names_are_apl_alexa_layouts_names_and_no_hint_says_alexa():
    card = cards.from_tool("board_status", done(), exists=always)
    for name in ("headerTitle", "headerSubtitle", "headerAttributionText",
                 "primaryText", "secondaryText", "listItems", "hintText"):
        assert name in card
    for item in card["listItems"]:
        assert {"primaryText", "secondaryText"} <= set(item)
    texts = [card["hintText"]] + [b["text"] for b in card["buttons"]] + \
        [b["utterance"] for b in card["buttons"]]
    assert not any("alexa" in t.lower() for t in texts)
