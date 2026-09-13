"""Shared value types for the Hardy Google Meet bot.

Kept tiny on purpose: every module in ``meetbot`` imports these, so they carry
no behaviour and no dependency beyond the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass

# JoinReceipt.state vocabulary. A join always ends in exactly one of these,
# said in words, never a silent success.
ADMITTED = "admitted"
WAITING_FOR_HOST = "waiting_for_host"
REFUSED = "refused"
SIGNED_OUT = "signed_out"
JOIN_STATES = frozenset({ADMITTED, WAITING_FOR_HOST, REFUSED, SIGNED_OUT})


@dataclass(frozen=True)
class Utterance:
    """One captioned stretch of speech, with the speaker as Meet names them."""

    speaker: str
    text: str
    t_start: float
    t_end: float
    is_self: bool = False


@dataclass(frozen=True)
class JoinReceipt:
    """How a join attempt ended: one of ``JOIN_STATES`` plus why, in words."""

    state: str
    detail: str

    @property
    def admitted(self) -> bool:
        return self.state == ADMITTED


@dataclass(frozen=True)
class SpokenReceipt:
    """Whether audio actually left for the meeting, and how long it ran."""

    spoken: bool
    detail: str
    seconds: float = 0.0
