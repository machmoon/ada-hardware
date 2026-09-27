"""Two seats and a real relay on a loopback port, with dictionaries for boards.

The fake board keeps what KiCad would: items by local KIID, a selection, and
``apply`` in one call. ``remap`` makes it assign its own KIID to every created
item, the case a second KiCad may produce.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from kicadcollab.merge import Item
from kicadcollab.relay import Relay, RelayConfigError
from kicadcollab.seat import Seat, SeatRefused


class FakeBoard:
    def __init__(
        self,
        items: dict[str, tuple[str, bytes]],
        *,
        name: str = "demo.kicad_pcb",
        remap: bool = False,
    ) -> None:
        self.items = dict(items)
        self.selection: set[str] = set()
        self._name = name
        self.remap = remap
        self.commits = 0

    def name(self) -> str:
        return self._name

    def snapshot(self):
        return dict(self.items)

    def selected(self) -> frozenset[str]:
        return frozenset(self.selection)

    def apply(self, pairs: Sequence[tuple[str | None, Item]]):
        self.commits += 1
        out = {}
        for local, item in pairs:
            if item.deleted:
                self.items.pop(local, None)
                out[item.id] = (local, None)
                continue
            target = (
                local
                if local is not None
                else (f"local-{item.id}" if self.remap else item.id)
            )
            self.items[target] = (item.kind, item.payload)
            out[item.id] = (target, item.payload)
        return out


START = {
    "U1": ("footprint", b"U1@0,0"),
    "R1": ("footprint", b"R1@5,0"),
    "T1": ("track", b"T1 a-b"),
}


async def settle(*conditions, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not all(c() for c in conditions):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("seats did not converge in time")
        await asyncio.sleep(0.02)


async def room(relay: Relay, *boards: FakeBoard, names=("ana", "ben"), token: str = ""):
    server = await relay.serve(0)
    port = server.sockets[0].getsockname()[1]
    stop = asyncio.Event()
    seats = [
        Seat(b, "r1", n, token=token, poll_s=0.02, on_event=lambda _t: None)
        for b, n in zip(boards, names, strict=False)
    ]
    tasks = []
    for s in seats:
        tasks.append(asyncio.create_task(s.run("127.0.0.1", port, stop=stop)))
        await asyncio.sleep(0.05)  # join in order, so "late joiner" means something
    return server, port, stop, seats, tasks


async def close(server, stop, tasks):
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    server.close()
    await server.wait_closed()


def test_a_move_on_one_board_appears_on_the_other_and_is_not_echoed():
    async def main():
        a, b = FakeBoard(START), FakeBoard(START)
        server, _, stop, seats, tasks = await room(Relay(), a, b)
        a.items["U1"] = ("footprint", b"U1@10,3")
        await settle(lambda: b.items["U1"] == ("footprint", b"U1@10,3"))
        commits = b.commits
        await asyncio.sleep(0.2)  # a few more polls on both sides
        assert b.commits == commits  # applied once, not bounced back and forth
        assert a.items == b.items
        await close(server, stop, tasks)

    asyncio.run(main())


def test_joining_with_the_same_file_changes_nothing():
    async def main():
        a, b = FakeBoard(START), FakeBoard(START)
        server, _, stop, _, tasks = await room(Relay(), a, b)
        await asyncio.sleep(0.2)
        assert a.commits == 0 and b.commits == 0
        await close(server, stop, tasks)

    asyncio.run(main())


def test_the_same_part_moved_on_both_boards_ends_the_same_on_both():
    async def main():
        a, b = FakeBoard(START), FakeBoard(START)
        server, _, stop, _, tasks = await room(Relay(), a, b)
        a.items["R1"] = ("footprint", b"R1@from-ana")
        b.items["R1"] = ("footprint", b"R1@from-ben")
        await settle(lambda: a.items["R1"] == b.items["R1"])
        await asyncio.sleep(0.2)
        assert a.items["R1"] == b.items["R1"]
        await close(server, stop, tasks)

    asyncio.run(main())


def test_a_part_selected_here_waits_until_it_is_let_go():
    async def main():
        a, b = FakeBoard(START), FakeBoard(START)
        server, _, stop, seats, tasks = await room(Relay(), a, b)
        b.selection = {"U1"}
        a.items["U1"] = ("footprint", b"U1@moved-by-ana")
        await settle(lambda: "U1" in seats[1].pending)
        assert b.items["U1"] == ("footprint", b"U1@0,0")
        b.selection = set()
        await settle(lambda: b.items["U1"] == ("footprint", b"U1@moved-by-ana"))
        await close(server, stop, tasks)

    asyncio.run(main())


def test_created_and_deleted_items_travel_even_when_kicad_assigns_its_own_id():
    async def main():
        a, b = FakeBoard(START), FakeBoard(START, remap=True)
        server, _, stop, _, tasks = await room(Relay(), a, b)
        a.items["V9"] = ("via", b"via at 3,3")
        await settle(lambda: ("via", b"via at 3,3") in b.items.values())
        assert "local-V9" in b.items
        # Ben moves the via he received; Ana sees it under her own id.
        b.items["local-V9"] = ("via", b"via at 4,4")
        await settle(lambda: a.items.get("V9") == ("via", b"via at 4,4"))
        # Ana deletes it; it leaves Ben's board too.
        del a.items["V9"]
        await settle(lambda: "local-V9" not in b.items)
        await close(server, stop, tasks)

    asyncio.run(main())


def test_a_late_joiner_is_handed_the_room_as_it_stands():
    async def main():
        a = FakeBoard(START)
        relay = Relay()
        server = await relay.serve(0)
        port = server.sockets[0].getsockname()[1]
        stop = asyncio.Event()
        ta = asyncio.create_task(
            Seat(a, "r1", "ana", poll_s=0.02, on_event=lambda _t: None).run(
                "127.0.0.1", port, stop=stop
            )
        )
        await asyncio.sleep(0.05)
        a.items["U1"] = ("footprint", b"U1@late")
        await settle(lambda: "U1" in relay.rooms["r1"].items)
        b = FakeBoard(START)
        tb = asyncio.create_task(
            Seat(b, "r1", "ben", poll_s=0.02, on_event=lambda _t: None).run(
                "127.0.0.1", port, stop=stop
            )
        )
        await settle(lambda: b.items["U1"] == ("footprint", b"U1@late"))
        await close(server, stop, [ta, tb])

    asyncio.run(main())


def test_a_different_board_or_a_wrong_token_is_refused_in_words():
    async def main():
        relay = Relay("s3cret")
        server = await relay.serve(0)
        port = server.sockets[0].getsockname()[1]
        stop = asyncio.Event()
        first = asyncio.create_task(
            Seat(
                FakeBoard(START),
                "r1",
                "ana",
                token="s3cret",
                poll_s=0.02,
                on_event=lambda _t: None,
            ).run("127.0.0.1", port, stop=stop)
        )
        await asyncio.sleep(0.05)
        with pytest.raises(SeatRefused, match="editing 'demo.kicad_pcb'"):
            await Seat(
                FakeBoard(START, name="other.kicad_pcb"), "r1", "ben", token="s3cret"
            ).run("127.0.0.1", port)
        with pytest.raises(SeatRefused, match="token"):
            await Seat(FakeBoard(START), "r1", "eve", token="guess").run(
                "127.0.0.1", port
            )
        with pytest.raises(SeatRefused, match="already in the room"):
            await Seat(FakeBoard(START), "r1", "ana", token="s3cret").run(
                "127.0.0.1", port
            )
        await close(server, stop, [first])

    asyncio.run(main())


def test_the_relay_will_not_serve_a_network_address_without_a_token():
    with pytest.raises(RelayConfigError, match="without a room token"):
        Relay("", host="0.0.0.0")
    Relay("", host="127.0.0.1")
    Relay("t", host="0.0.0.0")
