"""KiCad's installed libraries as a catalog (silkscreen.kicadlib).

Gated on the libraries being installed, the test_models3d.py convention: the
suite stays green without KiCad, so a green run without it has not exercised
this. Expected values are read straight from the library files with kiutils,
never from the code under test.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from silkscreen.kicadlib import footprint_dir, load_footprint, load_index, symbol_dir

needs_symbols = pytest.mark.skipif(
    symbol_dir() is None, reason="KiCad's symbol library is not installed"
)
needs_footprints = pytest.mark.skipif(
    footprint_dir() is None, reason="KiCad's footprint library is not installed"
)


@pytest.fixture(scope="module")
def index():
    return load_index()


@needs_symbols
def test_a_part_number_finds_its_library_symbol_first(index):
    top = index.search("AMS1117-3.3", 1)[0]
    assert top.lib_id == "Regulator_Linear:AMS1117-3.3"
    assert top.footprint == "Package_TO_SOT_SMD:SOT-223-3_TabPin2"


@needs_symbols
def test_a_derived_symbol_carries_its_parents_pins(index):
    """AMS1117-3.3 is (extends "AP1117-15"): no pins of its own in the file."""
    from kiutils.symbol import SymbolLib

    lib = SymbolLib().from_file(str(symbol_dir() / "Regulator_Linear.kicad_sym"))
    parent = next(s for s in lib.symbols if s.entryName == "AP1117-15")
    want = {(str(p.number), str(p.name)) for u in parent.units for p in u.pins}
    got = {(n, name) for n, name, _ in index.get("Regulator_Linear:AMS1117-3.3").pins}
    assert got == want and want


@needs_symbols
def test_manufacturer_placeholders_match_full_part_numbers(index):
    assert index.search("STM32F103C8T6", 1)[0].lib_id == "MCU_ST_STM32F1:STM32F103C8Tx"


@needs_symbols
def test_the_cache_reloads_the_same_catalog(index, tmp_path, monkeypatch):
    assert len(index) > 10_000
    reloaded = load_index()
    assert len(reloaded) == len(index)
    assert reloaded.get("Timer:NE555P") == index.get("Timer:NE555P")


@needs_footprints
def test_a_library_footprint_keeps_its_pads_courtyard_and_model():
    from kiutils.footprint import Footprint

    fid = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    lf = load_footprint(fid)
    path = footprint_dir() / "Package_TO_SOT_SMD.pretty" / "SOT-223-3_TabPin2.kicad_mod"
    raw = Footprint().from_file(str(path))
    assert sorted(p.number for p in lf.footprint.pads) == sorted(
        str(p.number) for p in raw.pads
    )
    # Re-centred on the courtyard, the offset maps each pad back to its
    # library position (KiCad frame, nm).
    ox, oy = lf.origin_offset_nm
    for ours, theirs in zip(lf.footprint.pads, raw.pads, strict=True):
        assert abs(ours.x_nm - ox - round(theirs.position.X * 1e6)) <= 1
        assert abs(ours.y_nm - oy - round(theirs.position.Y * 1e6)) <= 1
    assert lf.models and lf.models[0].endswith("SOT-223.step")
    assert lf.text.startswith("(footprint")


@needs_footprints
def test_a_missing_footprint_is_refused_in_words():
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_footprint("Package_SO:No_Such_Footprint")
    with pytest.raises(ValueError, match="Library:Name"):
        load_footprint("SOT-223")


@needs_symbols
def test_parts_resolve_exactly_or_not_at_all(index):
    from silkscreen.kicadlib.resolve import resolve_part

    assert resolve_part(index, "AMS1117-3.3").lib_id == "Regulator_Linear:AMS1117-3.3"
    assert resolve_part(index, "STM32F103C8T6").lib_id == "MCU_ST_STM32F1:STM32F103C8Tx"
    # Similar is not the same part: no fuzzy binding of a pinout.
    assert resolve_part(index, "AMS1117-3.3-XYZ") is None
    assert resolve_part(index, "U1") is None


@needs_symbols
def test_hallucinated_pin_numbers_are_corrected_to_the_library(index):
    """The model's usual mistake: right names, wrong numbers."""
    from silkscreen.kicadlib.resolve import check_pins, resolve_part

    entry = resolve_part(index, "AMS1117-3.3")
    lib = {name: number for number, name, _ in entry.pins}
    check = check_pins(entry, "AMS1117-3.3", {"VIN": "1", "GND": "3", "VOUT": "2"})
    assert check.errors == []
    assert check.pins == {"VIN": lib["VI"], "GND": lib["GND"], "VOUT": lib["VO"]}
    assert len(check.notes) == 2


