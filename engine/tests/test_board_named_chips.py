"""Chips drawn by part number, and pins that land on no pad.

Both rules came out of the robot-arm controller run (2026-09-13). The
pin-count rule drew a PCA9685 -- a part made only in TSSOP-28 and HVQFN-28 --
as a SOIC-28, and refused an ESP32 module outright; and a USB-C receptacle
declared with pins "1".."6" on a power-only part whose pads are A5/B12/...
placed with no copper on it at all, silently.
"""

from __future__ import annotations

import pytest
from silkscreen.board import package_errors, supported_packages_text
from silkscreen.footprints import UnsupportedPackage
from silkscreen.netlist import parse_circuit_spec


def _spec(devices: dict, nets: dict, passives: dict | None = None):
    return parse_circuit_spec(
        {"devices": devices, "passives": passives or {}, "nets": nets}
    )


def _footprint(name: str, pins: int):
    from silkscreen.board import _footprint_for_device

    return _footprint_for_device(name, pins, {})


@pytest.mark.parametrize(
    "name, pins, expected",
    [
        ("PCA9685PW", 28, "TSSOP-28"),
        ("PCA9685", 28, "TSSOP-28"),
        ("ESP32-WROOM-32E", 39, "ESP32-WROOM-32E"),
        ("ESP32-WROOM-32E-N4", 39, "ESP32-WROOM-32E"),
        ("TPS5450DDA", 9, "TI_SO-PowerPAD-8"),
        ("TPS5430DDAR", 9, "TI_SO-PowerPAD-8"),
        # The pin-count rule is untouched for everything else.
        ("CH340C", 16, "SOIC-16"),
        ("SN74HC126D", 14, "SOIC-14"),
    ],
)
def test_the_part_number_picks_the_land_pattern(name, pins, expected):
    assert _footprint(name, pins).name == expected


@pytest.mark.parametrize(
    "name, pins, fragment",
    [
        # The BS is the HVQFN-28 and must not be drawn as the PW's TSSOP.
        ("PCA9685BS", 28, "HVQFN-28"),
        # A named part whose count disagrees is refused, not drawn.
        ("PCA9685PW", 16, "28 pins"),
        ("ESP32-WROOM-32E", 38, "39 pins"),
        # A TPS5450 declared without its PowerPAD is the floating-slug board.
        ("TPS5450DDA", 8, "9 pins"),
        # The -32 (no E) and -32U are different footprints and not drawn.
        ("ESP32-WROOM-32", 39, "No package rule"),
        ("ESP32-WROOM-32UE", 39, "No package rule"),
    ],
)
def test_a_named_part_that_does_not_match_is_refused(name, pins, fragment):
    with pytest.raises(UnsupportedPackage, match=fragment):
        _footprint(name, pins)


def test_the_prompt_text_advertises_every_named_chip():
    text = supported_packages_text()
    for key in ("pca9685", "esp32_wroom_32e", "tps5450", "TSSOP-28", "PowerPAD"):
        assert key in text, key
    for package in ("USB_C_Receptacle_USB2.0_16P", "USB_C_Receptacle_Power"):
        assert package in text


def _usb_board(pins: dict[str, str], package: str):
    devices = {
        "CH340C": {"pins": {"GND": "1", "UD_P": "5", "UD_M": "6", "VCC": "16"}},
        "j_usb": {"kind": "connector", "package": package, "pins": pins},
    }
    nets = {
        "GND": ["CH340C.GND", "j_usb.GND"],
        "USB_DP": ["CH340C.UD_P", "j_usb.DP"],
        "USB_DM": ["CH340C.UD_M", "j_usb.DN"],
        "VBUS": ["CH340C.VCC", "j_usb.VBUS"],
    }
    return _spec(devices, nets)


def test_a_pin_numbered_for_a_pad_the_part_lacks_is_refused():
    """The measured failure: pins "1".."6" on the power-only receptacle."""
    spec = _usb_board(
        {"VBUS": "1", "GND": "2", "DP": "5", "DN": "6"}, "USB_C_Receptacle_Power"
    )
    errors = package_errors(spec)
    assert len(errors) == 1
    assert "no pad for pin(s)" in errors[0]
    assert "A5" in errors[0]  # the message names the pads that do exist


def test_the_usb2_receptacle_takes_real_contact_names():
    spec = _usb_board(
        {"VBUS": "A4", "GND": "A1", "DP": "A6", "DN": "A7"},
        "USB_C_Receptacle_USB2.0_16P",
    )
    # A1 is stacked with B12 and A4 with B9; B12/B9 are unconnected here,
    # which is a stacked land on two nets and must be refused.
    errors = package_errors(spec)
    assert any("one land on the part" in e for e in errors), errors


