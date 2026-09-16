"""The deterministic verifiers: electrical completeness and KiCad's own checks.

The circuit tests build their specs by hand and decide what ground is with
their own arithmetic (the ``test_kicad.py`` discipline): none of them asks
``net_class`` or the verifier which net is ground. The KiCad tests are gated
on ``kicad-cli`` the way ``test_routing.py`` gates, so a green run without
KiCad does not mean ERC was exercised -- and the ``unverified`` test is the
one that runs everywhere.
"""

from __future__ import annotations

import copy
import json

import pytest
from silkscreen.netlist import ValidationError, parse_circuit_spec
from silkscreen.verify import (
    Verdict,
    electrical_completeness,
    erc_from_spec,
    kicad_cli_path,
)

LDO = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "c_in": {"type": "capacitor", "value": "10uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
        "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "c_out.1"],
    },
}

needs_kicad = pytest.mark.skipif(
    kicad_cli_path() is None, reason="kicad-cli not installed"
)


def _spec(raw: dict):
    return parse_circuit_spec(json.dumps(raw))


def _failed(verdict: Verdict) -> dict[str, str]:
    return {c.name: c.severity for c in verdict.clauses if not c.passed}


# ---- the IR field --------------------------------------------------------


def test_a_declared_open_pin_must_exist_and_must_not_be_wired():
    raw = copy.deepcopy(LDO)
    raw["devices"]["AMS1117-3.3"]["no_connect"] = ["GND", "NOSUCH"]
    with pytest.raises(ValidationError) as exc:
        _spec(raw)
    text = str(exc.value)
    assert "'NOSUCH' under 'no_connect'" in text
    assert "'GND' is both wired" in text


def test_no_connect_must_be_a_list_of_names():
    raw = copy.deepcopy(LDO)
    raw["devices"]["AMS1117-3.3"]["no_connect"] = "GND"
    with pytest.raises(ValidationError, match="must be a list of pin names"):
        _spec(raw)


# ---- electrical completeness --------------------------------------------


def test_a_correct_regulator_passes_with_its_classification_in_evidence():
    verdict = electrical_completeness(_spec(LDO))
    assert verdict.status == "ok"
    assert verdict.evidence["ground_nets"] == ["GND"]
    assert set(verdict.evidence["rail_nets"]) == {"VIN", "+3V3"}
    assert verdict.evidence["devices"]["AMS1117-3.3"]["power_pins"] == {
        "GND": "ground",
        "VIN": "rail",
        "VOUT": "rail",
    }


def test_an_unwired_ground_pin_is_a_blocker_naming_the_pin():
    raw = copy.deepcopy(LDO)
    raw["nets"]["GND"].remove("AMS1117-3.3.GND")
    verdict = electrical_completeness(_spec(raw))
    assert verdict.status == "blocked"
    items = verdict.repair_items()
    assert len(items) == 1
    assert "AMS1117-3.3.GND" in items[0] and "ground net" in items[0]


def test_a_ground_pin_declared_open_is_a_warning_not_a_blocker():
    raw = copy.deepcopy(LDO)
    raw["nets"]["GND"].remove("AMS1117-3.3.GND")
    raw["devices"]["AMS1117-3.3"]["no_connect"] = ["GND"]
    verdict = electrical_completeness(_spec(raw))
    assert verdict.status == "ok"
    warnings = [c for c in verdict.clauses if c.severity == "warning"]
    assert any("declared no_connect" in c.detail for c in warnings)


def test_two_grounds_are_an_island_unless_a_resistor_joins_them():
    raw = copy.deepcopy(LDO)
    raw["nets"]["GND"] = ["AMS1117-3.3.GND", "c_in.2"]
    raw["nets"]["GND_OUT"] = ["c_out.2", "r_load.2"]
    raw["nets"]["+3V3"].append("r_load.1")
    raw["passives"]["r_load"] = {"type": "resistor", "value": "330"}
    islands = electrical_completeness(_spec(raw))
    assert _failed(islands) == {"one_ground": "blocker"}
    detail = islands.failures[0].detail
    assert "GND" in detail and "GND_OUT" in detail

    bridged = copy.deepcopy(raw)
    bridged["passives"]["r_tie"] = {"type": "resistor", "value": "0"}
    bridged["nets"]["GND"].append("r_tie.1")
    bridged["nets"]["GND_OUT"].append("r_tie.2")
    assert electrical_completeness(_spec(bridged)).status == "ok"


