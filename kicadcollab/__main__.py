"""``python -m kicadcollab relay`` and ``python -m kicadcollab join``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys

from .relay import DEFAULT_PORT, Relay, RelayConfigError
from .seat import DEFAULT_POLL_S, Seat, SeatRefused


def _relay(args: argparse.Namespace) -> int:
    try:
        relay = Relay(args.token, host=args.host)
    except RelayConfigError as exc:
        print(f"kicadcollab: {exc}", file=sys.stderr)
        return 2

    async def main() -> None:
        server = await relay.serve(args.port)
        print(
            f"kicadcollab relay on {args.host}:{args.port}"
            + ("" if args.token else " (no token: loopback only)"),
            flush=True,
        )
        async with server:
            await server.serve_forever()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    return 0


def _join(args: argparse.Namespace) -> int:
    host, _, port = args.relay.rpartition(":")
    if not host or not port.isdigit():
        print(
            f"kicadcollab: --relay must be host:port, got {args.relay!r}",
            file=sys.stderr,
        )
        return 2
    try:
        from .kipy_board import KipyBoard

        board = KipyBoard(args.socket)
    except ImportError as exc:
        print(
            f"kicadcollab: kicad-python is not installed in this venv ({exc}); "
            "run the seat with .venv-kicad/bin/python",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 -- every kipy connection failure, in words
        print(
            f"kicadcollab: could not reach a KiCad board editor ({exc}). "
            "Open the board in KiCad 9+ with "
            "Preferences > Plugins > Enable API server on.",
            file=sys.stderr,
        )
        return 2
    seat = Seat(
        board,
        args.room,
        args.seat,
        token=args.token,
        poll_s=args.poll,
        on_event=lambda text: print(f"[{args.seat}] {text}", flush=True),
    )
    try:
        asyncio.run(seat.run(host, int(port)))
    except SeatRefused as exc:
        print(f"kicadcollab: the relay refused this seat: {exc}", file=sys.stderr)
        return 1
    except (ConnectionError, OSError) as exc:
        print(f"kicadcollab: lost the relay at {args.relay}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kicadcollab")
    sub = parser.add_subparsers(dest="command", required=True)
    token_default = os.environ.get("KICADCOLLAB_TOKEN", "")
    r = sub.add_parser("relay", help="serve a room both seats connect to")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=DEFAULT_PORT)
    r.add_argument("--token", default=token_default)
    j = sub.add_parser("join", help="sync the board open in this KiCad into a room")
    j.add_argument("--room", required=True)
    j.add_argument("--seat", required=True, help="your name, shown to the other seat")
    j.add_argument("--relay", default=f"127.0.0.1:{DEFAULT_PORT}")
    j.add_argument("--token", default=token_default)
    j.add_argument("--socket", default=None, help="KiCad API socket (ipc://...)")
    j.add_argument("--poll", type=float, default=DEFAULT_POLL_S)
    args = parser.parse_args(argv)
    return _relay(args) if args.command == "relay" else _join(args)


if __name__ == "__main__":
    sys.exit(main())