def test_a_fully_wired_usb2_receptacle_passes():
    devices = {
        "CH340C": {"pins": {"GND": "1", "UD_P": "5", "UD_M": "6", "VCC": "16"}},
        "j_usb": {
            "kind": "connector",
            "package": "USB_C_Receptacle_USB2.0_16P",
            "pins": {
                "GND_A1": "A1", "GND_B12": "B12", "GND_B1": "B1", "GND_A12": "A12",
                "VBUS_A4": "A4", "VBUS_B9": "B9", "VBUS_B4": "B4", "VBUS_A9": "A9",
                "DP_A": "A6", "DP_B": "B6", "DN_A": "A7", "DN_B": "B7",
            },
        },
    }
    nets = {
        "GND": ["CH340C.GND", "j_usb.GND_A1", "j_usb.GND_B12", "j_usb.GND_B1",
                "j_usb.GND_A12"],
        "VBUS": ["CH340C.VCC", "j_usb.VBUS_A4", "j_usb.VBUS_B9", "j_usb.VBUS_B4",
                 "j_usb.VBUS_A9"],
        "USB_DP": ["CH340C.UD_P", "j_usb.DP_A", "j_usb.DP_B"],
        "USB_DM": ["CH340C.UD_M", "j_usb.DN_A", "j_usb.DN_B"],
    }
    assert package_errors(_spec(devices, nets)) == []


def _cap_board(leg1: str, leg2: str, value: str):
    devices = {
        "j_pwr": {"kind": "connector", "package": "TerminalBlock_2P_5.08mm",
                  "pins": {"V": "1", "G": "2"}},
    }
    nets = {leg1: ["j_pwr.V", "c_bulk.1"], leg2: ["j_pwr.G", "c_bulk.2"]}
    return _spec(devices, nets, {"c_bulk": {"type": "capacitor", "value": value}})


def test_an_electrolytic_with_its_plus_leg_on_ground_is_refused():
    errors = package_errors(_cap_board("GND", "+5V", "1000uF"))
    assert len(errors) == 1 and "leg 1 is +" in errors[0]


def test_an_electrolytic_the_right_way_round_passes():
    assert package_errors(_cap_board("+5V", "GND", "1000uF")) == []


def test_a_ceramic_has_no_polarity_to_refuse():
    assert package_errors(_cap_board("GND", "+5V", "10uF")) == []


# ------------------------------------------------------------ tied contacts


def test_a_partly_wired_usb2_receptacle_is_completed_not_refused():
    """The 2026-09-14 demo board: one GND and one VBUS land wired, the other
    lands shipped with no net. KiCad's own USB-C symbol gives each group one
    pin name, so wiring one contact wires the group."""
    from silkscreen.board import tie_package_pins

    spec = _usb_board(
        {"VBUS": "A4", "GND": "A1", "DP": "A6", "DN": "A7"},
        "USB_C_Receptacle_USB2.0_16P",
    )
    tied, notes = tie_package_pins(spec)

    assert package_errors(tied) == []
    j = next(d for d in tied.devices if d.name == "j_usb")
    assert set(j.pins.values()) >= {
        "A1", "A12", "B1", "B12", "A4", "A9", "B4", "B9", "A6", "B6", "A7", "B7"
    }
    nets = {c.net: set(c.endpoints) for c in tied.connections}
    by_number = {number: name for name, number in j.pins.items()}
    for net, numbers in {
        "GND": ("A1", "A12", "B1", "B12"),
        "VBUS": ("A4", "A9", "B4", "B9"),
        "USB_DP": ("A6", "B6"),
        "USB_DM": ("A7", "B7"),
    }.items():
        for number in numbers:
            assert f"j_usb.{by_number[number]}" in nets[net], (net, number)
    # Every tie is reported: 3 GND + 3 VBUS + 1 D+ + 1 D- pads.
    assert len(notes) == 8 and all("tied to" in n for n in notes)


def test_a_group_already_on_two_nets_is_left_for_the_short_check():
    from silkscreen.board import tie_package_pins

    devices = {
        "CH340C": {"pins": {"GND": "1", "RXD": "2", "VCC": "16"}},
        "j_usb": {
            "kind": "connector",
            "package": "USB_C_Receptacle_Power",
            "pins": {"GND_A": "A12", "GND_B": "B12", "V": "A9"},
        },
    }
    nets = {
        "GND": ["CH340C.GND", "j_usb.GND_A"],
        "OOPS": ["CH340C.RXD", "j_usb.GND_B"],
        "VBUS": ["j_usb.V", "CH340C.VCC"],
    }
    spec = _spec(devices, nets)
    tied, notes = tie_package_pins(spec)
    j = next(d for d in tied.devices if d.name == "j_usb")
    # The GND group disagrees, so nothing is guessed there...
    assert not any("A12" in n or "B12" in n for n in notes)
    # ...while the VBUS group, wired once, is completed.
    assert "B9" in set(j.pins.values())


