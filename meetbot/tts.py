"""Ada's voice for the Meet bot: text in, one complete mono WAV out.

Which voice answered is part of the answer. Every successful call returns a
:class:`Synthesis` whose ``source`` names the path that actually produced the
bytes, so :func:`meetbot.speak.say` can put it in ``SpokenReceipt.detail`` and
nobody has to listen to the call to tell Kokoro from the macOS robot.

The ladder, tried in order, each rung only when the one above could not speak:

1. **The running service**, ``POST {KALEO_TTS_URL or http://127.0.0.1:8081}/speak``
   (``service/app.py::_speak``). Asked for ``format: "pcm16"`` rather than
   ``wav``: the route's WAV header carries ``0xFFFFFFFF`` sizes because it
   streams (``service/tts.py::wav_header``), and the page decodes with
   ``AudioContext.decodeAudioData``, which wants a file whose length is true.
   The sample rate comes from the route's ``X-Kaleo-Sample-Rate`` header and
   the engine name from ``X-Kaleo-Tts-Engine``.
2. **The same engines in-process** (``service.tts.build_engines`` +
   ``service.tts.synthesize``), for a bot started without the service. This
   loads Kokoro into the bot's own process -- seconds and a few hundred MB --
   which is why it is second and not first.
3. **macOS ``say -o x.aiff``** then ``afconvert`` to 16-bit mono WAV. The
   robot, but it exists on every Mac and needs nothing installed.

Nothing returns a quiet zero: when every rung fails, :class:`TtsUnavailable`
names each rung and why, because an empty WAV played into a meeting is
silence reported as speech.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_SERVICE_URL",
    "Synthesis",
    "TtsUnavailable",
    "pcm16_to_wav",
    "synthesize",
    "synthesize_in_process",
    "synthesize_via_say",
    "synthesize_via_service",
    "wav_duration_s",
]

#: 8081, not 8080: 8080 is taken on James's machine (CLAUDE.md, Commands).
DEFAULT_SERVICE_URL = "http://127.0.0.1:8081"

#: The service's limit (``service/tts.py::MAX_TEXT_CHARS``). Checked here so a
#: long reply fails with its own sentence before three rungs each refuse it.
MAX_TEXT_CHARS = 1200

SERVICE_TIMEOUT_S = 60.0


class TtsUnavailable(RuntimeError):
    """No rung of the ladder produced audio; the message names every reason."""


@dataclass(frozen=True)
class Synthesis:
    """One utterance's audio and the path that produced it."""

    wav: bytes
    source: str

    @property
    def seconds(self) -> float:
        return wav_duration_s(self.wav)


def pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap mono signed 16-bit little-endian PCM in a WAV with true sizes."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_duration_s(data: bytes) -> float:
    """Duration of a PCM WAV, read from the file rather than assumed."""
    with wave.open(io.BytesIO(data), "rb") as w:
        rate = w.getframerate()
        return w.getnframes() / rate if rate else 0.0


def _check_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("there is nothing to say: text is empty")
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(
            f"text is {len(text)} characters; the limit is {MAX_TEXT_CHARS}. "
            "Split it into separate say() calls rather than truncating"
        )
    return text


def _require_audio(wav: bytes, source: str) -> Synthesis:
    try:
        seconds = wav_duration_s(wav)
    except (wave.Error, EOFError, struct.error) as exc:
        raise TtsUnavailable(
            f"{source} returned bytes that are not a WAV: {exc}"
        ) from exc
    if seconds <= 0.0:
        raise TtsUnavailable(f"{source} returned a WAV with no audio in it")
    return Synthesis(wav=wav, source=source)


# --------------------------------------------------------------------------
# Rung 1: the running service
# --------------------------------------------------------------------------


