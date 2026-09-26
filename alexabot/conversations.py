"""The simulated Alexa+ page's conversations: an event log per browser session.

Standard library only. The page talks to :class:`ConversationHost`; the agent
behind each conversation is whatever the injected factory opens (the Strands
agent in :mod:`alexabot.agent`, or a fake in the HTTP tests), and it reports
back only by writing events into its :class:`Conversation`.

The log is the page's single source of truth. Every event has a sequence
number, so the page can resume an EventSource from ``Last-Event-ID`` (the
HTML Standard's resume) and a JSON snapshot can be read from any ``after``.
Kinds: ``user``, ``agent_status``, ``card`` (an upsert by ``card.id``, logged
only when it changed), ``focus`` (the page shows that card again: a tool the
model called answered with a card that had not changed, so no ``card`` event
would move the screen back to it), ``ada``, ``progress``, ``notice``,
``error`` and ``trace``. ``ada``, ``progress`` and ``error`` are
**utterances**: each has an ``utterance_id``, and those are the only texts
the Polly route will speak.

Conversations live in memory; a restart loses them (the boards are in
SQLite). At most :data:`MAX_CONVERSATIONS` are kept; one idle for
:data:`IDLE_EVICT_S` is dropped to make room.
"""

from __future__ import annotations

import contextlib
import datetime
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "GREETING",
    "SCRIPTED_GREETING",
    "Busy",
    "Conversation",
    "ConversationHost",
    "EventLog",
    "Full",
    "UnknownConversation",
]

MAX_CONVERSATIONS = 16
IDLE_EVICT_S = 30 * 60
LOG_CAP = 500
TRACE_ARGS_MAX = 1024

GREETING = (
    "Hi, I'm Ada. Tell me what board you'd like, for example a three point three "
    "volt regulator powered from USB-C."
)
SCRIPTED_GREETING = (
    "Scripted mode: a rule-based agent and canned answers, so every board is the "
    "practice regulator. Tell me what board you'd like."
)
BUSY_SPEECH = "One moment, I'm still answering."
UTTERANCE_KINDS = frozenset({"ada", "progress", "error"})


#: Card fields that change without anything happening.
VOLATILE = frozenset({"elapsedS"})


def _without(card: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in card.items() if k not in keys}


class UnknownConversation(LookupError):
    pass


class Busy(RuntimeError):
    pass


class Full(RuntimeError):
    pass


def _now_iso() -> str:
    stamp = datetime.datetime.now(tz=datetime.UTC)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


class EventLog:
    """Append-only events with sequence numbers, capped, waitable."""

    def __init__(self, cap: int = LOG_CAP) -> None:
        self._events: list[dict[str, Any]] = []
        self._cond = threading.Condition()
        self._next = 1
        self._cap = cap
        self.closed = False

    @property
    def next_seq(self) -> int:
        with self._cond:
            return self._next

    def append(
        self, kind: str, fields: dict[str, Any],
        decorate: Callable[[int], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._cond:
            seq = self._next
            self._next += 1
            event = {"seq": seq, "at": _now_iso(), "kind": kind, **fields}
            if decorate is not None:
                event.update(decorate(seq))
            self._events.append(event)
            if len(self._events) > self._cap:
                del self._events[: len(self._events) - self._cap]
            self._cond.notify_all()
            return event

    def since(self, after: int) -> list[dict[str, Any]]:
        with self._cond:
            return [e for e in self._events if e["seq"] > after]

    def wait(self, after: int, timeout: float) -> list[dict[str, Any]]:
        """Events after ``after``, waiting up to ``timeout`` for the first."""
        with self._cond:
            self._cond.wait_for(
                lambda: self.closed
                or (self._events and self._events[-1]["seq"] > after),
                timeout=timeout,
            )
            return [e for e in self._events if e["seq"] > after]

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()


class AgentSession(Protocol):
    def run_turn(self, turn_id: str, text: str, hint: dict | None) -> None: ...

    def close(self) -> None: ...


@dataclass
class Conversation:
    """One browser session's state. Written by the agent and the poller."""

    id: str
    locale: str
    audio: bool
    log: EventLog = field(default_factory=EventLog)
    created: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)
    busy: bool = False
    turns: int = 0
    current_session_id: str | None = None
    latest_status: dict[str, Any] | None = None
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The card the page shows: the newest ``card`` or ``focus`` event's id.
    focus: str | None = None
    highlights: dict[str, list[str]] = field(default_factory=dict)
    utterances: dict[str, str] = field(default_factory=dict)
    session: Any = None
    _host_note: str | None = None
    _notices: set[str] = field(default_factory=set)
    lock: threading.RLock = field(default_factory=threading.RLock)

    # -- what the agent writes --------------------------------------------------

    def _utterance(self, kind: str, text: str, fields: dict[str, Any]) -> dict:
        def decorate(seq: int) -> dict[str, Any]:
            uid = f"u_{seq}"
            self.utterances[uid] = text
            audio = (f"/api/conversations/{self.id}/speech/{uid}.mp3"
                     if self.audio else None)
            return {"utterance_id": uid, "audio": audio}

        with self.lock:
            return self.log.append(kind, {**fields, "text": text}, decorate)

    def say(self, text: str, *, origin: str, turn_id: str | None = None) -> dict:
        """Ada says ``text``: ``origin`` is ``tool``, ``model`` or ``host``."""
        return self._utterance("ada", text, {
            "turn_id": turn_id, "origin": origin,
            "expects_reply": text.rstrip().endswith("?"),
        })

    def progress(self, text: str, *, state: str, session_id: str) -> dict:
        return self._utterance("progress", text,
                               {"state": state, "session_id": session_id})

    def error(self, text: str, *, turn_id: str | None = None) -> dict:
        return self._utterance("error", text, {"turn_id": turn_id})

    def notice(self, text: str, *, level: str = "info", once: str | None = None):
        with self.lock:
            if once is not None:
                if once in self._notices:
                    return None
                self._notices.add(once)
            return self.log.append("notice", {"level": level, "text": text})

    def status(self, state: str, turn_id: str | None = None) -> dict:
        return self.log.append("agent_status", {"turn_id": turn_id, "state": state})

    def trace(self, step: str, **fields: Any) -> dict:
        return self.log.append("trace", {"step": step, **fields})

    def upsert_card(self, card: dict[str, Any] | None) -> dict | None:
        """Log ``card`` when it differs from the one with its id.

        ``elapsedS`` alone does not count as a change: it moves on every poll,
        and a card event per poll would say nothing new.
        """
        if not card:
            return None
        with self.lock:
            before = self.cards.get(card["id"])
            if before is not None and _without(before, VOLATILE) == _without(
                    card, VOLATILE):
                return None
            self.cards[card["id"]] = card
            self.focus = card["id"]
            return self.log.append("card", {"card": card})

    def focus_card(self, card_id: str) -> dict | None:
        """Bring the card ``card_id`` back to the screen.

        :meth:`upsert_card` logs nothing for an unchanged card, which is right
        for a host poll (it says nothing new) and wrong for something the
        person asked: "back to the board" answers with the same board card, and
        without an event the screen would stay on the finding it came from.
        """
        with self.lock:
            if card_id not in self.cards:
                return None
            self.focus = card_id
            return self.log.append("focus", {"card_id": card_id})

    def set_host_note(self, note: str | None) -> None:
        with self.lock:
            self._host_note = note

    def take_host_note(self) -> str | None:
        with self.lock:
            note, self._host_note = self._host_note, None
            return note

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"conversation_id": self.id, "busy": self.busy,
                    "current_session_id": self.current_session_id,
                    "next_seq": self.log.next_seq, "cards": dict(self.cards),
                    "focus": self.focus}


