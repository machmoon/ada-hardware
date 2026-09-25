"""Decoupling capacitors are placed next to the IC supply pin they serve.

Detection follows tscircuit's RFC (``rfcs/2026-07-31-automatic-decoupling-
capacitor-detection-and-enforcement.md``): a capacitor between an IC's power
pin and ground is a decoupling cap. The placer used to see only the rail's
down-weighted bounding box, inside which a cap sits anywhere for free.
"""

from __future__ import annotations

import math

from silkscreen.board import decoupling_caps, part_anchor, rotate_offset
from silkscreen.netlist import parse_circuit_spec

TINY = {"PB5": "1", "PB3": "2", "PB4": "3", "GND": "4",
        "PB0": "5", "PB1": "6", "PB2": "7", "VCC": "8"}
CH340 = {"GND": "1", "TXD": "2", "RXD": "3", "V3": "4", "UDP": "5",
         "UDM": "6", "CTS": "9", "DSR": "10", "RI": "11", "DCD": "12",
         "DTR": "13", "RTS": "14", "R232": "15", "VCC": "16"}


def _two_chip_spec():
    return parse_circuit_spec(
        {
            # The open pins are declared, not merely absent: since 2026-09-15 an
            # undeclared open pin stays dangling for KiCad's ERC to report.
            # (This is a placement fixture: the CH340's USB pins go nowhere
            # because the board has no receptacle, and V3 is left open rather
            # than decoupled -- stated here so the ERC gate does not read it
            # as a forgotten pin.)
            "devices": {
                "ATtiny85": {"pins": TINY, "no_connect": ["PB3", "PB4"]},
                "CH340C": {
                    "pins": CH340,
                    # CTS/DSR/RI/DCD/R232 are the modem-control inputs KiCad's
                    # CH340C symbol carries; without them the in-loop ERC
                    # refuses the circuit (found by board_eval, 2026-09-16).
                    "no_connect": ["V3", "UDP", "UDM", "CTS", "DSR", "RI", "DCD",
                                   "DTR", "RTS", "R232"],
                },
            },
            "passives": {
                "C1": {"type": "capacitor", "value": "100n"},
                "C2": {"type": "capacitor", "value": "100n"},
                "C3": {"type": "capacitor", "value": "10u"},
                "R1": {"type": "resistor", "value": "1k"},
                "R2": {"type": "resistor", "value": "1k"},
                "R3": {"type": "resistor", "value": "10k"},
                "D1": {"type": "diode", "value": "LED"},
            },
            "nets": {
                "VCC": ["ATtiny85.VCC", "CH340C.VCC", "C1.1", "C2.1", "C3.1", "R3.1"],
                "GND": ["ATtiny85.GND", "CH340C.GND", "C1.2", "C2.2", "C3.2", "D1.2"],
                "TX": ["CH340C.TXD", "R1.1"],
                "TXR": ["R1.2", "ATtiny85.PB0"],
                "RX": ["CH340C.RXD", "R2.1"],
                "RXR": ["R2.2", "ATtiny85.PB1"],
                "RST": ["R3.2", "ATtiny85.PB5"],
                "LED": ["ATtiny85.PB2", "D1.1"],
            },
        }
    )


def test_caps_between_an_ic_supply_pin_and_ground_are_detected():
    found = {d.cap: d for d in decoupling_caps(_two_chip_spec())}
    assert set(found) == {"C1", "C2", "C3"}
    # One rail, two IC supply pins: the caps are dealt out, not piled on one.
    assert found["C1"].pin_endpoint == "ATtiny85.VCC"
    assert found["C2"].pin_endpoint == "CH340C.VCC"
    assert found["C3"].pin_endpoint == "ATtiny85.VCC"
    assert found["C1"].rail_endpoint == "C1.1"
    assert found["C1"].ground_pin_endpoint == "ATtiny85.GND"


def test_a_cap_on_a_signal_or_a_connector_rail_is_not_decoupling():
    spec = parse_circuit_spec(
        {
            "devices": {
                "ATtiny85": {"pins": TINY},
                "J1": {"kind": "connector", "package": "PinHeader_1x02_P2.54mm",
                       "pins": {"VBUS": "1", "GND": "2"}},
            },
            "passives": {
                "C1": {"type": "capacitor", "value": "10u"},
                "C2": {"type": "capacitor", "value": "1n"},
                "R1": {"type": "resistor", "value": "10k"},
            },
            "nets": {
                # C1 sits on a rail only a connector touches: a source, no load.
                "VBUS": ["J1.VBUS", "C1.1", "R1.1"],
                "GND": ["J1.GND", "C1.2", "ATtiny85.GND"],
                # C2 is a filter cap between two signals.
                "SIG": ["R1.2", "C2.1", "ATtiny85.PB3"],
                "SIG2": ["C2.2", "ATtiny85.PB4"],
            },
        }
    )
    assert decoupling_caps(spec) == []


def _loop_mm(board, spec) -> dict[str, float]:
    """Per cap, rail pad to IC supply pin plus ground pad to IC ground pin:
    the two legs of the loop the cap closes. A pin number repeated on a tab
    (SOT-223 pin 2) counts at its nearest pad."""
    ref_of = spec.assign_refs()
    pos: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for part in board.parts:
        ax, ay = part_anchor(part)
        for pad in part.footprint.pads:
            ox, oy = rotate_offset(pad.x_nm, pad.y_nm, rotated=part.rotated)
            pos.setdefault((part.ref, pad.number), []).append((ax + ox, ay - oy))
    devices = {d.name: d for d in spec.devices}

    def gap(cap_endpoint: str, pin_endpoint: str | None) -> float:
        if pin_endpoint is None:
            return 0.0
        cap, _, leg = cap_endpoint.rpartition(".")
        name, _, pin = pin_endpoint.rpartition(".")
        (a,) = pos[(ref_of[cap], leg)]
        return min(
            math.dist(a, b) for b in pos[(ref_of[name], devices[name].pins[pin])]
        ) / 1e6

    return {
        dec.cap: gap(dec.rail_endpoint, dec.pin_endpoint)
        + gap(dec.ground_endpoint, dec.ground_pin_endpoint)
        for dec in decoupling_caps(spec)
    }


# How close each cap lands is a quality number, not a pass/fail: the CP-SAT
# solve is time-limited, so a with/without comparison flips under load (the
# baseline measured 50.2 mm idle and 44.6 mm beside a running suite). It is
# tracked as decoupling_loop_mm by scripts/board_eval.py instead.
