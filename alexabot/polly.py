"""Ada's voice on the simulated Alexa+ page: Amazon Polly, neural, speaking only
what Ada already said.

The request is botocore's ``SynthesizeSpeech`` model
(``botocore/data/polly/2016-06-10/service-2.json``, develop ``86201a3``,
Apache-2.0): ``POST /v1/speech`` with the required ``OutputFormat``, ``Text``
and ``VoiceId``; ``Engine`` one of ``standard|neural|long-form|generative``;
``TextType`` ``ssml|text``; an mp3 sample rate of 8000 to 48000 (neural's
default 24000); ``Text`` at most "6000 characters total, of which no more than
3000 can be billed characters". The response is a streaming ``AudioStream``,
its ``ContentType``, and the billed count in ``x-amzn-RequestCharacters``.

Four rules, each tested (``test_alexa_sim_polly.py``):

* ``TextType`` is always ``text``, never ``ssml``: a sentence a model wrote
  cannot inject markup.
* The same text is synthesised once (an LRU cache keyed on voice, engine and
  text): a replay costs nothing.
* A hard call budget is taken **before** the request, so the cap cannot be
  overshot by a slow answer.
* Any error becomes :class:`PollyUnavailable` carrying the exception's class
  name only; its message is never logged or shown.

``boto3`` is imported inside :func:`make_client`, so this module imports
nothing outside the standard library.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_VOICE",
    "MAX_TEXT_CHARS",
    "Audio",
    "PollyBudgetSpent",
    "PollyError",
    "PollySpeaker",
    "PollyUnavailable",
    "TextTooLong",
    "make_client",
]

DEFAULT_VOICE = "Joanna"
DEFAULT_ENGINE = "neural"
DEFAULT_SAMPLE_RATE = "24000"
#: The billed-character ceiling of one ``SynthesizeSpeech`` call.
MAX_TEXT_CHARS = 3000
CACHE_SIZE = 64


class PollyError(RuntimeError):
    """No audio; ``reason`` is the one word the page is told."""

    reason = "polly_unavailable"


class PollyUnavailable(PollyError):
    """Polly could not be reached or refused; the message is a class name."""


class PollyBudgetSpent(PollyError):
    reason = "polly_budget"


class TextTooLong(PollyError):
    reason = "text_too_long"


@dataclass(frozen=True)
class Audio:
    data: bytes
    content_type: str
    billed_chars: int | None
    cached: bool = False


class PollySpeaker:
    """``SynthesizeSpeech`` behind a cache and a budget.

    ``client`` is a boto3 Polly client or anything with the same
    ``synthesize_speech`` method (the tests' fake). ``take`` is the budget: a
    callable that answers whether one more call may be made.
    """

    def __init__(
        self,
        client: Any,
        *,
        voice: str = DEFAULT_VOICE,
        engine: str = DEFAULT_ENGINE,
        sample_rate: str = DEFAULT_SAMPLE_RATE,
        take: Any = None,
    ) -> None:
        self.client = client
        self.voice = voice
        self.engine = engine
        self.sample_rate = sample_rate
        self._take = take if take is not None else (lambda: True)
        self._cache: OrderedDict[str, Audio] = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, text: str) -> str:
        return hashlib.sha256(
            f"{self.voice}|{self.engine}|{text}".encode()
        ).hexdigest()

    def synthesize(self, text: str) -> Audio:
        text = str(text or "").strip()
        if not text or len(text) > MAX_TEXT_CHARS:
            raise TextTooLong(f"{len(text)} characters")
        key = self._key(text)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return Audio(hit.data, hit.content_type, hit.billed_chars, True)
            if not self._take():
                raise PollyBudgetSpent("the Polly call budget is spent")
        try:
            response = self.client.synthesize_speech(
                Engine=self.engine,
                OutputFormat="mp3",
                SampleRate=self.sample_rate,
                Text=text,
                TextType="text",
                VoiceId=self.voice,
            )
            stream = response["AudioStream"]
            try:
                data = stream.read()
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
            headers = (response.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
            billed_raw = headers.get("x-amzn-requestcharacters")
            billed = int(billed_raw) if str(billed_raw or "").isdigit() else None
            content_type = str(response.get("ContentType") or "audio/mpeg")
        except PollyError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the class name only, ever
            raise PollyUnavailable(type(exc).__name__) from None
        if not data:
            raise PollyUnavailable("EmptyAudioStream")
        audio = Audio(bytes(data), content_type, billed)
        with self._lock:
            self._cache[key] = audio
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return audio


def make_client(region: str) -> Any:
    """A boto3 Polly client for ``region``; boto3 is the ``alexa`` extra."""
    import boto3

    return boto3.client("polly", region_name=region)
