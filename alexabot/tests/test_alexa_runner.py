"""The state machine, against recorded ``service/steps.py`` envelopes."""

import json
import threading

import pytest
from silkscreen.agents.model import ModelError

from alexabot import tools
from alexabot.runner import Runner
from alexabot.tests.fakes import FakeSteps, as_account, call, error, ok, wait_for


def _state(toolset, sid):
    return ok(call(toolset, "board_status", {"session_id": sid}))


def _until(toolset, sid, *states):
    return wait_for(lambda: (s := _state(toolset, sid))["state"] in states and s)


def _start(toolset, request_id="req-00000001", intent="a 3.3V regulator board"):
    return ok(call(toolset, "start_board_design",
                   {"intent": intent, "request_id": request_id}))


def _session(fake_steps, **kwargs):
    """A runner and toolset over ``fake_steps`` with its own store."""
    from alexabot.store import BoardStore

    runner = Runner(BoardStore(":memory:"), lambda: object(), object(),
                    steps=fake_steps, **kwargs).warm()
    return runner, tools.toolset(runner)


def test_state_transitions_follow_the_table(toolset, fake_steps):
    gates = {name: threading.Event()
             for name in ("start_once", "propose", "place", "route", "review")}
    fake_steps.gates.update(gates)
    started = _start(toolset)
    sid = started["session_id"]
    assert started["state"] == "reading" and started["poll_after_s"] == 5
    gates["start_once"].set()
    questions = _until(toolset, sid, "questions")
    assert questions["question"]["index"] == 0 and questions["poll_after_s"] is None
    proposing = ok(call(toolset, "answer_design_questions",
                        {"session_id": sid, "you_choose": True}))
    assert proposing["state"] == "proposing"
    gates["propose"].set()
    drafted = _until(toolset, sid, "drafted")
    assert drafted["summary"]["parts"] == 3
    assert ok(call(toolset, "continue_design", {"session_id": sid}))["state"] == (
        "placing")
    gates["place"].set()
    assert _until(toolset, sid, "routing")["summary"]["board_mm"] == [16.4, 8.8]
    gates["route"].set()
    assert _until(toolset, sid, "reviewing")["summary"]["routed_fraction"] == 1.0
    gates["review"].set()
    done = _until(toolset, sid, "done")
    assert done["summary"]["review"] == {"status": "ok", "detail": None,
                                         "findings": 3, "blockers": 1}
    assert fake_steps.names() == ["start_once", "propose", "place", "route", "review"]


def test_no_steps_call_runs_on_the_request_thread(toolset, fake_steps):
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    _until(toolset, sid, "drafted")
    call(toolset, "continue_design", {"session_id": sid})
    _until(toolset, sid, "done")
    here = threading.current_thread()
    assert fake_steps.calls and all(c["thread"] is not here for c in fake_steps.calls)
    assert all(c["thread"].name.startswith(f"alexabot-{sid}-")
               for c in fake_steps.calls)


def test_status_explain_recall_never_touch_steps(toolset, fake_steps):
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    before = len(fake_steps.calls)
    for _ in range(3):
        _state(toolset, sid)
        call(toolset, "explain_finding", {"session_id": sid})
        call(toolset, "recall_my_boards", {})
        call(toolset, "board_status", {})
    assert len(fake_steps.calls) == before


def test_plan_without_questions_goes_straight_to_proposing():
    fake = FakeSteps(questions=[])
    fake.gates["propose"] = gate = threading.Event()
    _, toolset = _session(fake)
    sid = _start(toolset)["session_id"]
    proposing = _until(toolset, sid, "proposing")
    assert proposing["question"] is None and proposing["answers"] == []
    gate.set()
    assert _until(toolset, sid, "drafted")["summary"]["parts"] == 3
    assert fake.calls[1]["payload"] == {"answers": {}}


def test_failed_plan_proposes_from_the_intent_and_keeps_the_warning():
    fake = FakeSteps(plan_ok=False)
    _, toolset = _session(fake)
    sid = _start(toolset)["session_id"]
    drafted = _until(toolset, sid, "drafted")
    notes = drafted["summary"]["notes"]
    assert notes == ["the plan could not be read after one repair"]
    assert drafted["question"] is None


