"""MeetSession against a static imitation of the Meet lobby and call.

Offline: headless bundled Chromium loads ``fake_meet.html`` from disk, so no
network, no Google account and no live Meet. What this proves is that the join
flow, the admission oracle and the end detection behave as designed against the
DOM shapes Vexa's selectors target -- not that live Meet still renders them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from meetbot import session as S  # noqa: E402
from meetbot.types import (  # noqa: E402
    ADMITTED,
    JOIN_STATES,
    REFUSED,
    SIGNED_OUT,
    WAITING_FOR_HOST,
)

FAKE = Path(__file__).with_name("fake_meet.html").resolve()


def _url(**params: object) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return FAKE.as_uri() + (f"?{query}" if query else "")


def _session(**overrides: object) -> S.MeetSession:
    opts: dict = {
        "poll_s": 0.05,
        "lobby_timeout_s": 3.0,
        "admit_timeout_s": 3.0,
        "alone_grace_s": 0.3,
        "startup_alone_s": 0.6,
        "allow_any_url": True,
        "channel": None,
        "log": lambda _m: None,
    }
    opts.update(overrides)
    return S.MeetSession(**opts)


def _run(coro_fn, tmp_path: Path, **overrides: object):
    async def main():
        s = _session(**overrides)
        await s.start(tmp_path / "profile", headless=True)
        try:
            return await coro_fn(s)
        finally:
            await s.leave()

    try:
        return asyncio.run(main())
    except Exception as exc:  # the browser binary itself may be missing
        if "Executable doesn't exist" in str(exc):
            pytest.skip(
                "Playwright Chromium not installed: playwright install chromium"
            )
        raise


# -- pure helpers ---------------------------------------------------------


def test_validate_meet_url_accepts_meet_links_only():
    assert S.validate_meet_url("meet.google.com/abc-defg-hij") == (
        "https://meet.google.com/abc-defg-hij"
    )
    for bad in (
        "https://evil.example/abc",
        "http://meet.google.com/abc-defg-hij",
        "https://meet.google.com/",
        "https://meet.google.com.evil.example/abc",
    ):
        with pytest.raises(ValueError):
            S.validate_meet_url(bad)


def test_pinned_locale_adds_hl_and_keeps_an_existing_one():
    assert S.with_pinned_locale("https://meet.google.com/abc") == (
        "https://meet.google.com/abc?hl=en"
    )
    assert S.with_pinned_locale("https://meet.google.com/abc?hl=de") == (
        "https://meet.google.com/abc?hl=de"
    )
    assert S.is_sign_in_url("https://accounts.google.com/v3/signin/identifier")
    assert not S.is_sign_in_url("https://meet.google.com/abc")


def test_join_before_start_raises():
    with pytest.raises(RuntimeError):
        asyncio.run(_session().join(_url()))


def test_non_meet_url_is_refused_by_default(tmp_path):
    async def go(s):
        s.allow_any_url = False
        with pytest.raises(ValueError):
            await s.join(_url())

    _run(go, tmp_path)


# -- join -----------------------------------------------------------------


def test_signed_in_lobby_joins_with_camera_off_and_mic_on(tmp_path):
    async def go(s):
        receipt = await s.join(_url(lobby="signedin", admit="instant"))
        state = await s.page.evaluate("window.fakeState")
        return receipt, state, s

    receipt, state, s = _run(go, tmp_path)
    assert receipt.state == ADMITTED, receipt.detail
    assert "signed-in account" in receipt.detail
    assert s.joined_as_guest is False
    assert state["cam"] is False
    assert state["mic"] is True  # Hardy has to be heard
    # Signed in, Meet shows the account name rather than the one asked for.
    assert s.display_name == "Pat Liu (Hardy account)"


def test_guest_lobby_types_the_name_and_knocks_then_is_admitted(tmp_path):
    async def go(s):
        receipt = await s.join(
            _url(lobby="guest", admit="knock", delay=400), display_name="Hardy"
        )
        return receipt, await s.page.evaluate("window.fakeState"), s

    receipt, state, s = _run(go, tmp_path)
    assert receipt.state == ADMITTED, receipt.detail
    assert "guest" in receipt.detail and "'Hardy'" in receipt.detail
    assert state["name"] == "Hardy"
    assert s.joined_as_guest is True


def test_guest_lobby_is_signed_out_when_guests_are_disabled(tmp_path):
    async def go(s):
        receipt = await s.join(_url(lobby="guest"))
        return receipt, await s.page.evaluate("window.fakeState.clicked")

    receipt, clicked = _run(go, tmp_path, allow_guest=False)
    assert receipt.state == SIGNED_OUT
    assert "sign-in" in receipt.detail
    assert clicked is False  # never knocked as an anonymous guest


def test_blocked_screen_on_a_signed_out_profile_says_signed_out(tmp_path):
    receipt = _run(lambda s: s.join(_url(lobby="blocked")), tmp_path)
    assert receipt.state == SIGNED_OUT
    assert "can't join this video call" in receipt.detail


def test_device_prompt_is_dismissed_before_the_join_click(tmp_path):
    async def go(s):
        return await s.join(_url(prompt=1, admit="instant"))

    receipt = _run(go, tmp_path)
    assert receipt.state == ADMITTED, receipt.detail


def test_host_denial_is_refused_even_with_stale_waiting_text(tmp_path):
    receipt = _run(lambda s: s.join(_url(admit="deny", delay=200)), tmp_path)
    assert receipt.state == REFUSED
    assert "denied" in receipt.detail


def test_unanswered_knock_is_waiting_for_host_and_can_still_be_admitted(tmp_path):
    async def go(s):
        first = await s.join(_url(admit="never"))
        await s.page.evaluate("window.fake.admit()")
        second = await s.wait_for_admission(2.0)
        return first, second

    first, second = _run(go, tmp_path, admit_timeout_s=0.5)
    assert first.state == WAITING_FOR_HOST
    assert "still knocking" in first.detail
    assert second.state == ADMITTED


def test_lingering_lobby_with_a_self_preview_tile_is_not_admitted(tmp_path):
    """Found live: the lobby's self-preview tile read as "admitted" in the gap
    between the click and the waiting-room copy, so Hardy never listened."""
    async def go(s):
        return await s.join(_url(admit="never", lag=400))

    receipt = _run(go, tmp_path, admit_timeout_s=1.0)
    assert receipt.state == WAITING_FOR_HOST, receipt.detail


def test_gemini_consent_prompt_holds_admission(tmp_path):
    async def go(s):
        await s.page.goto(_url())  # consent must be up before the click lands
        await s.page.evaluate("window.fake.consent(true)")
        return await s.wait_for_admission(0.5)

    receipt = _run(go, tmp_path)
    assert receipt.state == WAITING_FOR_HOST
    assert "consent" in receipt.detail


def test_missing_join_button_is_refused_with_what_was_on_screen(tmp_path):
    receipt = _run(lambda s: s.join(_url(lobby="empty")), tmp_path, lobby_timeout_s=0.4)
    assert receipt.state == REFUSED
    assert "no join button" in receipt.detail and "Turn off camera" in receipt.detail


def test_every_receipt_state_is_in_the_vocabulary(tmp_path):
    receipt = _run(lambda s: s.join(_url()), tmp_path)
    assert receipt.state in JOIN_STATES and receipt.detail


# -- init scripts ---------------------------------------------------------


def test_init_scripts_run_before_the_page_scripts(tmp_path):
    async def go(s):
        s.add_init_script("window.__hardyInit = 'after-start';")
        await s.join(_url())
        seen = await s.page.evaluate("window.__seenAtLoad")
        with pytest.raises(RuntimeError):
            s.add_init_script("window.late = 1")
        return seen

    async def main():
        s = _session()
        s.add_init_script("window.__hardyInit = 'before-start';")
        await s.start(tmp_path / "profile", headless=True)
        try:
            return await go(s)
        finally:
            await s.leave()

    # Registered after start: the later script wins because both ran, in order.
    assert asyncio.run(main()) == "after-start"


# -- end of call ----------------------------------------------------------


def _ended_after(action: str, tmp_path: Path, **params: object):
    async def go(s):
        receipt = await s.join(_url(**params))
        assert receipt.state == ADMITTED, receipt.detail
        waiter = asyncio.create_task(s.wait_until_ended())
        await asyncio.sleep(0.2)
        if action:
            await s.page.evaluate(action)
        reason = await asyncio.wait_for(waiter, 5)
        return reason, s.end_detail

    return _run(go, tmp_path)


def test_removed_by_host(tmp_path):
    reason, detail = _ended_after("window.fake.end('removed')", tmp_path)
    assert reason == S.END_REMOVED
    assert "removed" in detail


def test_call_ended(tmp_path):
    reason, _ = _ended_after("window.fake.end('ended')", tmp_path)
    assert reason == S.END_CALL_ENDED


def test_left_alone_after_everyone_leaves(tmp_path):
    reason, detail = _ended_after("window.fake.removeOthers()", tmp_path, others=2)
    assert reason == S.END_LEFT_ALONE
    assert "everyone else left" in detail


def test_nobody_ever_joined(tmp_path):
    reason, detail = _ended_after("", tmp_path, others=0)
    assert reason == S.END_STARTUP_ALONE
    assert "nobody else joined" in detail


def test_leave_ends_the_wait_as_left_and_is_idempotent(tmp_path):
    async def go(s):
        await s.join(_url())
        waiter = asyncio.create_task(s.wait_until_ended())
        await asyncio.sleep(0.2)
        await s.leave()
        await s.leave()
        return await asyncio.wait_for(waiter, 5)

    assert _run(go, tmp_path) == S.END_LEFT


def test_leave_clicks_the_leave_button_before_closing(tmp_path):
    async def go(s):
        await s.join(_url())
        seen: list[str] = []
        close = s._close

        async def spy():
            seen.append(await s.page.inner_text("#endmsg"))
            await close()

        s._close = spy
        await s.leave()
        return seen

    assert _run(go, tmp_path) == ["You left the meeting"]
