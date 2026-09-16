"""The webhook surface and the run: a ticket in, a board attached to it.

``POST /jira/events`` verifies the signature against the raw bytes, drops a
repeated delivery (keyed on the signature, as OpenHands' route does), answers
202 at once and does the work on a thread, because Jira gives a webhook about
ten seconds and a board takes minutes. Like Devin, Ada comments as soon as it
starts and again when it finishes; the finished comment is the same report the
Gmail summary sends (``googleapps.runner.email_body``), and the KiCad project
is attached to the ticket as a zip.

A follow-up ``@ada`` comment is a new run over the ticket plus the new
instruction. There is no session to forward it to, and the comment says so.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .jira import Config, JiraClient, JiraError, Trigger, parse_event, verify_signature

MAX_BODY = 1 << 20

Build = Callable[[str, Path], Any]


def default_build(intent: str, output: Path) -> Any:
    from silkscreen.agents import generate_pcb
    from silkscreen.agents.providers import worker_model

    return generate_pcb(worker_model(), intent, output=output)


def intent_for(summary: str, description: str, trigger: Trigger) -> str:
    parts = [summary.strip(), description.strip()]
    if trigger.instructions:
        parts.append(
            f"Additional instructions from {trigger.requester}: {trigger.instructions}"
        )
    return "\n\n".join(p for p in parts if p)


def run_ticket(
    client: JiraClient, trigger: Trigger, build: Build = default_build
) -> None:
    from googleapps.runner import RunOutcome, email_body

    key = trigger.issue_key
    verb = {"label": "labelled", "assigned": "assigned", "mention": "asked"}[
        trigger.how
    ]
    followup = (
        " This is a fresh run over the ticket and your note."
        if trigger.how == "mention"
        else ""
    )
    try:
        client.comment(
            key,
            f"Ada is designing this board ({verb} by {trigger.requester}).{followup}",
        )
        summary, description = client.issue(key)
        intent = intent_for(summary, description, trigger)
        if not intent:
            client.comment(
                key,
                "Ada did not run: the ticket has no summary or description to design from.",
            )
            return
        with tempfile.TemporaryDirectory(prefix="ada-jira-") as tmp:
            result = build(intent, Path(tmp) / key / "board.kicad_pcb")
            report = email_body(RunOutcome(result=result)).replace(
                "The attached .kicad_pcb opens in KiCad 7 or 8.",
                f"The attached {key}-ada.zip is the KiCad project.",
            )
            client.comment(key, "{noformat}\n" + report + "\n{noformat}")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(Path(tmp).rglob("*")):
                    if path.is_file():
                        zf.write(path, path.relative_to(tmp))
            client.attach(key, f"{key}-ada.zip", buf.getvalue())
    except Exception as exc:  # noqa: BLE001 - every failure is said on the ticket
        try:
            client.comment(
                key,
                f"Ada could not finish this board: {type(exc).__name__}: {str(exc)[:500]}",
            )
        except JiraError:
            print(
                f"[jirabot] {key}: {exc} (and the failure comment did not post)",
                file=sys.stderr,
            )


class Dispatcher:
    """Signature, duplicate and trigger checks; one paid run at a time."""

    def __init__(
        self, config: Config, client: JiraClient, build: Build = default_build
    ):
        self.config, self.client, self.build = config, client, build
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._slot = threading.Semaphore(1)
        self.threads: list[threading.Thread] = []

    def handle(self, body: bytes, signature: str | None) -> tuple[int, dict]:
        if not verify_signature(self.config.webhook_secret, body, signature):
            return 401, {"error": "bad signature"}
        with self._lock:
            if signature in self._seen:
                return 200, {"skipped": "duplicate delivery"}
            self._seen[signature] = None
            while len(self._seen) > 1000:
                self._seen.popitem(last=False)
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"error": "body is not JSON"}
        trigger = parse_event(self.config, payload if isinstance(payload, dict) else {})
        if isinstance(trigger, str):
            return 200, {"skipped": trigger}

        def work():
            with self._slot:
                run_ticket(self.client, trigger, self.build)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        return 202, {"accepted": trigger.issue_key, "how": trigger.how}


def make_server(dispatcher: Dispatcher, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply(200, {"ok": True}) if self.path == "/healthz" else self._reply(
                404, {}
            )

        def do_POST(self):
            if self.path != "/jira/events":
                return self._reply(404, {})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, {"error": "body too large"})
            self._reply(
                *dispatcher.handle(
                    self.rfile.read(length), self.headers.get("X-Hub-Signature")
                )
            )

    return ThreadingHTTPServer((host, dispatcher.config.port), Handler)


def main() -> int:  # pragma: no cover - entry point
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
    from silkscreen.cli import _load_dotenv

    _load_dotenv(Path.cwd() / ".env")
    try:
        config = Config.from_env()
    except JiraError as exc:
        print(f"jirabot: {exc}", file=sys.stderr)
        return 2
    server = make_server(Dispatcher(config, JiraClient(config)), host="0.0.0.0")
    print(f"jirabot: POST /jira/events on :{config.port} for {config.base_url}")
    server.serve_forever()
    return 0
