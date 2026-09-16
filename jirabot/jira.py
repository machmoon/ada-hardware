"""Jira Cloud: who asked Ada for what, and the REST calls to answer on the ticket.

The triggers are Devin's (docs.devin.ai/integrations/jira): add the ``ada``
label, assign the ticket to Ada's service account, or comment ``@ada`` with
instructions. The parsing shape is OpenHands' removed Jira integration
(``enterprise/integrations/jira/jira_payload.py`` at OpenHands ee9e78b): one
parser maps a webhook to exactly one of *run*, *skipped* (with the reason) or
*error*, and the signature is ``X-Hub-Signature: sha256=<hmac>`` over the raw
body (``enterprise/server/routes/integration/jira.py``). Comments go to REST v2,
which takes a plain string body, as OpenHands' ``send_message`` does.

Standard library only. The transport refuses redirects (a 3xx would carry the
Basic credentials to another host) and every URL must be the configured
``https://<site>.atlassian.net``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

from googleapps.transport import HttpRequest, HttpResponse, _NoRedirect

__all__ = [
    "Config",
    "JiraClient",
    "JiraError",
    "Trigger",
    "parse_event",
    "verify_signature",
]


class JiraError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    base_url: str  # https://<site>.atlassian.net
    email: str  # the service account Ada acts as
    api_token: str
    webhook_secret: str
    #: The service account's accountId: the assignment trigger, and self-skip.
    account_id: str = ""
    label: str = "ada"
    mention: str = "@ada"
    port: int = 8096

    def __post_init__(self):
        host = urlparse(self.base_url).hostname or ""
        if not self.base_url.startswith("https://") or not host.endswith(
            ".atlassian.net"
        ):
            raise JiraError(
                f"JIRA_BASE_URL must be https://<site>.atlassian.net, got {self.base_url!r}"
            )
        missing = [
            n
            for n, v in (
                ("JIRA_EMAIL", self.email),
                ("JIRA_API_TOKEN", self.api_token),
                ("JIRA_WEBHOOK_SECRET", self.webhook_secret),
            )
            if not v
        ]
        if missing:
            raise JiraError(f"missing {', '.join(missing)}")

    @classmethod
    def from_env(cls, env=None) -> Config:
        env = os.environ if env is None else env
        return cls(
            base_url=env.get("JIRA_BASE_URL", "").rstrip("/"),
            email=env.get("JIRA_EMAIL", ""),
            api_token=env.get("JIRA_API_TOKEN", ""),
            webhook_secret=env.get("JIRA_WEBHOOK_SECRET", ""),
            account_id=env.get("JIRA_ACCOUNT_ID", ""),
            label=env.get("JIRA_LABEL", "ada"),
            mention=env.get("JIRA_MENTION", "@ada"),
            port=int(env.get("JIRA_PORT", "8096")),
        )


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """``X-Hub-Signature: sha256=<hex>`` over the raw bytes, before parsing."""
    algo, _, given = (header or "").partition("=")
    if algo != "sha256" or not given:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


@dataclass(frozen=True)
class Trigger:
    """A request for Ada on one ticket. ``instructions`` is the comment text
    after the mention, empty for a label or an assignment."""

    issue_key: str
    how: str  # "label" | "assigned" | "mention"
    requester: str
    instructions: str = ""


def parse_event(config: Config, payload: dict) -> Trigger | str:
    """A :class:`Trigger`, or a string saying why nothing should run."""
    event = payload.get("webhookEvent", "")
    issue = payload.get("issue") or {}
    key = str(issue.get("key", ""))
    if event == "jira:issue_updated":
        actor = payload.get("user") or {}
        how = ""
        for item in (payload.get("changelog") or {}).get("items", []):
            field = item.get("field")
            if (
                field == "labels"
                and config.label in str(item.get("toString") or "").split()
            ):
                if config.label not in str(item.get("fromString") or "").split():
                    how = "label"
            elif (
                field == "assignee"
                and config.account_id
                and item.get("to") == config.account_id
            ):
                how = "assigned"
        if not how:
            return f"issue update does not add the {config.label!r} label or assign Ada"
    elif event == "comment_created":
        comment = payload.get("comment") or {}
        actor = comment.get("author") or {}
        body = str(comment.get("body", ""))
        # Exact mention, case-insensitive, not a prefix of a longer handle.
        match = re.search(
            rf"(?<![\w@]){re.escape(config.mention)}(?![\w-])", body, re.IGNORECASE
        )
        if not match:
            return f"comment does not mention {config.mention}"
        how = "mention"
        instructions = (body[: match.start()] + body[match.end() :]).strip()
    else:
        return f"unhandled webhook event {event!r}"
    account = str(actor.get("accountId", ""))
    if config.account_id and account == config.account_id:
        return "event was made by Ada's own service account"
    if not key:
        return "payload has no issue.key"
    return Trigger(
        issue_key=key,
        how=how,
        requester=str(actor.get("displayName") or account or "someone"),
        instructions=instructions if how == "mention" else "",
    )


Transport = Callable[[HttpRequest], HttpResponse]


def urllib_transport(timeout: float = 30.0) -> Transport:
    opener = urllib.request.build_opener(_NoRedirect())

    def send(request: HttpRequest) -> HttpResponse:
        req = urllib.request.Request(
            request.url,
            data=request.body or None,
            headers=request.headers,
            method=request.method,
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                return HttpResponse(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read())
        except urllib.error.URLError as exc:
            raise JiraError(f"network error: {exc.reason}") from exc

    return send


class JiraClient:
    def __init__(self, config: Config, transport: Transport | None = None):
        self.config = config
        self.transport = transport or urllib_transport()
        token = base64.b64encode(f"{config.email}:{config.api_token}".encode()).decode()
        self._auth = f"Basic {token}"

    def _send(
        self, method: str, path: str, body: bytes = b"", headers: dict | None = None
    ) -> dict:
        url = f"{self.config.base_url}{path}"
        if urlparse(url).hostname != urlparse(self.config.base_url).hostname:
            raise JiraError("refusing a request off the configured Jira site")
        response = self.transport(
            HttpRequest(
                method,
                url,
                {
                    "Authorization": self._auth,
                    "Accept": "application/json",
                    **(headers or {}),
                },
                body,
            )
        )
        if response.status >= 300:
            # Jira's errorMessages never echo the token; the URL is left out anyway.
            raise JiraError(
                f"{method} {path.split('?')[0]} -> HTTP {response.status}: "
                f"{response.body[:300].decode('utf-8', 'replace')}"
            )
        return json.loads(response.body) if response.body.strip() else {}

    def issue(self, key: str) -> tuple[str, str]:
        """``(summary, description)`` -- v2 returns the description as text."""
        fields = self._send(
            "GET", f"/rest/api/2/issue/{key}?fields=summary,description"
        ).get("fields", {})
        return str(fields.get("summary") or ""), str(fields.get("description") or "")

    def comment(self, key: str, text: str) -> None:
        self._send(
            "POST",
            f"/rest/api/2/issue/{key}/comment",
            json.dumps({"body": text}).encode(),
            {"Content-Type": "application/json"},
        )

    def attach(self, key: str, filename: str, data: bytes) -> None:
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        # Jira refuses attachment uploads without this XSRF opt-out header.
        self._send(
            "POST",
            f"/rest/api/2/issue/{key}/attachments",
            body,
            {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Atlassian-Token": "no-check",
            },
        )
