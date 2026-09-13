"""meetbot.speak and meetbot.tts, offline.

The browser tests serve a local page through ``page.route`` (no socket, but a
secure ``http://localhost`` origin, which ``getUserMedia`` requires). The page
takes the injected microphone exactly as Meet would, and measures what arrives
on it with its own AnalyserNode -- an independent reader, so a ``spoken=True``
is checked against samples rather than against the code's own claim.
"""

from __future__ import annotations

import asyncio
import http.server
import io
import math
import shutil
import struct
import threading
import wave

import pytest

from meetbot import speak, tts

SINE_S = 0.6


def sine_wav(seconds: float = SINE_S, freq: float = 440.0, rate: int = 24_000) -> bytes:
    frames = int(seconds * rate)
    pcm = b"".join(
        struct.pack("<h", int(12_000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(frames)
    )
    return tts.pcm16_to_wav(pcm, rate)


# --------------------------------------------------------------------------
# tts.py -- no browser needed
# --------------------------------------------------------------------------


def test_pcm16_to_wav_has_true_sizes() -> None:
    wav = sine_wav(0.5)
    with wave.open(io.BytesIO(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24_000)
        assert w.getnframes() == 12_000
    assert tts.wav_duration_s(wav) == pytest.approx(0.5)


class _FakeSpeak(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.status != 200:
            self.send_response(self.status)
            self.end_headers()
            self.wfile.write(b'{"error": "no engine"}')
            return
        with wave.open(io.BytesIO(sine_wav(0.25))) as w:
            pcm = w.readframes(w.getnframes())
        self.send_response(200)
        self.send_header("Content-Type", "audio/L16")
        self.send_header("X-Kaleo-Tts-Engine", "kokoro")
        self.send_header("X-Kaleo-Sample-Rate", "24000")
        self.end_headers()
        self.wfile.write(pcm)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def fake_service():
    server = http.server.HTTPServer(("127.0.0.1", 0), _FakeSpeak)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    _FakeSpeak.status = 200


def test_service_rung_wraps_pcm_and_names_engine(fake_service) -> None:
    base = f"http://127.0.0.1:{fake_service.server_port}"
    out = tts.synthesize_via_service("I'll try it", base_url=base)
    assert out.seconds == pytest.approx(0.25)
    assert "kokoro" in out.source and base in out.source


def test_service_error_is_named_not_silent(fake_service) -> None:
    _FakeSpeak.status = 503
    base = f"http://127.0.0.1:{fake_service.server_port}"
    with pytest.raises(tts.TtsUnavailable, match="503"):
        tts.synthesize_via_service("hello", base_url=base)


def test_ladder_names_every_failed_rung(monkeypatch) -> None:
    def refuse(name):
        def _f(*a, **k):
            raise tts.TtsUnavailable(f"{name} down")
        return _f

    monkeypatch.setattr(tts, "synthesize_via_service", refuse("svc"))
    monkeypatch.setattr(tts, "synthesize_in_process", refuse("inproc"))
    monkeypatch.setattr(tts, "synthesize_via_say", refuse("say"))
    with pytest.raises(tts.TtsUnavailable, match="svc down.*inproc down.*say down"):
        asyncio.run(tts.synthesize("hello"))


def test_empty_text_refused() -> None:
    with pytest.raises(ValueError):
        asyncio.run(tts.synthesize("   "))


@pytest.mark.skipif(not shutil.which("say"), reason="macOS say not available")
def test_say_rung_produces_real_audio() -> None:
    out = tts.synthesize_via_say("I'll try it")
    assert out.seconds > 0.3
    assert out.source == "macOS say + afconvert"


# --------------------------------------------------------------------------
# speak.py -- in a real Chromium
# --------------------------------------------------------------------------

ORIGIN = "http://localhost:8765"

RECEIVER = """<!doctype html><html><body>%s<script>
window.peak = 0; window.ready = false;
(async () => {
  const s = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
  window.tracks = s.getTracks().map(t => t.kind);
  const ctx = new AudioContext();
  await ctx.resume();
  const an = ctx.createAnalyser();
  an.fftSize = 2048;
  ctx.createMediaStreamSource(s).connect(an);
  const buf = new Float32Array(an.fftSize);
  setInterval(() => {
    an.getFloatTimeDomainData(buf);
    let m = 0;
    for (const x of buf) m = Math.max(m, Math.abs(x));
    window.peak = Math.max(window.peak, m);
  }, 10);
  window.ready = true;
})();
</script></body></html>"""

MEET_BUTTON = """<button id="mic" aria-label="Turn on microphone (ctrl + d)"
  onclick="const l = this.getAttribute('aria-label');
  window.clicks = (window.clicks || []).concat([l]);
  this.setAttribute('aria-label', l.startsWith('Turn on')
    ? 'Turn off microphone (ctrl + d)'
    : 'Turn on microphone (ctrl + d)')">mic</button>"""


class FakeSession:
    def __init__(self, page) -> None:
        self.page = page

    async def add_init_script(self, js: str) -> None:
        await self.page.add_init_script(js)


async def _with_page(body: str, fn, *, inject: bool = True, grab_mic: bool = True):
    pw_api = pytest.importorskip("playwright.async_api")
    async with pw_api.async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(args=list(speak.CHROMIUM_ARGS))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Playwright Chromium not launchable: {exc}")
        try:
            page = await browser.new_page()
            session = FakeSession(page)
            if inject:
                await session.add_init_script(speak.init_script())
            html = RECEIVER % body if grab_mic else f"<html><body>{body}</body></html>"
            await page.route(
                f"{ORIGIN}/**",
                lambda route: route.fulfill(content_type="text/html", body=html),
            )
            await page.goto(f"{ORIGIN}/call")
            if grab_mic:
                await page.wait_for_function("window.ready === true", timeout=10_000)
            return await fn(session, page)
        finally:
            await browser.close()


async def _fake_tts(text: str) -> tts.Synthesis:
    return tts.Synthesis(wav=sine_wav(), source="test sine")


def test_say_puts_non_silent_audio_on_the_injected_track() -> None:
    async def run(session, page):
        await asyncio.sleep(0.3)
        silent = await page.evaluate("window.peak")
        receipt = await speak.say(session, "I'll try it", tts=_fake_tts, mic="page")
        await asyncio.sleep(0.1)
        return silent, receipt, await page.evaluate("window.peak"), await page.evaluate(
            "window.tracks"
        )

    silent, receipt, peak, tracks = asyncio.run(_with_page("", run))
    assert silent < 0.01, "the injected track must carry silence before say()"
    assert receipt.spoken, receipt.detail
    assert receipt.seconds == pytest.approx(SINE_S, abs=0.05)
    assert "test sine" in receipt.detail
    assert peak > 0.1, f"no audio reached the page's microphone (peak {peak})"
    assert tracks == ["audio"], "a video request must not open a real camera"


def test_say_can_speak_twice() -> None:
    async def run(session, page):
        first = await speak.say(session, "one", tts=_fake_tts, mic="page")
        second = await speak.say(session, "two", tts=_fake_tts, mic="page")
        plays = await page.evaluate("window.__hardyVoice.status().plays")
        return first, second, plays

    first, second, plays = asyncio.run(_with_page("", run))
    assert first.spoken and second.spoken
    assert plays == 2


def test_meet_mode_unmutes_for_the_utterance_then_mutes_again() -> None:
    async def run(session, page):
        receipt = await speak.say(
            session, "I'll try it", tts=_fake_tts, mic="meet", release_after_s=0.1
        )
        label = await page.get_attribute("#mic", "aria-label")
        clicks = await page.evaluate("window.clicks")
        return receipt, clicks, label, await page.evaluate("window.peak")

    receipt, clicks, label, peak = asyncio.run(_with_page(MEET_BUTTON, run))
    assert receipt.spoken, receipt.detail
    assert "unmuted for it" in receipt.detail
    assert len(clicks) == 2 and clicks[0].startswith("Turn on")
    assert label.startswith("Turn on microphone"), "mic must be muted again"
    assert peak > 0.1


def test_meet_mode_refuses_without_a_mic_control() -> None:
    async def run(session, page):
        receipt = await speak.say(session, "hi", tts=_fake_tts, mic="meet")
        return receipt, await page.evaluate("window.__hardyVoice.status().plays")

    receipt, plays = asyncio.run(_with_page("", run))
    assert not receipt.spoken
    assert "control was not found" in receipt.detail
    assert plays == 0


def test_not_spoken_when_init_script_missing() -> None:
    async def run(session, page):
        return await speak.say(session, "hi", tts=_fake_tts, mic="page")

    receipt = asyncio.run(_with_page("", run, inject=False, grab_mic=False))
    assert not receipt.spoken and "init script" in receipt.detail


def test_not_spoken_when_page_never_took_a_microphone() -> None:
    async def run(session, page):
        return await speak.say(session, "hi", tts=_fake_tts, mic="page")

    receipt = asyncio.run(_with_page("", run, grab_mic=False))
    assert not receipt.spoken and "never asked for a microphone" in receipt.detail


def test_not_spoken_when_tts_fails() -> None:
    async def broken(text: str) -> bytes:
        raise tts.TtsUnavailable("every voice is down")

    async def run(session, page):
        return await speak.say(session, "hi", tts=broken, mic="page")

    receipt = asyncio.run(_with_page("", run))
    assert not receipt.spoken and "every voice is down" in receipt.detail


def test_not_spoken_when_page_stopped_the_track() -> None:
    async def run(session, page):
        await page.evaluate("window.__hardyVoice.issued.forEach(t => t.stop())")
        return await speak.say(session, "hi", tts=_fake_tts, mic="page")

    receipt = asyncio.run(_with_page("", run))
    assert not receipt.spoken and "no live, enabled" in receipt.detail
