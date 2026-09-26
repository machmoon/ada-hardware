"""Every sentence the tools say keeps KayLerch's voice rules.

``packages/agent/prompts/voice.md`` (KayLerch/alexa-skill-mcp-bridge at
``ca2c2ef``): at most three short sentences, one question and it last, no
symbols or URLs, never raw error text -- and the review is spoken the way
``frontend/src/lib/voice.js`` ``reviewSpeech`` speaks it.
"""

import datetime
import time

import pytest

from alexabot import speech
from alexabot.runner import empty_summary

FINDINGS = [
    {"number": 1, "severity": "blocker", "title": "VOUT has no bulk capacitor",
     "refs": ["U1", "C2"]},
    {"number": 2, "severity": "marginal", "title": "C1 is small", "refs": ["C1"]},
    {"number": 3, "severity": "note", "title": "Tab relief", "refs": ["U1"]},
]


def _done(**over):
    summary = empty_summary()
    summary.update(parts=3, nets=3, board_mm=[16.4, 8.8], routed_fraction=1.0,
                   unrouted=[], findings=list(FINDINGS),
                   review={"status": "ok", "detail": None, "findings": 3,
                           "blockers": 1})
    summary.update(over)
    return summary


UNROUTED = [{"net": n, "reason": "blocked"}
            for n in ("USB_D-", "USB_D+", "+3V3", "/EN", "SDA")]
QUESTION = {"index": 0, "ask": "How much current? Up to what?",
            "default": "500 mA. Probably.", "remaining": 1}

CASES = {
    "start": speech.start_speech(scripted=False),
    "start scripted": speech.start_speech(scripted=True),
    "reading": speech.status_speech("reading"),
    "reading queued": speech.status_speech("reading", queued=True),
    "first of one": speech.status_speech("questions", question=QUESTION,
                                         total_questions=1),
    "first of two": speech.status_speech("questions", question=QUESTION,
                                         total_questions=2),
    "next": speech.status_speech("questions", question=QUESTION, total_questions=2,
                                 answered=1, acknowledged=True),
    "proposing": speech.status_speech("proposing"),
    "proposing chose queued": speech.status_speech("proposing", queued=True,
                                                   you_chose=True),
    "drafted": speech.status_speech("drafted", summary=_done()),
    "drafted unknown": speech.status_speech("drafted", summary=empty_summary()),
    "placing": speech.status_speech("placing"),
    "routing": speech.status_speech("routing", summary=_done()),
    "reviewing": speech.status_speech("reviewing", summary=_done()),
    "reviewing partial": speech.status_speech(
        "reviewing", summary=_done(routed_fraction=0.8, unrouted=UNROUTED)),
    "stalled": speech.status_speech("routing", stalled=True),
    "done blocker": speech.status_speech("done", summary=_done()),
    "done unrouted": speech.status_speech(
        "done", summary=_done(routed_fraction=0.62, unrouted=UNROUTED)),
    "done clean": speech.status_speech("done", summary=_done(
        findings=[], review={"status": "ok", "detail": None, "findings": 0,
                             "blockers": 0})),
    "done notes only": speech.status_speech("done", summary=_done(
        findings=[FINDINGS[2]])),
    "done review failed": speech.status_speech("done", summary=_done(
        findings=[], review={"status": "failed", "detail": "ReviewError: {x}",
                             "findings": None, "blockers": None})),
    "done review not run": speech.status_speech("done", summary=_done(
        findings=[], review={"status": "not_run", "detail": None,
                             "findings": None, "blockers": None})),
    "replay done": speech.replay_speech(
        speech.status_speech("done", summary=_done())),
    "replay questions": speech.replay_speech(speech.status_speech(
        "questions", question=QUESTION, total_questions=2)),
    "explain": speech.explain_speech(
        {"severity": "blocker", "title": "VOUT has no bulk capacitor.",
         "detail": "It oscillates. Truly? Yes!", "refs": ["U1", "C2"],
         "suggested_fix": "Add a 22uF cap. See https://x.example/ds.pdf"},
        number=1, count=3, review_status="ok"),
    "explain not run": speech.explain_speech(None, number=1, count=None,
                                             review_status="not_run"),
    "explain failed": speech.explain_speech(None, number=1, count=None,
                                            review_status="failed"),
    "explain clean": speech.explain_speech(None, number=1, count=0,
                                           review_status="ok"),
    "recall none": speech.recall_speech([], total=0, query=None, total_all=0,
                                        now=time.time()),
    "recall none query": speech.recall_speech([], total=0, query="usb_c {x}",
                                              total_all=4, now=time.time()),
    "recall some": speech.recall_speech(
        [{"intent": "a 3.3V board? with {braces} and 50% duty and more words "
          "than twelve so it gets cut short here", "state": "done",
          "summary": _done(), "created_at": time.time()}],
        total=4, query="board", total_all=4, now=time.time()),
    "recall failed": speech.recall_speech(
        [{"intent": "x", "state": "failed", "summary": None,
          "failure_stage": "proposing", "created_at": time.time()}],
        total=1, query=None, total_all=1, now=time.time()),
    "recall restart": speech.recall_speech(
        [{"intent": "x", "state": "failed", "summary": None,
          "failure_stage": "restart", "created_at": time.time()}],
        total=1, query=None, total_all=1, now=time.time()),
    "recall unmeasured": speech.recall_speech(
        [{"intent": "x", "state": "done", "created_at": time.time(),
          "summary": _done(routed_fraction=None, unrouted=None)}],
        total=1, query=None, total_all=1, now=time.time()),
    "reviewing unmeasured": speech.status_speech(
        "reviewing", summary=_done(routed_fraction=None, unrouted=None)),
}
for _stage in ("reading", "proposing", "placing", "routing", "restart", "?"):
    for _cause in speech.CAUSE_WORDS:
        CASES[f"failed {_stage} {_cause}"] = speech.status_speech(
            "failed", failure={"stage": _stage, "cause": _cause, "reason": "x"})
