"""Flags and environment for ``python -m alexabot``, read once and checked once.

The zoombot rule (``zoombot/config.py``): a half-configured server comes up,
looks healthy, and fails on the first real call, so :func:`load_config`
refuses to start instead, and names **every** problem at once.

Secrets arrive through the environment and leave through nothing:
:meth:`Config.redacted` shows what was loaded as masks and lengths.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ALEXABOT_ENV",
    "DEFAULT_DB",
    "DEFAULT_PORT",
    "DEFAULT_STEPS_DIR",
    "Config",
    "ConfigError",
    "load_config",
]

#: Every variable this package reads, in one place, for the docs and
#: ``.env.example`` to be checked against.
ALEXABOT_ENV = (
    "ALEXABOT_PORT",
    "ALEXABOT_DB",
    "ALEXABOT_MAX_ACTIVE",
    "MCP_HTTP_TOKEN",
    "SILKSCREEN_API_KEYS_DB",
    "SILKSCREEN_STEPS_DIR",
    "MCP_TOOL_CALLS_PER_MINUTE",
)

DEFAULT_HOST = "127.0.0.1"
#: 8788 is ``silkscreen.mcp.http``; 8080/8081 are taken on this team's machines.
DEFAULT_PORT = 8789
#: ``~/.kaleo`` is where the app keeps its state (CLAUDE.md, Naming).
DEFAULT_DB = "~/.kaleo/alexa-boards.sqlite3"
DEFAULT_STEPS_DIR = "~/.kaleo/alexa-steps"
DEFAULT_MAX_ACTIVE = 4
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: Browser origins always accepted besides loopback; ``--allow-origin`` adds.
DEFAULT_ORIGINS = frozenset({"https://claude.ai", "https://www.claude.ai"})


class ConfigError(RuntimeError):
    """The server cannot start as configured; ``errors`` names every reason."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.errors = tuple(problems)
        super().__init__(
            "alexabot is not configured: " + "; ".join(self.errors)
            + ". See docs/alexa.md."
        )


@dataclass(frozen=True)
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    db: str = DEFAULT_DB
    max_active: int = DEFAULT_MAX_ACTIVE
    scripted: bool = False
    scripted_delay_s: float = 0.0
    origins: frozenset[str] = DEFAULT_ORIGINS
    #: ``MCP_HTTP_TOKEN``: one shared bearer token, the ``local`` account.
    token: str | None = None
    #: ``SILKSCREEN_API_KEYS_DB``: per-account ``ada_`` keys (service/auth.py).
    keys_db: str | None = None

    @property
    def auth_configured(self) -> bool:
        return bool(self.token or self.keys_db)

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    def redacted(self) -> dict[str, str]:
        return {
            "host": self.host,
            "port": str(self.port),
            "db": self.db,
            "max_active": str(self.max_active),
            "scripted": str(self.scripted),
            "MCP_HTTP_TOKEN": (
                f"<set, {len(self.token)} chars>" if self.token else "unset"
            ),
            "SILKSCREEN_API_KEYS_DB": self.keys_db or "unset",
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alexabot",
        description="Serve Ada's voice tools (MCP over Streamable HTTP).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=None,
                        help=f"default ALEXABOT_PORT or {DEFAULT_PORT}")
    parser.add_argument("--db", default=None,
                        help=f"board history; default ALEXABOT_DB or {DEFAULT_DB}")
    parser.add_argument("--scripted", action="store_true",
                        help="canned model answers: a whole session offline, no key")
    parser.add_argument("--scripted-delay", default="0", metavar="SECONDS",
                        help="with --scripted, wait this long before each answer")
    parser.add_argument("--allow-origin", action="append", default=[],
                        metavar="ORIGIN",
                        help="an extra browser origin to accept")
    return parser


def load_config(
    argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None
) -> Config:
    """Parse ``argv`` over ``env`` and check the result; every gap at once."""
    env = os.environ if env is None else env
    args = _parser().parse_args(list(argv) if argv is not None else None)
    problems: list[str] = []

    raw_port = args.port if args.port is not None else env.get("ALEXABOT_PORT", "")
    port = DEFAULT_PORT
    if str(raw_port).strip():
        try:
            port = int(str(raw_port).strip())
        except ValueError:
            problems.append(f"port must be a whole number, got {raw_port!r}")
        else:
            if not 0 <= port <= 65535:
                problems.append(f"port must be 0 to 65535, got {port}")

    raw_active = env.get("ALEXABOT_MAX_ACTIVE", "").strip()
    max_active = DEFAULT_MAX_ACTIVE
    if raw_active:
        try:
            max_active = int(raw_active)
        except ValueError:
            max_active = 0
        if max_active < 1:
            problems.append(
                "ALEXABOT_MAX_ACTIVE must be a positive whole number, got "
                f"{raw_active!r}"
            )

    try:
        delay = float(args.scripted_delay)
    except ValueError:
        delay = -1.0
    if delay < 0:
        problems.append(f"--scripted-delay must be 0 or more seconds, got "
                        f"{args.scripted_delay!r}")
    elif delay and not args.scripted:
        problems.append("--scripted-delay only means something with --scripted")

    db = args.db or env.get("ALEXABOT_DB", "").strip() or DEFAULT_DB
    if db != ":memory:":
        db = str(Path(db).expanduser())
    token = env.get("MCP_HTTP_TOKEN", "").strip() or None
    keys_db = env.get("SILKSCREEN_API_KEYS_DB", "").strip() or None

    config = Config(
        host=args.host,
        port=port,
        db=db,
        max_active=max_active,
        scripted=args.scripted,
        scripted_delay_s=max(delay, 0.0),
        origins=DEFAULT_ORIGINS | frozenset(args.allow_origin),
        token=token,
        keys_db=keys_db,
    )
    if not config.loopback and not config.auth_configured:
        # The tools spend a model key; a public address with no credential
        # is anyone's key (http.py binds loopback by default, transports.mdx:83).
        problems.append(
            f"--host {config.host} is not loopback and no credential is "
            "configured; set MCP_HTTP_TOKEN or SILKSCREEN_API_KEYS_DB"
        )
    if problems:
        raise ConfigError(problems)
    return config