def synthesize_via_service(
    text: str, *, base_url: str | None = None, voice: str | None = None
) -> Synthesis:
    """Ask the running ``silkscreen serve``/``service.app`` for PCM, wrap it."""
    base = base_url or os.environ.get("KALEO_TTS_URL") or DEFAULT_SERVICE_URL
    base = base.rstrip("/")
    body: dict[str, object] = {"text": _check_text(text), "format": "pcm16"}
    if voice:
        body["voice"] = voice
    request = urllib.request.Request(
        f"{base}/speak",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=SERVICE_TIMEOUT_S) as response:
            engine = response.headers.get("X-Kaleo-Tts-Engine") or "unknown engine"
            rate_header = response.headers.get("X-Kaleo-Sample-Rate")
            pcm = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise TtsUnavailable(f"service /speak answered {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TtsUnavailable(f"service /speak at {base} unreachable: {exc}") from exc
    try:
        rate = int(rate_header or "")
    except ValueError:
        raise TtsUnavailable(
            f"service /speak sent no usable X-Kaleo-Sample-Rate ({rate_header!r})"
        ) from None
    return _require_audio(
        pcm16_to_wav(pcm, rate), f"service /speak ({engine}) at {base}"
    )


# --------------------------------------------------------------------------
# Rung 2: the same engines, in this process
# --------------------------------------------------------------------------


def synthesize_in_process(text: str) -> Synthesis:
    """``service.tts`` loaded here; its own precedence picks the engine."""
    try:
        from service import tts as service_tts
    except Exception as exc:  # noqa: BLE001 - any import failure means "not this rung"
        raise TtsUnavailable(f"service.tts not importable here: {exc}") from exc
    try:
        request = service_tts.speak_request(
            {"text": _check_text(text), "format": "pcm16"}
        )
        engines = service_tts.build_engines()
        engine, frames = service_tts.synthesize(engines, request)
        pcm = b"".join(frames)
    except Exception as exc:  # noqa: BLE001 - TtsError, ValueError, a model load failure
        raise TtsUnavailable(f"in-process service.tts could not speak: {exc}") from exc
    return _require_audio(
        pcm16_to_wav(pcm, engine.sample_rate), f"in-process service.tts ({engine.name})"
    )


# --------------------------------------------------------------------------
# Rung 3: macOS say
# --------------------------------------------------------------------------


def synthesize_via_say(text: str, *, voice: str | None = None) -> Synthesis:
    """``say -o`` (it cannot write to a pipe) then ``afconvert`` to WAV."""
    say = shutil.which("say")
    afconvert = shutil.which("afconvert")
    if not say or not afconvert:
        raise TtsUnavailable(
            "macOS 'say'/'afconvert' not on PATH (this rung exists only on a Mac)"
        )
    text = _check_text(text)
    with tempfile.TemporaryDirectory(prefix="meetbot-say-") as tmp:
        aiff = Path(tmp) / "say.aiff"
        wav = Path(tmp) / "say.wav"
        cmd = [say, "-o", str(aiff)]
        if voice:
            cmd += ["-v", voice]
        try:
            subprocess.run(
                [*cmd, "--", text], check=True, capture_output=True, timeout=60
            )
            subprocess.run(
                [afconvert, "-f", "WAVE", "-d", "LEI16@24000", "-c", "1",
                 str(aiff), str(wav)],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (
            subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError
        ) as exc:
            stderr = (getattr(exc, "stderr", b"") or b"")[:200]
            raise TtsUnavailable(
                f"macOS say/afconvert failed: {exc} "
                f"{stderr.decode('utf-8', 'replace')}"
            ) from exc
        return _require_audio(wav.read_bytes(), "macOS say + afconvert")


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------


async def synthesize(
    text: str, *, base_url: str | None = None, in_process: bool = True
) -> Synthesis:
    """Walk the ladder off the event loop; raise naming every failed rung."""
    _check_text(text)
    rungs = [lambda: synthesize_via_service(text, base_url=base_url)]
    if in_process:
        rungs.append(lambda: synthesize_in_process(text))
    rungs.append(lambda: synthesize_via_say(text))
    reasons: list[str] = []
    for rung in rungs:
        try:
            return await asyncio.to_thread(rung)
        except TtsUnavailable as exc:
            reasons.append(str(exc))
    raise TtsUnavailable("no voice could speak: " + " | ".join(reasons))