for _kind, _text in speech.REFUSALS.items():
    CASES[f"refusal {_kind}"] = _text


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_template_is_at_most_three_sentences_one_question_last(name):
    assert speech.speech_problems(CASES[name]) == [], (name, CASES[name])


def test_no_symbols_urls_or_keys_in_speech():
    said = speech.spoken(
        "Use {x} on [VIN] at 50% -- see https://vendor.example/ds.pdf # USB_D* | ok"
    )
    assert speech.speech_problems(said) == []
    assert "a link" in said and "percent" in said and "USB D" in said
    explained = CASES["explain"]
    assert "http" not in explained and "22 microfarad" in explained


def test_skipped_review_is_not_run_failed_is_not_known_never_zero_findings():
    not_run = CASES["done review not run"]
    failed = CASES["done review failed"]
    assert "has not run" in not_run and "nothing is known" in not_run
    assert "failed" in failed and "nothing is known" in failed
    for text in (not_run, failed, CASES["explain not run"], CASES["explain failed"]):
        # No carve-out: "there are no findings" after a failed review is the
        # zero-findings claim the brief forbids, however it is introduced.
        assert "zero" not in text and "no findings" not in text
        assert "nothing to flag" not in text
    assert CASES["explain failed"] == (
        "The design review failed, so nothing is known about this board and I "
        "have nothing to explain."
    )
    # The raw detail never reaches speech.
    assert "ReviewError" not in failed


def test_clean_review_with_no_datasheets_is_not_a_sign_off():
    clean = CASES["done clean"]
    assert "nothing to flag" in clean
    assert "read no datasheets" in clean and "not a sign-off" in clean


