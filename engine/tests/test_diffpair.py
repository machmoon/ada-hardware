"""Coupled differential pairs: one path, two traces, one constant gap.

The property that makes a pair a pair is geometric, so these tests measure
it with geometry written here: every point on the coupled section of one
trace is exactly ``width + gap`` from the other trace's centreline. Nothing
below asks :mod:`silkscreen.diffpair` how far apart its traces are.
"""

from __future__ import annotations

import json
import math

import pytest
from silkscreen.diffpair import (
    DIFF_PAIR_GAP_NM,
    DIFF_PAIR_WIDTH_NM,
    CoupledRoute,
    PairFailure,
    route_pair,
)
from silkscreen.packing import Layer
from silkscreen.routing import DEFAULT_ROUTE_CLEARANCE_NM, RoutePad

PITCH = DIFF_PAIR_WIDTH_NM + DIFF_PAIR_GAP_NM
BOUNDS = {"min_x_nm": 0, "min_y_nm": 0, "max_x_nm": 30_000_000, "max_y_nm": 20_000_000}


def _pad(net, x_mm, y_mm, ref, **kw):
    return RoutePad(net=net, x_nm=round(x_mm * 1e6), y_nm=round(y_mm * 1e6),
                    w_nm=kw.pop("w", 300_000), h_nm=kw.pop("h", 1_000_000),
                    layer=kw.pop("layer", Layer.TOP), ref=ref, **kw)


def _ends(**kw):
    """U1's D+/D- at x=4 mm, 1.27 mm apart; U2's at x=26 mm, lower down."""
    p = (_pad("DP", 4, 10.635, "U1", **kw), _pad("DP", 26, 5.635, "U2"))
    n = (_pad("DM", 4, 9.365, "U1"), _pad("DM", 26, 4.365, "U2"))
    return p, n


def _pt_seg(px, py, t):
    ax, ay, bx, by = t.start_x_nm, t.start_y_nm, t.end_x_nm, t.end_y_nm
    vx, vy = bx - ax, by - ay
    span = vx * vx + vy * vy
    u = 0 if span == 0 else max(0, min(1, ((px - ax) * vx + (py - ay) * vy) / span))
    return math.hypot(px - ax - u * vx, py - ay - u * vy)


def _gap_profile(route: CoupledRoute, net: str, other: str, step_nm=50_000):
    """Distance from points along ``net``'s copper to ``other``'s copper."""
    mine = [t for t in route.tracks if t.net == net]
    theirs = [t for t in route.tracks if t.net == other]
    out = []
    for t in mine:
        n = max(1, round(t.length_nm / step_nm))
        for k in range(n + 1):
            x = t.start_x_nm + (t.end_x_nm - t.start_x_nm) * k / n
            y = t.start_y_nm + (t.end_y_nm - t.start_y_nm) * k / n
            out.append(min(_pt_seg(x, y, o) for o in theirs))
    return out


def test_an_open_board_routes_the_pair_at_a_constant_pitch():
    p, n = _ends()
    route = route_pair(p, n, pads=[*p, *n], **BOUNDS)
    assert isinstance(route, CoupledRoute), route
    profile = _gap_profile(route, "DP", "DM")
    # Along the coupled section the centre-to-centre spacing is the pitch,
    # to the nanometres rounding leaves; that section is most of the route.
    at_pitch = [d for d in profile if abs(d - PITCH) <= 5]
    assert len(at_pitch) / len(profile) > 0.6
    # And nowhere do the two traces come closer than the pitch allows.
    assert min(profile) >= PITCH - 5
    assert route.coupled_nm > 15_000_000
    assert route.gap_nm == DIFF_PAIR_GAP_NM
    assert route.as_dict()["coupled_ratio"] == round(route.coupled_ratio, 3)