def test_answers_one_at_a_time_then_you_choose_marks_defaults(toolset, fake_steps):
    fake_steps.gates["propose"] = threading.Event()
    sid = _start(toolset)["session_id"]
    first = _until(toolset, sid, "questions")
    assert "two quick questions" in first["speech"]
    assert first["speech"].endswith("How much current should the 3.3 volt rail supply?")
    one = ok(call(toolset, "answer_design_questions",
                  {"session_id": sid, "answers": [{"index": 0, "answer": "one amp"}]}))
    assert one["state"] == "questions" and one["question"]["index"] == 1
    assert one["speech"].startswith("Got it. Next")
    assert one["answers"] == [{"index": 0, "ask": fake_steps.questions[0]["ask"],
                               "answer": "one amp", "source": "user"}]
    chose = ok(call(toolset, "answer_design_questions",
                    {"session_id": sid, "you_choose": True}))
    assert chose["state"] == "proposing"
    assert chose["speech"].startswith("Okay, I'll go with my defaults.")
    assert [a["source"] for a in chose["answers"]] == ["user", "default"]
    assert chose["answers"][1]["answer"] == "no LED"
    fake_steps.gates["propose"].set()
    _until(toolset, sid, "drafted")
    # Only what the person said reaches propose; the default is the plan's.
    assert fake_steps.calls[1]["payload"] == {"answers": {"0": "one amp"}}
    # A retry with the same answer is a no-op; a different one is refused.
    same = call(toolset, "answer_design_questions",
                {"session_id": sid, "answers": [{"index": 0, "answer": "one amp"}]})
    assert ok(same)["state"] == "drafted"
    other = call(toolset, "answer_design_questions",
                 {"session_id": sid, "answers": [{"index": 0, "answer": "two amps"}]})
    assert "already being drafted" in error(other)


def test_answer_needs_something_and_a_real_index(toolset):
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    assert "you_choose" in error(call(toolset, "answer_design_questions",
                                      {"session_id": sid}))
    text = error(call(toolset, "answer_design_questions",
                      {"session_id": sid, "answers": [{"index": 2, "answer": "x"}]}))
    assert text == "There are only 2 questions; index them 0 to 1."


def test_answer_while_still_planning_is_refused_in_words(toolset, fake_steps):
    fake_steps.gates["start_once"] = gate = threading.Event()
    sid = _start(toolset)["session_id"]
    text = error(call(toolset, "answer_design_questions",
                      {"session_id": sid, "you_choose": True}))
    assert text.startswith("I don't have a question for you yet")
    gate.set()


def test_an_indexed_answer_while_still_planning_is_refused_in_words(
    toolset, fake_steps
):
    # The questions are not known while planning, so "this board has no
    # questions to answer" would be untrue; the planning refusal comes first.
    fake_steps.gates["start_once"] = gate = threading.Event()
    sid = _start(toolset)["session_id"]
    text = error(call(toolset, "answer_design_questions",
                      {"session_id": sid,
                       "answers": [{"index": 0, "answer": "one amp"}]}))
    assert text.startswith("I don't have a question for you yet")
    gate.set()


def test_continue_while_proposing_is_queued(toolset, fake_steps):
    fake_steps.gates["propose"] = gate = threading.Event()
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    queued = ok(call(toolset, "continue_design", {"session_id": sid}))
    assert queued["state"] == "proposing"
    assert "place and route it as soon as it's drafted" in queued["speech"]
    gate.set()
    assert _until(toolset, sid, "done")["summary"]["review"]["status"] == "ok"
    assert fake_steps.names() == ["start_once", "propose", "place", "route", "review"]


def test_continue_during_questions_is_refused_in_words(toolset):
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    text = error(call(toolset, "continue_design", {"session_id": sid}))
    assert text == "I need an answer to my question first, or you can say you choose."


def test_model_error_in_propose_is_failed_with_reason_and_no_raw_text_in_speech(
    toolset, fake_steps, runner
):
    fake_steps.fail["propose"] = ModelError("SECRET-PROVIDER-TEXT 429 quota")
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    failed = _until(toolset, sid, "failed")
    assert failed["failure"] == {
        "stage": "proposing",
        "reason": "ModelError: the AI model didn't answer",
    }
    # Not in the speech, and not in the data either: the agent is told to
    # prefer structuredContent, so the message stays in the row.
    assert "SECRET" not in failed["speech"]
    assert "SECRET" not in json.dumps(failed)
    row = runner.store.get("local", sid)
    assert row.failure_reason == "ModelError: SECRET-PROVIDER-TEXT 429 quota"
    assert failed["speech"] == (
        "I couldn't finish drafting the schematic: the AI model didn't answer. "
        "Nothing was ordered. Want me to start it over?"
    )


