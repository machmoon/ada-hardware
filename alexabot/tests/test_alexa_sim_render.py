"""The board image on the card: ``cards.board_svg`` over the independent
``audit`` reader. Offline; the fixture is the engine's STM32 board."""

import re
from pathlib import Path

from alexabot import cards

FIXTURE = Path(__file__).resolve().parents[2] / "engine" / "tests" / "fixtures" \
    / "ref.kicad_pcb"


def test_board_svg_draws_every_part_as_a_data_ref_group():
    svg = cards.board_svg(FIXTURE)
    refs = re.findall(r'<g data-ref="([^"]+)" class="part">', svg)
    expected = re.findall(r'\(property "Reference" "([^"]+)"', FIXTURE.read_text())
    assert refs and sorted(refs) == sorted(expected)
    assert 'aria-label="The routed board"' in svg


def test_board_svg_has_no_legend_counts():
    svg = cards.board_svg(FIXTURE)
    assert 'id="legend"' not in svg
    assert "0 blocker" not in svg


def test_board_svg_has_no_script_foreignobject_or_event_handlers():
    svg = cards.board_svg(FIXTURE)
    assert "<script" not in svg.lower() and "foreignobject" not in svg.lower()
    assert not re.search(r"\son[a-z]+\s*=", svg, re.IGNORECASE)


def test_a_hostile_ref_is_escaped(tmp_path):
    text = FIXTURE.read_text().replace('(property "Reference" "U1"',
                                       '(property "Reference" "\\"><script>x"', 1)
    hostile = tmp_path / "hostile.kicad_pcb"
    hostile.write_text(text)
    svg = cards.board_svg(hostile)
    assert "<script" not in svg
    assert "&lt;script&gt;" in svg or "&quot;&gt;&lt;script&gt;" in svg
