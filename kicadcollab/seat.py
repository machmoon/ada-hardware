"""One engineer's side: poll the open board, send what changed, apply what came.

KiCad's IPC API is request/response only -- kicad-python 0.8.0 has no change
notification of any kind (read 2026-09-27: no subscribe, listen or event call in
``kipy/``), so a seat polls: every ``poll_s`` it takes a snapshot of the open
board, lets :class:`merge.Tracker` say what changed since the last one, and
sends that. Incoming items are reconciled against what this seat holds
(:func:`merge.reconcile`) with the editor's selection standing in for "being
edited", applied in one KiCad commit (one undo step), and adopted by the tracker
so the next poll does not send them straight back.

Identity: both seats opened the same file, so an existing item has the same KIID
on both. An item one seat *creates* gets a KIID from that seat's KiCad, and the
other KiCad may assign its own when it is created there; ``aliases`` maps the
shared id to the local one both ways, so the tracker only ever sees shared ids.

The board is a :class:`BoardPort`; ``kipy_board.py`` is the KiCad one and the
tests drive a dictionary. Nothing here imports kipy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from . import wire
from .merge import Item, Tracker, reconcile

log = logging.getLogger("kicadcollab")

DEFAULT_POLL_S = 0.25


class BoardPort(Protocol):
    def name(self) -> str: ...

    def snapshot(self) -> Mapping[str, tuple[str, bytes]]:
        """Every synced item on the open board: ``local id -> (kind, payload)``."""

    def selected(self) -> frozenset[str]:
        """Local ids in the editor's selection."""

    def apply(
        self, items: Sequence[tuple[str | None, Item]]
    ) -> dict[str, tuple[str, bytes | None]]:
        """Apply ``(local id or None to create, item)`` pairs in one commit.

        Returns ``shared id -> (local id, payload as the board now reports it)``;
        the payload is ``None`` for a deletion.
        """


class SeatRefused(RuntimeError):
    """The relay said no; the reason is its sentence."""


@dataclass
class Seat:
    board: BoardPort
    room: str
    seat: str
    token: str = ""
    poll_s: float = DEFAULT_POLL_S
    on_event: Callable[[str], None] = field(default=lambda text: log.info(text))
    tracker: Tracker = field(default_factory=Tracker)
    shared_of: dict[str, str] = field(default_factory=dict)  # local id -> shared id
    local_of: dict[str, str] = field(default_factory=dict)  # shared id -> local id
    pending: dict[str, Item] = field(default_factory=dict)  # parked: selected here

    # -- board side, all synchronous (the caller serialises them) ---------------

    def _shared_snapshot(self) -> dict[str, tuple[str, bytes]]:
        return {
            self.shared_of.get(local, local): value
            for local, value in self.board.snapshot().items()
        }

    def _selected_shared(self) -> frozenset[str]:
        return frozenset(self.shared_of.get(i, i) for i in self.board.selected())

    def start(self) -> None:
        self.tracker.baseline(self._shared_snapshot())

    def local_changes(self) -> list[Item]:
        return self.tracker.observe(self._shared_snapshot())

    def receive(self, items: Sequence[Item]) -> list[Item]:
        """Reconcile and apply ``items``; returns what was applied."""
        editing = self._selected_shared()
        accepted, parked = reconcile(self.tracker.items, items, editing=editing)
        for item in parked:
            self.pending[item.id] = item
        for item in accepted:
            self.pending.pop(item.id, None)
        return self._apply(accepted)

    def retry_pending(self) -> list[Item]:
        """Parked items whose part has left the selection, if they still win."""
        if not self.pending:
            return []
        editing = self._selected_shared()
        ready = [i for i in self.pending.values() if i.id not in editing]
        return self.receive(ready) if ready else []

    def _apply(self, items: list[Item]) -> list[Item]:
        todo: list[tuple[str | None, Item]] = []
        applied: list[Item] = []
        for item in items:
            known = self.tracker.items.get(item.id)
            if known is not None and known.hash == item.hash:
                self.tracker.adopt(item)  # same content: take the version only
                applied.append(item)
                continue
            if item.deleted and (known is None or known.deleted):
                self.tracker.adopt(item)  # nothing here to delete
                continue
            local = self.local_of.get(item.id, item.id) if known is not None else None
            todo.append((local, item))
        if not todo:
            return applied
        result = self.board.apply(todo)
        for _, item in todo:
            got = result.get(item.id)
            if got is None:
                continue  # the board declined it; stays as it was
            local_id, payload = got
            if local_id != item.id:
                self.shared_of[local_id] = item.id
                self.local_of[item.id] = local_id
            self.tracker.adopt(item, payload)
            applied.append(item)
        return applied

    # -- relay side --------------------------------------------------------------

    async def run(
        self, host: str, port: int, *, stop: asyncio.Event | None = None
    ) -> None:
        """Join the room and sync until ``stop`` is set or the relay goes away."""
        stop = stop or asyncio.Event()
        lock = asyncio.Lock()  # kipy's client is one socket: one request at a time
        reader, writer = await asyncio.open_connection(
            host, port, limit=wire.MAX_LINE_BYTES
        )
        async with lock:
            name = await asyncio.to_thread(self.board.name)
            await asyncio.to_thread(self.start)
        writer.write(
            wire.encode(
                {
                    "type": "hello",
                    "room": self.room,
                    "token": self.token,
                    "seat": self.seat,
                    "board": name,
                }
            )
        )
        await writer.drain()
        first = wire.decode(await reader.readline())
        if first.get("type") != "welcome":
            writer.close()
            raise SeatRefused(str(first.get("reason") or "the relay refused this seat"))
        peers = first.get("peers") or []
        self.on_event(
            f"joined room {self.room!r} on {name}"
            + (f" with {', '.join(peers)}" if peers else ", first in the room")
        )
        async with lock:
            caught_up = await asyncio.to_thread(
                self.receive, wire.items_from_wire(first.get("items") or [])
            )
        if caught_up:
            self.on_event(f"caught up: {len(caught_up)} item(s) from the room")

        async def listen() -> None:
            while line := await reader.readline():
                try:
                    message = wire.decode(line)
                    if message["type"] == "peer":
                        verb = "joined" if message.get("joined") else "left"
                        self.on_event(f"{message.get('seat')} {verb}")
                        continue
                    if message["type"] != "items":
                        continue
                    items = wire.items_from_wire(message.get("items"))
                except wire.WireError as exc:
                    self.on_event(f"ignored a bad line from the relay: {exc}")
                    continue
                async with lock:
                    applied = await asyncio.to_thread(self.receive, items)
                if applied:
                    self.on_event(
                        f"applied {len(applied)} change(s) from {message.get('from')}"
                    )
            self.on_event("the relay closed the connection")

        async def poll() -> None:
            while not stop.is_set():
                async with lock:
                    changed = await asyncio.to_thread(self.local_changes)
                    await asyncio.to_thread(self.retry_pending)
                if changed:
                    writer.write(
                        wire.encode(
                            {
                                "type": "items",
                                "items": [wire.item_to_wire(i) for i in changed],
                            }
                        )
                    )
                    await writer.drain()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self.poll_s)

        failures: list[BaseException] = []

        async def guarded(body) -> None:
            # A dead KiCad or relay ends the seat loudly, never as a quiet hang.
            try:
                await body()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- re-raised below
                failures.append(exc)
            finally:
                stop.set()

        tasks = [
            asyncio.create_task(guarded(listen)),
            asyncio.create_task(guarded(poll)),
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            self.on_event("left the room")
        if failures:
            raise failures[0]