@needs_symbols
def test_a_pin_the_part_does_not_have_comes_back_with_the_real_pinout(index):
    from silkscreen.kicadlib.resolve import check_pins, resolve_part

    entry = resolve_part(index, "AMS1117-3.3")
    check = check_pins(entry, "AMS1117-3.3", {"EN": "4", "GND": "1"})
    assert len(check.errors) == 1
    assert "EN=4" in check.errors[0] and "1 GND" in check.errors[0]


@needs_symbols
def test_a_correct_number_under_another_spelling_is_trusted(index):
    """An NE555's RESET=4 is KiCad's ~{RST}; CV=5 is CONT. Both are right."""
    from silkscreen.kicadlib.resolve import check_pins, resolve_part

    entry = resolve_part(index, "NE555D")
    check = check_pins(entry, "NE555D", {"RESET": "4", "CV": "5", "GND": "1"})
    assert check.errors == [] and check.notes == []


@needs_symbols
@needs_footprints
def test_a_generated_board_uses_library_parts_with_corrected_pins(
    tmp_path, monkeypatch
):
    """End to end: wrong AMS1117 pin numbers corrected, real footprints emitted."""
    from kiutils.board import Board
    from silkscreen.agents import ScriptedModel, generate_pcb

    monkeypatch.setenv("SILKSCREEN_KICAD_LIBRARY", "1")
    spec = {
        "devices": {
            "AMS1117-3.3": {"pins": {"VIN": "1", "GND": "3", "VOUT": "2"}},
            "usb": {
                "kind": "connector",
                "package": "USB_C_Receptacle_Power",
                # The library symbol's CC pins are declared open: since
                # 2026-09-15 an undeclared open pin stays dangling for ERC.
                "pins": {"G1": "A12", "V1": "A9", "CC1": "A5", "CC2": "B5"},
                "no_connect": ["CC1", "CC2"],
            },
        },
        "passives": {
            "c_in": {"type": "capacitor", "value": "10uF"},
            "c_out": {"type": "capacitor", "value": "22uF"},
        },
        "nets": {
            "VIN": ["usb.V1", "AMS1117-3.3.VIN", "c_in.1"],
            "GND": ["usb.G1", "AMS1117-3.3.GND", "c_in.2", "c_out.2"],
            "+3V3": ["AMS1117-3.3.VOUT", "c_out.1"],
        },
    }
    model = ScriptedModel(
        by_marker={"designing a printed circuit board": json.dumps(spec)}
    )
    events = []
    out = tmp_path / "board.kicad_pcb"
    generate_pcb(
        model, "a 3.3V LDO", output=out, review=False, engine="sdk",
        on_event=events.append,
    )
    library = [e for e in events if e.get("event") == "propose.library"]
    assert library and library[0]["corrected"] == 2

    board = Board().from_file(str(out))
    reg = next(
        fp for fp in board.footprints if fp.libId.endswith("SOT-223-3_TabPin2")
    )
    assert reg.libId == "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
    nets = {p.number: (p.net.name if p.net else None) for p in reg.pads}
    # KiCad's AMS1117-3.3: 1 GND, 2 VO, 3 VI -- whatever the model wrote.
    assert nets["1"] == "GND" and nets["3"] == "VIN" and nets["2"] == "+3V3"
    assert any(m.path.endswith("SOT-223.step") for m in reg.models)

    # The schematic embeds KiCad's own symbol, readably, and agrees with it.
    sch = (tmp_path / "board.kicad_sch").read_text()
    assert '(symbol "Regulator_Linear:AMS1117-3.3"' in sch
    assert "(show_name)" not in sch  # kiutils' misread of KiCad 10's "no"
    footprint_props = re.findall(
        r'\(property "Footprint" [^\n]*\n\s*\(effects[^\n]*', sch
    )
    assert footprint_props and all("hide" in p for p in footprint_props)

    kicad_cli = shutil.which("kicad-cli") or (
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
        if Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli").exists()
        else None
    )
    if kicad_cli is None:
        return
    report = tmp_path / "erc.json"
    run = subprocess.run(
        [kicad_cli, "sch", "erc", "--severity-all", "--format", "json",
         "-o", str(report), str(tmp_path / "board.kicad_sch")],
        capture_output=True, text=True, timeout=180,
    )
    assert run.returncode == 0, run.stderr
    violations = [
        v for sheet in json.loads(report.read_text())["sheets"]
        for v in sheet["violations"]
    ]
    # A regulator's real power output plus a PWR_FLAG on the same rail was a
    # pin_to_pin error; the flag is now left off nets a power output drives.
    assert [v for v in violations if v["severity"] == "error"] == []


