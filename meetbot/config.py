"""Hardy's Meet-bot configuration, validated once, naming everything missing.

The ``meetings/config.py`` convention: every value is read here, a bad one is
a :class:`ConfigError` that names the variable, and a missing one is reported
together with every other missing one -- a first setup is one round of
corrections, not three. A half-configured bot that joins a call, talks, and
only then discovers it has no Slack token has already spent the part that
cannot be repeated: the meeting.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ConfigError",
    "HardyConfig",
    "parse_meet_url",
    "DEFAULT_ENGINE_URL",
]

#: ``silkscreen serve``'s address, the same default ``slackbot/bridge.py`` uses.
DEFAULT_ENGINE_URL = "http://127.0.0.1:8081"

#: Meet meeting codes are ``abc-defg-hij``. Anything else under meet.google.com
#: (``/new``, ``/landing``, a lookup alias) is not a meeting the bot can join.
_MEETING_CODE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")


class ConfigError(RuntimeError):
    """The bot is not configured well enough to join a call."""


def parse_meet_url(url: str) -> str:
    """Return the meeting code of a Meet URL, or raise naming what is wrong.

    Exact host, https only: this URL is typed into a signed-in Chrome profile,
    and ``https://meet.google.com@evil.example/abc-defg-hij`` must not be read
    as Meet (the userinfo-before-``@`` trap ``meetings/config.py`` documents).
    """
    url = (url or "").strip()
    if url.startswith("meet.google.com/"):
        url = "https://" + url  # the form people paste; session.py accepts it too
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "meet.google.com":
        raise ConfigError(
            f"not a Google Meet URL: {url!r} (expected "
            "https://meet.google.com/abc-defg-hij)"
        )
    code = parts.path.strip("/")
    if not _MEETING_CODE.match(code):
        raise ConfigError(
            f"{url!r} has no meeting code; expected a path like /abc-defg-hij"
        )
    return code


def _bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key}={raw!r} is not a boolean (use 1 or 0)")


def _number(env: dict[str, str], key: str, default, cast, *, minimum=None):
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key}={raw!r} is not a valid {cast.__name__}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key}={raw!r} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class HardyConfig:
    """Everything one ``python -m meetbot join`` needs."""

    google_api_key: str
    slack_token: str = ""
    #: A channel id (``C…``) or a user id (``U…``): posting to a user id opens
    #: the bot's DM with that person, and the reply poll reads the ``D…``
    #: channel Slack answers with.
    slack_channel: str = ""
    #: When set, only this Slack user's thread reply counts as the answer.
    slack_user: str = ""
    display_name: str = "Hardy"
    profile_dir: Path = field(
        default_factory=lambda: Path.home() / ".hardy" / "meet-profile"
    )
    headless: bool = False
    #: Worker model for the post-call extraction and questions.
    model: str = ""
    #: The in-call reply model. Latency is the product here: a reply that lands
    #: eight seconds after the sentence it answers has already talked over the
    #: next one, so this defaults to the cheap tier.
    reply_model: str = ""
    reply_cooldown_s: float = 20.0
    quiet_s: float = 1.2
    max_replies: int = 8
    reply_to_doubt: bool = True
    #: ``meetings.runner.DEFAULT_CONFIDENCE_FLOOR`` unless overridden.
    confidence_floor: float = 0.6
    #: How long to wait for a Slack answer to the open questions. 0 = do not wait.
    clarify_wait_s: float = 600.0
    #: With no answer in time: hand off the spec as stated (True), or stop (False).
    build_on_timeout: bool = True
    build: bool = True
    follow: bool = True
    engine_url: str = DEFAULT_ENGINE_URL
    engine_token: str = ""
    #: Upper bound on how long the bot stays in one call.
    max_call_s: float = 3 * 60 * 60

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_token and self.slack_channel)

    def redacted(self) -> dict[str, str]:
        """A printable view: a secret is a length, never a prefix or a tail."""

        def mask(value: str) -> str:
            return f"<set, {len(value)} chars>" if value else "<none>"

        return {
            "google_api_key": mask(self.google_api_key),
            "slack_token": mask(self.slack_token),
            "slack_channel": self.slack_channel or "<none>",
            "slack_user": self.slack_user or "<any>",
            "display_name": self.display_name,
            "profile_dir": str(self.profile_dir),
            "headless": str(self.headless),
            "model": self.model,
            "reply_model": self.reply_model,
            "reply_cooldown_s": str(self.reply_cooldown_s),
            "clarify_wait_s": str(self.clarify_wait_s),
            "build": str(self.build),
            "engine_url": self.engine_url,
            "engine_token": mask(self.engine_token),
        }

    @classmethod
    def from_env(
        cls, env: dict[str, str] | None = None, *, require_slack: bool = True
    ) -> HardyConfig:
        """Build from the environment, naming every missing variable at once."""
        from silkscreen.agents.model import CHEAP_MODEL, DEFAULT_MODEL

        from meetings.runner import DEFAULT_CONFIDENCE_FLOOR

        env = dict(os.environ if env is None else env)
        missing = []
        key = (
            env.get("GOOGLE_API_KEY", "").strip()
            or env.get("GEMINI_API_KEY", "").strip()
        )
        if not key:
            missing.append("GOOGLE_API_KEY")
        if require_slack:
            for name in ("SLACK_BOT_TOKEN", "HARDY_SLACK_CHANNEL"):
                if not env.get(name, "").strip():
                    missing.append(name)
        if missing:
            raise ConfigError(
                "missing required environment variable(s): "
                + ", ".join(missing)
                + ". HARDY_SLACK_CHANNEL is a channel id (C…) or your user id "
                "(U…) for a DM; pass --no-slack to run the call without Slack. "
                "See docs/meet-bot.md."
            )

        engine_url = (
            env.get("SILKSCREEN_ENGINE_URL", "").strip() or DEFAULT_ENGINE_URL
        ).rstrip("/")
        parts = urllib.parse.urlsplit(engine_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError(
                f"SILKSCREEN_ENGINE_URL is not an http(s) URL: {engine_url!r}"
            )
        floor = _number(
            env, "HARDY_CONFIDENCE_FLOOR", DEFAULT_CONFIDENCE_FLOOR, float, minimum=0.0
        )
        if floor > 1.0:
            raise ConfigError(f"HARDY_CONFIDENCE_FLOOR={floor} must be at most 1")
        profile = env.get("HARDY_PROFILE_DIR", "").strip()
        return cls(
            google_api_key=key,
            slack_token=env.get("SLACK_BOT_TOKEN", "").strip(),
            slack_channel=env.get("HARDY_SLACK_CHANNEL", "").strip(),
            slack_user=env.get("HARDY_SLACK_USER", "").strip(),
            display_name=env.get("HARDY_DISPLAY_NAME", "").strip() or "Hardy",
            profile_dir=Path(profile).expanduser()
            if profile
            else Path.home() / ".hardy" / "meet-profile",
            headless=_bool(env, "HARDY_HEADLESS", False),
            model=env.get("HARDY_MODEL", "").strip() or DEFAULT_MODEL,
            reply_model=env.get("HARDY_REPLY_MODEL", "").strip() or CHEAP_MODEL,
            reply_cooldown_s=_number(
                env, "HARDY_REPLY_COOLDOWN_S", 20.0, float, minimum=0.0
            ),
            quiet_s=_number(env, "HARDY_QUIET_S", 1.2, float, minimum=0.0),
            max_replies=_number(env, "HARDY_MAX_REPLIES", 8, int, minimum=0),
            reply_to_doubt=_bool(env, "HARDY_REPLY_TO_DOUBT", True),
            confidence_floor=floor,
            clarify_wait_s=_number(
                env, "HARDY_CLARIFY_WAIT_S", 600.0, float, minimum=0.0
            ),
            build_on_timeout=_bool(env, "HARDY_BUILD_ON_TIMEOUT", True),
            build=_bool(env, "HARDY_BUILD", True),
            follow=_bool(env, "HARDY_FOLLOW", True),
            engine_url=engine_url,
            engine_token=env.get("SILKSCREEN_ACCESS_TOKEN", "").strip(),
            max_call_s=_number(
                env, "HARDY_MAX_CALL_S", 3 * 60 * 60.0, float, minimum=1.0
            ),
        )
