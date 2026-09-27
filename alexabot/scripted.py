"""``--scripted``: a whole voice session offline, with no key and no network.

A judge who clones the repo should be able to talk to Ada end to end without
anyone's API key, so the workers can run on :class:`~silkscreen.agents.model.
ScriptedModel` -- the offline stand-in the whole test suite already uses --
answering by the markers the stages really put in their prompts. Every
request gets the same practice board, and every status says ``scripted:
true``; nothing pretends the canned answer was designed for what was asked.

The three answers are copies of ``service/tests/test_app.py``'s ``PLAN``,
``CIRCUIT`` and ``REVIEW`` (runtime code must not import a test module),
changed only where a copy would be untrue here:

* the plan says what it is -- a practice 3.3 V regulator board -- and its
  power source is ``unknown`` with the assumption stated, because the circuit
  has no connector and the fixture's barrel jack would be a plan the board
  does not follow; and it asks two questions, so a session exercises "answer
  one, then let Ada choose";
* the circuit and the review are verbatim, except that the blocker cites no
  datasheet page: nothing here reads one, so the fixture's citation would be
  an invented one. The review's one blocker, "VOUT has no bulk capacitor", is
  true of this circuit (100 nF is all there is on VOUT), which matters because
  the spoken blocker must be real.

No sourcing or case answer is needed: a voice session starts ``steps`` with
``prefetch`` off, so neither lane starts, and no datasheet answer because no
URL is given.

:class:`DelayedModel` adds a fixed wait before each answer. It exists to
prove the tools do not wait on the model (reviewer finding B1): with a
second and a half per call, every tool call still returns in well under one.
It keeps the wrapped model under ``inner``, not ``model``, so
``effort.model_name`` answers ``None`` rather than a name that is not true,
and it has no ``for_tier``, so ``effort.cheap_sibling`` offers no cheap tier.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

__all__ = [
    "BANNER",
    "CIRCUIT",
    "PLAN",
    "PROPOSE_MARKER",
    "REVIEW",
    "REVIEW_MARKER",
    "DelayedModel",
    "answers",
    "model_factory",
]

BANNER = (
    "SCRIPTED: canned answers from alexabot/scripted.py; every request gets the "
    "same practice regulator board; no API key, no network"
)

#: The opening words of the propose prompt (``agents/propose.py``) and of the
#: critic's (``agents/review.py``). The plan's marker is imported from
#: ``agents/plan.py`` itself in :func:`answers`.
PROPOSE_MARKER = "designing a printed circuit board"
REVIEW_MARKER = "reviewing a circuit someone else designed"

PLAN = {
    "building": "a practice 3.3 V regulator board (scripted mode)",
    "power": {
        "source": "unknown",
        "input_voltage": "5V",
        "connector_package": None,
        "battery_package": None,
        "notes": "scripted mode: VIN is fed from a bench supply",
    },
    "rails": [
        {"name": "+3V3", "voltage": "3.3V", "derived_from": "VIN",
         "purpose": "logic and sensors"}
    ],
    "blocks": [
        {"name": "regulation", "purpose": "step 5V down to 3.3V", "rail": "+3V3"}
    ],
    "connectivity": [],
    "assumptions": [
        "the practice board takes 5 V on VIN from a bench supply; it has no "
        "connector"
    ],
    "open_questions": [],
    "questions": [
        {"ask": "How much current should the 3.3 volt rail supply?",
         "default": "up to 500 milliamps"},
        {"ask": "Do you want a power indicator LED?", "default": "no LED"},
    ],
}

CIRCUIT = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "C1": {"type": "capacitor", "value": "10uF"},
        "C2": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "C1.1"],
        "GND": ["AMS1117-3.3.GND", "C1.2", "C2.2"],
        "VOUT": ["AMS1117-3.3.VOUT", "C2.1"],
    },
}

REVIEW = {
    "findings": [
        {
            "severity": "blocker",
            "title": "VOUT has no bulk capacitor",
            "detail": "The regulator needs bulk capacitance on its output to stay "
            "stable. Without it the loop oscillates.",
            "parts": ["AMS1117-3.3", "C2"],
            # Blank, not the fixture's "AMS1117-3.3 datasheet p.9": a voice
            # run reads no datasheet (summary.datasheets_read is 0), and the
            # critic's own prompt says to cite a page only when a supplied
            # fact supports it -- "An invented citation is worse than none"
            # (agents/review.py). A canned answer must not cite one either.
            "citation": "",
            "suggested_fix": "Add a 22uF tantalum from VOUT to GND.",
        },
        {
            "severity": "marginal",
            "title": "Input capacitor is smaller than recommended",
            "detail": "10uF works, but the datasheet recommends more where the "
            "supply leads are long.",
            "parts": ["C1"],
            "citation": "",
            "suggested_fix": "Raise C1 to 22uF.",
        },
        {
            "severity": "note",
            "title": "No thermal relief on the tab",
            "detail": "The package dissipates through its tab; copper area sets "
            "the usable current.",
            "parts": ["AMS1117-3.3"],
            "citation": "",
            "suggested_fix": "",
        },
    ]
}


def answers() -> dict[str, str]:
    """``ScriptedModel.by_marker`` for a session: plan, circuit, review."""
    from silkscreen.agents.plan import PLAN_MARKER

    return {
        PLAN_MARKER: json.dumps(PLAN),
        PROPOSE_MARKER: json.dumps(CIRCUIT),
        REVIEW_MARKER: json.dumps(REVIEW),
    }


class DelayedModel:
    """``inner``, answering ``delay_s`` late -- a slow provider, offline."""

    def __init__(self, inner: Any, delay_s: float) -> None:
        self.inner = inner
        self.delay_s = float(delay_s)

    def generate(self, prompt: str, **kwargs: Any) -> str:
        time.sleep(self.delay_s)
        return self.inner.generate(prompt, **kwargs)


def model_factory(delay_s: float = 0.0) -> Callable[[], Any]:
    """A fresh scripted model per board, delayed when ``delay_s`` is above 0."""
    from silkscreen.agents.model import ScriptedModel

    canned = answers()

    def build() -> Any:
        model = ScriptedModel(by_marker=dict(canned))
        return DelayedModel(model, delay_s) if delay_s > 0 else model

    return build
