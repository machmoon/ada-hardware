"""Hear a Google Meet: speaker-attributed utterances from Meet's own live captions.

Two ways exist to turn a Meet into text from inside the bot's browser, and this
module takes the first on evidence:

(a) **Read Meet's live captions out of the DOM.** Meet's server-side speech
    recognition already names every speaker, the local participant included
    (labelled "You"), and captions are available on free accounts. No audio
    plumbing, no model call per sentence, and nothing macOS-specific. This is
    how TranscripTonic, a maintained open-source Meet transcript extension,
    works: ``extension/content-scripts/google-meet/index.js``
    (``transcriptMutationCallbackGoogleMeet``) and
    ``extension/content-scripts/common-utils.js`` (``startTranscriptMonitor``,
    ``pushBufferToTranscript``) at upstream ``0cb5eb5``.

(b) **Capture each participant's WebRTC audio in the page and transcribe it.**
    Vexa does this (``core/meetings/modules/gmeet-capture/src/gmeet-capture.ts``
    and ``pcm-capture.ts``: one ``AudioContext`` + ``AudioWorklet`` per
    ``<audio>``/``<video>`` element, resampled to 16 kHz), but a WebRTC track
    carries no name, so Vexa attributes speakers by polling Meet's
    obfuscated "speaking glow" CSS classes (``gmeet-speakers.ts``) and its own
    header calls that "a known-bad foundation". joinly
    (``joinly/providers/browser/platforms/google_meet.py``) captures through a
    PulseAudio virtual speaker, which is Linux-only. For a live demo on a Mac,
    (b) adds a model call per chunk, a silence gate, and a guessed speaker
    name, each a new way to fail in front of an audience. It is the documented
    follow-up for a meeting where captions are switched off by policy; passing
    ``transcriber=`` today is a refusal in words, not a silent fallback.

What is taken from where:

* Where the captions live, and which children are captions: TranscripTonic's
  ``TRANSCRIPT_REGION`` (``div[role="region"][tabindex="0"]``), its rule that
  the region's last two children are not captions, and its reading of a caption
  block as ``[speaker element, text element]`` (``textEl.previousSibling`` is
  the name). Its ``"You" -> userName`` rewrite is how the bot's own speech is
  recognised here.
* Turning captions on: Attendee's ``button[aria-label="Turn on captions"]`` /
  ``"Turn off captions"`` confirmation
  (``bots/google_meet_bot_adapter/google_meet_ui_methods.py:click_captions_button``),
  with TranscripTonic's ``closed_caption_off`` icon as the second way to find it.
* Meeting still running: TranscripTonic's ``call_end`` icon in ``.google-symbols``.
* De-duplicating rewrites: Vexa's Teams caption reader
  (``core/meetings/modules/teams-capture/src/msteams-captions.ts``) -- Meet,
  like Teams, rewrites a caption in place as recognition refines it, so a block
  is emitted once it has stopped changing for a short window or a newer block
  has superseded it, and continuation is judged on a lowercased,
  punctuation-free form because refinement re-cases and re-punctuates. Its
  typed ``captions-absent`` / ``captions-lost`` observations are why a missing
  caption source raises here instead of yielding nothing.

One deliberate difference from TranscripTonic: it keeps one buffer per block
and saves it when the next speaker starts, which is fine for a file written at
the end of a meeting and too slow for a bot that has to answer while the
speaker is waiting. Here a block that stops changing is yielded, and if the
same speaker then keeps talking into the same block, only the new words are
yielded next time.

**Unverified against live Meet.** Every selector above is read from those
projects' current source, not observed here; they are gathered in one block
(``SELECTORS``) so one live run can correct them in one place. The aria-labels
are English, so a non-English Meet UI will not find the captions button and
will raise ``CaptionsUnavailableError`` saying so.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from .types import Utterance

__all__ = [
    "SELECTORS",
    "CaptionBlock",
    "CaptionStabilizer",
    "CaptionsLostError",
    "CaptionsUnavailableError",
    "ListenError",
    "init_script",
    "transcript_stream",
]

#: Every Meet DOM assumption this module makes, in one place. Sources are named
#: in the module docstring; none has been observed against a live meeting here.
SELECTORS: dict[str, Any] = {
    # TranscripTonic SELECTORS_GOOGLE_MEET.TRANSCRIPT_REGION.
    "region": 'div[role="region"][tabindex="0"]',
    # TranscripTonic: "the last and last but one are non transcript elements".
    "trailing_non_caption_children": 2,
    # Attendee click_captions_button.
    "captions_on_button": 'button[aria-label="Turn on captions"]',
    "captions_off_button": 'button[aria-label="Turn off captions"]',
    # TranscripTonic GOOGLE_SYMBOLS + TEXT_CAPTIONS / TEXT_CALL_END.
    "symbols": ".google-symbols",
    "icon_captions_off": "closed_caption_off",
    "icon_captions_on": "closed_caption",
    "icon_call_end": "call_end",
    # TranscripTonic pushBufferToTranscript: Meet names the local participant "You".
    "self_label": "You",
}

#: Blocks returned per snapshot. Older blocks are final and have been yielded;
#: bounding the payload keeps a two-hour meeting's poll as cheap as the first.
SNAPSHOT_TAIL = 12

_JS_OBJECT = "__hardyCaptions"


class ListenError(RuntimeError):
    """Base for every way the transcript stream stops without a meeting ending."""


class CaptionsUnavailableError(ListenError):
    """Captions never came on, so there is nothing to listen to.

    Raised instead of an empty stream: a brain handed silence concludes
    nobody spoke.
    """


class CaptionsLostError(CaptionsUnavailableError):
    """Captions were live and went away while the call was still running."""


def init_script() -> str:
    """JS that installs ``window.__hardyCaptions`` in every frame, before Meet loads.

    Optional: :func:`transcript_stream` installs the same object on demand if
    the session did not add this script. It only reads the DOM; the one thing
    it ever clicks is the captions button, and only when asked to.
    """
    return _INIT_JS.replace("__SELECTORS__", json.dumps(SELECTORS)).replace(
        "__TAIL__", str(SNAPSHOT_TAIL)
    )


_INIT_JS = r"""
(() => {
  if (window.__hardyCaptions) return;
  const S = __SELECTORS__;
  const TAIL = __TAIL__;
  const ids = new WeakMap();
  let nextId = 1;
  const idOf = (el) => {
    if (!ids.has(el)) ids.set(el, nextId++);
    return ids.get(el);
  };
  const icon = (text) =>
    Array.from(document.querySelectorAll(S.symbols)).find(
      (el) => (el.textContent || '').trim() === text);
  const buttonFor = (el) => (el ? (el.closest('button') || el) : null);
  const captionsState = () => {
    if (document.querySelector(S.captions_off_button)) return 'on';
    if (document.querySelector(S.captions_on_button)) return 'off';
    if (icon(S.icon_captions_off)) return 'off';
    if (icon(S.icon_captions_on)) return 'on';
    return 'unknown';
  };
  window.__hardyCaptions = {
    enable() {
      if (captionsState() === 'on') return 'on';
      const btn = document.querySelector(S.captions_on_button)
        || buttonFor(icon(S.icon_captions_off));
      if (!btn) return 'no-button';
      btn.click();
      return 'clicked';
    },
    snapshot() {
      const region = document.querySelector(S.region);
      const blocks = [];
      if (region) {
        const kids = Array.from(region.children);
        const keep = Math.max(0, kids.length - S.trailing_non_caption_children);
        const captions = kids.slice(0, keep);
        for (const block of captions.slice(-TAIL)) {
          const textEl = block.lastElementChild;
          const nameEl = textEl ? textEl.previousElementSibling : null;
          blocks.push({
            id: idOf(block),
            speaker: nameEl ? (nameEl.textContent || '').trim() : '',
            text: textEl ? (textEl.textContent || '').trim() : '',
          });
        }
      }
      return {
        region: !!region,
        inCall: !!icon(S.icon_call_end),
        captions: captionsState(),
        blocks,
      };
    },
  };
})();
"""


@dataclass
class CaptionBlock:
    """One caption block as Meet renders it: element id, speaker, text so far."""

    id: int
    speaker: str
    text: str


@dataclass
class _Tracked:
    speaker: str
    text: str
    first_seen: float
    changed_at: float
    said: list[str] = field(default_factory=list)  # words already yielded
    said_at: float | None = None  # when the unsaid tail started
    closed: bool = False


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def _unsaid(said: list[str], words: list[str]) -> list[str]:
    """The words of ``words`` not yet yielded, given ``said`` already was.

    Compared on a lowercased, punctuation-free form (Vexa's
    ``normalizeForContinuation``): "i don't think" revised to "I don't think,"
    adds nothing. Three shapes are handled:

    * grown or re-punctuated in place: ``words`` starts with ``said``;
    * trimmed from the front, as Meet does to a long monologue (TranscripTonic
      detects that by the text shrinking): the tail of ``said`` overlaps the
      head of ``words``;
    * rewritten: everything past a common prefix as long as ``said``.
    """
    a = [_norm(w) for w in said]
    b = [_norm(w) for w in words]
    if not a:
        return words
    if b[: len(a)] == a:
        return words[len(a) :]
    for k in range(min(len(a), len(b)), 0, -1):
        if a[-k:] == b[:k]:
            return words[k:]
    common = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        common += 1
    if common and len(b) > len(a):
        return words[len(a) :]
    if common:
        return []  # a revision of words already said, not new speech
    return words


class CaptionStabilizer:
    """Turns successive caption snapshots into de-duplicated utterances.

    Pure and clock-driven, so the rewrite rules are testable without a
    browser. A block's new words are released when its text has not changed
    for ``stabilize_s`` (Vexa's ``stabilizeMs``; 0.9 s there) or a newer block
    has appeared after it. Once a block has been superseded and flushed it is
    closed: Meet still polishes old blocks, and yielding those corrections
    would repeat what was already said.
    """

    def __init__(
        self,
        *,
        self_name: str = "Ada",
        self_labels: frozenset[str] | None = None,
        stabilize_s: float = 0.9,
    ) -> None:
        self.self_name = self_name
        labels = {SELECTORS["self_label"], self_name}
        if self_labels:
            labels |= set(self_labels)
        self.self_labels = frozenset(label.casefold() for label in labels)
        self.stabilize_s = stabilize_s
        self._blocks: dict[int, _Tracked] = {}
        self._order: list[int] = []
        self.withheld = 0  # blocks with text but no speaker: never given a name

    def feed(self, blocks: list[CaptionBlock], now: float) -> list[Utterance]:
        for block in blocks:
            tracked = self._blocks.get(block.id)
            if tracked is None:
                self._blocks[block.id] = _Tracked(block.speaker, block.text, now, now)
                self._order.append(block.id)
            elif tracked.text != block.text or (
                block.speaker and tracked.speaker != block.speaker
            ):
                tracked.text = block.text
                tracked.speaker = block.speaker or tracked.speaker
                tracked.changed_at = now
        out: list[Utterance] = []
        newest = self._order[-1] if self._order else None
        for block_id in self._order:
            tracked = self._blocks[block_id]
            if tracked.closed:
                continue
            superseded = block_id != newest
            if superseded or now - tracked.changed_at >= self.stabilize_s:
                out.extend(self._release(tracked, now, final=superseded))
                if superseded:
                    tracked.closed = True
        self._forget_closed()
        return out

    def flush(self, now: float) -> list[Utterance]:
        """Release every open block: the meeting ended, nothing more is coming."""
        out: list[Utterance] = []
        for block_id in self._order:
            tracked = self._blocks[block_id]
            if not tracked.closed:
                out.extend(self._release(tracked, now, final=True))
                tracked.closed = True
        self._forget_closed()
        return out

    def _release(
        self, tracked: _Tracked, now: float, *, final: bool
    ) -> list[Utterance]:
        words = tracked.text.split()
        new = _unsaid(tracked.said, words)
        if not new:
            if words:
                tracked.said = words
            return []
        speaker = tracked.speaker.strip()
        if not speaker:
            # Vexa's rule: unknown stays unknown. Wait for the name to render;
            # if the block closes without one, count it and give it no name.
            if final:
                self.withheld += 1
                tracked.said = words
            return []
        start = tracked.said_at if tracked.said_at is not None else tracked.first_seen
        tracked.said = words
        tracked.said_at = now
        is_self = speaker.casefold() in self.self_labels
        return [
            Utterance(
                speaker=self.self_name if is_self else speaker,
                text=" ".join(new),
                t_start=start,
                t_end=tracked.changed_at,
                is_self=is_self,
            )
        ]

    def _forget_closed(self) -> None:
        """Bound memory: drop closed blocks older than twice the snapshot tail.

        Older ids can no longer appear in a snapshot, so forgetting them cannot
        make an old block look new.
        """
        horizon = len(self._order) - SNAPSHOT_TAIL * 2
        if horizon <= 0:
            return
        kept = []
        for i, block_id in enumerate(self._order):
            if i < horizon and self._blocks[block_id].closed:
                del self._blocks[block_id]
            else:
                kept.append(block_id)
        self._order = kept


#: Playwright raises these while a page navigates or re-renders; one poll is
#: skipped rather than ending the stream.
_TRANSIENT = ("Execution context was destroyed", "navigation", "Target closed")


async def _snapshot(page: Any) -> dict[str, Any] | None:
    """One read of the captions state; None when the page has gone away.

    An empty dict means this poll saw nothing usable (the page was mid
    navigation) and should simply be retried.
    """
    if page.is_closed():
        return None
    try:
        return await page.evaluate(
            f"() => {{ {init_script()}; return window.{_JS_OBJECT}.snapshot(); }}"
        )
    except Exception as exc:
        if page.is_closed():
            return None
        if any(marker in str(exc) for marker in _TRANSIENT):
            return {}
        raise


async def _enable(page: Any) -> str:
    return await page.evaluate(
        f"() => {{ {init_script()}; return window.{_JS_OBJECT}.enable(); }}"
    )


async def transcript_stream(
    session: Any,
    *,
    transcriber: Any = None,
    self_name: str | None = None,
    poll_s: float = 0.25,
    stabilize_s: float = 0.9,
    enable_timeout_s: float = 30.0,
    lost_grace_s: float = 10.0,
    end_grace_s: float = 1.0,
    clock: Callable[[], float] = time.time,
) -> AsyncIterator[Utterance]:
    """Yield who said what in the meeting ``session`` is in, as captions settle.

    Waits for the in-call controls, turns Meet's captions on, then polls the
    captions every ``poll_s`` (Vexa's 250 ms) and yields each settled stretch
    of speech once. The bot's own speech comes back with ``is_self=True`` and
    ``speaker`` set to ``self_name``. Timestamps are ``clock()`` seconds
    (epoch by default): ``t_start`` is when the words first appeared,
    ``t_end`` when they last changed.

    Ends normally when the page closes or the ``call_end`` control has been
    gone for ``end_grace_s`` (so one re-render is not a hang-up), after
    flushing whatever was still settling. Raises
    :class:`CaptionsUnavailableError` when captions cannot be turned on or the
    captions region never appears within ``enable_timeout_s`` of joining, and
    :class:`CaptionsLostError` when the region disappears for ``lost_grace_s``
    while the call is still running.
    """
    if transcriber is not None:
        raise ListenError(
            "audio transcription is not built: meetbot.listen reads Meet's live "
            "captions (see the module docstring for why); call transcript_stream "
            "without transcriber="
        )
    page = session.page
    name = self_name or getattr(session, "display_name", None) or "Ada"
    stabilizer = CaptionStabilizer(self_name=name, stabilize_s=stabilize_s)

    joined_at: float | None = None  # first poll that saw the in-call controls
    live = False  # captions have been confirmed on at least once
    missing_since: float | None = None  # captions went away after being live
    last_enable: float | None = None
    last_enable_result = ""
    out_of_call_since: float | None = None

    async def try_enable(now: float) -> None:
        nonlocal last_enable, last_enable_result
        if last_enable is None or now - last_enable >= 2.0:
            last_enable_result = await _enable(page)
            last_enable = now

    while True:
        snap = await _snapshot(page)
        now = clock()
        if snap == {}:
            await asyncio.sleep(poll_s)
            continue
        if snap is not None and joined_at is not None and not snap["inCall"]:
            if out_of_call_since is None:
                out_of_call_since = now
        elif snap is not None:
            out_of_call_since = None
        ended = out_of_call_since is not None and now - out_of_call_since >= end_grace_s
        if snap is None or ended:
            for utterance in stabilizer.flush(now):
                yield utterance
            return
        if not snap["inCall"]:
            await asyncio.sleep(poll_s)  # in the lobby, or a control re-rendering
            continue
        if joined_at is None:
            joined_at = now

        # TranscripTonic notes the region exists whether captions are on or
        # off, so "on" is Meet's own button state, or caption text on screen.
        has_text = snap["region"] and any(b["text"] for b in snap["blocks"])
        state = snap["captions"]
        captions_on = state == "on" or (state == "unknown" and has_text)

        if captions_on and snap["region"]:
            live = True
            missing_since = None
            blocks = [
                CaptionBlock(b["id"], b["speaker"], b["text"]) for b in snap["blocks"]
            ]
            for utterance in stabilizer.feed(blocks, now):
                yield utterance
        elif not live:
            if not captions_on:
                await try_enable(now)
            if now - joined_at >= enable_timeout_s:
                raise CaptionsUnavailableError(
                    _never_on(last_enable_result, captions_on, enable_timeout_s)
                )
        else:
            if missing_since is None:
                missing_since = now
            if not captions_on:
                await try_enable(now)  # someone switched them off: try to recover
            if now - missing_since >= lost_grace_s:
                for utterance in stabilizer.flush(now):
                    yield utterance
                raise CaptionsLostError(
                    f"captions lost: Meet's captions went away for "
                    f"{lost_grace_s:.0f} s "
                    f"while the call was still running and could not be turned back on"
                )
        await asyncio.sleep(poll_s)


def _never_on(enable_result: str, captions_on: bool, timeout_s: float) -> str:
    if captions_on:
        return (
            f"captions unavailable: captions are on but no captions region "
            f"({SELECTORS['region']}) appeared within {timeout_s:.0f} s of joining; "
            f"Meet's captions DOM has probably changed"
        )
    if enable_result == "no-button":
        return (
            f"captions unavailable: no captions button found within {timeout_s:.0f} s "
            f"of joining (looked for {SELECTORS['captions_on_button']} and the "
            f"'{SELECTORS['icon_captions_off']}' icon; a non-English Meet UI "
            f"will not match)"
        )
    return (
        f"captions unavailable: pressed the captions button but Meet did not "
        f"confirm captions on within {timeout_s:.0f} s of joining"
    )
