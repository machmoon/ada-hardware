"""``python -m alexabot.sim``: the simulated Alexa+ experience, on one machine.

One process, loopback only, three parts:

* the alexabot MCP server (the six voice tools, :mod:`alexabot.app`) on
  ``--mcp-port`` (8789), unchanged;
* a Strands agent per browser conversation (:mod:`alexabot.agent`) whose MCP
  client reaches those tools over real Streamable HTTP -- the sim never calls
  the runner or ``service/steps.py`` itself;
* this server on ``--port`` (8790): the page (``alexabot/web/``, no build
  step), its JSON API, an EventSource stream per conversation, the Polly
  audio of what Ada said, and the routed board as an SVG.

With ``--memory`` (or ``ADA_AGENTCORE_MEMORY_ID``), each conversation also
reads the person's remembered design preferences from Amazon Bedrock
AgentCore Memory when it opens and writes their words back, one event per
spoken turn (:mod:`alexabot.memory`); ``--scripted`` uses an in-process
stand-in, and with neither, memory is off and the page says so.

It is a **simulation**. Nothing here is Alexa, an Alexa skill, or made by
Amazon; the page says so in its header, its greeting and its footer. The live
agent's model is Amazon Nova 2 Lite on Bedrock and its voice Amazon Polly;
``--scripted`` replaces both with a rule-based stand-in and the browser's own
voice, so a judge can run everything with no AWS account and no key.

The server is the standard library's ``ThreadingHTTPServer``, the convention
of ``service/app.py`` and ``slackbot``. It has no login, so a non-loopback
``--host`` is refused; it still checks the ``Host`` header (421, against DNS
rebinding) and a POST's ``Origin`` (403, so another tab cannot spend the
Bedrock or Polly budget), limits JSON bodies to 8 KiB, and sends a
Content-Security-Policy that allows nothing but this origin.

Exit codes: 0 on a clean stop, 2 when it cannot start as configured -- every
problem named at once (the ``zoombot/config.py`` rule).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import signal
import socket
import sys
import threading
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .conversations import Busy, ConversationHost, Full, UnknownConversation

__all__ = [
    "DEFAULT_PORT",
    "LABEL",
    "SIM_ENV",
    "SimApp",
    "SimConfig",
    "SimConfigError",
    "load_sim_config",
    "main",
    "make_sim_server",
]

LABEL = "Simulated Alexa+ experience"
DEFAULT_PORT = 8790
DEFAULT_MCP_PORT = 8789
DEFAULT_MAX_MODEL_CALLS = 60
DEFAULT_MAX_POLLY_CALLS = 60
DEFAULT_MAX_MEMORY_CALLS = 60
SCRIPTED_SIM_DELAY_S = 1.5
MAX_BODY = 8 * 1024
MAX_HINT_BYTES = 2 * 1024
MAX_TEXT = 500
MCP_SPEC = "2025-11-25"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

#: Every variable the sim reads besides alexabot's own (``config.ALEXABOT_ENV``).
SIM_ENV = (
    "ALEXA_SIM_PORT",
    "ALEXA_SIM_MODEL_ID",
    "ALEXA_SIM_POLLY_VOICE",
    "ALEXA_SIM_MAX_MODEL_CALLS",
    "ALEXA_SIM_MAX_POLLY_CALLS",
    "ALEXA_SIM_MCP_KEY",
    "AWS_REGION",
    "ADA_AGENTCORE_MEMORY_ID",
    "ADA_AGENTCORE_REGION",
    "ADA_AGENTCORE_MAX_CALLS",
)
MEMORY_KINDS = ("agentcore", "scripted", "off")
_ACCOUNT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")

INSTALL_LINE = 'pip install -e ".[dev,agents,cloud,adk,cad,alexa]"'

WEB_DIR = Path(__file__).resolve().parent / "web"
LIB_DIR = Path(__file__).resolve().parent.parent / "frontend" / "src" / "lib"
STATIC_FILES = {
    "sim.css": "text/css; charset=utf-8",
    "sim.js": "text/javascript; charset=utf-8",
    "mark.svg": "image/svg+xml",
    "fonts/libre-baskerville-latin-wght-normal.woff2": "font/woff2",
}
#: Served read-only from ``frontend/src/lib``; they import only each other.
LIB_FILES = frozenset({"voice.js", "format.js", "severity.js"})
CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
    "connect-src 'self'; font-src 'self'; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
_SESSION_ID = re.compile(r"\Abrd_[0-9a-f]{12}\Z")
_CONV_ID = re.compile(r"\Aconv_[0-9a-f]{16}\Z")
_UTTERANCE = re.compile(r"\Au_[0-9]{1,9}\Z")
_LOCALE = re.compile(r"\A[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?\Z")
SOURCES = frozenset({"voice", "typed", "chip"})
ADA_TOOLS = frozenset({"start_board_design", "answer_design_questions",
                       "continue_design", "board_status", "explain_finding",
                       "recall_my_boards"})
POLLY_FALLBACK_NOTICE = (
    "Amazon Polly isn't available now, so this browser's voice is speaking."
)

# -- configuration ----------------------------------------------------------------


class SimConfigError(RuntimeError):
    def __init__(self, problems: Sequence[str]) -> None:
        self.errors = tuple(problems)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class SimConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    mcp_port: int = DEFAULT_MCP_PORT
    mcp_url: str | None = None
    agent: str = "bedrock"
    workers: str = "live"
    tts: str = "polly"
    model_id: str = "us.amazon.nova-2-lite-v1:0"
    region: str = "us-east-1"
    voice: str = "Joanna"
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    max_polly_calls: int = DEFAULT_MAX_POLLY_CALLS
    db: str | None = None
    scripted_delay_s: float = 0.0
    memory: str = "off"
    memory_id: str | None = None
    memory_region: str = "us-east-1"
    memory_actor: str | None = None
    max_memory_calls: int = 60
    memory_reason: str | None = None

    @property
    def mode(self) -> str:
        return "scripted" if self.agent == "scripted" else "live"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m alexabot.sim",
        description="The simulated Alexa+ experience: a voice page, a Strands "
        "agent and Ada's MCP tools, on this machine. Not Alexa.",
    )
    p.add_argument("--scripted", action="store_true",
                   help="--agent scripted --workers scripted --tts browser: "
                   "offline, no AWS account, no key")
    p.add_argument("--agent", choices=("bedrock", "scripted"), default=None)
    p.add_argument("--workers", choices=("live", "scripted"), default=None)
    p.add_argument("--tts", choices=("polly", "browser"), default=None)
    p.add_argument("--model-id", default=None)
    p.add_argument("--region", default=None)
    p.add_argument("--voice", default=None)
    p.add_argument("--max-model-calls", default=None)
    p.add_argument("--max-polly-calls", default=None)
    p.add_argument("--port", default=None)
    p.add_argument("--mcp-port", default=None)
    p.add_argument("--mcp-url", default=None)
    p.add_argument("--db", default=None)
    p.add_argument("--scripted-delay", default=None, metavar="SECONDS")
    p.add_argument("--memory", choices=MEMORY_KINDS, default=None,
                   help="where design preferences live between conversations: "
                   "AgentCore Memory (default when a memory id is set), scripted "
                   "(default with --scripted) or off")
    p.add_argument("--memory-id", default=None,
                   help="the AgentCore memory id (ADA_AGENTCORE_MEMORY_ID)")
    p.add_argument("--memory-region", default=None,
                   help="the AgentCore Memory region (ADA_AGENTCORE_REGION; "
                   "default --region)")
    p.add_argument("--memory-actor", default=None,
                   help="whose preferences these are; needed with --mcp-url")
    p.add_argument("--max-memory-calls", default=None)
    p.add_argument("--host", default="127.0.0.1")
    return p


def _int(raw: Any, name: str, problems: list[str], *, low: int, high: int,
         default: int) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        problems.append(f"{name} must be a whole number, got {raw!r}")
        return default
    if not low <= value <= high:
        problems.append(f"{name} must be {low} to {high}, got {value}")
    return value


def load_sim_config(argv: Sequence[str] | None = None,
                    env: Mapping[str, str] | None = None) -> SimConfig:
    env = os.environ if env is None else env
    args = _parser().parse_args(list(argv) if argv is not None else None)
    problems: list[str] = []
    agent = args.agent or ("scripted" if args.scripted else "bedrock")
    workers = args.workers or ("scripted" if args.scripted else "live")
    tts = args.tts or ("polly" if agent == "bedrock" else "browser")
    port = _int(args.port if args.port is not None else env.get("ALEXA_SIM_PORT"),
                "--port", problems, low=0, high=65535, default=DEFAULT_PORT)
    mcp_port = _int(args.mcp_port, "--mcp-port", problems, low=0, high=65535,
                    default=DEFAULT_MCP_PORT)
    max_model = _int(args.max_model_calls if args.max_model_calls is not None
                     else env.get("ALEXA_SIM_MAX_MODEL_CALLS"), "--max-model-calls",
                     problems, low=0, high=100_000, default=DEFAULT_MAX_MODEL_CALLS)
    max_polly = _int(args.max_polly_calls if args.max_polly_calls is not None
                     else env.get("ALEXA_SIM_MAX_POLLY_CALLS"), "--max-polly-calls",
                     problems, low=0, high=100_000, default=DEFAULT_MAX_POLLY_CALLS)
    delay_raw = args.scripted_delay
    if delay_raw is None:
        delay = SCRIPTED_SIM_DELAY_S if workers == "scripted" else 0.0
    else:
        try:
            delay = float(delay_raw)
        except ValueError:
            delay = -1.0
        if delay < 0:
            problems.append(f"--scripted-delay must be 0 or more seconds, got "
                            f"{delay_raw!r}")
        elif delay and workers != "scripted":
            problems.append("--scripted-delay only means something with scripted "
                            "workers")
    if args.host not in LOOPBACK:
        problems.append(f"--host {args.host} is not loopback; the sim has no "
                        "login, so it only serves this machine")
    mcp_url = (args.mcp_url or "").strip() or None
    if mcp_url and not re.match(r"\Ahttps?://", mcp_url):
        problems.append(f"--mcp-url must be an http(s) URL, got {mcp_url!r}")
    region = args.region or env.get("AWS_REGION") or "us-east-1"
    memory, memory_id, actor, reason = _memory_choice(args, env, agent, mcp_url,
                                                      problems)
    max_memory = _int(args.max_memory_calls if args.max_memory_calls is not None
                      else env.get("ADA_AGENTCORE_MAX_CALLS"), "--max-memory-calls",
                      problems, low=0, high=100_000, default=DEFAULT_MAX_MEMORY_CALLS)
    if problems:
        raise SimConfigError(problems)
    return SimConfig(
        host=args.host, port=port, mcp_port=mcp_port, mcp_url=mcp_url,
        agent=agent, workers=workers, tts=tts,
        model_id=args.model_id or env.get("ALEXA_SIM_MODEL_ID")
        or "us.amazon.nova-2-lite-v1:0",
        region=region,
        voice=args.voice or env.get("ALEXA_SIM_POLLY_VOICE") or "Joanna",
        max_model_calls=max_model, max_polly_calls=max_polly, db=args.db,
        scripted_delay_s=max(delay, 0.0),
        memory=memory, memory_id=memory_id,
        memory_region=(args.memory_region or env.get("ADA_AGENTCORE_REGION")
                       or region),
        memory_actor=actor, max_memory_calls=max_memory, memory_reason=reason,
    )


def _memory_choice(args: argparse.Namespace, env: Mapping[str, str], agent: str,
                   mcp_url: str | None, problems: list[str]
                   ) -> tuple[str, str | None, str | None, str | None]:
    """``(kind, memory id, actor, reason it is off)``.

    An id means AgentCore; otherwise ``--scripted`` means the scripted
    stand-in; otherwise off. An operator who named a memory (``--memory`` or
    an id) gets a refusal, never a silent downgrade; only the implicit
    scripted default with ``--mcp-url`` and no actor turns itself off, and
    says why.
    """
    from .memory import MEMORY_ID, OFF_FIX

    memory_id = (args.memory_id or env.get("ADA_AGENTCORE_MEMORY_ID") or "").strip() \
        or None
    actor = (args.memory_actor or "").strip() or None
    explicit = args.memory
    kind = explicit or ("agentcore" if memory_id else
                        "scripted" if agent == "scripted" else "off")
    reason = None
    if kind == "agentcore" and memory_id is None:
        problems.append("--memory agentcore needs a memory id: set "
                        "ADA_AGENTCORE_MEMORY_ID or pass --memory-id (python "
                        "scripts/aws/agentcore_memory.py create prints it)")
    if kind == "agentcore" and memory_id and not MEMORY_ID.match(memory_id):
        problems.append("the AgentCore memory id must look like "
                        "<name>-<10 letters or digits>, as create prints it")
    if actor is not None and not _ACCOUNT.match(actor):
        problems.append("--memory-actor must be an account name: letters, digits "
                        "and . _ : @ -, at most 128")
    if mcp_url and kind != "off" and actor is None:
        if explicit or memory_id:
            problems.append("--mcp-url with memory needs --memory-actor: the sim "
                            "cannot tell whose preferences these are")
        else:
            kind = "off"
            reason = ("Memory is off with --mcp-url until --memory-actor names whose "
                      "preferences these are.")
    if kind == "off" and reason is None:
        reason = OFF_FIX
    return kind, (memory_id if kind == "agentcore" else None), actor, reason


# -- the page's server ------------------------------------------------------------


@dataclass
class SimApp:
    """Everything a request handler reads; built by :func:`build` or a test."""

    host: ConversationHost
    config: dict[str, Any]
    speaker: Any = None
    board_file: Callable[[str], Path | None] | None = None
    budget: Any = None
    closing: threading.Event = field(default_factory=threading.Event)
    keepalive_s: float = 15.0
    _svg_cache: dict[tuple[str, float], str] = field(default_factory=dict)
    _svg_lock: threading.Lock = field(default_factory=threading.Lock)

    def config_view(self) -> dict[str, Any]:
        view = dict(self.config)
        if self.budget is not None:
            view["budget"] = self.budget.snapshot()
        return view

    def svg(self, path: Path) -> str:
        from . import cards

        key = (str(path), path.stat().st_mtime)
        with self._svg_lock:
            if key in self._svg_cache:
                return self._svg_cache[key]
        drawn = cards.board_svg(path)
        with self._svg_lock:
            self._svg_cache = {key: drawn}
        return drawn


def _depth(value: Any, level: int = 0) -> int:
    if isinstance(value, dict):
        return max([level + 1] + [_depth(v, level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([level] + [_depth(v, level) for v in value])
    return level


def _check_turn(body: Any) -> tuple[str, str, dict[str, Any] | None]:
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be 1 to 500 characters")
    text = " ".join(text.split())
    if len(text) > MAX_TEXT:
        raise ValueError("text must be 1 to 500 characters")
    source = body.get("source", "typed")
    if source not in SOURCES:
        raise ValueError("source must be voice, typed or chip")
    hint = body.get("hint")
    if hint is None:
        return text, source, None
    if not isinstance(hint, dict) or hint.get("tool") not in ADA_TOOLS:
        raise ValueError("hint.tool must be one of Ada's six tools")
    arguments = hint.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("hint.arguments must be an object")
    if len(json.dumps(arguments)) > MAX_HINT_BYTES or _depth(arguments) > 2:
        raise ValueError("hint.arguments is too large or too deep")
    return text, source, {"tool": hint["tool"], "arguments": arguments}


class SimHandler(BaseHTTPRequestHandler):
    app: SimApp
    server_version = "AdaAlexaSim/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        path = str(getattr(self, "path", "") or "")
        # The event stream and the board image are polled; keep stderr quiet.
        if "/events" in path or path.startswith("/static/"):
            return
        sys.stderr.write(f"[alexa-sim] {fmt % args}\n")

    # -- responses ------------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Mapping[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any,
              extra: Mapping[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json", extra)

    # -- guards -----------------------------------------------------------------

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        return (self.headers.get("Host") or "").strip().lower() in allowed

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        return origin.strip().lower() in {
            f"http://127.0.0.1:{port}", f"http://localhost:{port}",
            f"http://[::1]:{port}"}

    def _body(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError:
            length = -1
        if length < 0:
            raise ValueError("Content-Length is not a number")
        if length > MAX_BODY:
            raise OverflowError
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    # -- dispatch ---------------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(421, {"error": "misdirected_request"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        parts = [p for p in path.split("/") if p]
        try:
            if path in ("/", "/index.html"):
                self._page()
            elif path == "/healthz":
                self._json(200, {"ok": True})
            elif len(parts) >= 2 and parts[0] == "static":
                self._static("/".join(parts[1:]))
            elif len(parts) == 2 and parts[0] == "lib":
                self._lib(parts[1])
            elif path == "/api/config":
                self._json(200, self.app.config_view())
            elif len(parts) == 3 and parts[:2] == ["api", "conversations"]:
                conv = self.app.host.get(parts[2])
                self._json(200, conv.snapshot())
            elif len(parts) == 4 and parts[:2] == ["api", "conversations"] \
                    and parts[3] == "events":
                self._events(parts[2], query)
            elif len(parts) == 5 and parts[:2] == ["api", "conversations"] \
                    and parts[3] == "speech":
                self._speech(parts[2], parts[4])
            elif len(parts) == 4 and parts[:2] == ["api", "boards"]:
                self._board(parts[2], parts[3])
            else:
                self._json(404, {"error": "not_found"})
        except UnknownConversation:
            self._json(404, {"error": "unknown_conversation"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(421, {"error": "misdirected_request"})
            return
        if not self._origin_ok():
            self._json(403, {"error": "forbidden_origin"})
            return
        path = urllib.parse.urlsplit(self.path).path
        parts = [p for p in path.split("/") if p]
        try:
            body = self._body()
        except OverflowError:
            self._json(413, {"error": "too_large", "limit": MAX_BODY})
            return
        except ValueError:
            self._json(400, {"error": "bad_json"})
            return
        try:
            if parts == ["api", "conversations"]:
                self._create(body)
            elif len(parts) == 4 and parts[:2] == ["api", "conversations"] \
                    and parts[3] == "turns":
                self._turn(parts[2], body)
            else:
                self._json(404, {"error": "not_found"})
        except UnknownConversation:
            self._json(404, {"error": "unknown_conversation"})

    # -- routes -----------------------------------------------------------------

    def _page(self) -> None:
        body = (WEB_DIR / "index.html").read_bytes()
        self._send(200, body, "text/html; charset=utf-8", {
            "Content-Security-Policy": CSP, "Referrer-Policy": "no-referrer"})

    def _static(self, name: str) -> None:
        content_type = STATIC_FILES.get(name)
        if content_type is None:
            self._json(404, {"error": "not_found"})
            return
        self._send(200, (WEB_DIR / name).read_bytes(), content_type)

    def _lib(self, name: str) -> None:
        if name not in LIB_FILES or not (LIB_DIR / name).is_file():
            self._json(404, {"error": "not_found"})
            return
        self._send(200, (LIB_DIR / name).read_bytes(),
                   "text/javascript; charset=utf-8")

    def _create(self, body: Any) -> None:
        locale = body.get("locale") if isinstance(body, dict) else None
        if not isinstance(locale, str) or not _LOCALE.match(locale):
            locale = "en-US"
        try:
            conv = self.app.host.create(locale)
        except Full:
            self._json(503, {"error": "full",
                             "detail": "every conversation is busy; try again soon"})
            return
        self._json(201, {"conversation_id": conv.id,
                         "events_url": f"/api/conversations/{conv.id}/events",
                         "mode": self.app.config.get("mode"),
                         "label": LABEL})

    def _turn(self, cid: str, body: Any) -> None:
        if not _CONV_ID.match(cid):
            raise UnknownConversation(cid)
        try:
            text, source, hint = _check_turn(body)
        except ValueError as exc:
            self._json(400, {"error": "bad_request", "detail": str(exc)})
            return
        try:
            accepted = self.app.host.turn(cid, text, source, hint)
        except Busy as exc:
            self._json(409, {"error": "busy", "speech": str(exc)})
            return
        self._json(202, accepted)

    def _events(self, cid: str, query: Mapping[str, list[str]]) -> None:
        conv = self.app.host.get(cid)
        after_raw = (self.headers.get("Last-Event-ID")
                     or (query.get("after") or ["0"])[0])
        try:
            after = max(0, int(after_raw))
        except ValueError:
            after = 0
        if "text/event-stream" not in (self.headers.get("Accept") or ""):
            self._json(200, {"events": conv.log.since(after),
                             "next": conv.log.next_seq, "busy": conv.busy})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not self.app.closing.is_set():
                events = conv.log.wait(after, self.app.keepalive_s)
                if self.app.closing.is_set() or conv.log.closed:
                    break
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                chunks = []
                for event in events:
                    chunks.append(
                        f"id: {event['seq']}\nevent: {event['kind']}\n"
                        f"data: {json.dumps(event)}\n\n")
                    after = event["seq"]
                self.wfile.write("".join(chunks).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _speech(self, cid: str, file_name: str) -> None:
        conv = self.app.host.get(cid)
        uid = file_name[:-4] if file_name.endswith(".mp3") else ""
        text = conv.utterances.get(uid) if _UTTERANCE.match(uid) else None
        if text is None:
            self._json(404, {"error": "unknown_utterance"})
            return
        if self.app.speaker is None:
            self._json(404, {"error": "no_audio", "fallback": "browser",
                             "reason": "browser_voice"})
            return
        from .polly import PollyError

        try:
            audio = self.app.speaker.synthesize(text)
        except PollyError as exc:
            conv.notice(POLLY_FALLBACK_NOTICE, level="warn", once="polly")
            conv.trace("polly", error=type(exc).__name__, reason=exc.reason)
            self._json(404, {"error": "no_audio", "fallback": "browser",
                             "reason": exc.reason})
            return
        if not audio.cached:
            conv.trace("polly", utterance_id=uid, bytes=len(audio.data),
                       billed_chars=audio.billed_chars)
        self._send(200, audio.data, audio.content_type or "audio/mpeg")

    def _board(self, sid: str, file_name: str) -> None:
        if file_name not in ("board.svg", "board.kicad_pcb") or \
                not _SESSION_ID.match(sid):
            self._json(404, {"error": "not_found"})
            return
        if self.app.board_file is None:
            self._json(503, {"error": "board_images_off",
                             "detail": "the board file is on the server that made it"})
            return
        path = self.app.board_file(sid)
        if path is None:
            self._json(404, {"error": "no_board"})
            return
        if file_name == "board.kicad_pcb":
            self._send(200, path.read_bytes(), "application/octet-stream", {
                "Content-Disposition": f'attachment; filename="{path.name}"'})
            return
        try:
            svg = self.app.svg(path)
        except Exception as exc:  # noqa: BLE001 -- the class name only
            self._json(500, {"error": "render_failed", "class": type(exc).__name__})
            return
        self._send(200, svg.encode("utf-8"), "image/svg+xml")


def make_sim_server(app: SimApp, host: str = "127.0.0.1",
                    port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundSimHandler", (SimHandler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


# -- building the whole thing --------------------------------------------------------


def board_file_lookup(store: Any, account: str,
                      steps_dir: Path) -> Callable[[str], Path | None]:
    """``session_id`` to the routed ``.kicad_pcb`` it names, or ``None``.

    The row must be this sim's account's, its ``files.board`` set, inside the
    steps directory, and a ``.kicad_pcb`` that exists.
    """
    root = steps_dir.expanduser().resolve()

    def lookup(session_id: str) -> Path | None:
        if not _SESSION_ID.match(session_id):
            return None
        board = store.get(account, session_id)
        if board is None or not board.summary:
            return None
        raw = ((board.summary.get("files") or {}).get("board")) or None
        if not raw:
            return None
        path = Path(raw).expanduser().resolve()
        if path.suffix != ".kicad_pcb" or not path.is_file():
            return None
        if root != path and root not in path.parents:
            return None
        return path

    return lookup


def _port_free(port: int) -> bool:
    if port == 0:
        return True
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            return exc.errno not in (errno.EADDRINUSE, errno.EACCES)
    return True


def _strands_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("strands-agents")
    except Exception:  # noqa: BLE001
        return None


def preflight(config: SimConfig) -> list[str]:
    """Every reason the sim cannot start; ``[]`` when it can."""
    problems: list[str] = []
    try:
        import strands  # noqa: F401
    except ImportError:
        problems.append(f"the alexa extra is not installed (Strands Agents); run "
                        f"{INSTALL_LINE}")
    needs = [name for name, used in (
        ("Bedrock", config.agent == "bedrock"), ("Polly", config.tts == "polly"),
        ("AgentCore Memory", config.memory == "agentcore")) if used]
    if needs:
        try:
            import boto3
        except ImportError:
            problems.append(f"boto3 is not installed; run {INSTALL_LINE}")
        else:
            try:
                credentials = boto3.Session().get_credentials()
            except Exception:  # noqa: BLE001 -- never echo why
                credentials = None
            if credentials is None:
                memory_way = (", --memory scripted or --memory off"
                              if config.memory == "agentcore" else "")
                problems.append(f"no AWS credentials for {' and '.join(needs)}; run "
                                f"with --scripted{memory_way}, or sign in with the "
                                "AWS CLI")
    if config.workers == "live" and config.mcp_url is None:
        from .app import _provider_problem

        problem = _provider_problem()
        if problem:
            problems.append(f"{problem} Or pass --workers scripted.")
    if config.mcp_url is None and not _port_free(config.mcp_port):
        problems.append(f"port {config.mcp_port} is busy for the MCP tools; pass "
                        "--mcp-port 0 for a free one")
    if not _port_free(config.port):
        problems.append(f"port {config.port} is busy for the page; pass --port 0 "
                        "or another port")
    return problems


@dataclass
class Sim:
    """A running sim: both servers, the MCP client and the conversations."""

    config: SimConfig
    app: SimApp
    server: ThreadingHTTPServer
    host: ConversationHost
    factory: Any
    client: Any
    mcp_server: ThreadingHTTPServer | None = None
    runner: Any = None
    mcp_url: str = ""
    threads: list[threading.Thread] = field(default_factory=list)
    #: ``shutdown`` waits for ``serve_forever`` to return, so it is only
    #: called on a server that was started.
    serving: bool = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def serve_in_background(self) -> Sim:
        thread = threading.Thread(target=self.server.serve_forever, args=(0.05,),
                                  daemon=True, name="alexa-sim-http")
        thread.start()
        self.threads.append(thread)
        self.serving = True
        return self

    def close(self) -> None:
        self.app.closing.set()
        for session in getattr(self.factory, "sessions", []):
            session.close()
        self.host.close()
        self.host.join(10)
        for session in getattr(self.factory, "sessions", []):
            session.poller.join(10)
            # The writer drains what the last turns queued (at most 5 s each).
            session.memory.close(5)
        if self.serving:
            with contextlib.suppress(Exception):
                self.server.shutdown()
        self.server.server_close()
        with contextlib.suppress(Exception):
            self.client.stop(None, None, None)
        if self.mcp_server is not None:
            with contextlib.suppress(Exception):
                self.mcp_server.shutdown()
            self.mcp_server.server_close()
        if self.runner is not None:
            self.runner.join(10)
            self.runner.store.close()


def build(
    config: SimConfig,
    *,
    env: Mapping[str, str] | None = None,
    poll_min_s: float | None = None,
    poll_cap_s: float | None = None,
    polly_client: Any = None,
    memory_clients: tuple[Any, Any] | None = None,
) -> Sim:
    """Start the MCP tools (unless ``--mcp-url``), the MCP client, and bind the
    page's server; nothing serves until :meth:`Sim.serve_in_background` or
    ``serve_forever``. ``poll_*``, ``polly_client`` and ``memory_clients``
    (``(bedrock-agentcore, bedrock-agentcore-control)``) are the tests' seams.

    AgentCore Memory is probed first (one ``GetMemory``), so a resource that
    is missing, not ACTIVE or differently shaped refuses before anything
    starts."""
    from strands.tools.mcp import MCPClient

    from . import agent as voice
    from . import app as alexa_app
    from . import auth, tools
    from . import memory as mem
    from .config import load_config

    env = os.environ if env is None else env
    budget = voice.Budget(config.max_model_calls, config.max_polly_calls,
                          config.max_memory_calls)
    memory = _build_memory(config, budget, memory_clients)
    actor = mem.actor_id(config.memory_actor) if config.memory_actor else None
    runner = None
    mcp_server = None
    images = config.mcp_url is None
    credential = (env.get("ALEXA_SIM_MCP_KEY") or "").strip() or None
    board_file = None
    if config.mcp_url is None:
        argv = ["--port", str(config.mcp_port)]
        if config.db:
            argv += ["--db", config.db]
        if config.workers == "scripted":
            argv += ["--scripted", "--scripted-delay", str(config.scripted_delay_s)]
        alexa_config = load_config(argv, env)
        credential = credential or alexa_config.token
        runner = alexa_app.startup(
            alexa_config,
            banner="SCRIPTED workers: canned answers from alexabot/scripted.py; "
            "every board is the practice regulator")
        mcp_server = alexa_app.make_server(alexa_config, runner=runner)
        # Polls arrive every few seconds; the MCP access log would drown the
        # banner. The page's own server still logs its turns.
        mcp_server.RequestHandlerClass.log_message = lambda *a, **k: None
        threading.Thread(target=mcp_server.serve_forever, args=(0.05,),
                         daemon=True, name="alexa-sim-mcp").start()
        mcp_url = f"http://127.0.0.1:{mcp_server.server_address[1]}/mcp"
        subject = auth.make_verifier(alexa_config)(credential)
        if subject is None:
            raise SimConfigError([
                "the MCP tools need a credential: set ALEXA_SIM_MCP_KEY to an ada_ "
                "key (or MCP_HTTP_TOKEN) so the sim's agent can call them"])
        steps_dir = Path(os.environ.get("SILKSCREEN_STEPS_DIR", "~/.kaleo/alexa-steps"))
        board_file = board_file_lookup(runner.store, subject.subject, steps_dir)
        # Whose preferences: the account whose boards these are, hashed.
        actor = actor or mem.actor_id(subject.subject)
        server_name = tools.SERVER_INFO["title"]
    else:
        mcp_url = config.mcp_url
        server_name = "Ada"
    headers = {"Authorization": f"Bearer {credential}"} if credential else None
    client = MCPClient(url=mcp_url, headers=headers, application_name="ada-alexa-sim",
                       startup_timeout=30)
    client.start()
    listed = client.list_tools_sync()
    if config.agent == "scripted":
        from .scripted_agent import scripted_model

        model_factory = scripted_model
        model_id = None
    else:
        model_factory = voice.bedrock_model_factory(config.model_id, config.region)
        model_id = config.model_id
    factory = voice.VoiceAgentFactory(
        model_factory=model_factory,
        tools=listed,
        server_name=server_name,
        server_instructions=client.server_instructions or "",
        budget=budget,
        model_id=model_id,
        metered=config.agent == "bedrock",
        images=images,
        poll_min_s=voice.HOST_POLL_MIN_S if poll_min_s is None else poll_min_s,
        poll_cap_s=voice.HOST_POLL_CAP_S if poll_cap_s is None else poll_cap_s,
        memory=memory,
        actor=actor,
    )
    speaker = None
    if config.tts == "polly":
        from .polly import PollySpeaker, make_client

        speaker = PollySpeaker(polly_client or make_client(config.region),
                               voice=config.voice, take=budget.take_polly)
    host = ConversationHost(factory, audio=speaker is not None,
                            scripted=config.agent == "scripted")
    strands_version = _strands_version()
    framework = f"Strands Agents {strands_version}" if strands_version else \
        "Strands Agents"
    config_view: dict[str, Any] = {
        "label": LABEL,
        "mode": config.mode,
        "agent": (
            {"kind": "scripted", "framework": framework, "model_id": None}
            if config.agent == "scripted" else
            {"kind": "bedrock", "model_id": config.model_id, "region": config.region,
             "framework": framework}
        ),
        "workers": config.workers,
        "tts": ({"kind": "polly", "voice": config.voice, "engine": "neural"}
                if speaker is not None else {"kind": "browser"}),
        "mcp": {"url": mcp_url, "server": server_name, "spec": MCP_SPEC,
                "tools": [t.tool_name for t in listed]},
        "board_images": images,
        "memory": memory_view(config, memory),
    }
    app = SimApp(host=host, config=config_view, speaker=speaker,
                 board_file=board_file, budget=budget)
    server = make_sim_server(app, config.host, config.port)
    return Sim(config=config, app=app, server=server, host=host, factory=factory,
               client=client, mcp_server=mcp_server, runner=runner, mcp_url=mcp_url)


def _build_memory(config: SimConfig, budget: Any,
                  clients: tuple[Any, Any] | None) -> Any:
    from . import memory as mem

    if config.memory == "scripted":
        return mem.ScriptedMemory()
    if config.memory != "agentcore" or not config.memory_id:
        return mem.MemoryOff(config.memory_reason or mem.OFF_FIX)
    data, control = clients or mem.make_clients(config.memory_region)
    if budget.take_memory():
        problems = mem.probe(control, config.memory_id)
    else:
        problems = [f"AgentCore Memory: {mem.REASONS['budget']}"]
    if problems:
        raise SimConfigError(problems)
    return mem.AgentCoreMemory(data, config.memory_id, region=config.memory_region,
                               take=budget.take_memory)


def memory_view(config: SimConfig, memory: Any) -> dict[str, Any]:
    """``/api/config``'s memory block: what and where, never the memory id,
    the account or a credential."""
    from . import memory as mem

    view = {"kind": memory.kind, "name": mem.MEMORY_NAME, "strategy": None,
            "namespace": mem.NAMESPACE_TEMPLATE, "region": None, "reason": None}
    view.update(memory.describe())
    if memory.kind == "off":
        view["name"] = view["namespace"] = None
    return view


def banner(sim: Sim) -> str:
    c = sim.config
    if c.agent == "scripted":
        who = "SCRIPTED: a rule-based agent, no model, no AWS"
    else:
        who = f"live: {c.model_id} on Amazon Bedrock ({c.region})"
    voice = f"Amazon Polly {c.voice} (neural)" if c.tts == "polly" else \
        "the browser's voice"
    workers = "Ada's workers canned" if c.workers == "scripted" else \
        "Ada's workers on the configured model"
    if c.memory == "agentcore":
        remembers = f"AgentCore Memory in {c.memory_region}"
    elif c.memory == "scripted":
        remembers = "scripted memory, on this machine"
    else:
        remembers = "memory off"
    return (f"{LABEL} at {sim.url} ({who}; {workers}; {voice}; {remembers}). MCP "
            f"tools at {sim.mcp_url}. This is a simulation; it is not Alexa.")


def main(argv: list[str] | None = None) -> int:
    from .app import _load_dotenv

    _load_dotenv()
    try:
        config = load_sim_config(argv)
    except SimConfigError as exc:
        for problem in exc.errors:
            print(f"alexabot.sim: {problem}", file=sys.stderr)
        return 2
    problems = preflight(config)
    if problems:
        for problem in problems:
            print(f"alexabot.sim: {problem}", file=sys.stderr)
        return 2
    from .app import StartupError

    try:
        sim = build(config)
    except SimConfigError as exc:
        for problem in exc.errors:
            print(f"alexabot.sim: {problem}", file=sys.stderr)
        return 2
    except StartupError as exc:
        print(f"alexabot.sim: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 -- the class name only
        print(f"alexabot.sim: could not start ({type(exc).__name__})",
              file=sys.stderr)
        return 2
    print(banner(sim), file=sys.stderr, flush=True)

    def stop(*_: Any) -> None:
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, stop)
    try:
        sim.serving = True
        sim.server.serve_forever(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        sim.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
