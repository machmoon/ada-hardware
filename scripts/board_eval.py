"""Score the boards Ada lays out, the way a fab or a reviewing engineer would.

    python scripts/board_eval.py                    # scoreboard, writes board_eval.json
    python scripts/board_eval.py --compare old.json # and the delta against a saved run

The unit tests check that each function does what its author meant. None of
them asks whether the *product* got better: whether a board passes KiCad's own
ERC, DRC and schematic parity, how much of it routed, how big it is, and how
far each decoupling cap sits from its pin. This does, on fixed known-good
circuits, so a change to placement or routing shows up as a number.

The circuits are ``scripts/design_quality.py``'s hand-checked selftest circuits
plus the two-chip decoupling board. Each goes through the real
:func:`silkscreen.agents.generate_pcb` -- KiCad library, placement, schematic,
routing -- with a scripted model that answers that circuit, so no key is needed
and only the deterministic engine is being scored. Proposal quality (does the
model pick the right circuit) is ``design_quality.py``'s job and costs money.

The metrics follow PCBWorld (arXiv 2607.05915): DRC and ERC violations, routing
completion and wirelength, plus board area. KiCad is the authority, so without
``kicad-cli`` the KiCad columns read "n/a", never 0.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [
    str(ROOT / "engine"), str(ROOT / "engine" / "tests"), str(ROOT / "scripts")
]

from design_quality import _SELFTEST, spec_as_dict  # noqa: E402
from silkscreen.agents import generate_pcb  # noqa: E402
from silkscreen.agents.model import ScriptedModel  # noqa: E402
from silkscreen.units import to_mm  # noqa: E402
from test_board_decoupling import _loop_mm, _two_chip_spec  # noqa: E402

KICAD_CLI = (
    shutil.which("kicad-cli")
    or "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
)


#: The hard case: what an engineer means by "an ESP32 dev board". USB-C in,
#: CH340C serial, AMS1117 3.3 V, EN RC reset and a BOOT button, a power LED,
#: and a decoupling cap on every supply pin. Pin numbers are KiCad's own
#: (RF_Module:ESP32-WROOM-32E, Interface_USB:CH340C, the USB-C 16P receptacle).
ESP32_DEVBOARD = {
    "devices": {
        "j_usb": {"kind": "connector", "package": "USB_C_Receptacle_USB2.0_16P",
                  "pins": {
            "GND_A1": "A1", "GND_B12": "B12", "GND_B1": "B1", "GND_A12": "A12",
            "VBUS_A4": "A4", "VBUS_B9": "B9", "VBUS_B4": "B4", "VBUS_A9": "A9",
            "DP_A": "A6", "DP_B": "B6", "DN_A": "A7", "DN_B": "B7",
            "CC1": "A5", "CC2": "B5", "SBU1": "A8", "SBU2": "B8"},
            "no_connect": ["SBU1", "SBU2"]},
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
        # The modem-control pins are declared and left open, as on a board whose
        # EN/BOOT are buttons rather than DTR/RTS auto-reset: CTS/DSR/RI/DCD are
        # inputs with internal pull-ups, R232 is the optional RS232 level
        # enable. Without the declaration the in-loop ERC (verify/, 2026-09-15)
        # refuses the circuit and a scripted model has no repair to offer.
        "CH340C": {"pins": {"GND": "1", "TXD": "2", "RXD": "3", "V3": "4",
                            "UD_P": "5", "UD_M": "6", "CTS": "9", "DSR": "10",
                            "RI": "11", "DCD": "12", "DTR": "13", "RTS": "14",
                            "R232": "15", "VCC": "16"},
                   "no_connect": ["CTS", "DSR", "RI", "DCD", "DTR", "RTS", "R232"]},
        "ESP32-WROOM-32E": {"pins": {"GND": "1", "VDD": "2", "EN": "3", "IO0": "25",
                                     "IO2": "24", "RXD0": "34", "TXD0": "35"},
                            "no_connect": ["IO2"]},
        "SW_EN": {"kind": "switch", "package": "SW_SPST_TL3305A",
                  "pins": {"A": "1", "B": "2"}},
        "SW_BOOT": {"kind": "switch", "package": "SW_SPST_TL3305A",
                    "pins": {"A": "1", "B": "2"}},
    },
    "passives": {
        "R_CC1": {"type": "resistor", "value": "5k1"},
        "R_CC2": {"type": "resistor", "value": "5k1"},
        "C_VBUS": {"type": "capacitor", "value": "10uF"},
        "C_3V3": {"type": "capacitor", "value": "22uF"},
        "C_ESP": {"type": "capacitor", "value": "100nF"},
        "C_CH": {"type": "capacitor", "value": "100nF"},
        "C_V3": {"type": "capacitor", "value": "100nF"},
        "R_EN": {"type": "resistor", "value": "10k"},
        "C_EN": {"type": "capacitor", "value": "1uF"},
        "R_BOOT": {"type": "resistor", "value": "10k"},
        "R_LED": {"type": "resistor", "value": "1k"},
        "D_PWR": {"type": "diode", "value": "green LED"},
    },
    "nets": {
        "VBUS": ["j_usb.VBUS_A4", "j_usb.VBUS_B9", "j_usb.VBUS_B4", "j_usb.VBUS_A9",
                 "AMS1117-3.3.VIN", "C_VBUS.1"],
        "GND": ["j_usb.GND_A1", "j_usb.GND_B12", "j_usb.GND_B1", "j_usb.GND_A12",
                "AMS1117-3.3.GND", "CH340C.GND", "ESP32-WROOM-32E.GND", "R_CC1.2",
                "R_CC2.2", "C_VBUS.2", "C_3V3.2", "C_ESP.2", "C_CH.2", "C_V3.2",
                "C_EN.2", "SW_EN.B", "SW_BOOT.B", "D_PWR.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "C_3V3.1", "CH340C.VCC", "C_CH.1",
                 "ESP32-WROOM-32E.VDD", "C_ESP.1", "R_EN.1", "R_BOOT.1", "R_LED.1"],
        "CH_V3": ["CH340C.V3", "C_V3.1"],
        "USB_DP": ["j_usb.DP_A", "j_usb.DP_B", "CH340C.UD_P"],
        "USB_DM": ["j_usb.DN_A", "j_usb.DN_B", "CH340C.UD_M"],
        "CC1": ["j_usb.CC1", "R_CC1.1"],
        "CC2": ["j_usb.CC2", "R_CC2.1"],
        "EN": ["ESP32-WROOM-32E.EN", "R_EN.2", "C_EN.1", "SW_EN.A"],
        "BOOT": ["ESP32-WROOM-32E.IO0", "R_BOOT.2", "SW_BOOT.A"],
        "LED": ["R_LED.2", "D_PWR.1"],
        "ESP_RX": ["ESP32-WROOM-32E.RXD0", "CH340C.TXD"],
        "ESP_TX": ["ESP32-WROOM-32E.TXD0", "CH340C.RXD"],
    },
}


def cases() -> dict[str, dict]:
    out = {name: spec for name, spec in _SELFTEST.items()}
    out["two_chip_usb"] = spec_as_dict(_two_chip_spec())
    out["esp32_devboard"] = ESP32_DEVBOARD
    return out


def kicad_counts(pcb: Path, sch: Path) -> dict[str, int | str]:
    if not Path(KICAD_CLI).exists():
        return {"erc_errors": "n/a", "drc_errors": "n/a", "unconnected": "n/a",
                "parity": "n/a"}

    def run(*args) -> dict:
        report = pcb.with_suffix(f".{args[0]}{len(args)}.json")
        subprocess.run([KICAD_CLI, *args, "--format", "json", "-o", str(report)],
                       capture_output=True, check=False)
        return json.loads(report.read_text()) if report.exists() else {}

    erc = run("sch", "erc", "--severity-error", str(sch))
    drc = run("pcb", "drc", "--severity-error", "--schematic-parity", "--refill-zones",
              str(pcb))
    return {
        "erc_errors": sum(len(s.get("violations", [])) for s in erc.get("sheets", [])),
        "drc_errors": len(drc.get("violations", [])),
        "unconnected": len(drc.get("unconnected_items", [])),
        "parity": len(drc.get("schematic_parity", [])),
    }


def score(name: str, spec: dict, workdir: Path) -> dict:
    model = ScriptedModel(responses=[json.dumps(spec)] * 4)
    pcb = workdir / name / f"{name}.kicad_pcb"
    started = time.monotonic()
    result = generate_pcb(model, f"eval circuit {name}", output=pcb, review=False,
                          plan=False, max_repairs=0, time_limit_s=20, engine="sdk")
    seconds = time.monotonic() - started
    w, h = result.board.size_mm
    route = result.route
    loops = _loop_mm(result.board, result.spec)
    row = {
        "parts": result.spec.part_count(),
        "nets": result.spec.net_count(),
        "area_mm2": round(w * h, 1),
        "solver": result.board.solver_status,
        "completion": round(route.completion, 3) if route else None,
        "unrouted": sorted(route.unrouted) if route else None,
        "copper_mm": round(to_mm(route.routed_length_nm), 1) if route else None,
        "vias": len(route.vias) if route else None,
        "decoupling_loop_mm": round(sum(loops.values()), 1) if loops else None,
        "seconds": round(seconds, 1),
    }
    row.update(kicad_counts(pcb, pcb.with_suffix(".kicad_sch")))
    return row


COLUMNS = ["parts", "area_mm2", "completion", "copper_mm", "vias", "decoupling_loop_mm",
           "erc_errors", "drc_errors", "unconnected", "parity", "seconds"]


def print_table(rows: dict[str, dict], before: dict[str, dict] | None) -> None:
    print(f"{'case':<14}" + "".join(f"{c:>20}" for c in COLUMNS))
    for name, row in rows.items():
        cells = []
        for c in COLUMNS:
            v, old = row.get(c), (before or {}).get(name, {}).get(c)
            text = "-" if v is None else str(v)
            numbers = isinstance(v, (int, float)) and isinstance(old, (int, float))
            if numbers and v != old:
                text += f" ({v - old:+.3g})"
            cells.append(f"{text:>20}")
        print(f"{name:<14}" + "".join(cells))
        if row.get("unrouted"):
            print(f"{'':<14}unrouted: {', '.join(row['unrouted'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--compare", type=Path, help="a previous board_eval.json")
    parser.add_argument("-o", "--output", type=Path, default=Path("board_eval.json"))
    parser.add_argument("--keep", type=Path, help="keep the KiCad projects here")
    parser.add_argument("--only", nargs="*", help="case names to run")
    args = parser.parse_args()
    # The product path: KiCad's installed library, unless explicitly switched off.
    os.environ.pop("SILKSCREEN_KICAD_LIBRARY", None)
    workdir = args.keep or Path(tempfile.mkdtemp(prefix="board-eval-"))
    rows = {n: score(n, s, workdir) for n, s in cases().items()
            if not args.only or n in args.only}
    before = json.loads(args.compare.read_text())["cases"] if args.compare else None
    print_table(rows, before)
    args.output.write_text(
        json.dumps({"kicad_cli": KICAD_CLI, "cases": rows}, indent=2)
    )
    failing = [n for n, r in rows.items() if any(isinstance(r[k], int) and r[k] for k in
               ("erc_errors", "drc_errors", "unconnected", "parity"))]
    print(f"\nwrote {args.output}; KiCad-clean: {len(rows) - len(failing)}/{len(rows)}"
          + (f" (failing: {', '.join(failing)})" if failing else ""))
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
