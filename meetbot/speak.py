"""Making Hardy speak out loud in a Google Meet call -- and saying which way it went.

Where the design comes from
---------------------------

Two well-used open-source meeting bots solve "a browser bot talks", and they
solve it two ways:

* **Vexa** (github.com/Vexa-ai/vexa, tag ``vexa-0.10.6``) plays TTS into a
  PulseAudio ``tts_sink`` whose monitor is Chromium's microphone, unmuting the
  sink with ``pactl`` only around playback
  (``services/vexa-bot/core/src/services/tts-playback.ts``, ``unmuteTtsAudio`` /
  ``muteTtsAudio``), and toggles the in-meeting mic button by aria-label
  (``services/vexa-bot/core/src/services/microphone.ts``,
  ``toggleGoogleMeetMic``). PulseAudio does not exist on the Mac this demo runs
  on, so the audio half is not portable; the mic-button half is used below.
* **Attendee** (github.com/attendee-labs/attendee, commit ``60e885d``) needs no
  OS audio device at all: an init script replaces
  ``navigator.mediaDevices.getUserMedia`` so every audio request gets a
  *clone* of one ``MediaStreamAudioDestinationNode`` track, and speech is
  ``AudioBufferSourceNode``\\ s connected to that node
  (``bots/web_bot_adapter/shared_chromedriver_payload.js``, class
  ``BotOutputManager``: ``_createSourceAudioTrack``,
  ``_installGetUserMediaInterceptor``, ``playPCMAudio``). The mic is turned on
  before audio and off two seconds after the queue drains
  (``_processAudioQueue``'s ``turnOffMicTimeout``), using Meet's
  ``button[aria-label="Turn on microphone"]``
  (``bots/google_meet_bot_adapter/google_meet_chromedriver_payload.js``,
  ``turnOnMic``/``turnOffMic``).

This module is Attendee's mechanism, because it speaks on demand, any number
of times, with nothing installed: ``--use-file-for-fake-audio-capture`` loops
one file from launch and cannot say a sentence decided mid-call. Load-bearing
details kept from Attendee: the source track is created lazily on the first
audio ``getUserMedia`` (its comment: created in the constructor, it plays
through the speakers), callers get ``.clone()``\\ s so the page stopping its
track cannot kill the source, and a video-only request is not given a real
camera. Two deliberate deviations, each stated: the gain node is **not**
connected to ``audioContext.destination`` (Attendee's is; on a laptop sitting
in the same call that is Hardy echoing through the room's own speakers), and
the page resolves a promise when the ``AudioBufferSourceNode`` fires ``ended``
-- Attendee's queue is fire-and-forget, and this module's receipt needs proof.

The honesty rule
----------------

``SpokenReceipt.spoken`` is ``True`` only when all of these were observed, not
assumed: audio was synthesized; the page took an injected microphone track that
is still ``live`` and ``enabled``; the AudioContext was ``running``; the buffer
source played to its ``ended`` event with the context clock advancing at least
the clip's length; and, in ``mic="meet"`` mode, Meet's own control read
"Turn off microphone" (i.e. live) before playback began. Anything short of that
is ``spoken=False`` with the reason in ``detail``. That is the
``zoombot/speak.py`` ``AUDIBLE_SPEAKERS`` rule applied to one call: "Hardy
replied" must never read as "the room heard Hardy" when it did not.

Unverified on live Meet: whether Meet's audio processing (noise suppression,
its voice-activity gate) passes TTS at full level; whether Meet re-acquires
the mic on unmute (the code waits for a live track either way); and the
English aria-labels, which a localised Meet UI will not match -- that case
refuses in words rather than playing into a muted call.
"""

from __future__ import annotations

import asyncio
import base64
import json
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

from . import tts as _tts
from .types import SpokenReceipt

__all__ = [
    "CHROMIUM_ARGS",
    "MEET_MIC_LIVE_SELECTOR",
    "MEET_MIC_MUTED_SELECTOR",
    "init_script",
    "say",
    "synthesize",
]

#: Launch flags the join agent should pass. Without the autoplay one an
#: AudioContext created by script (no user gesture) stays ``suspended`` and the
#: injected track carries silence; ``say`` refuses in that case rather than
#: "playing" into it. Attendee passes the same flag in
#: ``bots/web_bot_adapter/web_bot_adapter.py``.
CHROMIUM_ARGS: tuple[str, ...] = ("--autoplay-policy=no-user-gesture-required",)

#: Meet's own mic button, by state. ``*=`` rather than ``=`` because Meet has
#: shipped the label with a shortcut suffix; Vexa's ``toggleGoogleMeetMic``
#: matches by substring for that reason, Attendee's ``turnOnMic`` exactly.
MEET_MIC_MUTED_SELECTOR = 'button[aria-label*="Turn on microphone"]'
MEET_MIC_LIVE_SELECTOR = 'button[aria-label*="Turn off microphone"]'

