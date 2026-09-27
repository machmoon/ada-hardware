"""``--scripted``: the canned answers are real answers, and the mode says so."""

import json
import os

import pytest
from silkscreen.agents.effort import cheap_sibling, model_name
from silkscreen.agents.model import ScriptedModel
from silkscreen.agents.plan import PLAN_MARKER, parse_plan_response
from silkscreen.agents.review import run_review
from silkscreen.netlist import parse_circuit_spec
from silkscreen.verify.circuit import electrical_completeness

from alexabot import app, scripted, tools
from alexabot.config import Config
from alexabot.runner import Runner
from alexabot.store import BoardStore
from alexabot.tests.fakes import call, ok, wait_for


def test_every_scripted_answer_passes_the_real_parser():
    plan = parse_plan_response(json.dumps(scripted.PLAN))
    assert [(q.ask, q.default) for q in plan.questions] == [
        (q["ask"], q["default"]) for q in scripted.PLAN["questions"]]
    assert plan.power.source == "unknown" and plan.assumptions
    spec = parse_circuit_spec(json.dumps(scripted.CIRCUIT))
    assert spec.part_count() == 3
    model = ScriptedModel(by_marker={scripted.REVIEW_MARKER:
                                     json.dumps(scripted.REVIEW)})
    outcome = run_review(model, spec, facts=[], refute=False)
    titles = [f.title for f in outcome.findings if f.severity.value == "blocker"]
    assert titles == ["VOUT has no bulk capacitor"]


def test_the_canned_circuit_passes_electrical_completeness():
    verdict = electrical_completeness(parse_circuit_spec(json.dumps(scripted.CIRCUIT)))
    assert verdict.status == "ok", verdict.repair_items()


def test_every_scripted_marker_is_asked_during_a_session(steps_dir):
    built = []
    factory = scripted.model_factory(0.0)

    def recording():
        built.append(factory())
        return built[-1]

    runner = Runner(BoardStore(":memory:"), recording, None, scripted=True).warm()
    toolset = tools.toolset(runner)
    sid = ok(call(toolset, "start_board_design",
                  {"intent": "a board", "request_id": "scr-00000001"}))["session_id"]
    wait_for(lambda: ok(call(toolset, "board_status", {"session_id": sid}))["state"]
             == "questions", timeout=60)
    call(toolset, "answer_design_questions", {"session_id": sid, "you_choose": True})
    call(toolset, "continue_design", {"session_id": sid})
    done = wait_for(lambda: (s := ok(call(toolset, "board_status",
                                          {"session_id": sid})))["state"]
                    in ("done", "failed") and s, timeout=60)
    assert done["state"] == "done", done["failure"]
    assert runner.join(10)
    prompts = [c["prompt"] for c in built[0].calls]
    for marker in (PLAN_MARKER, scripted.PROPOSE_MARKER, scripted.REVIEW_MARKER):
        assert any(marker in p for p in prompts), marker
    # prefetch is off: no case and no sourcing call was spent.
    assert len(prompts) == 3


def test_delayed_model_names_no_model_and_offers_no_cheap_tier():
    delayed = scripted.DelayedModel(ScriptedModel(), 0.01)
    assert model_name(delayed) is None
    assert cheap_sibling(delayed) is None
    assert not hasattr(delayed, "model")


def test_scripted_main_sets_the_conftest_env_pair_and_prints_the_banner(
    monkeypatch, capsys, tmp_path
):
    for name in ("SILKSCREEN_ERC_IN_LOOP", "SILKSCREEN_KICAD_LIBRARY",
                 "SILKSCREEN_STEPS_DIR", "MCP_HTTP_TOKEN", "SILKSCREEN_API_KEYS_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(app, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        app, "build_runner",
        lambda config, **kw: Runner(BoardStore(":memory:"), lambda: None, object(),
                                    steps=object()),
    )

    class Stopped:
        server_address = ("127.0.0.1", 8789)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    seen: dict[str, Config] = {}

    def fake_make_server(config, **kwargs):
        seen["config"] = config
        return Stopped()

    monkeypatch.setattr(app, "make_server", fake_make_server)
    assert app.main(["--scripted", "--db", str(tmp_path / "b.sqlite3")]) == 0
    assert os.environ["SILKSCREEN_ERC_IN_LOOP"] == "0"
    assert os.environ["SILKSCREEN_KICAD_LIBRARY"] == "0"
    assert os.environ["SILKSCREEN_STEPS_DIR"].endswith("alexa-steps")
    err = capsys.readouterr().err
    assert scripted.BANNER in err and "no auth, loopback only" in err
    assert seen["config"].scripted is True and seen["config"].host == "127.0.0.1"


@pytest.mark.parametrize("delay", ["-1", "soon"])
def test_a_bad_scripted_delay_is_refused(delay, monkeypatch, capsys):
    monkeypatch.setattr(app, "_load_dotenv", lambda: None)
    assert app.main(["--scripted", "--scripted-delay", delay]) == 2
    assert "--scripted-delay" in capsys.readouterr().err
