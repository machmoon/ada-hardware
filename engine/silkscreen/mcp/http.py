"""The MCP server over Streamable HTTP, for clients that cannot spawn a process.

Claude Desktop and Claude Code speak stdio and launch :mod:`.server` directly.
claude.ai custom connectors -- and therefore Claude in Chrome -- accept only a
remote HTTP MCP URL, so this module puts the same :func:`.server.handle`
behind one HTTP endpoint. It is the Streamable HTTP transport of MCP
2025-11-25 (``docs/specification/2025-11-25/basic/transports.mdx`` in
modelcontextprotocol/modelcontextprotocol at the release tag ``38c84e9``),
reduced to what a stateless, JSON-only server needs. Where the spec leaves a
choice open, python-sdk's server decides it: ``src/mcp/server/streamable_http.py``
at v1.30.0 (``8c2fa6e``, "v1" below) and v2.2.0 (``9972c21``, "v2").

* One endpoint path, ``/mcp``, for POST and GET (``:72``).
* A POST carries one JSON-RPC message (``:96``). A request is answered 200
  ``application/json`` (``:103-105``), a JSON-RPC error included, since that
  is still an answer. A notification or a client's response is answered 202
  with no body (``:97-99``) and runs nothing. A body that is not UTF-8 JSON, or
  not a message, is 400 with a JSON-RPC error that has no id (``:100-102``;
  v1 ``:533-549``).
* ``MCP-Protocol-Version`` is read on every request except an ``initialize``
  POST. Absent means ``2025-03-26`` (``:276-279``; v1 ``types.py:35``), and a
  value outside ``SUPPORTED_PROTOCOL_VERSIONS`` is 400 (``:281-282``; v1
  ``_validate_protocol_version``, ``:907-927``), GET and DELETE included.
  ``initialize`` is exempt: the header belongs to "subsequent requests"
  (``:267-268``) and v1 skips it there (``:553-571``). That 400 is also what
  lets python-sdk 2.2.0's client in: it probes ``2026-07-28`` first, and any
  HTTP error sends it back to ``initialize`` at ``2025-11-25``.
* GET is 405 (``:142-144``): nothing here pushes server-initiated messages.
  DELETE is 405 too (``:221-222``): no ``MCP-Session-Id`` is ever assigned, so
  there is no session to end, and 405 is python-sdk's answer when it has none
  (v1 ``:797-806``, v2 ``:825-832``). A stray session id on a request is
  ignored, as python-sdk's stateless mode does.
* ``Origin`` is validated on every request, and a foreign one is 403 with a
  JSON-RPC error that has no id (``:80-82``), against DNS rebinding: a browser
  page must not be able to point a request at this server from another
  origin. Absent is allowed, as python-sdk allows it
  (``src/mcp/server/transport_security.py:73-79``), because a connector's
  server-side client sends none.
* HTTP/1.1 framing is RFC 9112's, which ``http.server`` leaves to the
  handler. A body is delimited by ``Content-Length`` or by the ``chunked``
  transfer coding, which is decoded, since a recipient MUST be able to
  (section 7.1). A tunnel in front may re-frame a request that way. Framing
  that cannot be trusted is 400 with ``Connection: close``: both headers at
  once, ``Transfer-Encoding`` on HTTP/1.0, ``chunked`` not last or twice, a
  malformed chunk, or a ``Content-Length`` that is not plain digits. A
  coding other than ``chunked`` is 501 (sections 6.1, 6.3). These are
  gunicorn's checks (``gunicorn/http/message.py`` at ``391dbc7``,
  ``gunicorn/http/body.py`` at ``2f9b2e4``). Header values are read without
  the whitespace around them (RFC 9110 section 5.5).

Deliberate deviations, stated rather than hidden:

* **Batches, for 2025-03-26 only.** A JSON array is accepted when the version
  in force is ``2025-03-26`` -- the header says so, or there is no header --
  because that revision says a server MUST support receiving batches
  (``2025-03-26/basic/index.mdx:97-99``). 2025-06-18 removed them, so an
  array under any other version is 400. python-sdk refuses every array.
* **An absent ``Accept`` is allowed.** A present one that admits none of
  ``application/json``, ``application/*`` and ``*/*`` is 406, v2's wildcard
  rule (``check_accept_headers``, ``:87-102``); an absent one means any type
  (RFC 9110 section 12.5.1), where python-sdk answers 406.
* **No ``Content-Type`` check.** The spec has no such rule, and the Origin
  check already stops a cross-site browser POST; v1 answers 415.
* **No OAuth.** Authentication is the optional bearer token below, not the
  OAuth 2.1 authorization the spec recommends for HTTP (``basic/index.mdx:115``,
  a SHOULD).

It binds the loopback address by default (``:83``). Reaching it from claude.ai
takes a public HTTPS tunnel in front, and the endpoint carries no auth of its
own unless ``MCP_HTTP_TOKEN`` is set, in which case every request must carry
``Authorization: Bearer <token>`` or use the path ``/mcp/<token>`` (the form
claude.ai's connector dialog can take, since it accepts a URL and nothing
else; the URL is then the credential and is masked in the log).
``generate_board`` spends the Gemini key, so a tunnel to an unauthenticated
instance should not be left up unattended.

Run it with::

    python -m silkscreen.mcp.http --port 8788
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import server as _server
from .server import (
    INVALID,
    INVALID_REQUEST,
    PARSE_ERROR,
    REQUEST,
    SUPPORTED_PROTOCOL_VERSIONS,
    ParseError,
    classify,
    decode,
    dumps,
    error_without_id,
    handle,
)

__all__ = ["ENDPOINT", "MAX_BODY_BYTES", "Handler", "main", "make_server"]

ENDPOINT = "/mcp"
#: Larger than the stdio server ever sees in practice: a circuit plus a
#: testbench is kilobytes, and a body this size is a mistake, not a request.
MAX_BODY_BYTES = 1 << 20
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
#: Origins a browser may reach this server from. A connector's server-side
#: client sends no Origin at all, which is the case that actually matters.
DEFAULT_ORIGINS = frozenset({"https://claude.ai", "https://www.claude.ai"})
#: Named so, not ``protocol_version``: on a ``BaseHTTPRequestHandler`` that
#: attribute is the HTTP version.
VERSION_HEADER = "MCP-Protocol-Version"
#: The version a request with no ``MCP-Protocol-Version`` is taken to speak,
#: and the only one whose clients may send a batch.
ASSUMED_VERSION = "2025-03-26"
#: The ``Accept`` values under which ``application/json`` is acceptable.
_JSON_MEDIA_TYPES = frozenset({"application/json", "application/*", "*/*"})
#: Bounds on the parts of a chunked body that are not content, gunicorn's
#: ``DEFAULT_MAX_CHUNK_SIZE_LINE`` and ``DEFAULT_MAX_TRAILER_SECTION``
#: (``gunicorn/http/body.py`` at ``2f9b2e4``). Content is bounded by
#: ``MAX_BODY_BYTES`` however it is framed.
MAX_CHUNK_LINE_BYTES = 8190
MAX_TRAILER_BYTES = 8190 * 32
#: ``1*DIGIT`` and ``1*HEXDIG`` in ASCII. ``int()`` would also take ``+5``,
#: ``5_0`` and non-ASCII digits, which a proxy in front would read differently
#: -- Werkzeug's ``_plain_int`` rule (``src/werkzeug/_internal.py``).
_DECIMAL = re.compile(rb"[0-9]+")
_HEX = re.compile(rb"[0-9A-Fa-f]+")
#: Optional whitespace, the only thing RFC 9110 section 5.6.3 lets surround a
#: field value.
_OWS = " \t"


def _loopback_origin(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def _accepts_json(accept: str | None) -> bool:
    """Whether a response in ``application/json`` is acceptable.

    Media-type parameters (``;q=``) are dropped, as v2 drops them.
    """
    if accept is None or not accept.strip():
        return True
    offered = {part.split(";", 1)[0].strip().lower() for part in accept.split(",")}
    return bool(offered & _JSON_MEDIA_TYPES)


class Handler(BaseHTTPRequestHandler):
    """One MCP endpoint; everything else is 404."""

    allowed_origins: frozenset[str] = DEFAULT_ORIGINS
    token: str | None = None
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------

    def _field(self, name: str) -> str | None:
        """A header's value, without the whitespace around it.

        That whitespace is not part of the value, and a parser MUST exclude it
        before evaluating one (RFC 9110 section 5.5); ``http.server`` keeps the
        trailing part, so ``MCP-Protocol-Version: 2025-11-25 `` was refused.
        """
        value = self.headers.get(name)
        return None if value is None else value.strip(_OWS)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        line = fmt % args
        if self.token:
            # A capability URL is the credential; it never reaches a log,
            # the googleapps rule for the Chat webhook URL.
            line = line.replace(self.token, "<token>")
        print(f"{self.address_string()} {line}", file=sys.stderr)

    def _presented_token(self) -> str | None:
        """The bearer header, or the last path segment of ``/mcp/<token>``.

        The path form exists because claude.ai's custom-connector form takes
        a URL and nothing else -- no header, and no OAuth server here to
        answer a 401 -- so the URL has to carry the secret, the way Zapier's
        and Composio's MCP endpoints do.
        """
        auth = self.headers.get("Authorization") or ""
        scheme, _, presented = auth.partition(" ")
        if scheme.lower() == "bearer" and presented.strip():
            return presented.strip()
        path = self.path.split("?", 1)[0]
        if path.startswith(ENDPOINT + "/"):
            return path[len(ENDPOINT) + 1 :] or None
        return None

    def _send(
        self,
        code: int,
        payload: Any = None,
        *,
        close: bool = False,
        extra: dict[str, str] | None = None,
    ) -> None:
        body = b"" if payload is None else dumps(payload).encode()
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        if close:
            # Also sets self.close_connection, so the handler loop stops
            # after this response instead of parsing the unread body.
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _refuse(
        self,
        code: int,
        message: str,
        *,
        rpc_code: int = INVALID_REQUEST,
        data: Any = None,
        close: bool = False,
        extra: dict[str, str] | None = None,
    ) -> None:
        """An HTTP error whose body is a JSON-RPC error with no id, the form
        the spec allows beside every refusal (``transports.mdx:81,100-102``)."""
        self._send(
            code, error_without_id(rpc_code, message, data), close=close, extra=extra
        )

    def _gate(self) -> bool:
        """Origin and bearer checks, before any body is read.

        A refusal leaves the body unread, and on a keep-alive connection the
        next parse would start inside it -- the bridge log showed a 401
        followed by ``Unsupported method ('{"jsonrpc"...POST')``. So every
        refusal answers ``Connection: close``.
        """
        path = self.path.split("?", 1)[0]
        on_endpoint = path == ENDPOINT or (
            self.token is not None and path.startswith(ENDPOINT + "/")
        )
        if not on_endpoint:
            self._send(404, {"error": f"the MCP endpoint is {ENDPOINT}"}, close=True)
            return False
        origin = self._field("Origin")
        if (
            origin
            and origin not in self.allowed_origins
            and not _loopback_origin(origin)
        ):
            self._refuse(403, f"origin not allowed: {origin}", close=True)
            return False
        if self.token is not None:
            presented = self._presented_token()
            if presented is None or not hmac.compare_digest(presented, self.token):
                self._send(401, close=True, extra={"WWW-Authenticate": "Bearer"})
                return False
        return True

    def _version_refused(self, *, close: bool = False) -> bool:
        """Answer 400 for an ``MCP-Protocol-Version`` this server does not speak.

        The body carries ``data.supported`` and ``data.requested``, the shape of
        the spec's own unsupported-version error (``basic/lifecycle.mdx:273-288``).
        """
        version = self._field(VERSION_HEADER)
        if version is None or version in SUPPORTED_PROTOCOL_VERSIONS:
            return False
        self._refuse(
            400,
            f"unsupported {VERSION_HEADER}: {version!r}; this server speaks "
            f"{', '.join(SUPPORTED_PROTOCOL_VERSIONS)}",
            data={"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": version},
            close=close,
        )
        return True

    def _read_body(self) -> bytes | None:
        """The request's content, framed as RFC 9112 section 6.3 says.

        Every framing refusal closes the connection, because the bytes left
        on it can no longer be told apart from the next request (the
        ``_gate`` rule). The checks are gunicorn's ``Request.set_body_reader``
        (``gunicorn/http/message.py`` at ``391dbc7``).
        """
        encodings = self.headers.get_all("Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length")
        if encodings:
            return self._read_chunked(",".join(encodings), bool(lengths))
        if not lengths:
            return b""  # Neither header: no content (item 7).
        # A list of equal valid values is that one value (item 5).
        values = {
            value.strip(_OWS).encode("latin-1")
            for field in lengths
            for value in field.split(",")
        }
        if len(values) != 1 or not _DECIMAL.fullmatch(next(iter(values))):
            self._refuse(400, "invalid Content-Length", close=True)
            return None
        length = int(next(iter(values)))
        if length > MAX_BODY_BYTES:
            self._refuse(413, f"body over {MAX_BODY_BYTES} bytes", close=True)
            return None
        return self.rfile.read(length)

    def _read_chunked(self, field: str, has_length: bool) -> bytes | None:
        """Decode a ``chunked`` body (RFC 9112 section 7.1), which an HTTP/1.1
        recipient MUST be able to parse; ``http.server`` cannot, and read one
        as an empty body."""
        codings = [c.strip(_OWS).lower() for c in field.split(",") if c.strip(_OWS)]
        if self.request_version == "HTTP/1.0":
            # Section 6.1: faulty framing, whatever else the message says.
            why = "Transfer-Encoding in an HTTP/1.0 request"
        elif not codings or codings[-1] != "chunked":
            why = "the final transfer coding must be chunked"  # 6.3 item 4.
        elif "chunked" in codings[:-1]:
            why = "chunked is applied more than once"  # 6.1.
        elif has_length:
            # 6.3 item 3: both at once is how requests are smuggled.
            why = "both Transfer-Encoding and Content-Length"
        else:
            why = ""
        if why:
            self._refuse(400, f"unreadable message framing: {why}", close=True)
            return None
        if len(codings) > 1:
            # 6.1: a transfer coding this server does not understand is 501.
            self._refuse(
                501,
                f"transfer coding not supported: {', '.join(codings[:-1])}",
                close=True,
            )
            return None

        body = bytearray()
        while True:
            line = self.rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
            if not line.endswith(b"\r\n"):
                return self._bad_chunk("a chunk-size line is unterminated or too long")
            size, semicolon, extension = line[:-2].partition(b";")
            if semicolon:
                # Extensions are ignored (7.1.1); whitespace is allowed only
                # before one, and a bare CR never, as gunicorn checks.
                if b"\r" in extension:
                    return self._bad_chunk("a chunk extension holds a bare CR")
                size = size.rstrip(b" \t")
            if not _HEX.fullmatch(size):
                return self._bad_chunk(f"invalid chunk size {size[:16]!r}")
            # A Python int does not overflow, which is what 7.1 asks of a
            # recipient facing a long hex numeral.
            length = int(size, 16)
            if length == 0:
                break
            if len(body) + length > MAX_BODY_BYTES:
                self._refuse(413, f"body over {MAX_BODY_BYTES} bytes", close=True)
                return None
            data = self.rfile.read(length)
            if len(data) != length or self.rfile.read(2) != b"\r\n":
                return self._bad_chunk("a chunk is truncated or unterminated")
            body += data
        # The trailer section ends at an empty line. Its fields are
        # discarded, which 7.1.2 allows.
        trailer = 0
        while True:
            line = self.rfile.readline(MAX_CHUNK_LINE_BYTES + 1)
            if line == b"\r\n":
                return bytes(body)
            trailer += len(line)
            if not line.endswith(b"\r\n") or trailer > MAX_TRAILER_BYTES:
                return self._bad_chunk("the trailer section is unterminated or long")

    def _bad_chunk(self, why: str) -> None:
        self._refuse(400, f"malformed chunked body: {why}", close=True)
        return None

    # -- verbs -------------------------------------------------------------

    def do_POST(self) -> None:
        if not self._gate():
            return
        if not _accepts_json(self._field("Accept")):
            self._refuse(
                406,
                "Not Acceptable: this server answers in application/json, which "
                "the Accept header does not admit",
                close=True,
            )
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            message = decode(raw)
        except ParseError as exc:
            self._refuse(400, str(exc), rpc_code=PARSE_ERROR)
            return
        initialize = isinstance(message, dict) and message.get("method") == "initialize"
        if not initialize and self._version_refused():
            return
        if isinstance(message, list):
            self._post_batch(message)
            return
        kind = classify(message)
        response = handle(message)
        if kind == INVALID:
            self._send(400, response)
        elif kind == REQUEST:
            self._send(200, response)
        else:
            # A notification or a response: accepted, nothing to say.
            self._send(202)

    def _post_batch(self, messages: list[Any]) -> None:
        version = self._field(VERSION_HEADER) or ASSUMED_VERSION
        if version != ASSUMED_VERSION:
            self._refuse(
                400,
                f"a JSON array (batch) is not a message in {version}: batches "
                "were removed in 2025-06-18, so send one message per POST",
            )
            return
        if not messages:
            self._refuse(400, "an empty batch is not a message")
            return
        responses = [
            response
            for response in (handle(m, batched=True) for m in messages)
            if response is not None
        ]
        if not responses:
            # Only notifications (or responses) arrived: 202, no body.
            self._send(202)
            return
        self._send(200, responses)

    def do_GET(self) -> None:
        if not self._gate() or self._version_refused(close=True):
            return
        # No SSE stream is offered; the spec names 405 for exactly this.
        self._refuse(
            405,
            "this server offers no SSE stream; send messages with POST",
            close=True,
            extra={"Allow": "POST"},
        )

    def do_DELETE(self) -> None:
        if not self._gate() or self._version_refused(close=True):
            return
        self._refuse(
            405,
            "this server assigns no session, so there is none to end",
            close=True,
            extra={"Allow": "POST"},
        )


def make_server(
    host: str = DEFAULT_HOST,
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
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="an extra browser origin to accept (loopback and claude.ai always are)",
    )
    args = parser.parse_args(argv)
    try:
        _server.configure_rate_limit()
    except ValueError as exc:
        print(f"silkscreen-mcp-http: {exc}", file=sys.stderr)
        return 2
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
