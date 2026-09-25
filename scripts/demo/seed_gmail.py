"""Seed the demo inbox: the "which pin is AVDD" thread, the datasheet mail, onboarding.

The people are staged; Ada is not. Gmail's ``users.messages.insert`` puts a
complete RFC 822 message into the signed-in mailbox exactly as given, ``From``
header included, which ``messages.send`` refuses. That is how the cast's mail
appears in Pat's inbox without any other account existing. It needs one extra
scope, ``gmail.insert``, granted once into its own token file so the product's
token (``googleapps``: send, readonly, calendar) is never widened.

Shapes follow the Gmail API reference as of 2026-09-24:
``POST https://gmail.googleapis.com/gmail/v1/users/me/messages``
``?internalDateSource=dateHeader`` with JSON ``{"raw": <base64url RFC 822>,
"labelIds": ["INBOX", "UNREAD"]}``; threading by ``In-Reply-To`` /
``References`` the way ``googleapps/gmail.py`` builds messages with the
stdlib ``email`` package. OAuth reuses ``googleapps.auth.run_auth_flow`` and
``access_token`` (loopback, PKCE S256) with the transport's host allowlist,
and the client id comes from ``~/.kaleo/google.env`` through
``service.envfiles.apply_saved_env`` exactly as the service loads it.

Usage (the first run opens the browser for the one-time consent):
  python scripts/demo/seed_gmail.py --datasheet path/to/AMS1117.pdf
  python scripts/demo/seed_gmail.py --page          only the 02:13 page mail
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from googleapps import auth  # noqa: E402
from googleapps.config import load_config  # noqa: E402
from googleapps.transport import (  # noqa: E402
    HttpRequest,
    ensure_google_url,
    urllib_transport,
)
from service.envfiles import apply_saved_env  # noqa: E402

INSERT_SCOPE = "https://www.googleapis.com/auth/gmail.insert"
TOKEN_PATH = Path("~/.kaleo/demo-gmail-token.json").expanduser()
#: ``messages.insert`` is a POST on the messages collection (the REST
#: reference maps the method to ``POST /gmail/v1/users/{userId}/messages``);
#: only ``send`` and ``import`` have their own path segment. Measured
#: 2026-09-24: ``/messages/insert`` answers an HTML 404 from Google's edge.
INSERT_URL = ensure_google_url(
    "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    "?internalDateSource=dateHeader"
)
DOMAIN = "perchrobotics.example"

CAST = {
    "maya": ("Maya Chen", f"maya@{DOMAIN}"),
    "raj": ("Raj Patel", f"raj@{DOMAIN}"),
    "bob": ("Bob Okafor", f"bob@{DOMAIN}"),
    "priya": ("Priya Nair", f"priya@{DOMAIN}"),
    "sam": ("Sam Lindqvist", f"sam@{DOMAIN}"),
    "it": ("IT Helpdesk", f"helpdesk@{DOMAIN}"),
    "pd": ("PagerDuty", f"no-reply@pagerduty.{DOMAIN}"),
}

AVDD_THREAD = [
    ("sam", "Quick question: which pin is AVDD on the AMS1117? The datasheet has "
            "three tables."),
    ("raj", "Isn't it pin 3? Or is that Vout."),
    ("maya", "Pin 3 is Vout on SOT-223. Pin 2 is also Vout (the tab). Pin 1 is "
             "ADJ/GND."),
    ("bob", "Do we even need AVDD? Can we not just use 5 volts."),
    ("sam", "So Vin is... pin 3? Sorry."),
    ("priya", "Please take this to #hw-feedr, half the company is cc'd."),
    ("raj", "+1"),
    ("maya", "Vin is pin 3 on the fixed version, Vout is pin 2 and the tab. I am "
             "going to draw it."),
    ("sam", "Drawing would help. Also which cap goes on the output, the 22 uF "
            "ceramic?"),
    ("priya", "That ceramic is why rev A browns out. Ask Ada, it reads the "
              "datasheet."),
    ("bob", "Who is Ada?"),
    ("maya", "The new hardware engineer. Started this morning. Ada, over to you."),
]

ONBOARDING = [
    ("it", "Welcome to Perch Robotics: your laptop is ready",
     "Hi Ada,\n\nYour laptop is ready for pickup at the front desk. Please bring "
     "your badge.\n\nIT Helpdesk"),
    ("priya", "On-call rota: you're on this week",
     "Ada,\n\nI put you on the rota from tonight. PagerDuty will page you. Try to "
     "ack within five minutes.\n\nPriya"),
]

DATASHEET_MAIL = (
    "maya", "AMS1117 datasheet for rev B",
    "Ada,\n\nRev B power board: 3.3 V LDO off USB-C, green power LED. Datasheet "
    "attached. Please keep the mounting holes where they are; Raj already printed "
    "the case.\n\nMaya",
)

PAGE_MAIL = (
    "pd", "[PagerDuty] PAGE #4471: feedr-rev-a browned out in the field",
    "Triggered 02:13 PDT.\nService: feedr-rev-a\nDetails: 3 units browned out "
    "this month. Output cap is ceramic; LDO unstable under load.\nOn-call: Ada\n"
    "Ack within 5 minutes.",
)


def _message(who: str, subject: str, body: str, when: datetime, *, to: str,
             msgid: str, refs: list[str] | None = None,
             attachment: tuple[str, bytes] | None = None) -> EmailMessage:
    name, addr = CAST[who]
    msg = EmailMessage()
    msg["From"] = f"{name} <{addr}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = msgid
    if refs:
        msg["In-Reply-To"] = refs[-1]
        msg["References"] = " ".join(refs)
    msg.set_content(body)
    if attachment:
        fname, data = attachment
        msg.add_attachment(data, maintype="application", subtype="pdf",
                           filename=fname)
    return msg


def _insert(token: str, msg: EmailMessage) -> str:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    body = json.dumps({"raw": raw, "labelIds": ["INBOX", "UNREAD"]}).encode()
    resp = urllib_transport()(HttpRequest(
        method="POST", url=INSERT_URL, body=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    ))
    if resp.status != 200:
        raise RuntimeError(f"insert failed: HTTP {resp.status}")
    return resp.json().get("id", "?")


def _token() -> str:
    apply_saved_env()
    config = dataclasses.replace(load_config(), token_path=TOKEN_PATH)
    config.require_oauth()
    transport = urllib_transport()
    if auth.token_status(TOKEN_PATH) == "missing":
        print("opening the browser for a one-time consent (gmail.insert); "
              "this waits up to 30 minutes", flush=True)
        # The default loopback wait is 300 s, sized for someone already at the
        # keyboard; a demo seed is started ahead of time, so wait longer and
        # print the URL in case the browser did not open.
        def show(url: str) -> None:
            print(f"consent URL: {url}", flush=True)

        authorize = auth._loopback_authorize(  # noqa: SLF001
            timeout_s=1800.0, on_url=show
        )
        auth.run_auth_flow(config, transport, scopes=(INSERT_SCOPE,),
                           authorize=authorize)
    return auth.access_token(config, transport, require=(INSERT_SCOPE,))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to", default="ada@perchrobotics.example",
                    help="the address shown in To")
    ap.add_argument("--datasheet", type=Path,
                    help="AMS1117 PDF to attach to Maya's mail")
    ap.add_argument("--page", action="store_true",
                    help="only the PagerDuty mail, dated now")
    args = ap.parse_args(argv)
    token = _token()
    now = datetime.now(UTC).astimezone()
    to = args.to

    if args.page:
        who, subject, body = PAGE_MAIL
        _insert(token, _message(who, subject, body, now, to=to,
                                msgid=make_msgid(domain=DOMAIN)))
        print("page mail inserted")
        return 0

    when = now - timedelta(days=2, hours=3)
    for who, subject, body in ONBOARDING:
        _insert(token, _message(who, subject, body, when, to=to,
                                msgid=make_msgid(domain=DOMAIN)))
        when += timedelta(minutes=17)
    refs: list[str] = []
    when = now - timedelta(days=1, hours=6)
    for i, (who, body) in enumerate(AVDD_THREAD):
        msgid = make_msgid(domain=DOMAIN)
        subject = ("" if i == 0 else "Re: " * min(i, 3)) + "which pin is AVDD"
        _insert(token, _message(who, subject, body, when, to=to, msgid=msgid,
                                refs=refs or None))
        refs.append(msgid)
        when += timedelta(minutes=23)
        time.sleep(0.2)
    attachment = None
    if args.datasheet:
        attachment = (args.datasheet.name, args.datasheet.read_bytes())
    who, subject, body = DATASHEET_MAIL
    _insert(token, _message(who, subject, body, now - timedelta(hours=2), to=to,
                            msgid=make_msgid(domain=DOMAIN),
                            attachment=attachment))
    print(f"inserted {len(ONBOARDING) + len(AVDD_THREAD) + 1} messages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
