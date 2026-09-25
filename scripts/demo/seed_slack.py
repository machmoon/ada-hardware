"""Seed a demo Slack workspace with Perch Robotics: channels, colleagues, a page.

The people are staged; Ada is not. This posts the cast's lines through one bot
token with ``username`` and ``icon_emoji`` overrides (Slack's
``chat.postMessage`` with the ``chat:write.customize`` scope), creates the
three channels the video shows, and posts the 02:13 page plus Ada's
acknowledgement. Everything Ada does afterwards in those channels comes from
``python -m slackbot socket`` and ``python -m meetbot join``, not from here.

Slack Web API shapes follow the method reference as of 2026-09-24:
``conversations.create`` {name}, ``conversations.join`` {channel},
``conversations.setTopic`` {channel, topic}, ``chat.postMessage``
{channel, text, username, icon_emoji, thread_ts}, ``reactions.add``
{channel, timestamp, name}, ``chat.delete`` {channel, ts},
``conversations.history`` {channel, limit}. Host allowlist and error hygiene
follow ``slackbot/slack.py``: only ``slack.com``, and no token in any message.

Usage (SLACK_BOT_TOKEN in the environment or .env):
  python scripts/demo/seed_slack.py            channels, colleagues
  python scripts/demo/seed_slack.py --page     the 02:13 page and Ada's ack
  python scripts/demo/seed_slack.py --react TS celebrate Ada's board message
  python scripts/demo/seed_slack.py --reset    delete this bot's own messages
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOST = "slack.com"
API = f"https://{HOST}/api/"
TOPIC_MARK = "perch-demo"

CHANNELS = {
    "alerts": "PagerDuty pages. On-call this week: Ada.",
    "standup": "Tuesday 9:00. Cameras optional, coffee mandatory.",
    "hw-feedr": "Feedr rev B power board. Ada builds here.",
}

CAST = {
    "maya": ("Maya Chen", ":woman-technologist:"),
    "raj": ("Raj Patel", ":gear:"),
    "bob": ("Bob Okafor", ":necktie:"),
    "priya": ("Priya Nair", ":rotating_light:"),
    "sam": ("Sam Lindqvist", ":student:"),
    "it": ("IT Helpdesk", ":computer:"),
    "pd": ("PagerDuty", ":pager:"),
}

# (channel, who, text); "ada" means the bot itself, no override.
LINES = [
    ("standup", "priya",
     "Standup in 5. Agenda: rev A brown-outs (again), rev B power board, "
     "who owns the fab order."),
    ("standup", "raj", "Can rev B be USB-C? Investors love USB-C."),
    ("standup", "maya",
     "The power LED has to be green. Non-negotiable. I will not elaborate."),
    ("standup", "bob", "We could just use a 5 volt rail and call it a day?"),
    ("standup", "sam",
     "Quick one: which pin is AVDD on the regulator? I replied to the thread, "
     "it's 40 deep now."),
    ("hw-feedr", "maya",
     "Rev B request: 3.3 V LDO off USB-C, green power LED, AMS1117 like rev A "
     "but with a real output cap this time. Datasheet is in your inbox."),
    ("hw-feedr", "raj",
     "Mounting holes where they were. Case is already printed, do not move them."),
    ("hw-feedr", "priya",
     "Field report: 3 units browned out this month. Rev A's output cap is "
     "ceramic and the LDO does not like it."),
    ("hw-feedr", "it",
     "Welcome Ada. Your laptop is ready for pickup at the front desk."),
    ("hw-feedr", "ada", "Thanks. I don't have hands, but I appreciate it."),
]

PAGE = ("alerts", "pd",
        "PAGE #4471 triggered 02:13 PDT: feedr-rev-a browned out in the field "
        "(3rd this month). On-call: Ada. Ack within 5 min.")
ACK = ("alerts", "ada",
       "Acknowledged in 0.4 s. Pulling rev A's schematic and the field report. "
       "I don't sleep.")

REACTIONS = ["tada", "rocket", "eyes", "green_heart"]


class SlackError(RuntimeError):
    pass


def _token() -> str:
    tok = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not tok:
        env = Path.cwd() / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("SLACK_BOT_TOKEN="):
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not tok.startswith("xoxb-"):
        raise SlackError("SLACK_BOT_TOKEN is missing or not an xoxb- bot token")
    return tok


def _call(tok: str, method: str, **params) -> dict:
    url = API + method
    if urllib.parse.urlsplit(url).netloc != HOST:
        raise SlackError("refusing a request off slack.com")
    body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(int(exc.headers.get("Retry-After", "2")))
                continue
            raise SlackError(f"{method}: HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise SlackError(f"{method}: {exc.reason}") from None
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown error')}")
        return data
    raise SlackError(f"{method}: rate limited four times")


def _channels(tok: str) -> dict[str, str]:
    """name -> id for the demo channels, creating and joining what is missing."""
    existing: dict[str, str] = {}
    cursor = None
    while True:
        page = _call(tok, "conversations.list", limit=200, cursor=cursor,
                     types="public_channel", exclude_archived=True)
        for ch in page.get("channels", []):
            existing[ch["name"]] = ch["id"]
        cursor = page.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
    ids = {}
    for name, topic in CHANNELS.items():
        cid = existing.get(name)
        if not cid:
            cid = _call(tok, "conversations.create", name=name)["channel"]["id"]
            print(f"created #{name} {cid}")
        _call(tok, "conversations.join", channel=cid)
        _call(tok, "conversations.setTopic", channel=cid,
              topic=f"{topic} [{TOPIC_MARK}]")
        ids[name] = cid
    return ids


def _post(tok: str, ids: dict[str, str], line: tuple[str, str, str],
          thread_ts: str | None = None) -> str:
    channel, who, text = line
    params = {"channel": ids[channel], "text": text, "thread_ts": thread_ts}
    if who != "ada":
        name, emoji = CAST[who]
        params.update(username=name, icon_emoji=emoji)
    ts = _call(tok, "chat.postMessage", **params)["ts"]
    time.sleep(0.6)  # Slack's posting tier: about one message per second
    return ts


def seed(tok: str) -> None:
    ids = _channels(tok)
    for line in LINES:
        _post(tok, ids, line)
    print("seeded", ", ".join(f"#{n}={i}" for n, i in ids.items()))
    print(f"put in .env: HARDY_SLACK_CHANNEL={ids['hw-feedr']}")


def page(tok: str) -> None:
    ids = _channels(tok)
    ts = _post(tok, ids, PAGE)
    time.sleep(1.5)
    _post(tok, ids, ACK, thread_ts=ts)
    _call(tok, "reactions.add", channel=ids["alerts"], timestamp=ts,
          name="white_check_mark")
    print("paged; Ada acknowledged in the thread")


def react(tok: str, ts: str) -> None:
    ids = _channels(tok)
    for name in REACTIONS:
        try:
            _call(tok, "reactions.add", channel=ids["hw-feedr"], timestamp=ts,
                  name=name)
        except SlackError as exc:
            if "already_reacted" not in str(exc):
                raise
    _post(tok, ids, ("hw-feedr", "raj", "Ship it."), thread_ts=ts)
    _post(tok, ids, ("hw-feedr", "maya", "It's green. We're good."), thread_ts=ts)
    print("reacted")


def reset(tok: str) -> None:
    ids = _channels(tok)
    me = _call(tok, "auth.test")["user_id"]
    removed = 0
    for cid in ids.values():
        hist = _call(tok, "conversations.history", channel=cid, limit=200)
        for msg in hist.get("messages", []):
            if msg.get("user") == me or msg.get("bot_id"):
                try:
                    _call(tok, "chat.delete", channel=cid, ts=msg["ts"])
                    removed += 1
                    time.sleep(0.6)
                except SlackError as exc:
                    print(f"kept {msg['ts']}: {exc}")
    print(f"deleted {removed} of this bot's messages")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--page", action="store_true")
    ap.add_argument("--react", metavar="TS")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args(argv)
    tok = _token()
    if args.reset:
        reset(tok)
    elif args.page:
        page(tok)
    elif args.react:
        react(tok, args.react)
    else:
        seed(tok)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SlackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
