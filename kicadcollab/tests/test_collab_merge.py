"""The merge rule, clause by clause against Excalidraw's
``shouldDiscardRemoteElement``."""

from __future__ import annotations

from kicadcollab.merge import Item, Tracker, reconcile, should_discard_remote


def item(
    version: int, nonce: int, payload: bytes = b"x", *, deleted: bool = False
) -> Item:
    return Item("k1", "footprint", b"" if deleted else payload, version, nonce, deleted)


def test_a_remote_item_with_no_local_copy_is_taken():
    assert not should_discard_remote(None, item(1, 5))


def test_the_higher_version_wins_either_way():
    assert should_discard_remote(item(3, 9), item(2, 1))
    assert not should_discard_remote(item(2, 1), item(3, 9))


def test_a_tie_goes_to_the_lower_nonce_on_both_seats():
    a, b = item(4, 10, b"a"), item(4, 20, b"b")
    # Seat A holds a, receives b: keeps a. Seat B holds b, receives a: takes a.
    assert should_discard_remote(a, b)
    assert not should_discard_remote(b, a)


def test_an_item_being_edited_here_is_not_overwritten():
    assert should_discard_remote(item(1, 1), item(9, 0), editing=False) is False
    local = {"k1": item(1, 1)}
    accepted, parked = reconcile(local, [item(9, 0, b"new")], editing=frozenset({"k1"}))
    assert accepted == [] and [p.version for p in parked] == [9]


def test_a_deletion_is_ordered_like_any_other_edit():
    moved, deleted = item(2, 5, b"moved"), item(3, 5, deleted=True)
    accepted, _ = reconcile({"k1": moved}, [deleted])
    assert accepted == [deleted]
    accepted, _ = reconcile({"k1": item(4, 5, b"moved again")}, [deleted])
    assert accepted == []


def test_baseline_sends_nothing_and_the_first_edit_is_version_one():
    t = Tracker()
    t.baseline({"a": ("footprint", b"p0"), "b": ("via", b"v0")})
    assert t.observe({"a": ("footprint", b"p0"), "b": ("via", b"v0")}) == []
    changed = t.observe({"a": ("footprint", b"p1"), "b": ("via", b"v0")})
    assert [(c.id, c.version, c.payload) for c in changed] == [("a", 1, b"p1")]


def test_a_vanished_item_becomes_a_tombstone_and_a_new_one_starts_at_one():
    t = Tracker()
    t.baseline({"a": ("track", b"t0")})
    changed = t.observe({"n": ("track", b"t9")})
    by_id = {c.id: c for c in changed}
    assert by_id["a"].deleted and by_id["a"].version == 1
    assert by_id["n"].version == 1 and not by_id["n"].deleted


def test_adopting_a_remote_item_is_not_seen_as_a_local_change():
    t = Tracker()
    t.baseline({"a": ("footprint", b"p0")})
    remote = Item("a", "footprint", b"p7", 7, 3)
    t.adopt(remote, b"p7-as-kicad-reports-it")
    assert t.observe({"a": ("footprint", b"p7-as-kicad-reports-it")}) == []
    assert t.items["a"].version == 7