def test_a_part_with_no_tied_groups_is_returned_unchanged():
    from silkscreen.board import tie_package_pins

    spec = _spec(
        {
            "CH340C": {"pins": {"GND": "1", "VCC": "16"}},
            "j_pwr": {
                "kind": "connector",
                "package": "TerminalBlock_2P_5.08mm",
                "pins": {"V": "1", "G": "2"},
            },
        },
        {"VCC": ["CH340C.VCC", "j_pwr.V"], "GND": ["CH340C.GND", "j_pwr.G"]},
    )
    tied, notes = tie_package_pins(spec)
    assert notes == [] and tied is spec


def test_kicad_names_a_no_connect_pin_the_way_eeschema_does():
    """eeschema/sch_pin.cpp::GetDefaultNetName and CTX_NETNAME escaping."""
    from silkscreen.board import kicad_unconnected_net

    assert kicad_unconnected_net("J1", "D+1", "A6") == "unconnected-(J1-D+1-PadA6)"
    assert kicad_unconnected_net("R1", "1", "1") == "unconnected-(R1-Pad1)"
    assert kicad_unconnected_net("U1", "A/B", "3") == "unconnected-(U1-A{slash}B-Pad3)"


def test_the_written_board_carries_ties_and_no_connects_but_routes_neither(tmp_path):
    """Read back with kiutils, independently of the emitter's own tables."""
    from kiutils.board import Board
    from silkscreen.board import (
        build_board,
        emit_kicad_pcb,
        route_board,
        tie_package_pins,
    )

    spec = _usb_board(
        {"VBUS": "A4", "GND": "A1", "DP": "A6", "DN": "A7", "CC": "A5"},
        "USB_C_Receptacle_USB2.0_16P",
    )
    spec, _ = tie_package_pins(spec)
    board = build_board(spec, time_limit_s=5)
    result = route_board(board)
    assert not any(net.startswith("unconnected-(") for net in result.unrouted)

    path = tmp_path / "b.kicad_pcb"
    path.write_text(emit_kicad_pcb(board))
    nets = {}
    for fp in Board().from_file(str(path)).footprints:
        for pad in fp.pads:
            nets[(fp.properties.get("Reference"), pad.number)] = (
                pad.net.name if pad.net else None
            )
    j = next(ref for ref, number in nets if number == "A6")
    # The tied lands are on the group's net...
    assert {nets[(j, n)] for n in ("A1", "A12", "B1", "B12")} == {"GND"}
    assert {nets[(j, n)] for n in ("A4", "A9", "B4", "B9")} == {"VBUS"}
    # ...the declared-but-unwired pin carries KiCad's no-connect name...
    assert nets[(j, "A5")] == f"unconnected-({j}-CC-PadA5)"
    # ...and a pad the part has but the circuit never declared stays netless.
    assert nets[(j, "A8")] is None


def test_an_esp32_wired_by_its_used_pins_ties_the_stacked_ground():
    # Measured 2026-09-14 (scripts/board_eval.py esp32_devboard): a correct
    # dev board naming only the pins it uses was refused as "declares 35 pins".
    # KiCad's symbol stacks GND on pads 1/15/38/39, so wiring GND wires all four.
    from silkscreen.board import tie_package_pins

    spec = _spec(
        {"ESP32-WROOM-32E": {"pins": {"GND": "1", "VDD": "2", "EN": "3", "TXD0": "35"}},
         "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
        {"GND": ["ESP32-WROOM-32E.GND", "AMS1117-3.3.GND"],
         "+3V3": ["ESP32-WROOM-32E.VDD", "AMS1117-3.3.VOUT"],
         "EN": ["ESP32-WROOM-32E.EN", "ESP32-WROOM-32E.TXD0"]},
    )
    tied, notes = tie_package_pins(spec)
    esp = next(d for d in tied.devices if d.name == "ESP32-WROOM-32E")
    assert {"1", "15", "38", "39"} <= set(esp.pins.values())
    gnd = next(c for c in tied.connections if c.net == "GND")
    assert {e for e in gnd.endpoints if e.startswith("ESP32")} == {
        f"ESP32-WROOM-32E.{name}" for name, n in esp.pins.items() if n in ("1", "15", "38", "39")}
    assert len(notes) == 3 and package_errors(tied) == []