def test_review_exception_is_done_with_review_failed(toolset, fake_steps, runner):
    fake_steps.fail["review"] = RuntimeError("critic exploded")
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    _until(toolset, sid, "drafted")
    call(toolset, "continue_design", {"session_id": sid})
    done = _until(toolset, sid, "done", "failed")
    assert done["state"] == "done" and done["failure"] is None
    review = done["summary"]["review"]
    assert review == {"status": "failed",
                      "detail": "RuntimeError: something went wrong on my side",
                      "findings": None, "blockers": None}
    assert "nothing is known" in done["speech"] and "exploded" not in json.dumps(done)
    stored = runner.store.get("local", sid).summary["review"]
    assert stored["error"] == "RuntimeError: critic exploded"


def test_skipped_review_is_not_run_never_zero(toolset, fake_steps):
    fake_steps.review_status = "skipped"
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    call(toolset, "continue_design", {"session_id": sid})
    done = _until(toolset, sid, "done")
    assert done["summary"]["review"] == {"status": "not_run", "detail": None,
                                         "findings": None, "blockers": None}
    assert done["summary"]["findings"] == []
    assert "has not run" in done["speech"]


def test_evicted_steps_session_is_failed_working_copy_expired(toolset, fake_steps):
    from service.steps import StepNotFound

    fake_steps.fail["place"] = StepNotFound("no step session 'steps1'")
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    _until(toolset, sid, "drafted")
    call(toolset, "continue_design", {"session_id": sid})
    failed = _until(toolset, sid, "failed")
    assert failed["failure"]["stage"] == "placing"
    assert "its working copy expired" in failed["speech"]


def test_unrouted_nets_are_all_named_in_the_summary():
    unrouted = {n: "blocked" for n in ("USB_D-", "USB_D+", "+3V3", "EN", "SDA")}
    fake = FakeSteps(unrouted=unrouted, completion=0.625)
    _, toolset = _session(fake)
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    call(toolset, "continue_design", {"session_id": sid})
    done = _until(toolset, sid, "done")
    assert [u["net"] for u in done["summary"]["unrouted"]] == list(unrouted)
    assert done["summary"]["routed_fraction"] == 0.625
    assert "62 percent routed" in done["speech"] and "and two more" in done["speech"]


def test_summary_fields_are_null_until_known_never_zero(toolset, fake_steps):
    for name in ("propose", "place", "route", "review"):
        fake_steps.gates[name] = threading.Event()
    sid = _start(toolset)["session_id"]
    assert _until(toolset, sid, "questions")["summary"] is None
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    fake_steps.gates["propose"].set()
    drafted = _until(toolset, sid, "drafted")["summary"]
    assert drafted["parts"] == 3
    for field in ("board_mm", "placement_status", "routed_fraction", "unrouted"):
        assert drafted[field] is None, field
    assert drafted["review"] == {"status": "not_run", "detail": None,
                                 "findings": None, "blockers": None}
    call(toolset, "continue_design", {"session_id": sid})
    fake_steps.gates["place"].set()
    routing = _until(toolset, sid, "routing")["summary"]
    assert routing["routed_fraction"] is None and routing["unrouted"] is None
    fake_steps.gates["route"].set()
    fake_steps.gates["review"].set()
    _until(toolset, sid, "done")


def test_one_live_worker_per_account_and_max_active():
    fake = FakeSteps()
    fake.gates["start_once"] = gate = threading.Event()
    _, toolset = _session(fake, max_active=2)
    with as_account("alice"):
        _start(toolset, "req-alice-1")
        busy = error(call(toolset, "start_board_design",
                          {"intent": "another board", "request_id": "req-alice-2"}))
        assert busy.startswith("I'm still working on your last board.")
    with as_account("bob"):
        _start(toolset, "req-bob-0001")
    with as_account("carol"):
        full = error(call(toolset, "start_board_design",
                          {"intent": "a board", "request_id": "req-carol-1"}))
        assert full == "I'm at capacity right now. Please try again in a few minutes."
    gate.set()
    with as_account("alice"):
        wait_for(lambda: _state(toolset, _latest(toolset))["state"] == "questions")
        # Parked on a question, alice holds no worker and may start another.
        _start(toolset, "req-alice-2", "another board")


