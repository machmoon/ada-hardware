"""The case drawings: every number measured from the B-rep, every line on A4.

Expected values are computed inline from the raw literals -- a plate with a
drilled hole, a boss and a slot of known size, and the hole positions of the
case test board through the same frame math ``test_enclosure_cad.py`` writes out
by hand -- never read back from ``cad.py`` or from ``drawing.py`` itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from silkscreen.enclosure.board_shape import board_envelope
from silkscreen.enclosure.cad import build_enclosure, kernel_available
from test_enclosure_cad import BOARD, HOLES, LIP_CLEARANCE, WALL, _board_text, make_spec

needs_build123d = pytest.mark.skipif(
    not kernel_available(), reason="build123d not installed (pip install -e '.[cad]')"
)

A4_W, A4_H = 297.0, 210.0


def _plate():
    """60 x 40 x 5 plate: a 3.2 mm hole through at (15, 20), a 6 mm boss 4 mm
    tall at (45, 20), and a 10 x 4 slot through, centred at (30, 32)."""
    from build123d import Align, Box, Cylinder, Pos

    low = (Align.MIN, Align.MIN, Align.MIN)
    plate = Box(60, 40, 5, align=low)
    plate -= Pos(15, 20, 0) * Cylinder(
        1.6, 5, align=(Align.CENTER, Align.CENTER, Align.MIN)
    )
    plate += Pos(45, 20, 5) * Cylinder(
        3, 4, align=(Align.CENTER, Align.CENTER, Align.MIN)
    )
    plate -= Pos(25, 30, 0) * Box(10, 4, 5, align=low)
    return plate


@needs_build123d
def test_measure_finds_the_hole_the_boss_and_the_slot_at_their_true_sizes():
    from silkscreen.enclosure.drawing import measure

    m = measure(_plate())
    assert m.size_mm == pytest.approx((60, 40, 9))
    bores = [h for h in m.holes if h.kind == "bore"]
    bosses = [h for h in m.holes if h.kind == "boss"]
    assert len(bores) == 1 and len(bosses) == 1
    assert bores[0].diameter_mm == pytest.approx(3.2)
    assert bores[0].centre_mm[:2] == pytest.approx((15, 20))
    assert bores[0].span_mm == pytest.approx((0, 5))
    assert bosses[0].diameter_mm == pytest.approx(6)
    assert bosses[0].centre_mm[:2] == pytest.approx((45, 20))
    assert bosses[0].span_mm == pytest.approx((5, 9))
    slots = [o for o in m.outlines if o.axis == "Z"]
    assert len(slots) == 1  # top and bottom faces: one slot
    assert (slots[0].width_mm, slots[0].height_mm) == pytest.approx((10, 4))
    assert slots[0].centre_mm == pytest.approx((30, 32))
    assert slots[0].levels_mm == pytest.approx((0, 5))


@needs_build123d
def test_a_partial_cylinder_is_counted_never_drawn_as_a_hole():
    from build123d import Align, Box, fillet
    from silkscreen.enclosure.drawing import measure

    box = Box(20, 20, 10, align=(Align.MIN, Align.MIN, Align.MIN))
    rounded = fillet(box.edges().filter_by(lambda e: e.length == 10), radius=2)
    m = measure(rounded)
    assert m.holes == ()
    assert m.partial_cylinders == 4
    assert "not dimensioned" in m.schedule()[-1]


@needs_build123d
def test_the_case_bores_sit_where_the_board_holes_are(tmp_path: Path):
    from silkscreen.enclosure.drawing import measure

    board = tmp_path / "holes.kicad_pcb"
    board.write_text(_board_text(holes=True), encoding="utf-8")
    model = build_enclosure(make_spec(mount="holes"), board_envelope(board))
    m = measure(model.base)
    origin = model.base.bounding_box().min
    expected = sorted(
        (
            WALL + LIP_CLEARANCE + (hx - BOARD["x0"]) - origin.X,
            WALL + LIP_CLEARANCE + (BOARD["y1"] - hy) - origin.Y,
        )
        for hx, hy in HOLES.values()
    )
    bores = sorted(
        h.centre_mm[:2] for h in m.holes if h.kind == "bore" and h.axis == "Z"
    )
    assert len(bores) == len(expected)
    for got, want in zip(bores, expected, strict=True):
        assert got == pytest.approx(want, abs=1e-3)
    # The USB cutout is one outline through the left wall, not one per face,
    # and its clear size is the smaller of the chamfered mouth and the throat.
    through_x = [o for o in m.outlines if o.axis == "X"]
    assert len(through_x) == 1
    o = through_x[0]
    assert o.width_mm * o.height_mm <= o.largest_mm[0] * o.largest_mm[1]
    assert len(o.levels_mm) == 2


@needs_build123d
def test_the_sheet_stays_on_a4_and_carries_both_line_layers(tmp_path: Path):
    from silkscreen.enclosure.drawing import compose, draw_part

    _, scale, groups = compose(_plate(), part="plate", title="t")
    assert scale == 1.0
    for name, shapes in groups.items():
        for shape in shapes:
            bb = shape.bounding_box()
            assert -A4_W / 2 <= bb.min.X and bb.max.X <= A4_W / 2, name
            assert -A4_H / 2 <= bb.min.Y and bb.max.Y <= A4_H / 2, name
    sheet = draw_part(_plate(), tmp_path / "plate.svg", part="plate", title="t")
    text = sheet.path.read_text(encoding="utf-8")
    width = float(re.search(r'width="([\d.]+)mm"', text).group(1))
    assert width <= A4_W
    assert 'id="Visible"' in text and 'id="Hidden"' in text
    # Curves were flattened before export: build123d's curve writer is what
    # made one lid take 68 s, so no cubic or arc path command may remain.
    assert not re.search(r' d="[^"]*[CQA]', text)


@needs_build123d
def test_a_part_too_big_for_a4_is_scaled_down_and_says_so(tmp_path: Path):
    from build123d import Box
    from silkscreen.enclosure.drawing import compose

    _, scale, groups = compose(Box(300, 200, 40), part="big", title="t")
    assert scale < 1.0
    for shapes in groups.values():
        for shape in shapes:
            bb = shape.bounding_box()
            assert -A4_W / 2 <= bb.min.X and bb.max.X <= A4_W / 2
