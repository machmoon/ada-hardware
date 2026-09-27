"""Which copy of a board item wins when two people edit one board at once.

The rule is Excalidraw's, copied by name and shape from
``packages/excalidraw/data/reconcile.ts`` (excalidraw/excalidraw ``6c90855``,
MIT): every item carries a ``version`` that its editor bumps on each change and
a random ``nonce`` drawn with each bump, and a remote copy is discarded when

- the item is being edited here (Excalidraw: ``editingTextElement``,
  ``resizingElement``, ``newElement``; KiCad has no such state in its API, so
  "selected in this editor" stands in for it),
- the local copy's version is higher, or
- the versions tie and the local nonce is lower or equal -- a deterministic
  tie-break both seats reach independently, so they converge without talking.

A deletion is a tombstone (``deleted=True``) with its own version, Excalidraw's
``isDeleted``, so "you moved it" and "I deleted it" are ordered by the same rule.

One deviation, stated: Excalidraw drops a remote edit to an element being edited
locally for good. Here the seat parks it (:meth:`Seat.pending` in ``seat.py``)
and retries after the item leaves the selection, because a KiCad selection can
last minutes and a dropped move would leave the two boards different until
someone touched that part again.

Nothing in this module knows KiCad: an item's ``payload`` is opaque bytes (the
seat puts KiCad's own protobuf there) and ``digest`` is how a change is seen.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace


def new_nonce() -> int:
    """Excalidraw's ``randomInteger()``: 31 bits, fits JSON and every client."""
    return secrets.randbits(31)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Item:
    """One board item as both seats see it.

    ``id`` is the shared identity (KiCad's KIID as text), ``kind`` what the
    payload decodes as (``footprint``, ``track``, ``arc``, ``via``).
    """

    id: str
    kind: str
    payload: bytes
    version: int
    nonce: int
    deleted: bool = False

    @property
    def hash(self) -> str:
        return "deleted" if self.deleted else digest(self.payload)


def should_discard_remote(
    local: Item | None, remote: Item, *, editing: bool = False
) -> bool:
    """``shouldDiscardRemoteElement``, clause for clause."""
    if local is None:
        return False
    return (
        editing
        or local.version > remote.version
        or (local.version == remote.version and local.nonce <= remote.nonce)
    )


def reconcile(
    local: Mapping[str, Item],
    remote: Iterable[Item],
    *,
    editing: frozenset[str] = frozenset(),
) -> tuple[list[Item], list[Item]]:
    """``(accepted, parked)``: the remote items that win and should be applied,
    and the ones that would win but for the local selection."""
    accepted: list[Item] = []
    parked: list[Item] = []
    seen: set[str] = set()
    for item in remote:
        if item.id in seen:
            continue
        seen.add(item.id)
        mine = local.get(item.id)
        if should_discard_remote(mine, item):
            continue
        if item.id in editing and mine is not None:
            parked.append(item)
            continue
        accepted.append(item)
    return accepted, parked


class Tracker:
    """Turns successive snapshots of one board into versioned items.

    ``observe`` compares a snapshot (``id -> (kind, payload)``) with what it saw
    last and returns only what changed, each with its version bumped and a new
    nonce; an id that disappeared becomes a tombstone. ``adopt`` records an item
    that came from the other seat, so applying it is not seen as a local edit
    and sent straight back (the echo every naive two-way sync has).
    """

    def __init__(self) -> None:
        self.items: dict[str, Item] = {}

    def baseline(self, snapshot: Mapping[str, tuple[str, bytes]]) -> None:
        """The board as opened: every item at version 0, nonce 0, sent to nobody.

        Two seats that opened the same file agree on it without a word, and any
        edit either makes is version 1, which beats it everywhere. (Excalidraw
        starts a scene loaded from a file the same way: its elements keep the
        versions the file had and nothing is broadcast until one changes.)
        """
        self.items = {
            item_id: Item(item_id, kind, payload, 0, 0)
            for item_id, (kind, payload) in snapshot.items()
        }

    def observe(self, snapshot: Mapping[str, tuple[str, bytes]]) -> list[Item]:
        changed: list[Item] = []
        for item_id, (kind, payload) in snapshot.items():
            known = self.items.get(item_id)
            if (
                known is not None
                and not known.deleted
                and known.hash == digest(payload)
            ):
                continue
            version = (known.version + 1) if known is not None else 1
            item = Item(item_id, kind, payload, version, new_nonce())
            self.items[item_id] = item
            changed.append(item)
        for item_id, known in list(self.items.items()):
            if not known.deleted and item_id not in snapshot:
                tomb = replace(
                    known,
                    payload=b"",
                    version=known.version + 1,
                    nonce=new_nonce(),
                    deleted=True,
                )
                self.items[item_id] = tomb
                changed.append(tomb)
        return changed

    def adopt(self, item: Item, payload: bytes | None = None) -> None:
        """Take ``item``'s version as ours. ``payload`` is what the editor reports
        after applying it, when that differs from what was sent (KiCad may
        normalise a field), so the next ``observe`` does not see a change."""
        self.items[item.id] = (
            item if payload is None else replace(item, payload=payload)
        )
