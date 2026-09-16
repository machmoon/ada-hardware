"""Differential pairs through a reversible connector's interleaved pins.

A USB-C receptacle carries D- and D+ twice, in one row, as ``B7 A6 A7 B6``
(D-, D+, D-, D+). Measured 2026-09-16 on the ESP32 eval board: whichever leg
the router laid first took the only channel through the row, and the other
was never routed. :func:`silkscreen.board.plan_pair_joins` ties each pair's
copies with a loop round the pin ends -- one net on each side -- before
routing. These tests use a hand-made row with the connector's real pitch and
pad sizes, and check the loops with geometry computed here.
"""

from __future__ import annotations

from silkscreen.board import plan_pair_joins
from silkscreen.packing import Layer
from silkscreen.routing import RoutePad

PITCH = 500_000
Y = 9_065_000


def _row(nets: list[str]) -> list[RoutePad]:
    return [
        RoutePad(net=n, x_nm=5_000_000 + i * PITCH, y_nm=Y, w_nm=300_000,
                 h_nm=1_150_000, layer=Layer.TOP, ref="J1", number=f"P{i}")
        for i, n in enumerate(nets)
    ]


def test_interleaved_pairs_get_one_loop_each_on_opposite_sides():
    pads = _row(["CC1", "USB_DM", "USB_DP", "USB_DM", "USB_DP", "SBU"])
    tracks, seen = plan_pair_joins(pads, [("USB_DP", "USB_DM")])
    by_net: dict[str, list] = {}
    for t in tracks:
        by_net.setdefault(t.net, []).append(t)
    assert sorted(by_net) == ["USB_DM", "USB_DP"]
    sides = {}
    for net, legs in by_net.items():
        assert len(legs) == 3
        across = [t for t in legs if t.start_y_nm == t.end_y_nm]
        assert len(across) == 1
        sides[net] = across[0].start_y_nm - Y
        # The loop clears the pad ends by the pour clearance plus half a track.
        assert abs(sides[net]) == 1_150_000 // 2 + 300_000 + 100_000
    assert sides["USB_DM"] * sides["USB_DP"] < 0  # opposite sides
    # One copy of each net is handed to the router as a plain obstacle.
    hidden = [p for p in seen if p.net == "" and p.ref == "J1"]
    assert sorted(p.number for p in hidden) == ["P2", "P3"]
    # The copy each net keeps is outside the other net's loop.
    kept = {p.net: p.x_nm for p in seen if p.net in ("USB_DM", "USB_DP")}
    dm_xs = [p.x_nm for p in pads if p.net == "USB_DM"]
    assert not min(dm_xs) <= kept["USB_DP"] <= max(dm_xs)


def test_flip_swaps_the_sides():
    pads = _row(["USB_DM", "USB_DP", "USB_DM", "USB_DP"])
    plain, _ = plan_pair_joins(pads, [("USB_DP", "USB_DM")])
    flipped, _ = plan_pair_joins(pads, [("USB_DP", "USB_DM")], flip=True)

    def side(tracks, net):
        across = [t for t in tracks if t.net == net and t.start_y_nm == t.end_y_nm]
        return across[0].start_y_nm - Y

    assert side(plain, "USB_DM") == -side(flipped, "USB_DM")


def test_pairs_that_do_not_interleave_are_left_to_the_router():
    pads = _row(["USB_DP", "USB_DP", "USB_DM", "USB_DM"])
    tracks, seen = plan_pair_joins(pads, [("USB_DP", "USB_DM")])
    assert tracks == []
    assert seen == pads


def test_a_loop_that_would_graze_another_pad_is_not_drawn():
    pads = _row(["USB_DM", "USB_DP", "USB_DM", "USB_DP"])
    # A foreign pad sitting right where the upper loop would run.
    pads.append(RoutePad(net="VBUS", x_nm=5_750_000, y_nm=Y + 1_000_000,
                         w_nm=300_000, h_nm=300_000, layer=Layer.TOP, ref="J1",
                         number="X"))
    tracks, seen = plan_pair_joins(pads, [("USB_DP", "USB_DM")])
    tracks_flip, _ = plan_pair_joins(pads, [("USB_DP", "USB_DM")], flip=True)
    assert tracks == [] and tracks_flip == []
    assert seen == pads
