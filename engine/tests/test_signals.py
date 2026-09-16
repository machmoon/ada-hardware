"""Bus design checks (silkscreen.signals), on circuits written to break them."""

from __future__ import annotations

import pytest
from silkscreen.netlist import parse_circuit_spec
from silkscreen.signals import (
    diff_pairs,
    net_classes,
    resistance_ohms,
    signal_errors,
    signal_notes,
)


def _spec(devices, nets, passives=None):
    return parse_circuit_spec(
        {"devices": devices, "passives": passives or {}, "nets": nets}
    )


MCU = {"pins": {"VDD": "1", "GND": "2", "SDA": "3", "SCL": "4", "TXD": "5", "RXD": "6"}}
SENSOR = {"pins": {"VDD": "1", "GND": "2", "SDA": "3", "SCL": "4"}}
BRIDGE = {"pins": {"TXD": "2", "RXD": "3", "UD+": "5", "UD-": "6"}}


@pytest.mark.parametrize(
    "text, ohms",
    [
        ("4k7", 4700.0),
        ("4.7k", 4700.0),
        ("10k", 10000.0),
        ("2R2", 2.2),
        ("1M", 1e6),
        ("470", 470.0),
    ],
)
def test_resistance_reads_rkm_and_mega(text, ohms):
    assert resistance_ohms(text) == pytest.approx(ohms)


def test_a_correct_i2c_bus_and_crossed_uart_pass():
    spec = _spec(
        {"STM32F103C8T6": MCU, "BME280": SENSOR, "CH340C": BRIDGE},
        {
            "SDA": ["STM32F103C8T6.SDA", "BME280.SDA", "R1.1"],
            "SCL": ["STM32F103C8T6.SCL", "BME280.SCL", "R2.1"],
            "+3V3": ["R1.2", "R2.2", "STM32F103C8T6.VDD", "BME280.VDD"],
            "UART_A": ["STM32F103C8T6.TXD", "CH340C.RXD"],
            "UART_B": ["STM32F103C8T6.RXD", "CH340C.TXD"],
        },
        {
            "R1": {"type": "resistor", "value": "4k7"},
            "R2": {"type": "resistor", "value": "4.7k"},
        },
    )
    assert signal_errors(spec) == []


def test_missing_and_out_of_range_pull_ups_and_tx_to_tx_are_repair_items():
    spec = _spec(
        {"STM32F103C8T6": MCU, "BME280": SENSOR, "CH340C": BRIDGE},
        {
            "SDA": ["STM32F103C8T6.SDA", "BME280.SDA", "R1.1"],
            "SCL": ["STM32F103C8T6.SCL", "BME280.SCL"],
            "+3V3": ["R1.2", "STM32F103C8T6.VDD", "BME280.VDD"],
            "UART_A": ["STM32F103C8T6.TXD", "CH340C.TXD"],
            "UART_B": ["STM32F103C8T6.RXD", "CH340C.RXD"],
        },
        {"R1": {"type": "resistor", "value": "47k"}},
    )
    errors = "\n".join(signal_errors(spec))
    assert "SCL net 'SCL' has no pull-up" in errors
    assert "pull-up R1 on 'SDA' is 47k" in errors
    assert "joins TX to TX" in errors and "joins RX to RX" in errors


def test_an_i2c_line_leaving_the_board_is_a_note_not_an_error():
    spec = _spec(
        {
            "STM32F103C8T6": MCU,
            "J_QWIIC": {
                "kind": "connector",
                "package": "JST_SH_4P",
                "pins": {"SDA": "3", "SCL": "4"},
            },
        },
        {
            "SDA": ["STM32F103C8T6.SDA", "J_QWIIC.SDA"],
            "SCL": ["STM32F103C8T6.SCL", "J_QWIIC.SCL"],
        },
    )
    assert signal_errors(spec) == []
    assert any("leaves the board" in note for note in signal_notes(spec))


def test_usb_pair_is_recognised_and_becomes_a_net_class():
    spec = _spec(
        {
            "CH340C": BRIDGE,
            "J1": {
                "kind": "connector",
                "package": "USB_C_Receptacle_USB2.0_16P",
                "pins": {"DP": "A6", "DN": "A7"},
            },
        },
        {"USB_DP": ["CH340C.UD+", "J1.DP"], "USB_DM": ["CH340C.UD-", "J1.DN"]},
    )
    (pair,) = diff_pairs(spec)
    assert (pair.name, pair.positive, pair.negative, pair.impedance_ohms) == (
        "USB",
        "USB_DP",
        "USB_DM",
        90.0,
    )
    assert net_classes(spec) == [
        {"name": "USB", "nets": ["USB_DP", "USB_DM"], "impedance_ohms": 90.0}
    ]


def test_the_project_file_carries_the_pair_class_without_touching_clearance():
    import json

    from silkscreen.schematic import emit_kicad_pro

    settings = json.loads(
        emit_kicad_pro(
            "x", [{"name": "USB", "nets": ["USB_DP", "USB_DM"], "impedance_ohms": 90.0}]
        )
    )["net_settings"]
    usb = settings["classes"][1]
    assert (
        usb["name"] == "USB_90R" and "clearance" not in usb and "track_width" not in usb
    )
    assert {p["pattern"] for p in settings["netclass_patterns"]} == {"USB_DP", "USB_DM"}