#: Attendee turns the mic off 2 s after its audio queue drains
#: (``_processAudioQueue``); Vexa's ``scheduleAutoMute`` defaults to 2000 ms.
#: The tail lets the WebRTC encoder flush the last syllable before the mute.
DEFAULT_RELEASE_AFTER_S = 2.0

#: How long to wait for a mic toggle to take, and for a live track after it.
TOGGLE_TIMEOUT_S = 4.0

_INIT_SCRIPT = r"""
(() => {
  if (window.__hardyVoice) return;
  const md = navigator.mediaDevices;
  if (!md || typeof md.getUserMedia !== "function") {
    window.__hardyVoice = { installed: false,
      reason: "navigator.mediaDevices.getUserMedia is unavailable"
        + " (not a secure context?)" };
    return;
  }
  const original = md.getUserMedia.bind(md);
  const v = { installed: true, ctx: null, gain: null, dest: null, source: null,
              issued: [], plays: 0 };

  // Lazily, as Attendee's _createSourceAudioTrack does. Deliberately NOT
  // connected to ctx.destination: nothing Hardy says plays on this machine.
  v.ensure = () => {
    if (v.source) return;
    v.ctx = new AudioContext();
    v.gain = v.ctx.createGain();
    v.gain.gain.value = 1.0;
    v.dest = v.ctx.createMediaStreamDestination();
    v.gain.connect(v.dest);
    v.source = v.dest.stream.getAudioTracks()[0] || null;
  };

  const wants = (c, kind) => !!(c && c[kind] !== false && c[kind] != null);
  md.getUserMedia = async function hardyGetUserMedia(constraints) {
    const needAudio = wants(constraints, "audio");
    const needVideo = wants(constraints, "video");
    if (!needAudio && !needVideo) return original(constraints);
    const stream = new MediaStream();
    if (needAudio) {
      v.ensure();
      const clone = v.source.clone();
      v.issued.push(clone);
      stream.addTrack(clone);
    }
    return stream;
  };

  v.status = () => {
    const live = v.issued.filter((t) => t.readyState === "live");
    return { installed: true, issued: v.issued.length, live: live.length,
             enabled: live.filter((t) => t.enabled).length,
             ctx: v.ctx ? v.ctx.state : "none", plays: v.plays };
  };

  v.play = async (b64) => {
    v.ensure();
    if (v.ctx.state !== "running") { try { await v.ctx.resume(); } catch (e) {} }
    if (v.ctx.state !== "running") {
      return { played: false, reason: "AudioContext is " + v.ctx.state +
        "; launch Chromium with --autoplay-policy=no-user-gesture-required" };
    }
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    let buffer;
    try {
      buffer = await v.ctx.decodeAudioData(bytes.buffer);
    } catch (e) {
      return { played: false, reason: "decodeAudioData refused the audio: " + e };
    }
    const src = v.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(v.gain);
    const started = v.ctx.currentTime;
    const ended = await new Promise((resolve) => {
      const guard = setTimeout(() => resolve(false), (buffer.duration + 5) * 1000);
      src.onended = () => { clearTimeout(guard); resolve(true); };
      src.start();
    });
    const elapsed = v.ctx.currentTime - started;
    src.disconnect();
    if (ended) v.plays += 1;
    return { played: ended && elapsed >= buffer.duration * 0.95,
             reason: ended ? "" : "the buffer source never reached 'ended'",
             duration: buffer.duration, elapsed, status: v.status() };
  };

  window.__hardyVoice = v;
})();
"""

TtsFn = Callable[[str], Awaitable["bytes | _tts.Synthesis"]]

_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()


def init_script() -> str:
    """JS to register with ``session.add_init_script`` before navigating to Meet."""
    return _INIT_SCRIPT


async def synthesize(text: str) -> bytes:
    """WAV bytes for ``text`` from the TTS ladder in :mod:`meetbot.tts`."""
    return (await _tts.synthesize(text)).wav


def _lock_for(page: Any) -> asyncio.Lock:
    try:
        lock = _LOCKS.get(page)
        if lock is None:
            lock = _LOCKS[page] = asyncio.Lock()
        return lock
    except TypeError:  # not weak-referenceable; one lock per call is still safe-ish
        return asyncio.Lock()


async def _meet_mic_state(page: Any) -> str | None:
    """``"live"``, ``"muted"``, or ``None`` when Meet's control is not on the page."""
    return await page.evaluate(
        """([muted, live]) => document.querySelector(live) ? "live"
             : document.querySelector(muted) ? "muted" : null""",
        [MEET_MIC_MUTED_SELECTOR, MEET_MIC_LIVE_SELECTOR],
    )


async def _set_meet_mic(page: Any, want: str) -> bool:
    """Click Meet's mic button until it reads ``want``; True once it does."""
    state = await _meet_mic_state(page)
    if state == want:
        return True
    if state is None:
        return False
    selector = MEET_MIC_MUTED_SELECTOR if want == "live" else MEET_MIC_LIVE_SELECTOR
    await page.click(selector, timeout=TOGGLE_TIMEOUT_S * 1000)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOGGLE_TIMEOUT_S
    while loop.time() < deadline:
        if await _meet_mic_state(page) == want:
            return True
        await asyncio.sleep(0.1)
    return False


