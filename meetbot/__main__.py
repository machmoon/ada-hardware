"""``python -m meetbot join <meet-url>`` -- Ada attends a call, then builds.

Reads ``.env`` the way ``python -m slackbot`` does (``silkscreen.cli._load_dotenv``,
setdefault semantics, so an exported variable wins).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))


def main(argv: list[str] | None = None) -> int:
    from silkscreen.cli import _load_dotenv

    parser = argparse.ArgumentParser(prog="python -m meetbot")
    sub = parser.add_subparsers(dest="command", required=True)
    join = sub.add_parser("join", help="join a Meet call as Ada")
    join.add_argument("url", help="https://meet.google.com/abc-defg-hij")
    join.add_argument(
        "--no-slack",
        action="store_true",
        help="no recap, questions or progress in Slack",
    )
    join.add_argument(
        "--no-build",
        action="store_true",
        help="do not hand anything to the laptop overlay",
    )
    join.add_argument("--headless", action="store_true")
    join.add_argument(
        "--profile", type=Path, help="Chromium profile (HARDY_PROFILE_DIR)"
    )
    join.add_argument(
        "--wait",
        type=float,
        metavar="S",
        help="seconds to wait for a Slack answer (HARDY_CLARIFY_WAIT_S)",
    )
    join.add_argument(
        "--no-follow",
        action="store_true",
        help="hand off the build but do not post its progress",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    _load_dotenv(Path.cwd() / ".env")

    from .config import ConfigError, AdaConfig, parse_meet_url

    try:
        parse_meet_url(args.url)
        config = AdaConfig.from_env(require_slack=not args.no_slack)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    changes: dict = {}
    if args.no_slack:
        changes.update(slack_token="", slack_channel="")
    if args.no_build:
        changes["build"] = False
    if args.no_follow:
        changes["follow"] = False
    if args.headless:
        changes["headless"] = True
    if args.profile:
        changes["profile_dir"] = args.profile.expanduser()
    if args.wait is not None:
        changes["clarify_wait_s"] = max(0.0, args.wait)
    config = replace(config, **changes)
    for key, value in config.redacted().items():
        logging.getLogger("meetbot").info("config %s = %s", key, value)

    from silkscreen.agents.model import GeminiModel, ModelError

    from .clarify import ThreadClient
    from .runner import MeetEngineClient, run

    try:
        model = GeminiModel(config.model, api_key=config.google_api_key)
        reply_model = GeminiModel(config.reply_model, api_key=config.google_api_key)
    except ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    slack = ThreadClient(config.slack_token) if config.slack_enabled else None
    engine = None
    if config.build:
        engine = MeetEngineClient(config.engine_url, config.engine_token)

    try:
        report = asyncio.run(
            run(
                args.url,
                config,
                model=model,
                reply_model=reply_model,
                slack=slack,
                engine=engine,
            )
        )
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    print("\n".join(report.lines()))
    if report.join is None or not report.join.admitted:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