def test_unrouted_nets_are_named_three_then_and_more():
    text = CASES["done unrouted"]
    assert "62 percent routed" in text
    assert (
        "five nets left unrouted: USB D minus, USB D plus, 3.3 volt and two more"
        in text
    )
    two = speech.status_speech("done", summary=_done(
        routed_fraction=0.9, unrouted=UNROUTED[:2]))
    assert "two nets left unrouted: USB D minus and USB D plus" in two
    # 99.6 percent with a net left is never a hundred.
    almost = speech.status_speech("done", summary=_done(
        routed_fraction=0.996, unrouted=UNROUTED[:1]))
    assert "99 percent" in almost and "fully" not in almost


@pytest.mark.parametrize(
    ("raw", "said"),
    [("+3V3", "3.3 volt"), ("+5V", "5 volt"), ("/EN", "EN"),
     ("USB_D-", "USB D minus"), ("USB_D+", "USB D plus"), ("+12V", "12 volt"),
     ("GND", "GND"), ("1V8", "1.8 volt")],
)
def test_speakable_net(raw, said):
    assert speech.speakable_net(raw) == said


@pytest.mark.parametrize(
    ("raw", "said"),
    [("10uF", "10 microfarad"), ("100 nF", "100 nanofarad"), ("22µF", "22 microfarad"),
     ("500mA", "500 milliamp"), ("3.3V", "3.3 volt"), ("10kΩ", "10 kilohm"),
     ("4.7Ω", "4.7 ohm"), ("50%", "50 percent"), ("8MHz", "8 megahertz"),
     ("C2", "C2"), ("U1 and C12", "U1 and C12")],
)
def test_units_table(raw, said):
    assert speech.spoken(raw) == said


def test_failure_cause_comes_from_the_class_not_the_message():
    from silkscreen.agents.model import ModelError
    from silkscreen.agents.propose import ProposalError
    from silkscreen.agents.providers import NoProviderConfigured
    from silkscreen.agents.resilience import AllProvidersFailed
    from silkscreen.footprints import UnsupportedPackage
    from silkscreen.netlist import ValidationError

    from service.amend import RunCancelled
    from service.steps import StepNotFound

    assert speech.cause_key(ModelError("validation failed, package expired")) == "model"
    assert speech.cause_key(NoProviderConfigured("x")) == "model"
    assert speech.cause_key(AllProvidersFailed.__new__(AllProvidersFailed)) == "model"
    proposal = ProposalError("the AI model didn't answer", [])
    assert speech.cause_key(proposal) == "validation"
    assert speech.cause_key(ValidationError(["x"])) == "validation"
    assert speech.cause_key(UnsupportedPackage("x")) == "package"
    assert speech.cause_key(StepNotFound("model error")) == "expired"
    assert speech.cause_key(RunCancelled("x")) == "cancelled"
    assert speech.cause_key(KeyError("ModelError")) == "other"


def test_when_phrases():
    now = time.mktime(datetime.datetime(2026, 9, 26, 15, 0).timetuple())
    day = 86400
    assert speech.when_phrase(now - 3600, now) == "earlier today"
    assert speech.when_phrase(now - day, now) == "yesterday"
    assert speech.when_phrase(now - 3 * day, now) == "three days ago"
    assert speech.when_phrase(now - 20 * day, now) == "on September 6"


def test_speech_never_claims_kicad_checked_anything():
    for text in CASES.values():
        assert "KiCad" not in text and "ERC" not in text and "DRC" not in text


def test_unmeasured_routing_is_never_spoken_as_routed_or_unrouted():
    # routed_fraction None means the route step reported no completion: not
    # "every net routed", and not "not routed" either.
    reviewing = CASES["reviewing unmeasured"]
    assert reviewing.startswith("Routing is done.")
    assert "every net" not in reviewing
    recalled = CASES["recall unmeasured"]
    assert "routing wasn't measured" in recalled
    assert "not routed" not in recalled and "fully" not in recalled


def test_a_restart_failure_reads_as_a_restart_in_the_history():
    assert CASES["recall restart"].split(". ")[1] == (
        "It stopped when the service restarted."
    )
    assert speech.headline("failed", None, "restart") == (
        "failed when the service restarted"
    )
    assert speech.headline("failed", None, "routing") == (
        "failed while routing the copper"
    )
