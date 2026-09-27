"""The room server both KiCad seats connect to (asyncio, standard library only).

Each room holds the seats in it and the reconciled items everyone has sent, by
the same rule the seats use (:func:`merge.reconcile`), so a seat that joins late
is handed the board as it now stands. A room is bound to the first board file
name it sees: a seat that opened a different board is refused in words rather
than having someone else's parts written into it.

Access: every ``hello`` must carry the relay's token. Binding anything but a
loopback address without a token is refused at start, because the items are a
whole board and the port would hand them to anyone who can reach it.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
from dataclasses import dataclass, field

from . import wire
from .merge import Item, reconcile

DEFAULT_PORT = 8797
MAX_SEATS_PER_ROOM = 8


class RelayConfigError(ValueError):
    pass


@dataclass
class Room:
    board: str
    items: dict[str, Item] = field(default_factory=dict)
    seats: dict[str, asyncio.StreamWriter] = field(default_factory=dict)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Relay:
    def __init__(self, token: str = "", *, host: str = "127.0.0.1") -> None:
        if not token and not _is_loopback(host):
            raise RelayConfigError(
                f"refusing to serve on {host} without a room token: anyone who can "
                "reach the port would get the whole board (set --token or "
                "KICADCOLLAB_TOKEN)"
            )
        self.token = token
        self.host = host
        self.rooms: dict[str, Room] = {}

    def _authorised(self, token: object) -> bool:
        if not self.token:
            return True
        return isinstance(token, str) and hmac.compare_digest(token, self.token)

    async def _send(self, writer: asyncio.StreamWriter, message: dict) -> None:
        try:
            writer.write(wire.encode(message))
            await writer.drain()
        except (ConnectionError, RuntimeError):
            pass  # the seat is gone; its reader loop will clean it up

    async def _refuse(self, writer: asyncio.StreamWriter, reason: str) -> None:
        await self._send(writer, {"type": "refused", "reason": reason})
        writer.close()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            hello = wire.decode(await reader.readline())
        except (wire.WireError, ValueError, ConnectionError):
            writer.close()
            return
        room_name, seat, board = (
            hello.get("room"),
            hello.get("seat"),
            hello.get("board"),
        )
        if hello.get("type") != "hello" or not self._authorised(hello.get("token")):
            await self._refuse(writer, "wrong room token")
            return
        for name, value in (("room", room_name), ("seat", seat), ("board", board)):
            if not isinstance(value, str) or not value or len(value) > 128:
                await self._refuse(writer, f"hello has no usable {name}")
                return
        room = self.rooms.setdefault(room_name, Room(board=board))
        if room.board != board:
            await self._refuse(
                writer,
                f"room {room_name!r} is editing {room.board!r}, "
                f"and this seat opened {board!r}",
            )
            return
        if seat in room.seats:
            await self._refuse(writer, f"seat name {seat!r} is already in the room")
            return
        if len(room.seats) >= MAX_SEATS_PER_ROOM:
            await self._refuse(writer, f"room is full ({MAX_SEATS_PER_ROOM} seats)")
            return
        await self._send(
            writer,
            {
                "type": "welcome",
                "peers": sorted(room.seats),
                "items": [wire.item_to_wire(i) for i in room.items.values()],
            },
        )
        for other in room.seats.values():
            await self._send(other, {"type": "peer", "seat": seat, "joined": True})
        room.seats[seat] = writer
        try:
            while line := await reader.readline():
                try:
                    message = wire.decode(line)
                    if message["type"] != "items":
                        continue
                    items = wire.items_from_wire(message.get("items"))
                except wire.WireError:
                    continue  # one bad line from a peer is not a reason to drop it
                accepted, _ = reconcile(room.items, items)
                for item in accepted:
                    room.items[item.id] = item
                if not accepted:
                    continue
                out = {
                    "type": "items",
                    "from": seat,
                    "items": [wire.item_to_wire(i) for i in accepted],
                }
                for name, other in list(room.seats.items()):
                    if name != seat:
                        await self._send(other, out)
        except (ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            room.seats.pop(seat, None)
            for other in room.seats.values():
                await self._send(other, {"type": "peer", "seat": seat, "joined": False})
            if not room.seats:
                # The last seat left: nobody holds this board any more, so the
                # room's copy would only ever be stale. Excalidraw keeps nothing
                # server-side either.
                self.rooms.pop(room_name, None)
            writer.close()

    async def serve(self, port: int = DEFAULT_PORT) -> asyncio.Server:
        return await asyncio.start_server(
            self.handle, self.host, port, limit=wire.MAX_LINE_BYTES
        )