def test_a_rail_on_the_ground_net_is_a_short():
    raw = copy.deepcopy(LDO)
    raw["nets"]["GND"].append("AMS1117-3.3.VOUT")
    raw["nets"]["+3V3"] = ["c_out.1", "r.1"]
    raw["nets"]["VIN"].append("r.2")
    raw["passives"]["r"] = {"type": "resistor", "value": "1k"}
    verdict = electrical_completeness(_spec(raw))
    assert "rail_to_ground:GND" in _failed(verdict)
    assert verdict.status == "blocked"


def test_a_supply_pin_on_an_oddly_named_net_is_only_a_warning():
    raw = copy.deepcopy(LDO)
    raw["nets"]["CH_V3"] = raw["nets"].pop("+3V3")
    verdict = electrical_completeness(_spec(raw))
    assert verdict.status == "ok"
    names = {c.name for c in verdict.clauses}
    assert "rail_pin_on_signal_net:AMS1117-3.3.VOUT" in names


def test_a_missing_decoupling_capacitor_is_a_warning():
    raw = {
        "devices": {
            "NE555D": {
                "pins": {"GND": "1", "OUT": "3", "VCC": "8"},
                "no_connect": ["OUT"],
            }
        },
        "passives": {"r": {"type": "resistor", "value": "1k"}},
        "nets": {"VCC": ["NE555D.VCC", "r.2"], "GND": ["NE555D.GND", "r.1"]},
    }
    verdict = electrical_completeness(_spec(raw))
    assert verdict.status == "ok"
    assert _failed(verdict) == {"decoupled:NE555D:VCC": "warning"}


def test_a_device_with_no_power_pins_is_reported_not_checked():
    raw = {
        "devices": {
            "J": {
                "kind": "connector",
                "package": "JST_PH_2P",
                "pins": {"A": "1", "B": "2"},
            }
        },
        "passives": {"r": {"type": "resistor", "value": "1k"}},
        "nets": {"A": ["J.A", "r.1"], "B": ["J.B", "r.2"]},
    }
    verdict = electrical_completeness(_spec(raw))
    assert verdict.evidence["not_checked"] == ["J"]


# ---- KiCad ------------------------------------------------------------------


def test_erc_without_kicad_is_unverified_never_ok(monkeypatch):
    monkeypatch.setattr("silkscreen.verify.kicad.kicad_cli_path", lambda: None)
    verdict = erc_from_spec(_spec(LDO))
    assert verdict.status == "unverified"
    assert not verdict.ok
    assert "kicad-cli" in verdict.unverified_reason
    assert verdict.repair_items() == []


@needs_kicad
def test_kicad_erc_sees_the_forgotten_ground_pin_and_not_the_declared_one():
    """The measurement behind ``Device.no_connect``.

    Before the field, both circuits below drew a no-connect on the regulator's
    GND and ERC reported nothing. Now the forgotten pin is a
    ``pin_not_connected`` error and the declared one is silent.
    """
    forgotten = copy.deepcopy(LDO)
    forgotten["nets"]["GND"].remove("AMS1117-3.3.GND")
    verdict = erc_from_spec(_spec(forgotten))
    assert verdict.status == "blocked"
    assert [c.name for c in verdict.failures] == ["erc.pin_not_connected"]
    assert "U1" in verdict.failures[0].refs
    assert "not under no_connect" in verdict.failures[0].detail

    declared = copy.deepcopy(forgotten)
    declared["devices"]["AMS1117-3.3"]["no_connect"] = ["GND"]
    assert erc_from_spec(_spec(declared)).status == "ok"
    assert erc_from_spec(_spec(LDO)).status == "ok"


@needs_kicad
def test_the_repair_loop_refuses_the_forgotten_ground(monkeypatch):
    """One round, the model's answer leaves GND off, the loop names the pin."""
    from silkscreen.agents.model import ScriptedModel
    from silkscreen.agents.propose import ProposalError, propose_circuit

    monkeypatch.setenv("SILKSCREEN_ERC_IN_LOOP", "1")
    forgotten = copy.deepcopy(LDO)
    forgotten["nets"]["GND"].remove("AMS1117-3.3.GND")
    events = []
    with pytest.raises(ProposalError) as exc:
        propose_circuit(
            ScriptedModel(responses=[json.dumps(forgotten)]),
            "an LDO",
            facts=[],
            max_repairs=0,
            on_event=events.append,
        )
    errors = exc.value.errors
    assert any("AMS1117-3.3.GND" in e for e in errors)
    assert any(e.startswith("erc.pin_not_connected") for e in errors)
    verdicts = [e for e in events if e["event"] == "propose.verdict"]
    assert [v["verifier"] for v in verdicts] == ["electrical_completeness", "erc"]
    assert all(v["status"] == "blocked" for v in verdicts)