@pytest.mark.parametrize("with_wall", [False, True])
def test_the_gap_holds_on_the_slant_as_well_as_the_straight(with_wall):
    """Every coupled segment -- 45-degree ones included -- keeps the pitch
    along its whole length. Only the pad leads at each end may differ."""
    p, n = _ends()
    extra = [RoutePad(net="VCC", x_nm=15_000_000, y_nm=8_000_000, w_nm=2_000_000,
                      h_nm=8_000_000, layer=Layer.TOP, ref="U3")] if with_wall else []
    route = route_pair(p, n, pads=[*p, *n, *extra], **BOUNDS)
    mine = [t for t in route.coupled_tracks if t.net == "DP"]
    theirs = [t for t in route.tracks if t.net == "DM"]
    assert mine and set(mine) <= set(route.tracks)
    # Within pitch * tan(22.5 deg) of a 45-degree bend the outer trace's
    # mitre is farther from the inner one than the pitch, up to
    # pitch / cos(22.5 deg) at the apex: that is the geometry of any mitred
    # pair. Everywhere else on a run the spacing is the pitch.
    near_bend = PITCH * math.tan(math.radians(22.5))
    apex = PITCH / math.cos(math.radians(22.5))
    diagonals = checked = 0
    for t in mine:
        dx, dy = t.end_x_nm - t.start_x_nm, t.end_y_nm - t.start_y_nm
        diagonals += bool(dx and dy)
        for k in range(1, 40):
            along = t.length_nm * k / 40
            x, y = t.start_x_nm + dx * k / 40, t.start_y_nm + dy * k / 40
            gap = min(_pt_seg(x, y, o) for o in theirs)
            assert PITCH - 2 <= gap <= apex + 2, (t, k, gap)
            if near_bend + 2 < along < t.length_nm - near_bend - 2:
                assert abs(gap - PITCH) <= 2, (t, k, gap)
                checked += 1
    assert diagonals >= 1 and checked > 50


def test_both_traces_start_and_end_on_their_own_pads():
    p, n = _ends()
    route = route_pair(p, n, pads=[*p, *n], **BOUNDS)
    for net, (a, b) in (("DP", p), ("DM", n)):
        tracks = [t for t in route.tracks if t.net == net]
        points = {(t.start_x_nm, t.start_y_nm) for t in tracks} | {
            (t.end_x_nm, t.end_y_nm) for t in tracks
        }
        assert (a.x_nm, a.y_nm) in points and (b.x_nm, b.y_nm) in points
        # A chain: every interior point is shared by exactly two segments.
        ends = [pt for t in tracks for pt in
                ((t.start_x_nm, t.start_y_nm), (t.end_x_nm, t.end_y_nm))]
        odd = {pt for pt in ends if ends.count(pt) % 2}
        assert odd == {(a.x_nm, a.y_nm), (b.x_nm, b.y_nm)}


def test_an_obstacle_on_the_straight_line_is_cleared_and_the_pair_stays_coupled():
    p, n = _ends()
    wall = RoutePad(net="VCC", x_nm=15_000_000, y_nm=8_000_000, w_nm=2_000_000,
                    h_nm=8_000_000, layer=Layer.TOP, ref="U3")
    route = route_pair(p, n, pads=[*p, *n, wall], **BOUNDS)
    assert isinstance(route, CoupledRoute), route
    for t in route.tracks:
        # Independent box distance, every sampled point of every segment.
        for k in range(21):
            x = t.start_x_nm + (t.end_x_nm - t.start_x_nm) * k / 20
            y = t.start_y_nm + (t.end_y_nm - t.start_y_nm) * k / 20
            dx = max(abs(x - wall.x_nm) - wall.w_nm / 2, 0)
            dy = max(abs(y - wall.y_nm) - wall.h_nm / 2, 0)
            edge = math.hypot(dx, dy) - t.width_nm / 2
            assert edge >= DEFAULT_ROUTE_CLEARANCE_NM - 1
    assert min(_gap_profile(route, "DP", "DM")) >= PITCH - 5


def test_a_blocked_corridor_is_a_named_failure():
    p, n = _ends()
    wall = RoutePad(net="VCC", x_nm=15_000_000, y_nm=10_000_000, w_nm=1_000_000,
                    h_nm=20_000_000, layer=Layer.TOP, ref="U3")
    found = route_pair(p, n, pads=[*p, *n, wall], **BOUNDS)
    assert isinstance(found, PairFailure)
    assert "corridor" in found.reason


