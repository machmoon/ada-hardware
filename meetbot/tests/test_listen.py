"""meetbot.listen: Meet captions in, de-duplicated speaker-attributed utterances out.

Two halves. The stabilizer tests are pure (no browser) and pin the rewrite
rules. The page tests drive a local HTML stand-in for Meet's captions DOM
(``fixtures/meet_captions.html``) through a real headless Chromium, skipped
when Playwright or its browser is not installed. Neither touches the network
or a real Meet, so a green run says nothing about Meet's live DOM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meetbot.listen import (
    CaptionBlock,
    CaptionsLostError,
    CaptionStabilizer,
    CaptionsUnavailableError,
    ListenError,
    init_script,
    transcript_stream,
)
from meetbot.types import Utterance

FIXTURE = (Path(__file__).parent / "fixtures" / "meet_captions.html").as_uri()


# ---- the stabilizer, no browser -------------------------------------------


def feed(stab, t, *blocks):
    return stab.feed([CaptionBlock(*b) for b in blocks], t)


def texts(utterances):
    return [(u.speaker, u.text, u.is_self) for u in utterances]


def test_growing_caption_is_yielded_once_after_it_settles():
    stab = CaptionStabilizer(stabilize_s=1.0)
    assert feed(stab, 0.0, (1, "Pat", "I don't")) == []
    assert feed(stab, 0.4, (1, "Pat", "I don't think this")) == []
    assert feed(stab, 0.8, (1, "Pat", "i don't think this will work")) == []
    assert feed(stab, 1.2, (1, "Pat", "I don't think this will work.")) == []
    out = feed(stab, 2.2, (1, "Pat", "I don't think this will work."))
    assert texts(out) == [("Pat", "I don't think this will work.", False)]
    assert out[0].t_start == 0.0 and out[0].t_end == 1.2
    # Unchanged, re-cased or re-punctuated: nothing new was said.
    assert feed(stab, 3.5, (1, "Pat", "I don't think this will work.")) == []
    assert feed(stab, 5.0, (1, "Pat", "i dont think this will work")) == []


def test_same_speaker_continuing_yields_only_the_new_words():
    stab = CaptionStabilizer(stabilize_s=1.0)
    feed(stab, 0.0, (1, "Pat", "Use a coin cell."))
    assert texts(feed(stab, 1.0, (1, "Pat", "Use a coin cell."))) == [
        ("Pat", "Use a coin cell.", False)
    ]
    feed(stab, 3.0, (1, "Pat", "Use a coin cell. Actually two"))
    out = feed(stab, 4.0, (1, "Pat", "Use a coin cell. Actually two"))
    assert texts(out) == [("Pat", "Actually two", False)]
    assert out[0].t_start == 1.0  # the new words began after the last release


def test_front_trimmed_monologue_does_not_repeat():
    stab = CaptionStabilizer(stabilize_s=1.0)
    feed(stab, 0.0, (1, "Pat", "one two three four five"))
    feed(stab, 1.0, (1, "Pat", "one two three four five"))
    feed(stab, 2.0, (1, "Pat", "four five six seven"))
    assert texts(feed(stab, 3.0, (1, "Pat", "four five six seven"))) == [
        ("Pat", "six seven", False)
    ]


def test_superseded_block_is_flushed_at_once_and_later_polish_ignored():
    stab = CaptionStabilizer(stabilize_s=10.0)
    feed(stab, 0.0, (1, "Pat", "I don't think this will work"))
    out = feed(stab, 0.5, (1, "Pat", "I don't think this will work"), (2, "Sam", "Why"))
    assert texts(out) == [("Pat", "I don't think this will work", False)]
    out = feed(
        stab, 0.9, (1, "Pat", "I don't think this will work at all"), (2, "Sam", "Why")
    )
    assert out == []
    assert texts(stab.flush(1.0)) == [("Sam", "Why", False)]


def test_own_speech_is_marked_self_under_either_label():
    stab = CaptionStabilizer(self_name="Ada", stabilize_s=0.0)
    out = feed(stab, 0.0, (1, "You", "Why not?"), (2, "ada", "Tell me more"))
    out += stab.flush(1.0)
    assert texts(out) == [("Ada", "Why not?", True), ("Ada", "Tell me more", True)]


def test_empty_text_is_never_yielded_and_a_nameless_block_waits_for_its_name():
    stab = CaptionStabilizer(stabilize_s=0.0)
    assert feed(stab, 0.0, (1, "Pat", ""), (2, "Pat", "   ")) == []
    assert feed(stab, 1.0, (3, "", "hello there")) == []
    out = feed(stab, 2.0, (3, "Pat", "hello there"))
    assert texts(out) == [("Pat", "hello there", False)]
    assert all(u.text.strip() for u in out)


def test_block_that_closes_without_a_name_is_withheld_not_named():
    stab = CaptionStabilizer(stabilize_s=5.0)
    feed(stab, 0.0, (1, "", "who said this"))
    assert feed(stab, 1.0, (1, "", "who said this"), (2, "Pat", "hi")) == []
    assert stab.withheld == 1


def test_memory_is_bounded_over_a_long_meeting():
    stab = CaptionStabilizer(stabilize_s=0.0)
    for i in range(500):
        feed(stab, float(i), (i, "Pat", f"sentence {i}"))
    assert len(stab._blocks) <= 30


def test_transcriber_is_refused_in_words():
    class Session:
        page = None

    async def first():
        return await anext(transcript_stream(Session(), transcriber=object()))

    with pytest.raises(ListenError, match="not built"):
        asyncio.run(first())


# ---- a real browser against the fixture page ------------------------------


class FakeSession:
    display_name = "Ada"

    def __init__(self, page):
        self.page = page


async def _with_page(query, body, *, init=False):
    playwright_api = pytest.importorskip("playwright.async_api")
    async with playwright_api.async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # browser binary not downloaded
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            if init:
                await page.add_init_script(init_script())
            await page.goto(FIXTURE + query)
            return await body(page)
        finally:
            await browser.close()


async def _collect(session, **kwargs):
    kwargs = {"poll_s": 0.05, "stabilize_s": 0.3, "end_grace_s": 0.2, **kwargs}
    return [u async for u in transcript_stream(session, **kwargs)]


def test_scripted_meeting_turns_captions_on_and_yields_each_turn_once():
    async def body(page):
        utterances = await asyncio.wait_for(_collect(FakeSession(page)), 15)
        assert await page.evaluate("window.fixture.captionsOn()")
        return utterances

    utterances = asyncio.run(_with_page("?timeline=1", body, init=True))
    assert texts(utterances) == [
        ("Pat Liu", "I don't think this will work.", False),
        ("Ada", "Why not? What worries you?", True),
        ("Pat Liu", "The battery is too small.", False),
    ]
    assert all(isinstance(u, Utterance) and u.t_start <= u.t_end for u in utterances)


def test_stream_arrives_in_near_real_time_without_an_init_script():
    async def body(page):
        stream = transcript_stream(FakeSession(page), poll_s=0.05, stabilize_s=0.3)
        loop = asyncio.get_running_loop()
        first = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.5)  # captions are on by now
        said = loop.time()
        await page.evaluate("window.fixture.say('Sam', 'Can it run on USB power?')")
        utterance = await asyncio.wait_for(first, 5)
        await stream.aclose()
        return utterance, loop.time() - said

    utterance, latency = asyncio.run(_with_page("", body))
    assert (utterance.speaker, utterance.text) == ("Sam", "Can it run on USB power?")
    assert latency < 1.5


def test_no_captions_button_raises_captions_unavailable():
    async def body(page):
        with pytest.raises(CaptionsUnavailableError, match="no captions button"):
            await asyncio.wait_for(_collect(FakeSession(page), enable_timeout_s=0.4), 5)

    asyncio.run(_with_page("?nobutton=1", body))


def test_button_that_does_not_turn_captions_on_raises_in_words():
    async def body(page):
        with pytest.raises(CaptionsUnavailableError, match="did not confirm"):
            await asyncio.wait_for(_collect(FakeSession(page), enable_timeout_s=0.4), 5)

    asyncio.run(_with_page("?stuck=1", body))


def test_captions_vanishing_mid_call_raises_lost_after_flushing():
    async def body(page):
        got = []

        async def run():
            async for u in transcript_stream(
                FakeSession(page), poll_s=0.05, stabilize_s=5.0, lost_grace_s=0.3
            ):
                got.append(u)

        task = asyncio.ensure_future(run())
        await asyncio.sleep(0.4)
        await page.evaluate("window.fixture.say('Pat Liu', 'half a thought')")
        await asyncio.sleep(0.2)
        await page.evaluate("window.fixture.removeRegion()")
        with pytest.raises(CaptionsLostError, match="captions lost"):
            await asyncio.wait_for(task, 5)
        return got

    got = asyncio.run(_with_page("", body))
    assert texts(got) == [("Pat Liu", "half a thought", False)]


def test_closing_the_page_ends_the_stream_with_what_was_said():
    async def body(page):
        async def close_soon():
            await asyncio.sleep(0.4)
            await page.evaluate("window.fixture.say('Pat Liu', 'goodbye')")
            await asyncio.sleep(0.1)
            await page.close()

        closer = asyncio.ensure_future(close_soon())
        utterances = await asyncio.wait_for(
            _collect(FakeSession(page), stabilize_s=5.0), 5
        )
        await closer
        return utterances

    assert texts(asyncio.run(_with_page("", body))) == [("Pat Liu", "goodbye", False)]
