"""The relay protocol: one JSON object per line, in both directions.

    seat  -> relay  {"type": "hello", "room", "token", "seat", "board"}
    relay -> seat   {"type": "welcome", "peers": [...], "items": [...]}
                  | {"type": "refused", "reason": "..."}   (then the relay hangs up)
    seat  -> relay  {"type": "items", "items": [...]}
    relay -> seats  {"type": "items", "from": "<seat>", "items": [...]}  (others)
    relay -> seats  {"type": "peer", "seat": "<seat>", "joined": true|false}

An item is ``{"id", "kind", "payload" (base64), "version", "nonce", "deleted"}``.
The shape follows excalidraw-room's ``server-broadcast`` -> ``client-broadcast``
to the rest of a room, and its ``new-user`` notice (excalidraw/excalidraw-room
``03ff435`` ``src/index.ts``, MIT). Two deviations, stated: no end-to-end
encryption (the relay is meant to run on a machine one of the engineers
controls, and the room token keeps other people out), and the relay keeps the
room's reconciled items so a late joiner gets them in ``welcome``, where
excalidraw-room holds nothing and makes an existing client resend the scene --
a KiCad seat that is mid-drag should not have to stop and dump its board.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from .merge import Item

#: A footprint's protobuf carries every pad and graphic; a large connector is a
#: few tens of KB. Four MB per line bounds a hostile or broken peer's memory use.
MAX_LINE_BYTES = 4 * 1024 * 1024
KINDS = frozenset({"footprint", "track", "arc", "via"})


class WireError(ValueError):
    """A line that is not a message this protocol has."""


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE_BYTES:
        raise WireError(f"line of {len(line)} bytes, over {MAX_LINE_BYTES}")
    try:
        message = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WireError(f"not JSON: {exc}") from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise WireError("not an object with a type")
    return message


def item_to_wire(item: Item) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "payload": base64.b64encode(item.payload).decode("ascii"),
        "version": item.version,
        "nonce": item.nonce,
        "deleted": item.deleted,
    }


def item_from_wire(raw: Any) -> Item:
    """One item, checked field by field; any fault is a :class:`WireError`."""
    if not isinstance(raw, dict):
        raise WireError("item is not an object")
    item_id, kind = raw.get("id"), raw.get("kind")
    version, nonce, deleted = (
        raw.get("version"),
        raw.get("nonce"),
        raw.get("deleted", False),
    )
    if not isinstance(item_id, str) or not item_id or len(item_id) > 64:
        raise WireError("item id missing or not a short string")
    if kind not in KINDS:
        raise WireError(f"item kind {kind!r} is not one of {sorted(KINDS)}")
    for name, value in (("version", version), ("nonce", nonce)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise WireError(f"item {name} is not a non-negative integer")
    if not isinstance(deleted, bool):
        raise WireError("item deleted is not a boolean")
    try:
        payload = base64.b64decode(raw.get("payload") or "", validate=True)
    except (binascii.Error, TypeError, ValueError):
        raise WireError("item payload is not base64") from None
    if not deleted and not payload:
        raise WireError("a live item has an empty payload")
    return Item(item_id, kind, payload, version, nonce, deleted)


def items_from_wire(raw: Any) -> list[Item]:
    if not isinstance(raw, list):
        raise WireError("items is not a list")
    return [item_from_wire(r) for r in raw]