@pytest.mark.parametrize(
    ("kw", "words"),
    [({"through_hole": True}, "through-hole"), ({"layer": Layer.BOTTOM}, "layers")],
)
def test_what_is_not_modelled_is_refused_in_words(kw, words):
    p, n = _ends(**kw)
    found = route_pair(p, n, pads=[*p, *n], **BOUNDS)
    assert isinstance(found, PairFailure)
    assert words in found.reason


def test_the_project_file_declares_the_same_pair_geometry():
    from silkscreen.schematic import emit_kicad_pro

    pro = json.loads(emit_kicad_pro(
        "x", net_classes=[{"name": "USB", "nets": ["DP", "DM"], "impedance_ohms": 90}]
    ))
    cls = pro["net_settings"]["classes"][1]
    assert cls["diff_pair_width"] == DIFF_PAIR_WIDTH_NM / 1e6
    assert cls["diff_pair_gap"] == DIFF_PAIR_GAP_NM / 1e6


#: A USB-C receptacle feeding a CH340C, the USB half of the ESP32 eval board
#: (``scripts/board_eval.py``), pin numbers KiCad's own.
USB_SERIAL = {
    "devices": {
        "j_usb": {
            "kind": "connector", "package": "USB_C_Receptacle_USB2.0_16P", "pins": {
            "GND_A1": "A1", "GND_B12": "B12", "GND_B1": "B1", "GND_A12": "A12",
            "VBUS_A4": "A4", "VBUS_B9": "B9", "VBUS_B4": "B4", "VBUS_A9": "A9",
            "DP_A": "A6", "DP_B": "B6", "DN_A": "A7", "DN_B": "B7",
            "CC1": "A5", "CC2": "B5", "SBU1": "A8", "SBU2": "B8"},
            "no_connect": ["SBU1", "SBU2"]},
        "CH340C": {"pins": {"GND": "1", "TXD": "2", "RXD": "3", "V3": "4",
                            "UD_P": "5", "UD_M": "6", "VCC": "16"},
                   "no_connect": ["TXD", "RXD"]},
    },
    "passives": {
        "R_CC1": {"type": "resistor", "value": "5k1"},
        "R_CC2": {"type": "resistor", "value": "5k1"},
        "C_CH": {"type": "capacitor", "value": "100nF"},
        "C_V3": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VBUS": ["j_usb.VBUS_A4", "j_usb.VBUS_B9", "j_usb.VBUS_B4", "j_usb.VBUS_A9",
                 "CH340C.VCC", "C_CH.1"],
        "GND": ["j_usb.GND_A1", "j_usb.GND_B12", "j_usb.GND_B1", "j_usb.GND_A12",
                "CH340C.GND", "R_CC1.2", "R_CC2.2", "C_CH.2", "C_V3.2"],
        "CH_V3": ["CH340C.V3", "C_V3.1"],
        "USB_DP": ["j_usb.DP_A", "j_usb.DP_B", "CH340C.UD_P"],
        "USB_DM": ["j_usb.DN_A", "j_usb.DN_B", "CH340C.UD_M"],
        "CC1": ["j_usb.CC1", "R_CC1.1"],
        "CC2": ["j_usb.CC2", "R_CC2.1"],
    },
}


def test_a_generated_usb_board_carries_a_coupled_pair():
    """The whole path: a USB-C to CH340 circuit, placed and routed. The
    connector's duplicated pins are joined first, which leaves each net two
    terminals, and the pair is then laid coupled."""
    from silkscreen.board import build_board, route_board
    from silkscreen.netlist import parse_circuit_spec

    board = build_board(parse_circuit_spec(json.dumps(USB_SERIAL)), time_limit_s=5.0)
    result = route_board(board)
    assert board.coupled_pairs, [w for w in result.warnings if "coupled" in w]
    pair = board.coupled_pairs[0]
    assert pair["nets"] == ["USB_DP", "USB_DM"]
    assert pair["gap_mm"] == DIFF_PAIR_GAP_NM / 1e6
    assert pair["coupled_mm"] > 0
    assert {"USB_DP", "USB_DM"} <= set(result.routed)
    assert not {"USB_DP", "USB_DM"} & set(result.unrouted)