def test_a_connector_family_is_accepted_by_the_netlist_without_kicad():
    from silkscreen.netlist import parse_circuit_spec

    spec = parse_circuit_spec({
        "devices": {"J1": {"kind": "connector", "package": "JST_GH_4P",
                           "pins": {"VCC": "1", "GND": "4"}}},
        "passives": {}, "nets": {},
    })
    assert spec.devices[0].package == "JST_GH_4P"


@needs_footprints
def test_connector_families_resolve_to_installed_footprints_with_3d_bodies():
    # tscircuit's family + pinCount (props connector.ts), resolved by KLC name.
    from silkscreen.kicadlib.connectors import _has_model, resolve

    cases = {
        "JST_GH_4P": "Connector_JST:JST_GH_BM04B-GHS-TBT_1x04-1MP_P1.25mm_Vertical",
        "JST_XH_3P": "Connector_JST:JST_XH_B3B-XH-A_1x03_P2.50mm_Vertical",
        "TERMINAL_5.08_2P": "Connector_Phoenix_MSTB:PhoenixContact_MSTBA_2,5_2-G-5,08_1x02_P5.08mm_Horizontal",
        "IDC_2.54_10P": "Connector_IDC:IDC-Header_2x05_P2.54mm_Vertical",
    }
    for spec, lib_id in cases.items():
        assert resolve(spec, 0).lib_id == lib_id
    # KiCad ships no model for the horizontal XT60PW; the vertical one has a body.
    xt60 = resolve("XT60_2P", 2)
    assert _has_model(*xt60.lib_id.split(":"))
    assert resolve("IDC_2.54_9P", 0).symbol == "Connector_Generic:Conn_02x05_Odd_Even"
    assert resolve("XT60_4P", 4) is None


@needs_footprints
@needs_symbols
def test_a_board_with_library_connectors_faces_its_wire_entry_outward(monkeypatch):
    monkeypatch.delenv("SILKSCREEN_KICAD_LIBRARY", raising=False)
    from silkscreen.board import build_board
    from silkscreen.netlist import parse_circuit_spec

    spec = parse_circuit_spec({
        "devices": {
            "J_MOTOR": {"kind": "connector", "package": "TERMINAL_5.08_2P", "pins": {"A": "1", "B": "2"}},
            "J_SENSOR": {"kind": "connector", "package": "JST_GH_4P",
                         "pins": {"VCC": "1", "SDA": "2", "SCL": "3", "GND": "4"}},
        },
        "passives": {"R1": {"type": "resistor", "value": "4k7"}},
        "nets": {"A": ["J_MOTOR.A", "R1.1"], "SDA": ["J_SENSOR.SDA", "R1.2"],
                 "GND": ["J_MOTOR.B", "J_SENSOR.GND"]},
    })
    board = build_board(spec, time_limit_s=5)
    motor = next(p for p in board.parts if p.footprint.name.startswith("PhoenixContact"))
    assert motor.footprint.library is not None
    # The mouth faces the solver's bottom edge: the part's box starts at y=0.
    assert motor.y_nm <= min(p.y_nm for p in board.parts)
