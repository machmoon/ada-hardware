"""A Chromium tab that sits in a Google Meet call as a participant named Ada.

This module owns the browser and the room: launch a persistent, signed-in
Chromium profile, walk the pre-join lobby, knock or join, say in words whether
the host let us in, notice when the call is over, and leave. It knows nothing
about speech, captions or boards -- ``listen.py`` reads ``page``, ``speak.py``
registers a ``getUserMedia`` override through :meth:`MeetSession.add_init_script`,
and ``runner.py`` strings them together.

It deliberately reverses ``meetings/``'s "no headless bot in the room"
decision: the product now wants Ada audible in the call, and nothing in
Google's published APIs can put a speaking participant in a Meet.

Prior art, read at source rather than remembered (both shallow-cloned
2026-09-13):

* **Vexa** (github.com/Vexa-ai/vexa @ 59e2c41), the production multi-platform
  meeting bot. Its Google Meet join layer lives in
  ``core/meetings/modules/join/src/googlemeet/``. Copied from it:

  - the selector vocabulary and its *ordering rule* -- exact English text
    first, the structural ``button[jsname]:not([aria-label]):has(span)``
    backstop last (``selectors.ts:271-310, 363-377``);
  - pinning the UI language with ``--lang``/``--accept-lang``, the context
    ``locale`` and ``?hl=en`` on the Meet URL, which is what makes the English
    selectors correct by construction rather than lucky (``browser-args.ts:21-45``,
    ``join.ts:293-311``);
  - the signed-out probe: a guest lobby renders a name input and a signed-in
    lobby never does (``selectors.ts:379-391``, ``join.ts:28-42``);
  - the admission oracle: rejection before waiting before admitted, the
    waiting-room text as a *negative guard* on admitted (lobby toolbars are
    false positives), DOM *presence* of participant tiles rather than
    visibility (the toolbar auto-hides), the "Backgrounds and effects" phantom
    tile excluded from the count, a pointer wiggle before visibility probes,
    and the Gemini "take notes" consent prompt treated as not-yet-admitted
    (``admission.ts:158-306``, ``selectors.ts:21-130``);
  - explicit host-denial copy kept apart from ambiguous error affordances
    (``admission.ts:49-67``);
  - the removal copy (``selectors.ts:245-269``) and the leave-button matcher
    order (``selectors.ts:413-452``, ``shared/leave-click.ts``);
  - the persistent context launched with
    ``ignore_default_args=["--enable-automation"]`` and
    ``--disable-blink-features=AutomationControlled``
    (``remote-browser/src/browser.ts:35-46``).

* **Google-Meet-AI-Attendence-Agent**
  (github.com/code-with-idrees/Google-Meet-AI-Attendence-Agent @ 01d482d), the
  small Python + Playwright + faster-whisper + Ollama agent. Copied from it
  (``meeting_agent.py``): a ``launch_persistent_context`` profile directory so
  the Google sign-in survives runs (``:64-67``); detecting the sign-in wall by
  the page landing on ``accounts.google.com`` and waiting, headed, for a human
  to sign in (``:180-191``); the "You can't join this video call" blocked
  screen (``:165-178``); and ``button[aria-label="Chat with everyone"]`` as an
  in-call signal (``:243``). Its only end-of-meeting signal is the page
  closing (``:492-494``); that is kept as one reason among several.

Deviations, each for a stated reason:

* **The microphone is turned ON, not muted.** Both upstreams mute the mic
  because they only listen (Vexa ``join.ts:403-416``; attendance agent
  ``:201-206`` with blind Ctrl+D). Ada has to be heard, and Meet sends no
  audio from a muted participant whatever ``getUserMedia`` returns.
* **No stealth plugin, no container flags.** Vexa layers
  ``puppeteer-extra-plugin-stealth`` and a WebGL spoof on top because it runs
  on GPU-less datacenter Linux (``browser.ts:14-20, 47-82``); this runs on the
  user's own laptop with a real GPU, and there is no maintained Python port of
  that plugin. ``--no-sandbox``/``--disable-gpu``/``--incognito`` are dropped
  for the same reason (``--incognito`` would also wipe the sign-in).
* **Real Chrome when present.** Google's sign-in page often refuses the
  bundled "Chrome for Testing" build as "may not be secure"; the persistent
  profile therefore launches the installed Google Chrome channel when there is
  one. That is still Chromium.
* **Removal copy is narrowed.** Vexa's list includes ``[role="alert"]`` and
  "Reconnecting" (``selectors.ts:255-268``); in a live call those are transient
  toasts, and ending the session on one would walk Ada out of a meeting that
  is still going.
* **Aloneness is by head count, not audio silence.** Vexa leaves after ten
  minutes of remote-audio silence (``services/bot/src/aloneness.ts:4``) because
  it owns an audio capture; this module does not, so it counts real
  participant tiles with a grace period, and says which of the two
  (never-joined, everyone-left) happened.

Everything here is **unverified against live Google Meet** from this repo:
the selectors are Vexa's production copy as of 2026-09 and the tests drive a
static imitation of the lobby and call DOM (``tests/fake_meet.html``). See
the module constants for every string a live run should confirm.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .types import (
    ADMITTED,
    REFUSED,
    SIGNED_OUT,
    WAITING_FOR_HOST,
    JoinReceipt,
)

DEFAULT_PROFILE_DIR = Path.home() / ".hardy" / "meet-profile"
MEET_HOST = "meet.google.com"
SIGN_IN_HOST = "accounts.google.com"
SIGN_IN_URL = (
    "https://accounts.google.com/ServiceLogin?continue=https://meet.google.com/"
)
DEFAULT_LOCALE = "en-US"
CHROME_APP = Path("/Applications/Google Chrome.app")

# wait_until_ended() reasons. One of these, always; ``end_detail`` says why.
END_PAGE_CLOSED = "page_closed"  # the window was closed under us
END_REMOVED = "removed"  # a host removed Ada
END_CALL_ENDED = "call_ended"  # the host ended the call for everyone
END_LEFT_ALONE = "left_alone"  # others were here, then everyone left
END_STARTUP_ALONE = "startup_alone"  # nobody else ever joined
END_LEFT = "left"  # our own leave() ran
END_REASONS = frozenset(
    {
        END_PAGE_CLOSED,
        END_REMOVED,
        END_CALL_ENDED,
        END_LEFT_ALONE,
        END_STARTUP_ALONE,
        END_LEFT,
    }
)

# ---------------------------------------------------------------------------
# Launch flags. Vexa browser-args.ts:47-71 minus the container-only ones (see
# the module docstring), plus the pinned locale of browser-args.ts:38-45.
# ---------------------------------------------------------------------------
LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--use-fake-ui-for-media-stream",  # auto-accept the mic/camera prompt
    "--autoplay-policy=no-user-gesture-required",
    "--disable-infobars",
)
IGNORE_DEFAULT_ARGS = ("--enable-automation",)

# ---------------------------------------------------------------------------
# Selectors. Playwright selector syntax: unquoted ``text=foo`` is a
# case-insensitive substring, quoted ``text="foo"`` is exact (Vexa
# selectors.ts:4-10). Lists are ORDERED: the first visible entry wins.
# ---------------------------------------------------------------------------

# Guest lobby name field -- Vexa selectors.ts:352-361 (structural first).
NAME_INPUT = (
    'input[jsname][type="text"]',
    'div[jscontroller] input[type="text"]',
    'input[type="text"][aria-label="Your name"]',
    'input[placeholder*="name" i]',
)
# Narrower probe for "this is a guest lobby" -- Vexa selectors.ts:386-391.
SIGNED_OUT_PROBE = (
    'input[jsname][type="text"]',
    'div[jscontroller] input[type="text"]',
    'input[type="text"][aria-label="Your name"]',
)
# Primary join CTA -- Vexa selectors.ts:301-310 and 370-377 merged: exact
# text first, structural backstop last. "Switch here" appears when the same
# account is already in the call on another device.
JOIN_CTA = (
    'button:has-text("Join now")',
    'button:has-text("Ask to join")',
    'button:has-text("Switch here")',
    'button:has-text("Join")',
    "button[jsname]:not([aria-label]):has(span)",
)
# Lobby device toggles -- Vexa selectors.ts:335-345. We turn the camera off
# and the microphone ON (module docstring: Ada must be heard).
CAMERA_IS_ON = ('button[aria-label*="Turn off camera"]',)
MIC_IS_OFF = ('button[aria-label*="Turn on microphone"]',)
# First-visit device dialogs. NOT from either upstream (neither handles them;
# Vexa launches with fake devices so Meet never asks). Copy is unverified --
# confirm against a fresh profile. "Use microphone" first because Ada needs
# it; the rest only close the dialog.
DEVICE_PROMPT_BUTTONS = (
    'button:has-text("Use microphone and camera")',
    'button:has-text("Use microphone")',
    '[role="dialog"] button:has-text("Got it")',
    '[role="dialog"] button:has-text("Dismiss")',
)

# Waiting room -- Vexa selectors.ts:31-55.
WAITING = (
    "text=Asking to be let in",
    "text=You'll join the call when someone lets you",
    "text=You’ll join the call when someone lets you",
    'text="Please wait until a meeting host brings you into the call"',
    'text="Waiting for the host to let you in"',
    '[aria-label*="Asking to be let in"]',
)
# Explicit host denial -- Vexa admission.ts:54-63 (the copy that no other
# signal may overturn).
HOST_DENIAL = (
    "text=denied your request",
    "text=Your request to join was denied",
    "text=You were denied",
    "text=weren't allowed to join",
    "text=weren’t allowed to join",
    "text=not allowed to join",
    'button:has-text("Ask to join again")',
)
# Meet refusing the join outright -- attendance agent meeting_agent.py:166-171
# plus Vexa selectors.ts:98-111.
BLOCKED = (
    "text=You can't join this video call",
    "text=You can’t join this video call",
    "text=can't join this call",
    "text=can’t join this call",
    'text="Meeting not found"',
    "text=Check your meeting code",
    "text=This meeting has ended",
    "text=Meeting link expired",
)
# Gemini "take notes for me" consent gate -- Vexa selectors.ts:71-84.
CONSENT_PROMPT = (
    '[role="dialog"]:has-text("take notes for me")',
    '[role="alertdialog"]:has-text("take notes for me")',
    '[role="dialog"]:has-text("taking notes")',
)
# Positive in-call signals that are never in the lobby -- Vexa
# selectors.ts:21-29 plus the attendance agent's chat button (:243).
IN_CALL = (
    'button[aria-label="Leave call"]',
    'button[aria-label*="Share screen"]',
    'button[aria-label*="Present now"]',
    'button[aria-label="Chat with everyone"]',
)
# The lobby's own join buttons, by label. Found live on 2026-09-13: the lobby
# self-preview carries a participant tile and a self name, so right after
# "Ask to join" (before the waiting-room copy renders) tiles alone read as
# admitted. While one of these is on screen Ada is not in the call.
# (JOIN_CTA's last, structural selector also matches in-call buttons, so it
# cannot be this guard.)
LOBBY_CTA = JOIN_CTA[:3]
PARTICIPANT_TILE = "[data-participant-id]"
SELF_NAME = "[data-self-name]"
EFFECTS_TILE_MARKERS = ("visual_effects", "backgrounds and effects")

# Terminal in-call screens. Removal copy is checked before generic endings so
# the reason is specific. Vexa selectors.ts:246-253 narrowed (docstring);
# "removed" copy and the post-call "Rejoin" button are unverified additions.
REMOVED = (
    "text=You've been removed from the meeting",
    "text=You’ve been removed from the meeting",
    "text=removed you from the meeting",
    "text=You've been removed",
    "text=You’ve been removed",
)
CALL_ENDED = (
    'text="Call ended"',
    'text="Meeting ended"',
    "text=The call has ended",
    "text=ended the meeting for everyone",
    "text=You left the meeting",
    'button:has-text("Rejoin")',
)
# Leave -- Vexa selectors.ts:417-436, in order, CSS only.
LEAVE_BUTTONS = (
    'button[aria-label="Leave call"]',
    'button[aria-label*="Leave call"]',
    '[role="toolbar"] button[aria-label*="Leave"]',
    'button[aria-label*="Hang up"]',
)
LEAVE_CONFIRM = (
    'button:has-text("Just leave the call")',
    'button:has-text("Just leave the meeting")',
    '[role="dialog"] button:has-text("Leave")',
)
# Sign-in state lives in these cookies on .google.com once a Google account is
# signed in to the profile.
SIGNED_IN_COOKIES = frozenset({"SID", "__Secure-1PSID", "__Secure-3PSID"})


def _log_stderr(message: str) -> None:
    print(f"[meetbot.session] {message}", file=sys.stderr, flush=True)


def with_pinned_locale(meet_url: str, locale: str = DEFAULT_LOCALE) -> str:
    """Add ``hl=<lang>`` so Meet renders the lobby the selectors were written for.

    A signed-in account's own language otherwise wins over the browser locale
    (Vexa ``join.ts:293-311``). An existing ``hl`` is kept.
    """
    lang = locale.split("-")[0].strip()
    parts = urlsplit(meet_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not lang or any(k == "hl" for k, _ in query):
        return meet_url
    query.append(("hl", lang))
    return urlunsplit(parts._replace(query=urlencode(query)))


def validate_meet_url(meet_url: str) -> str:
    """Return a normalised ``https://meet.google.com/...`` URL or raise ValueError.

    The profile is signed in to a Google account, so this browser only ever
    goes where a Meet link says, never to an arbitrary page a caller passed.
    """
    url = meet_url.strip()
    if url.startswith(MEET_HOST):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != MEET_HOST:
        raise ValueError(
            f"not a Google Meet link: {meet_url!r} "
            f"(expected https://{MEET_HOST}/xxx-xxxx-xxx)"
        )
    if parts.path.strip("/") == "":
        raise ValueError(f"Meet link has no meeting code: {meet_url!r}")
    return url


def is_sign_in_url(url: str) -> bool:
    return urlsplit(url).hostname == SIGN_IN_HOST


def _default_channel() -> str | None:
    return "chrome" if CHROME_APP.exists() else None


_AUTO = object()


class MeetSession:
    """One Chromium profile, one Meet call.

    Lifecycle: ``add_init_script`` (any number, any time before ``join``) ->
    ``start`` -> ``join`` -> ``wait_until_ended`` -> ``leave``. Also an async
    context manager that calls ``leave`` on exit.
    """

    def __init__(
        self,
        *,
        poll_s: float = 1.0,
        lobby_timeout_s: float = 45.0,
        admit_timeout_s: float = 300.0,
        alone_grace_s: float = 30.0,
        startup_alone_s: float = 900.0,
        allow_guest: bool = True,
        allow_any_url: bool = False,
        channel: Any = _AUTO,
        locale: str = DEFAULT_LOCALE,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.poll_s = poll_s
        self.lobby_timeout_s = lobby_timeout_s
        self.admit_timeout_s = admit_timeout_s
        self.alone_grace_s = alone_grace_s
        self.startup_alone_s = startup_alone_s
        self.allow_guest = allow_guest
        # Tests point join() at a file:// imitation; production never does.
        self.allow_any_url = allow_any_url
        self.channel = _default_channel() if channel is _AUTO else channel
        self.locale = locale
        self.log = log or _log_stderr

        self.init_scripts: list[str] = []
        self._applied_scripts = 0
        self.display_name: str = "Ada"
        self.joined_as_guest: bool | None = None
        self.receipt: JoinReceipt | None = None
        self.end_reason: str | None = None
        self.end_detail: str = ""

        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._headless = False
        self._leaving = False
        self._left = False

    # -- init scripts -------------------------------------------------------

    def add_init_script(self, js: str) -> None:
        """Register JS that runs in every document before Meet's own scripts.

        Scripts registered before ``start`` are applied at launch; ones
        registered later are applied before ``join`` navigates. A script added
        after ``join`` has navigated would miss the Meet document, so that
        raises rather than silently doing nothing.
        """
        if self._page is not None and self._page.url not in ("", "about:blank"):
            raise RuntimeError(
                "add_init_script after join() navigated: the script would not "
                "run in the Meet page; register it before join()"
            )
        self.init_scripts.append(js)

    async def _apply_pending_scripts(self) -> None:
        while self._applied_scripts < len(self.init_scripts):
            await self._context.add_init_script(
                script=self.init_scripts[self._applied_scripts]
            )
            self._applied_scripts += 1

    # -- launch -------------------------------------------------------------

    @property
    def page(self) -> Any:
        if self._page is None:
            raise RuntimeError("MeetSession.start() has not been called")
        return self._page

    async def start(
        self, profile_dir: Path = DEFAULT_PROFILE_DIR, headless: bool = False
    ) -> None:
        """Launch the persistent Chromium profile at ``profile_dir``."""
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - exercised by importorskip
            raise RuntimeError(
                "Playwright is not installed: pip install -e '.[meet]' && "
                "python -m playwright install chromium"
            ) from exc
        if self._context is not None:
            raise RuntimeError("MeetSession.start() called twice")
        profile_dir = Path(profile_dir).expanduser()
        profile_dir.mkdir(parents=True, exist_ok=True)
        lang = self.locale.split("-")[0]
        accept = f"{self.locale},{lang}" if lang != self.locale else self.locale
        args = [*LAUNCH_ARGS, f"--lang={self.locale}", f"--accept-lang={accept}"]

        self._headless = headless
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
                channel=self.channel,
                args=args,
                ignore_default_args=list(IGNORE_DEFAULT_ARGS),
                locale=self.locale,
                viewport=None if not headless else {"width": 1280, "height": 800},
                permissions=["microphone", "camera"],
            )
        except Exception:
            await self._playwright.stop()
            self._playwright = None
            raise
        await self._apply_pending_scripts()
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self.log(
            f"browser up: profile={profile_dir} channel={self.channel or 'chromium'} "
            f"headless={headless}"
        )

    # -- sign-in ------------------------------------------------------------

    async def is_signed_in(self) -> bool:
        """True when the profile holds a Google account session cookie."""
        if self._context is None:
            raise RuntimeError("MeetSession.start() has not been called")
        cookies = await self._context.cookies("https://accounts.google.com")
        return any(c.get("name") in SIGNED_IN_COOKIES for c in cookies)

    async def sign_in(self, timeout_s: float = 300.0) -> bool:
        """Open Google's sign-in page and wait for a human to finish signing in.

        Only meaningful headed: this is the one-time step that makes the
        profile at ``~/.hardy/meet-profile`` a signed-in one. Returns whether a
        session cookie is present at the end, never raises on timeout.
        """
        if await self.is_signed_in():
            self.log("profile is already signed in to Google")
            return True
        if self._headless:
            self.log(
                "profile is signed out and the browser is headless, so nobody can "
                "sign in; run `python -m meetbot.session sign-in` once, headed"
            )
            return False
        self.log("sign in to Google in the browser window; waiting")
        await self.page.goto(SIGN_IN_URL)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await self.is_signed_in():
                self.log("signed in; the profile will remember it")
                return True
            await asyncio.sleep(1.0)
        self.log(f"no Google sign-in within {timeout_s:.0f}s")
        return False

    # -- DOM probes ---------------------------------------------------------

    async def _first_visible(self, selectors: tuple[str, ...]) -> str | None:
        page = self._page
        for sel in selectors:
            try:
                if await page.locator(sel).first.is_visible():
                    return sel
            except Exception:
                continue  # a detached node or odd selector never denies the rest
        return None

    async def _click_first(self, selectors: tuple[str, ...]) -> str | None:
        sel = await self._first_visible(selectors)
        if sel is None:
            return None
        try:
            await self._page.locator(sel).first.click(timeout=5000)
        except Exception as exc:
            self.log(f"click on {sel} failed: {exc}")
            return None
        return sel

    async def _real_tile_count(self) -> int:
        """Participant tiles, excluding the self-view effects phantom.

        Vexa admission.ts:164-185: Meet gives the "Backgrounds and effects"
        panel a ``data-participant-id`` too, in lobby and call alike.
        """
        try:
            labels = await self._page.locator(PARTICIPANT_TILE).evaluate_all(
                "els => els.map(e => e.getAttribute('aria-label')"
                " || (e.textContent || '').trim())"
            )
        except Exception:
            return 0
        return sum(
            1
            for label in labels
            if label and not any(m in label.lower() for m in EFFECTS_TILE_MARKERS)
        )

    async def _wake_toolbar(self) -> None:
        # Meet hides the toolbar without pointer activity (Vexa admission.ts:226-238).
        with contextlib.suppress(Exception):
            await self._page.mouse.move(640, 360)
            await self._page.mouse.move(700, 420)

    async def _is_admitted(self) -> bool:
        if await self._first_visible(WAITING):
            return False
        if await self._first_visible(CONSENT_PROMPT):
            return False
        if await self._first_visible(LOBBY_CTA):
            return False
        await self._wake_toolbar()
        if await self._first_visible(IN_CALL) is not None:
            return True
        # Tiles only count once the lobby is gone (guards above), and never
        # when the name box of a guest lobby is still on screen.
        if await self._first_visible(NAME_INPUT):
            return False
        return await self._real_tile_count() > 0

    async def _visible_button_labels(self) -> list[str]:
        try:
            labels = await self._page.locator("button").evaluate_all(
                "els => els.filter(e => e.offsetWidth > 0 && e.offsetHeight > 0)"
                ".map(e => (e.getAttribute('aria-label') || e.textContent || '')"
                ".replace(/\\s+/g, ' ').trim()).filter(Boolean)"
            )
        except Exception:
            return []
        return labels[:12]

    async def _context_note(self) -> str:
        labels = await self._visible_button_labels()
        shown = " | ".join(f'"{t}"' for t in labels) if labels else "(none)"
        return f"url={self._page.url} visible buttons: {shown}"

    def _signed_out_receipt(self, why: str) -> JoinReceipt:
        return JoinReceipt(
            SIGNED_OUT,
            f"{why}. Sign the profile in once, headed: "
            "`python -m meetbot.session sign-in`",
        )

    # -- join ---------------------------------------------------------------

    async def join(self, meet_url: str, display_name: str = "Ada") -> JoinReceipt:
        """Walk the lobby and ask to join. Never a silent success.

        Waits up to ``admit_timeout_s`` for the host; if still in the lobby
        then, answers ``waiting_for_host`` and leaves the tab knocking, so the
        caller can :meth:`wait_for_admission` again.
        """
        if self._context is None:
            raise RuntimeError("MeetSession.start() has not been called")
        url = meet_url if self.allow_any_url else validate_meet_url(meet_url)
        if urlsplit(url).hostname == MEET_HOST:
            url = with_pinned_locale(url, self.locale)
        self.display_name = display_name
        self._leaving = False
        await self._apply_pending_scripts()

        self.log(f"opening {url}")
        await self._page.goto(url, wait_until="domcontentloaded")
        receipt = await self._walk_lobby(display_name)
        if receipt is None:
            receipt = await self.wait_for_admission(self.admit_timeout_s)
        self.receipt = receipt
        self.log(f"join: {receipt.state} -- {receipt.detail}")
        return receipt

    async def _walk_lobby(self, display_name: str) -> JoinReceipt | None:
        """Get to the point of having clicked the join CTA, or say why not."""
        deadline = time.monotonic() + self.lobby_timeout_s
        cta: str | None = None
        while time.monotonic() < deadline:
            page = self._page
            if page.is_closed():
                return JoinReceipt(REFUSED, "the browser window closed before joining")
            if is_sign_in_url(page.url):
                return self._signed_out_receipt(
                    "Meet sent the browser to the Google sign-in page; this "
                    "meeting does not admit signed-out guests"
                )
            if hit := await self._first_visible(BLOCKED):
                if self._context is not None and not await self.is_signed_in():
                    return self._signed_out_receipt(
                        f"Meet refused the join ({hit}) and the profile is not "
                        "signed in to Google"
                    )
                return JoinReceipt(REFUSED, f"Meet refused the join: {hit}")
            if await self._is_admitted():
                self.joined_as_guest = False
                return JoinReceipt(ADMITTED, "already in the call on arrival")
            await self._click_first(DEVICE_PROMPT_BUTTONS)
            cta = await self._first_visible(JOIN_CTA)
            if cta is not None:
                break
            await asyncio.sleep(self.poll_s)
        if cta is None:
            return JoinReceipt(
                REFUSED,
                f"no join button appeared within {self.lobby_timeout_s:.0f}s "
                f"({await self._context_note()})",
            )

        guest = await self._first_visible(SIGNED_OUT_PROBE) is not None
        self.joined_as_guest = guest
        if guest:
            if not self.allow_guest:
                return self._signed_out_receipt(
                    "the lobby asks for a name, so the profile is signed out and "
                    "guest joins are disabled"
                )
            name_sel = await self._first_visible(NAME_INPUT)
            if name_sel is not None:
                await self._page.locator(name_sel).first.fill(display_name)
                self.log(f"guest lobby: name set to {display_name!r}")
            # A guest CTA stays disabled until the name is typed; re-resolve.
            cta = await self._first_visible(JOIN_CTA) or cta

        await self._set_devices()
        # A device dialog can appear only once the toggles are touched.
        await self._click_first(DEVICE_PROMPT_BUTTONS)
        clicked = await self._click_first((cta, *JOIN_CTA))
        if clicked is None:
            return JoinReceipt(
                REFUSED,
                f"found the join button but could not click it "
                f"({await self._context_note()})",
            )
        self.log(f"clicked join CTA via {clicked}")
        return None

    async def _set_devices(self) -> None:
        if await self._click_first(CAMERA_IS_ON):
            self.log("camera turned off")
        if await self._click_first(MIC_IS_OFF):
            self.log("microphone turned on (Ada speaks)")

    async def wait_for_admission(self, timeout_s: float) -> JoinReceipt:
        """Poll the admission oracle: denial, then refusal, then waiting, then in.

        Order follows Vexa admission.ts:95-151 and 427-451: a denial can leave
        stale waiting-room text behind, so it is checked first.
        """
        deadline = time.monotonic() + timeout_s
        saw_waiting = False
        saw_consent = False
        while True:
            page = self._page
            if page.is_closed():
                return JoinReceipt(REFUSED, "the browser window closed while joining")
            if hit := await self._first_visible(HOST_DENIAL):
                return JoinReceipt(
                    REFUSED, f"the host denied the request to join ({hit})"
                )
            if hit := await self._first_visible(BLOCKED):
                return JoinReceipt(REFUSED, f"Meet refused the join: {hit}")
            if await self._first_visible(WAITING):
                saw_waiting = True
            elif await self._first_visible(CONSENT_PROMPT):
                saw_consent = True
            elif await self._is_admitted() and (
                await asyncio.sleep(self.poll_s) or await self._is_admitted()
            ):
                # Two readings a poll apart: the lobby re-renders between the
                # click and the waiting-room copy, and one frame is not a call.
                await self._read_self_name()
                how = (
                    "as a guest" if self.joined_as_guest else "as the signed-in account"
                )
                return JoinReceipt(
                    ADMITTED, f"in the call {how}, shown as {self.display_name!r}"
                )
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self.poll_s)

        if saw_consent:
            return JoinReceipt(
                WAITING_FOR_HOST,
                "held behind Gemini's take-notes consent prompt after "
                f"{timeout_s:.0f}s; a person in the call has to accept or decline it",
            )
        if saw_waiting:
            return JoinReceipt(
                WAITING_FOR_HOST,
                f"asked to join; nobody let Ada in within {timeout_s:.0f}s "
                "(still knocking)",
            )
        return JoinReceipt(
            REFUSED,
            f"clicked join but saw neither the waiting room nor the call within "
            f"{timeout_s:.0f}s ({await self._context_note()})",
        )

    async def _read_self_name(self) -> None:
        # Signed in, Meet shows the account's name, not the one we asked for.
        with contextlib.suppress(Exception):
            loc = self._page.locator(SELF_NAME).first
            if await loc.count():
                name = (await loc.get_attribute("data-self-name") or "").strip()
                if name:
                    self.display_name = name

    # -- in call ------------------------------------------------------------

    async def wait_until_ended(self) -> str:
        """Block until the call is over for Ada; return one of ``END_REASONS``.

        ``end_detail`` carries the sentence. Head count: Meet renders a tile per
        participant including Ada, so one real tile means alone.
        """
        if self._page is None:
            raise RuntimeError("MeetSession.start() has not been called")
        started = time.monotonic()
        saw_others = False
        alone_since: float | None = None
        while True:
            reason = await self._check_ended()
            if reason is not None:
                return reason
            count = await self._real_tile_count()
            now = time.monotonic()
            if count >= 2:
                saw_others = True
                alone_since = None
            elif saw_others:
                alone_since = alone_since if alone_since is not None else now
                if now - alone_since >= self.alone_grace_s:
                    return self._ended(
                        END_LEFT_ALONE,
                        f"everyone else left; alone for {self.alone_grace_s:.0f}s",
                    )
            elif now - started >= self.startup_alone_s:
                return self._ended(
                    END_STARTUP_ALONE,
                    f"nobody else joined within {self.startup_alone_s:.0f}s",
                )
            await asyncio.sleep(self.poll_s)

    async def _check_ended(self) -> str | None:
        page = self._page
        if page.is_closed():
            if self._leaving or self._left:
                return self._ended(END_LEFT, "Ada left the call")
            return self._ended(END_PAGE_CLOSED, "the Meet window was closed")
        if self._leaving or self._left:
            return self._ended(END_LEFT, "Ada left the call")
        if hit := await self._first_visible(REMOVED):
            return self._ended(END_REMOVED, f"a host removed Ada ({hit})")
        if hit := await self._first_visible(CALL_ENDED):
            return self._ended(END_CALL_ENDED, f"the call ended ({hit})")
        return None

    def _ended(self, reason: str, detail: str) -> str:
        self.end_reason, self.end_detail = reason, detail
        self.log(f"ended: {reason} -- {detail}")
        return reason

    # -- leave --------------------------------------------------------------

    async def leave(self) -> None:
        """Hang up if still in the call, then close the browser. Idempotent."""
        if self._left:
            return
        self._leaving = True
        page = self._page
        try:
            if page is not None and not page.is_closed():
                await self._wake_toolbar()
                clicked = await self._click_first(LEAVE_BUTTONS)
                if clicked:
                    self.log(f"left the call via {clicked}")
                    await asyncio.sleep(min(0.5, self.poll_s))
                    await self._click_first(LEAVE_CONFIRM)
                elif self.receipt is not None and self.receipt.admitted:
                    self.log(
                        "no leave button visible (the call may already be over); "
                        "closing the window, which also drops Ada from the call"
                    )
        finally:
            self._left = True
            await self._close()

    async def _close(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._context = None
        self._playwright = None

    async def __aenter__(self) -> MeetSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.leave()


async def _sign_in_main(profile_dir: Path, timeout_s: float) -> int:
    session = MeetSession()
    await session.start(profile_dir, headless=False)
    try:
        ok = await session.sign_in(timeout_s)
    finally:
        await session._close()
    print("signed in" if ok else "not signed in")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """``python -m meetbot.session sign-in [--profile DIR] [--timeout S]``."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m meetbot.session")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "sign-in", help="open a headed browser to sign Ada's profile in"
    )
    p.add_argument("--profile", type=Path, default=DEFAULT_PROFILE_DIR)
    p.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)
    return asyncio.run(_sign_in_main(args.profile, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
