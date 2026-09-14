"""The MCP server over Streamable HTTP, for clients that cannot spawn a process.

Claude Desktop and Claude Code speak stdio and launch :mod:`.server` directly.
claude.ai custom connectors -- and therefore Claude in Chrome -- accept only a
remote HTTP MCP URL, so this module puts the same :func:`.server.handle`
behind one HTTP endpoint. It is the transport in the MCP specification
(``docs/specification/2025-03-26/basic/transports.mdx`` in
modelcontextprotocol/modelcontextprotocol), reduced to the parts a stateless
server needs and nothing invented:

* one endpoint path for POST and GET (``/mcp``);
* a POST body is one JSON-RPC message or a batch; a body holding at least
  one *request* is answered with ``application/json``, a body of only
  notifications or responses with ``202 Accepted`` and no body;
* GET answers ``405``, which the spec allows for a server that offers no SSE
  stream -- nothing here pushes server-initiated messages;
* no ``Mcp-Session-Id`` is assigned (the spec says MAY, and ``handle`` keeps
  no state between calls), so DELETE has nothing to end and answers ``204``;
* the ``Origin`` header is validated on every request, which the spec
  requires against DNS rebinding: a browser page must not be able to point a
  request at this server from another origin. Absent is allowed because a
  connector's server-side client sends none.

It binds the loopback address by default. Reaching it from claude.ai takes a
public HTTPS tunnel in front, and the endpoint carries no auth of its own
unless ``MCP_HTTP_TOKEN`` is set, in which case every request must carry
``Authorization: Bearer <token>``. ``generate_board`` spends the Gemini key,
so a tunnel to an unauthenticated instance should not be left up unattended.

Run it with::

    python -m silkscreen.mcp.http --port 8788
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from .server import INVALID_REQUEST, PARSE_ERROR, handle

__all__ = ["ENDPOINT", "MAX_BODY_BYTES", "Handler", "main", "make_server"]

ENDPOINT = "/mcp"
#: Larger than the stdio server ever sees in practice: a circuit plus a
#: testbench is kilobytes, and a body this size is a mistake, not a request.
MAX_BODY_BYTES = 1 << 20
DEFAULT_PORT = 8788
#: Origins a browser may reach this server from. A connector's server-side
#: client sends no Origin at all, which is the case that actually matters.
DEFAULT_ORIGINS = frozenset({"https://claude.ai", "https://www.claude.ai"})


def _loopback_origin(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


class Handler(BaseHTTPRequestHandler):
    """One MCP endpoint; everything else is 404."""

    allowed_origins: frozenset[str] = DEFAULT_ORIGINS
    token: str | None = None
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        print(f"{self.address_string()} {fmt % args}", file=sys.stderr)

    def _send(self, code: int, payload: Any = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _gate(self) -> bool:
        """Origin and bearer checks, before any body is read."""
        if self.path.split("?", 1)[0] != ENDPOINT:
            self._send(404, {"error": f"the MCP endpoint is {ENDPOINT}"})
            return False
        origin = self.headers.get("Origin")
        if (
            origin
            and origin not in self.allowed_origins
            and not _loopback_origin(origin)
        ):
            self._send(403, {"error": "origin not allowed"})
            return False
        if self.token is not None:
            auth = self.headers.get("Authorization") or ""
            scheme, _, presented = auth.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                presented.strip(), self.token
            ):
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
        return True

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "body too large"})
            return None
        return self.rfile.read(length)

    # -- verbs -------------------------------------------------------------

    def do_POST(self) -> None:
        if not self._gate():
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": PARSE_ERROR, "message": f"invalid JSON: {exc}"},
                },
            )
            return
        batch = isinstance(message, list)
        messages = message if batch else [message]
        if not messages or not all(isinstance(m, dict) for m in messages):
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": INVALID_REQUEST,
                        "message": "request must be an object or a non-empty array",
                    },
                },
            )
            return
        responses = [r for r in (handle(m) for m in messages) if r is not None]
        if not responses:
            # Only notifications (or responses) arrived: 202, no body.
            self._send(202)
            return
        self._send(200, responses if batch else responses[0])

    def do_GET(self) -> None:
        if not self._gate():
            return
        # No SSE stream is offered; the spec names 405 for exactly this.
        self.send_response(405)
        self.send_header("Allow", "POST, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:
        if not self._gate():
            return
        self._send(204)


def make_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    token: str | None = None,
    origins: frozenset[str] = DEFAULT_ORIGINS,
) -> ThreadingHTTPServer:
    """A bound server; ``port=0`` picks a free one (the tests use that)."""
    handler = type(
        "BoundHandler", (Handler,), {"token": token, "allowed_origins": origins}
    )
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m silkscreen.mcp.http",
        description="Serve the Silkscreen MCP server over Streamable HTTP.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="an extra browser origin to accept (loopback and claude.ai always are)",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("MCP_HTTP_TOKEN") or None
    origins = DEFAULT_ORIGINS | frozenset(args.allow_origin)
    server = make_server(args.host, args.port, token=token, origins=origins)
    print(
        f"MCP over HTTP at http://{args.host}:{args.port}{ENDPOINT} "
        f"({'bearer token required' if token else 'no auth'})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