def _latest(toolset):
    return ok(call(toolset, "board_status", {}))["session_id"]


def test_stalled_after_threshold(store, fake_steps):
    clock = [1000.0]
    runner = Runner(store, lambda: object(), object(), steps=fake_steps,
                    clock=lambda: clock[0], stall_after_s=300).warm()
    toolset = tools.toolset(runner)
    fake_steps.gates["start_once"] = gate = threading.Event()
    sid = _start(toolset)["session_id"]
    clock[0] += 299
    assert _state(toolset, sid)["stalled"] is False
    clock[0] += 2
    stalled = _state(toolset, sid)
    assert stalled["stalled"] is True and stalled["state"] == "reading"
    assert "may be stuck" in stalled["speech"]
    gate.set()
    assert runner.join(10)


def test_a_board_from_an_earlier_process_is_failed_not_spoken_as_progress(
    store, fake_steps
):
    board, _ = store.claim("local", "req-00000009", "old board",
                           session_id="brd_old", now=1.0)
    store.set_state("local", "brd_old", "routing", now=2.0)
    runner = Runner(store, lambda: object(), object(), steps=fake_steps).warm()
    status = ok(call(tools.toolset(runner), "board_status", {"session_id": "brd_old"}))
    assert status["state"] == "failed" and status["failure"]["stage"] == "restart"
    assert status["speech"].startswith(
        "I couldn't finish this board because the service restarted.")


@pytest.mark.parametrize("tool", ["board_status", "recall_my_boards"])
def test_a_read_racing_the_workers_last_write_leaves_the_board_done(
    tool, store, fake_steps
):
    """A status read before the worker's final write, and judged after the
    worker dropped its run, must not rewrite a finished board as a restart.

    The interleaving is forced: the reader is paused between its SELECT and
    the runner's lock while the review answers and the worker finishes --
    a legal order, since nothing holds a lock across the two. Unfixed, the
    row ended ``failed``/``restart`` about 1 time in 800 under stress.
    """
    fake_steps.gates["review"] = review = threading.Event()
    runner = Runner(store, lambda: object(), object(), steps=fake_steps).warm()
    toolset = tools.toolset(runner)
    sid = _start(toolset)["session_id"]
    _until(toolset, sid, "questions")
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    _until(toolset, sid, "drafted")
    call(toolset, "continue_design", {"session_id": sid})
    _until(toolset, sid, "reviewing")

    read, resume, paused = threading.Event(), threading.Event(), []
    real_get, real_recall = store.get, store.recall

    def pause_once(rows):
        if threading.current_thread().name == "reader" and not paused:
            paused.append(True)
            read.set()
            assert resume.wait(10)
        return rows

    store.get = lambda *a: pause_once(real_get(*a))
    store.recall = lambda *a, **k: pause_once(real_recall(*a, **k))
    answered = {}
    args = {"session_id": sid} if tool == "board_status" else {}
    reader = threading.Thread(
        target=lambda: answered.setdefault("r", ok(call(toolset, tool, args))),
        name="reader",
    )
    try:
        reader.start()
        assert read.wait(10)
        review.set()
        wait_for(lambda: sid not in runner._runs)  # wrote done, dropped the run
        assert real_get("local", sid).state == "done"
        resume.set()
        reader.join(10)
    finally:
        store.get, store.recall = real_get, real_recall
        resume.set()
    assert runner.join(10)
    row = real_get("local", sid)
    assert row.state == "done" and row.failure_stage is None
    seen = (answered["r"] if tool == "board_status" else answered["r"]["boards"][0])
    assert seen["state"] == "done"


@pytest.mark.parametrize("sid", ["brd_nope", "brd_theirs"])
def test_unknown_and_foreign_boards_get_one_answer(toolset, sid, runner):
    runner.store.claim("mallory", "req-00000001", "theirs", session_id="brd_theirs",
                       now=1.0)
    text = error(call(toolset, "board_status", {"session_id": sid}))
    assert text == "I can't find that board. Ask me to list your boards to find it."


def test_no_boards_yet_is_an_error_not_an_invented_status(toolset):
    text = error(call(toolset, "board_status", {}))
    assert text.startswith("You haven't started a board with me yet.")
