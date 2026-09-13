"""A proposal failing on parts the builder cannot draw gets more rounds.

The 2026-09-13 robotic-arm demo: at ``fast`` effort (one repair round) the
model substituted an ``arduino_nano`` connector the builder has no land pattern
for, the one repair still named it, and the run died as ``ProposalError`` --
reported to the user as "internal error".
"""

from __future__ import annotations

import copy
import json

import pytest
from silkscreen.agents.model import ScriptedModel
from silkscreen.agents.propose import (
    ESCALATED_MAX_REPAIRS,
    ProposalError,
    propose_circuit,
)
from test_agents import GOOD_CIRCUIT


def _with_bad_connector(package: str) -> str:
    circuit = copy.deepcopy(GOOD_CIRCUIT)
    circuit["devices"]["NANO-HEADER"] = {
        "kind": "connector",
        "package": package,
        "pins": {"D2": "1", "GND": "2"},
    }
    circuit["nets"]["GND"].append("NANO-HEADER.GND")
    circuit["nets"]["SIG"] = ["NANO-HEADER.D2", "DRV8837.IN2"]
    return json.dumps(circuit)


def test_unsupported_parts_escalate_and_a_later_round_can_succeed():
    model = ScriptedModel(
        responses=[
            _with_bad_connector("arduino_nano"),
            _with_bad_connector("ArduinoNanoV3"),
            json.dumps(GOOD_CIRCUIT),
        ]
    )
    events: list[dict] = []
    spec, attempts = propose_circuit(
        model, "a motor driver", max_repairs=1, on_event=events.append
    )
    assert len(attempts) == 3
    escalations = [e for e in events if e["event"] == "propose.escalated"]
    assert len(escalations) == 1
    assert escalations[0]["max_repairs"] == 2


def test_escalation_is_capped_and_the_error_names_the_unsupported_parts():
    packages = [f"arduino_nano_v{i}" for i in range(10)]
    model = ScriptedModel(responses=[_with_bad_connector(p) for p in packages])
    with pytest.raises(ProposalError, match="No valid circuit after") as caught:
        propose_circuit(model, "a motor driver", max_repairs=1)
    assert len(model.calls) == ESCALATED_MAX_REPAIRS + 1
    err = caught.value
    assert err.unsupported and "arduino_nano" in err.unsupported[0]
    assert err.errors
    assert "connector" in err.supported_packages
    assert "can draw" in str(err)
    assert "more than the 1 repair" in str(err)


def test_a_model_repeating_the_same_unsupported_part_is_not_escalated():
    model = ScriptedModel(responses=[_with_bad_connector("arduino_nano")] * 5)
    with pytest.raises(ProposalError) as caught:
        propose_circuit(model, "a motor driver", max_repairs=1)
    # The one budgeted repair came back identical, so no escalation.
    assert len(model.calls) == 2
    assert caught.value.unsupported


def test_plain_netlist_errors_never_escalate():
    broken = copy.deepcopy(GOOD_CIRCUIT)
    broken["nets"]["GND"] = ["AMS1117-3.3.GND", "DRV8837.NOPE"]
    model = ScriptedModel(responses=[json.dumps(broken)] * 5)
    with pytest.raises(ProposalError) as caught:
        propose_circuit(model, "a motor driver", max_repairs=1)
    assert len(model.calls) == 2
    assert caught.value.unsupported == []