async def _voice_status(page: Any) -> dict[str, Any] | None:
    return await page.evaluate(
        "() => window.__hardyVoice ? (window.__hardyVoice.installed"
        " ? window.__hardyVoice.status() : window.__hardyVoice) : null"
    )


async def _wait_for_live_track(page: Any, timeout_s: float) -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    status = await _voice_status(page)
    while status and status.get("installed") and not status.get("enabled"):
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0.1)
        status = await _voice_status(page)
    return status


async def say(
    session: Any,
    text: str,
    *,
    tts: TtsFn | None = None,
    mic: str = "meet",
    release_after_s: float = DEFAULT_RELEASE_AFTER_S,
) -> SpokenReceipt:
    """Speak ``text`` into the call through the injected microphone track.

    ``mic="meet"`` unmutes Meet's own control for the utterance and restores
    its previous state afterwards (refusing if the control cannot be found or
    will not flip). ``mic="page"`` skips the button, for a page that is not
    Meet -- the offline tests. ``tts`` replaces the synthesis ladder; it may
    return WAV bytes or a :class:`meetbot.tts.Synthesis`.
    """
    if mic not in ("meet", "page"):
        raise ValueError("mic must be 'meet' or 'page'")
    page = session.page

    try:
        produced = await (tts or _tts.synthesize)(text)
    except Exception as exc:  # noqa: BLE001 - every failure becomes a receipt
        return SpokenReceipt(False, f"not spoken: no audio was synthesized: {exc}")
    if isinstance(produced, _tts.Synthesis):
        wav, source = produced.wav, produced.source
    else:
        wav, source = bytes(produced), "caller-supplied tts"
    if not wav:
        return SpokenReceipt(False, f"not spoken: {source} returned no audio")

    async with _lock_for(page):
        try:
            return await _play_locked(page, wav, source, mic, release_after_s)
        except Exception as exc:  # noqa: BLE001 - a closed page, a navigation
            return SpokenReceipt(
                False, f"not spoken: the page failed during playback: {exc}"
            )


async def _play_locked(
    page: Any, wav: bytes, source: str, mic: str, release_after_s: float
) -> SpokenReceipt:
    if True:
        status = await _voice_status(page)
        if status is None:
            return SpokenReceipt(
                False,
                "not spoken: the voice init script is not in this page; register "
                "init_script() with session.add_init_script before navigating",
            )
        if not status.get("installed"):
            return SpokenReceipt(False, f"not spoken: {status.get('reason')}")
        if not status.get("issued"):
            return SpokenReceipt(
                False, "not spoken: the page never asked for a microphone, so no "
                "injected track exists to carry audio"
            )

        restore: str | None = None
        if mic == "meet":
            before = await _meet_mic_state(page)
            if before is None:
                return SpokenReceipt(
                    False,
                    "not spoken: Meet's microphone control was not found "
                    f"({MEET_MIC_MUTED_SELECTOR} / {MEET_MIC_LIVE_SELECTOR}); "
                    "refusing to play into a call that may be muted",
                )
            if before == "muted":
                if not await _set_meet_mic(page, "live"):
                    return SpokenReceipt(
                        False, "not spoken: Meet's microphone would not unmute"
                    )
                restore = "muted"

        try:
            status = await _wait_for_live_track(page, TOGGLE_TIMEOUT_S)
            if not status or not status.get("enabled"):
                return SpokenReceipt(
                    False,
                    "not spoken: no live, enabled injected microphone track "
                    f"(page reports {json.dumps(status)})",
                )
            result = await page.evaluate(
                "(b64) => window.__hardyVoice.play(b64)",
                base64.b64encode(wav).decode("ascii"),
            )
            if restore and release_after_s > 0:
                await asyncio.sleep(release_after_s)
        finally:
            mute_note = ""
            if restore:
                try:
                    if not await _set_meet_mic(page, restore):
                        mute_note = "; WARNING: Meet's microphone did not re-mute"
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    mute_note = f"; WARNING: re-muting Meet failed: {exc}"

        seconds = float(result.get("duration") or 0.0)
        if not result.get("played"):
            return SpokenReceipt(
                False,
                f"not spoken: {result.get('reason') or 'playback did not complete'} "
                f"(audio from {source}){mute_note}",
            )
        after = result.get("status") or {}
        if not after.get("enabled"):
            return SpokenReceipt(
                False,
                "not spoken: audio played but no enabled microphone track was "
                f"carrying it by the end (audio from {source}){mute_note}",
                seconds,
            )
        if restore:
            where = "Meet microphone unmuted for it"
        elif mic == "meet":
            where = "Meet microphone already live"
        else:
            where = "page mode, no Meet control"
        return SpokenReceipt(
            True,
            f"spoke {seconds:.2f}s via injected getUserMedia track; audio from "
            f"{source}; {where}{mute_note}",
            seconds,
        )
