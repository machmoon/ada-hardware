"""The style pass: model-written build123d, sandboxed, kernel-gated.

Needs the ``cad`` extra (build123d), gated the ``test_spice.py`` way. Every
test drives a :class:`ScriptedModel`, so no network; the scripts themselves
really run in the sandboxed child and the kernel really re-measures them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

build123d = pytest.importorskip("build123d")

from silkscreen.agents.enclosure import EnclosureProposal  # noqa: E402
from silkscreen.agents.enclosure_style import (  # noqa: E402
    STYLE_MARKER,
    restyle_enclosure,
)
from silkscreen.agents.model import ScriptedModel  # noqa: E402
from silkscreen.enclosure.board_shape import board_envelope  # noqa: E402
from silkscreen.enclosure.cad import build_enclosure  # noqa: E402
from silkscreen.enclosure.ir import parse_enclosure_spec  # noqa: E402
from silkscreen.enclosure.kernel import verify_model  # noqa: E402
from silkscreen.enclosure.restyle import extract_script, run_style_script  # noqa: E402

BOARD = Path(__file__).parent / "fixtures" / "assembly_board.kicad_pcb"

ROUNDED = """```python
def style(base, lid, facts):
    def corners(shape, r):
        bb = shape.bounding_box()
        edges = [
            e for e in shape.edges().filter_by(bd.Axis.Z)
            if min(abs(e.center().X - bb.min.X), abs(e.center().X - bb.max.X)) < 1e-3
            and min(abs(e.center().Y - bb.min.Y), abs(e.center().Y - bb.max.Y)) < 1e-3
        ]
        return bd.fillet(edges, r)
    return corners(base, 1.5), corners(lid, 1.5)
```"""

#: Cuts a pocket straight through the floor: valid solid, broken case.
HOLED_FLOOR = """```python
def style(base, lid, facts):
    x, y, _ = facts["outer_mm"]
    tool = bd.Box(4, 4, 10).moved(bd.Location((x / 2, y / 2, 0)))
    return base - tool, lid
```"""


@pytest.fixture(scope="module")
def case():
    envelope = board_envelope(BOARD)
    spec = parse_enclosure_spec({})
    built = build_enclosure(spec, envelope)
    report = verify_model(built, spec, envelope)
    assert report.passed, report.text()
    return envelope, EnclosureProposal(spec, 0, built, report, "")


def test_a_style_that_keeps_every_clause_is_accepted(case):
    envelope, proposal = case
    llm = ScriptedModel(by_marker={STYLE_MARKER: ROUNDED})
    events = []
    out = restyle_enclosure(llm, proposal, envelope, on_event=events.append)

    assert out.script and "fillet" in out.script
    assert out.warnings == ()
    assert out.proposal.kernel.passed
    assert out.proposal.model is not proposal.model
    # The model was shown the case it is restyling.
    (doc,) = llm.calls[0]["documents"]
    assert doc.mime_type == "image/png" and doc.data[:4] == b"\x89PNG"
    assert events == [{"event": "enclosure.style", "round": 1, "accepted": True}]


def test_a_broken_script_goes_back_with_its_traceback_and_is_repaired(case):
    envelope, proposal = case
    llm = ScriptedModel(responses=["```python\ndef style(:\n```", ROUNDED])
    out = restyle_enclosure(llm, proposal, envelope)

    assert out.rounds == 1 and out.proposal.kernel.passed
    repair = llm.calls[1]["prompt"]
    assert "SyntaxError" in repair and "def style(:" in repair


def test_a_restyle_that_breaks_a_clause_is_rejected_and_the_plain_case_ships(case):
    envelope, proposal = case
    llm = ScriptedModel(responses=[HOLED_FLOOR] * 2)
    out = restyle_enclosure(llm, proposal, envelope, max_repairs=1)

    assert out.proposal is proposal  # the verified plain case, untouched
    assert out.script is None
    assert out.warnings and "plain case ships" in out.warnings[0]
    repair = llm.calls[1]["prompt"]
    assert "kernel rejected the restyle" in repair
    # All thirteen sampled clauses pass this hole (measured); the exact
    # boolean gate is what names it.
    assert "base:" in repair and "of the cavity" in repair


def test_the_sandbox_refuses_the_network_and_a_runaway_loop(case):
    _, proposal = case
    net = "import socket\ndef style(b, l, f):\n    socket.create_connection(('1.1.1.1', 80), timeout=2)\n    return b, l"
    result = run_style_script(proposal.model, net, timeout_s=20)
    assert result.model is None
    loop = run_style_script(
        proposal.model,
        "def style(b, l, f):\n    while True:\n        pass",
        timeout_s=3,
    )
    assert loop.model is None and "limit" in loop.error


def test_extract_script_takes_the_fenced_block():
    assert extract_script("here:\n```python\nx = 1\n```\nthanks") == "x = 1"
    assert extract_script("x = 2") == "x = 2"
