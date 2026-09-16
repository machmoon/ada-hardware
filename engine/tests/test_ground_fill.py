"""The ground fill: two copper pours instead of a routed ground net.

Pat, 2026-09-15: "most boards you do a fill for GND on both layers". The
router used to try to lay GND as tracks and, on a 40-part board, ran out of
search budget behind the signal nets. Now every ground-class net is carried
by a zone on each copper layer; the router never touches it, the emitter
draws the zones, and KiCad fills them when it can. The gated test proves the
part that matters: after KiCad fills the pour, DRC reports nothing
unconnected on a board whose ground was never routed.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from silkscreen.board import (
    board_pads,
    build_board,
    emit_kicad_pcb,
    route_board,
    write_board,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.verify.kicad import kicad_cli_path

REGULATOR = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "Cin": {"type": "capacitor", "value": "10uF"},
        "Cout": {"type": "capacitor", "value": "22uF"},
        "Rload": {"type": "resistor", "value": "330"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "Cin.1"],
        "GND": ["AMS1117-3.3.GND", "Cin.2", "Cout.2", "Rload.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "Cout.1", "Rload.1"],
    },
}

needs_kicad = pytest.mark.skipif(
    kicad_cli_path() is None, reason="kicad-cli not installed"
)


@pytest.fixture(scope="module")
def routed():
    board = build_board(parse_circuit_spec(json.dumps(REGULATOR)), time_limit_s=5.0)
    result = route_board(board)
    return board, result


def test_ground_is_filled_not_routed(routed):
    board, result = routed
    assert result.filled == ["GND"]
    assert board.filled_nets == ["GND"]
    assert "GND" not in result.routed and "GND" not in result.unrouted
    assert result.unrouted == {}
    # Ground carries no *routed* copper: its only tracks are fan-outs, each a
    # straight run from a GND pad centre to a GND via (plan_fanout, placed
    # before routing, the FreeRouting order).
    assert not any(t.net == "GND" for t in result.tracks)
    pads = {(p.x_nm, p.y_nm) for p in board_pads(board) if p.net == "GND"}
    vias = {(v.x_nm, v.y_nm) for v in board.vias if v.net == "GND"}
    fanouts = [t for t in board.tracks if t.net == "GND"]
    assert fanouts
    for t in fanouts:
        assert (t.start_x_nm, t.start_y_nm) in pads
        assert (t.end_x_nm, t.end_y_nm) in vias
        assert t.start_x_nm == t.end_x_nm or t.start_y_nm == t.end_y_nm
    assert "1 by copper fill" in result.summary()


def test_ground_is_stitched_and_every_via_keeps_clearance(routed):
    """Stitching vias tie the two pours; each one is checked here with
    independent geometry against every pad and every foreign track."""
    board, _ = routed
    gnd_vias = [v for v in board.vias if v.net == "GND"]
    assert board.stitching_vias == len(gnd_vias) > 0
    need = 0.3e6 + 0.3e6  # via radius + pour clearance, in nm
    for v in gnd_vias:
        assert 0 < v.x_nm < board.width_nm and 0 < v.y_nm < board.height_nm
        for p in board_pads(board):
            dx = max(abs(v.x_nm - p.x_nm) - p.w_nm / 2, 0)
            dy = max(abs(v.y_nm - p.y_nm) - p.h_nm / 2, 0)
            assert (dx * dx + dy * dy) ** 0.5 >= need - 1, (v, p.ref, p.number)
        for t in board.tracks:
            if t.net == "GND":
                continue
            ax, ay, bx, by = t.start_x_nm, t.start_y_nm, t.end_x_nm, t.end_y_nm
            span = (bx - ax) ** 2 + (by - ay) ** 2
            dot = (v.x_nm - ax) * (bx - ax) + (v.y_nm - ay) * (by - ay)
            u = max(0, min(1, dot / span)) if span else 0
            cx, cy = ax + u * (bx - ax), ay + u * (by - ay)
            gap = ((v.x_nm - cx) ** 2 + (v.y_nm - cy) ** 2) ** 0.5
            assert gap >= need + t.width_nm / 2 - 1, (v, t)


def test_surface_pads_on_the_pour_connect_solidly(routed):
    """KiCad's pad ``zone_connect 2``: no thermal spokes to starve."""
    board, _ = routed
    text = emit_kicad_pcb(board)
    pads = [line for line in text.splitlines() if line.lstrip().startswith("(pad ")]
    gnd = [line for line in pads if '"GND")' in line]
    assert gnd and all("(zone_connect 2)" in line for line in gnd if " smd " in line)
    assert not any("(zone_connect" in line for line in pads if '"GND")' not in line)


def test_the_emitter_draws_one_zone_per_copper_layer(routed):
    board, _ = routed
    text = emit_kicad_pcb(board)
    zones = [line for line in text.splitlines() if line.lstrip().startswith("(zone ")]
    assert len(zones) == 2
    assert all('(net_name "GND")' in z for z in zones)
    assert sorted(z.split('(layer "')[1].split('"')[0] for z in zones) == [
        "B.Cu",
        "F.Cu",
    ]
    net_index = text.split('"GND")')[0].rsplit("(net ", 1)[1].strip()
    assert all(f"(zone (net {net_index})" in z for z in zones)
    # The pour covers the outline: its corners are the Edge.Cuts corners.
    edge = [
        line for line in text.splitlines() if "gr_line" in line and "Edge.Cuts" in line
    ]
    first_corner = edge[0].split("(start ")[1].split(")")[0]
    assert f"(xy {first_corner})" in zones[0]


def test_ground_fill_can_be_switched_off():
    board = build_board(parse_circuit_spec(json.dumps(REGULATOR)), time_limit_s=5.0)
    result = route_board(board, ground_fill=False)
    assert result.filled == [] and board.filled_nets == []
    assert board.stitching_vias == 0
    assert "GND" in result.routed or "GND" in result.unrouted
    assert "(zone " not in emit_kicad_pcb(board)


@needs_kicad
def test_drc_with_the_pour_filled_sees_ground_connected(routed, tmp_path):
    """The claim behind the feature, checked by the authority.

    The file is written with the zones unfilled (see ``write_board``); KiCad
    computes the fill for the check with ``--refill-zones`` and reports no
    unconnected item on a board whose GND was never routed. The verifier's
    :func:`drc` runs the same flag.
    """
    board, _ = routed
    path = write_board(board, tmp_path / "b.kicad_pcb")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.kicad_pcb"]
    assert "(filled_polygon" not in path.read_text()
    report = tmp_path / "drc.json"
    subprocess.run(
        [
            kicad_cli_path(),
            "pcb",
            "drc",
            "--severity-error",
            "--refill-zones",
            "--format",
            "json",
            "-o",
            str(report),
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    drc = json.loads(report.read_text())
    assert drc["violations"] == []
    assert drc["unconnected_items"] == []
    # kicad-cli itself leaves a .kicad_prl behind; the verifier does not.
    (tmp_path / "b.kicad_prl").unlink()
    report.unlink()
    from silkscreen.verify import drc as verify_drc

    assert verify_drc(path).ok
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.kicad_pcb"]
