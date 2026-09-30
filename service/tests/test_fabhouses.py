"""The order panel's per-house block (``service/fabhouses.py``).

Driven through the engine's own ``fab_house_report`` over a stub board whose
only geometry is its size, so each expected price is computed here, inline,
from OSH Park's published rule rather than read back from the code under test.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

from silkscreen.fabhouse import fab_house_report
from silkscreen.order import OrderOptions
from silkscreen.units import mm

from service.fabhouses import BOUNDARY, fab_houses_block

MARGIN_MM = 2.0  # fabhouse.quote's default outline margin


def _board(w_mm: float, h_mm: float):
    return SimpleNamespace(width_nm=mm(w_mm), height_nm=mm(h_mm), tracks=[], vias=[])


def _block(w_mm: float, h_mm: float, quantity: int = 3) -> dict:
    report = fab_house_report(_board(w_mm, h_mm), OrderOptions(quantity=quantity))
    return fab_houses_block(report)


def _by_id(block: dict) -> dict:
    return {card["id"]: card for card in block["houses"]}


def _oshpark_cents(w_mm: float, h_mm: float, quantity: int, per_sq_in: int) -> int:
    area = ((w_mm + 2 * MARGIN_MM) / 25.4) * ((h_mm + 2 * MARGIN_MM) / 25.4)
    return round(per_sq_in * area * math.ceil(quantity / 3))


def test_oshpark_is_priced_by_its_published_rule_and_recommended():
    block = _block(41.4, 73.65)
    cards = _by_id(block)
    osh = cards["oshpark-2layer"]
    expected = _oshpark_cents(41.4, 73.65, 3, 500)
    assert osh["buildable"] is True and osh["blockers"] == []
    assert osh["price"]["total_cents"] == expected
    assert osh["price"]["boards"] == 3
    assert osh["price"]["text"] == f"3 boards, ${expected / 100:,.2f}"
    assert osh["price"]["basis"] == "published-rule"
    assert osh["price_note"] is None
    assert osh["lead_time_days"] == [9, 12]
    assert osh["quote_url"] == "https://oshpark.com/"
    # Super Swift costs twice as much, so the standard service is the pick.
    swift = cards["oshpark-2layer-swift"]
    assert swift["price"]["total_cents"] == _oshpark_cents(41.4, 73.65, 3, 1000)
    assert block["recommended"] == "oshpark-2layer"
    assert osh["recommended"] is True
    assert [c["id"] for c in block["houses"] if c["recommended"]] == ["oshpark-2layer"]
    assert "Lowest published price" in block["recommended_reason"]


def test_a_quantity_rounds_up_to_the_unit_the_house_sells():
    osh = _by_id(_block(20, 20, quantity=5))["oshpark-2layer"]
    assert osh["price"]["boards"] == 6
    assert osh["price"]["total_cents"] == _oshpark_cents(20, 20, 5, 500)


def test_an_unpriced_house_carries_no_number_at_all():
    """JLCPCB and PCBWay quote only through authenticated APIs."""
    cards = _by_id(_block(30, 30))
    for house_id, name in (("jlcpcb-2layer", "JLCPCB"), ("pcbway-2layer", "PCBWay")):
        card = cards[house_id]
        assert card["price"] is None
        assert card["price_note"] == f"Quote on {name}"
        assert "credentials" in card["unpriced_reason"]
        assert card["quote_url"].startswith("https://")
        assert "$" not in json.dumps(card)


def test_a_house_that_will_not_build_it_says_why():
    # 1 mm + 2x2 mm margin = a 5 mm board: under OSH Park's 250 mil side and
    # at PCBWay's 5 mm minimum, over JLCPCB's 3 mm.
    block = _block(1, 1)
    cards = _by_id(block)
    osh = cards["oshpark-2layer"]
    assert osh["buildable"] is False
    assert [b["code"] for b in osh["blockers"]] == ["board-too-small"]
    assert "6.35" in osh["blockers"][0]["detail"]
    # Its price is still shown -- the house would charge that -- but it is
    # not recommended, and the pick falls to a house that will build it.
    assert cards["jlcpcb-2layer"]["buildable"] is True
    assert block["recommended"] == "jlcpcb-2layer"
    assert "No house here publishes a price" in block["recommended_reason"]


def test_nothing_is_recommended_when_no_house_will_build_it():
    block = _block(2000, 2000)
    assert all(not c["buildable"] for c in block["houses"])
    assert block["recommended"] is None and block["recommended_reason"] is None
    assert not any(c["recommended"] for c in block["houses"])


def test_the_block_says_who_pays_and_that_nothing_was_ordered():
    block = _block(30, 30)
    assert block["boundary"] == BOUNDARY
    assert "You place it and pay the fab" in BOUNDARY
    assert "nothing has been ordered or paid for" in BOUNDARY


def test_an_absent_report_is_an_empty_panel_not_an_error():
    block = fab_houses_block(None)
    assert block["houses"] == [] and block["recommended"] is None


def test_the_module_cannot_reach_the_network_or_a_checkout():
    import service.fabhouses as module

    text = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("urllib", "http.client", "requests", "socket", "submit_order("):
        assert forbidden not in text