class AgentFactory(Protocol):
    def open(self, conversation: Conversation) -> AgentSession: ...


class ConversationHost:
    """The page-facing seam: create, resume and talk to conversations."""

    def __init__(
        self,
        factory: AgentFactory,
        *,
        audio: bool,
        scripted: bool,
        max_conversations: int = MAX_CONVERSATIONS,
        idle_evict_s: float = IDLE_EVICT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.factory = factory
        self.audio = audio
        self.scripted = scripted
        self.max_conversations = max_conversations
        self.idle_evict_s = idle_evict_s
        self._clock = clock
        self._lock = threading.Lock()
        self._conversations: dict[str, Conversation] = {}
        self._threads: set[threading.Thread] = set()

    def _evict_locked(self) -> None:
        now = self._clock()
        for cid, conv in list(self._conversations.items()):
            if not conv.busy and now - conv.last_active > self.idle_evict_s:
                self._drop_locked(cid)
        if len(self._conversations) >= self.max_conversations:
            idle = sorted((c for c in self._conversations.values() if not c.busy),
                          key=lambda c: c.last_active)
            if not idle:
                raise Full("every conversation is busy")
            self._drop_locked(idle[0].id)

    def _drop_locked(self, cid: str) -> None:
        conv = self._conversations.pop(cid, None)
        if conv is None:
            return
        conv.log.close()
        if conv.session is not None:
            with contextlib.suppress(Exception):  # dropping must not fail
                conv.session.close()

    def create(self, locale: str = "en-US") -> Conversation:
        with self._lock:
            self._evict_locked()
            cid = "conv_" + secrets.token_hex(8)
            conv = Conversation(id=cid, locale=locale, audio=self.audio)
            conv.last_active = self._clock()
            self._conversations[cid] = conv
        conv.session = self.factory.open(conv)
        conv.say(SCRIPTED_GREETING if self.scripted else GREETING, origin="host")
        return conv

    def get(self, cid: str) -> Conversation:
        with self._lock:
            conv = self._conversations.get(cid)
        if conv is None:
            raise UnknownConversation(cid)
        return conv

    def turn(self, cid: str, text: str, source: str,
             hint: dict[str, Any] | None = None) -> dict[str, Any]:
        conv = self.get(cid)
        with conv.lock:
            if conv.busy:
                raise Busy(BUSY_SPEECH)
            conv.busy = True
            conv.turns += 1
            turn_id = f"t_{conv.turns}"
            conv.last_active = self._clock()
            user = conv.log.append("user", {"turn_id": turn_id, "text": text,
                                            "source": source})
            conv.status("thinking", turn_id)
        thread = threading.Thread(target=self._run, args=(conv, turn_id, text, hint),
                                  name=f"alexa-sim-{cid}-{turn_id}", daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return {"turn_id": turn_id, "seq": user["seq"]}

    def _run(self, conv: Conversation, turn_id: str, text: str,
             hint: dict[str, Any] | None) -> None:
        try:
            conv.session.run_turn(turn_id, text, hint)
        except Exception as exc:  # noqa: BLE001 -- a turn never raises to nobody
            conv.error("Sorry, something went wrong on my side. Please try again "
                       "in a moment.", turn_id=turn_id)
            conv.trace("turn", turn_id=turn_id, error=type(exc).__name__)
        finally:
            with conv.lock:
                conv.busy = False
                conv.last_active = self._clock()
            conv.status("idle", turn_id)
            with self._lock:
                self._threads.discard(threading.current_thread())

    def join(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in threads)

    def close(self) -> None:
        with self._lock:
            for cid in list(self._conversations):
                self._drop_locked(cid)

    def __len__(self) -> int:
        with self._lock:
            return len(self._conversations)
