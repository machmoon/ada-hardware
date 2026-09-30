"""The order step's "Order your board" block: one card per fab service.

``silkscreen.fabhouse.fab_house_report`` already puts every house's
capability check and quote into the order manifest
(``order.manifest.fab_houses``). That list is the engine's record -- every
issue as the gate raised it, every quote field. This module reshapes it into
what a person deciding where to send the board needs to read at a glance:
will this house build it, and if not, why; what it costs where a price
exists; how long it takes where the house documents that; and the link to
the house's own quote page. It computes nothing the engine did not, and it
is additive: the manifest is untouched.

Layout prior art: Kitspace's "Order PCBs" menu
(``kitspace/kitspace-v2 frontend/src/components/Board/OrderPCBs.jsx`` at
``f7adaab``, AGPL-3.0, read for design only, no code copied) puts the Gerber
download beside one outbound link per fab and hands price comparison to a
third party. Ada keeps that shape -- the package, then one link per house --
and deviates in one place for a stated reason: the engine already runs each
house's capability check and OSH Park's published price rule, so each card
carries its own verdict and price instead of sending the reader elsewhere
to find out.

Honesty rules, each pinned by ``service/tests/test_fabhouses.py``:

* A price is shown only when the quote's basis is not ``unavailable``. An
  unpriced house gets ``price: None`` and ``price_note`` telling the reader
  to get the quote on that house's own page -- never a zero, never an
  estimate.
* ``recommended`` is the cheapest *priced* house that will build the board,
  or, when none is priced, the first buildable one; ``None`` when nothing
  here will build it. The reason travels with the pick.
* ``boundary`` says what Ada did and what it did not: it prepared and priced
  the order; the person pays the fab. No function here, or anywhere it is
  called from, places or pays for an order (``fabhouse.submit_order``
  refuses unconditionally).
"""

from __future__ import annotations

from typing import Any

#: The sentence every "Order your board" panel ends with.
BOUNDARY = (
    "Ada prepared and priced this order. You place it and pay the fab on "
    "their own site; nothing has been ordered or paid for."
)


def _money(cents: int, currency: str) -> str:
    symbol = "$" if currency == "USD" else f"{currency} "
    return f"{symbol}{cents / 100:,.2f}"


def _card(entry: dict[str, Any]) -> dict[str, Any]:
    quote = entry.get("quote") or {}
    issues = entry.get("issues") or []
    blockers = [
        {"code": i.get("code"), "title": i.get("title"), "detail": i.get("detail")}
        for i in issues
        if i.get("severity") == "blocker"
    ]
    warnings = [
        {"code": i.get("code"), "title": i.get("title"), "detail": i.get("detail")}
        for i in issues
        if i.get("severity") != "blocker"
    ]
    house = entry.get("house") or quote.get("house") or "the fab"
    total = quote.get("total_cents")
    price = None
    price_note = None
    if quote.get("priced") and isinstance(total, int):
        boards = quote.get("boards_ordered") or quote.get("quantity")
        currency = quote.get("currency") or "USD"
        price = {
            "total_cents": total,
            "currency": currency,
            "boards": boards,
            "text": f"{boards} board{'' if boards == 1 else 's'}, "
            f"{_money(total, currency)}",
            "basis": quote.get("basis"),
            "shipping_cents": quote.get("shipping_cents"),
        }
    else:
        price_note = f"Quote on {house}"
    lead = quote.get("lead_time_days")
    lead_time_days = (
        list(lead)
        if isinstance(lead, (list, tuple)) and len(lead) == 2 and max(lead) > 0
        else None
    )
    return {
        "id": entry.get("id"),
        "house": house,
        "service": entry.get("service"),
        "buildable": bool(entry.get("buildable")),
        "blockers": blockers,
        "warnings": warnings,
        "price": price,
        "price_note": price_note,
        "unpriced_reason": quote.get("unavailable_reason") or None,
        "lead_time_days": lead_time_days,
        "quote_url": quote.get("quote_url"),
        "source_url": quote.get("source_url"),
        "recommended": False,
    }


def _pick(cards: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    buildable = [c for c in cards if c["buildable"]]
    priced = [c for c in buildable if c["price"] is not None]
    if priced:
        best = min(priced, key=lambda c: c["price"]["total_cents"])
        return best["id"], (
            f"Lowest published price among the houses that will build this "
            f"board: {best['price']['text']} at {best['house']} "
            f"{best['service']}."
        )
    if buildable:
        first = buildable[0]
        return first["id"], (
            f"{first['house']} will build this board. No house here publishes "
            f"a price for it; get the quote on {first['house']}'s own page."
        )
    return None, None


def fab_houses_block(report: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Shape ``fab_house_report``'s list into the order panel's block."""
    cards = [_card(entry) for entry in (report or [])]
    recommended, reason = _pick(cards)
    for card in cards:
        card["recommended"] = card["id"] == recommended
    return {
        "houses": cards,
        "recommended": recommended,
        "recommended_reason": reason,
        "boundary": BOUNDARY,
    }
