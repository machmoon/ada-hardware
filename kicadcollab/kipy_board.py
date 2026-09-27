"""The :class:`seat.BoardPort` for a running KiCad board editor, over its IPC API.

Runs in the bridge venv (``.venv-kicad``, where kicad-python lives; see
``desktop/kicad_live.py`` for why it cannot share the engine's venv) and imports
``kipy`` only when constructed, so the package and its tests load without it.

What is synced: footprints (the whole ``FootprintInstance`` protobuf, so a move,
a rotation, a flip or a field edit all travel), straight and arc tracks, and
vias. Zones, graphics and text are not yet: a zone refill would change its
payload on every edit near it and flood the room, and that needs its own rule.

The payload is KiCad's own protobuf (``SerializeToString``), so nothing is
re-encoded by hand and a field this adapter does not know still arrives. An
update sets the protobuf's ``id`` to this board's local KIID before
``update_items``, which "matches by internal UUID"; a create lets KiCad keep or
assign the KIID and reports what it assigned, which the seat records as an alias.

Requires KiCad 9 or newer with Preferences > Plugins > Enable API server.
**Unverified live**: written against kicad-python 0.8.0's source, not yet run
against a KiCad (none is installed on the machine it was written on).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .merge import Item


class KipyBoard:
    def __init__(self, socket_path: str | None = None) -> None:
        from kipy import KiCad
        from kipy.board_types import ArcTrack, FootprintInstance, Track, Via
        from kipy.proto.board import board_types_pb2 as pb
        from kipy.proto.common.types.base_types_pb2 import KIID

        self._kicad = KiCad(socket_path=socket_path) if socket_path else KiCad()
        self._board = self._kicad.get_board()
        self._KIID = KIID
        # kind -> (wrapper class, protobuf message class)
        self._types = {
            "footprint": (FootprintInstance, pb.FootprintInstance),
            "track": (Track, pb.Track),
            "arc": (ArcTrack, pb.Arc),
            "via": (Via, pb.Via),
        }
        self._kind_of = {
            FootprintInstance: "footprint",
            Track: "track",
            ArcTrack: "arc",
            Via: "via",
        }

    def name(self) -> str:
        return Path(self._board.name or "").name

    def _kind(self, item) -> str | None:
        return self._kind_of.get(type(item))

    def snapshot(self) -> Mapping[str, tuple[str, bytes]]:
        out: dict[str, tuple[str, bytes]] = {}
        for item in (
            *self._board.get_footprints(),
            *self._board.get_tracks(),
            *self._board.get_vias(),
        ):
            kind = self._kind(item)
            if kind is not None:
                out[item.id.value] = (kind, item.proto.SerializeToString())
        return out

    def selected(self) -> frozenset[str]:
        return frozenset(i.id.value for i in self._board.get_selection())

    def _wrapper(self, item: Item, local_id: str | None):
        wrapper_cls, message_cls = self._types[item.kind]
        message = message_cls()
        message.ParseFromString(item.payload)
        if local_id is not None:
            message.id.value = local_id
        return wrapper_cls(message)

    def apply(
        self, items: Sequence[tuple[str | None, Item]]
    ) -> dict[str, tuple[str, bytes | None]]:
        result: dict[str, tuple[str, bytes | None]] = {}
        deletes = [
            (local, it) for local, it in items if it.deleted and local is not None
        ]
        updates = [
            (local, it) for local, it in items if not it.deleted and local is not None
        ]
        creates = [it for local, it in items if not it.deleted and local is None]
        commit = self._board.begin_commit()
        try:
            if deletes:
                self._board.remove_items_by_id(
                    [self._KIID(value=local) for local, _ in deletes]
                )
                for local, it in deletes:
                    result[it.id] = (local, None)
            if updates:
                done = self._board.update_items(
                    [self._wrapper(it, local) for local, it in updates]
                )
                by_local = {d.id.value: d for d in done}
                for local, it in updates:
                    got = by_local.get(local)
                    if got is not None:
                        result[it.id] = (local, got.proto.SerializeToString())
            if creates:
                made = self._board.create_items(
                    [self._wrapper(it, None) for it in creates]
                )
                # CreateItems answers in request order (one created_items entry per
                # request item in kipy's create_items), so pair them positionally.
                for it, got in zip(creates, made, strict=True):
                    result[it.id] = (got.id.value, got.proto.SerializeToString())
        except Exception:
            self._board.drop_commit(commit)
            raise
        self._board.push_commit(commit, "kicadcollab: changes from a collaborator")
        return result
