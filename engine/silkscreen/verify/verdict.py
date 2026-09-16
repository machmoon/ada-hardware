"""The one verdict type every verifier answers with.

Modelled on the two kernels already in the tree -- ``enclosure/kernel.py``'s
``Clause(name, passed, margin_nm, detail)`` and ``agents/simulate.py``'s
``ClauseVerdict`` with its signed ``margin`` -- so a SPICE or case verdict
passes through unchanged rather than being flattened into strings. Three
states, three representations (the ``specreview.py`` rule):

* ``status == "ok"``: every blocking clause passed;
* ``status == "blocked"``: at least one blocking clause failed, and it says
  which and why;
* ``status == "unverified"``: the verifier could not run (no ``kicad-cli``,
  no library index) and ``unverified_reason`` names the fix. Never ``ok``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = ["Clause", "Verdict", "Severity"]

Severity = Literal["blocker", "warning"]


@dataclass(frozen=True)
class Clause:
    """One thing checked, and how it went."""

    name: str
    passed: bool
    detail: str
    #: ``blocker`` fails the verdict; ``warning`` is reported and carried.
    severity: Severity = "blocker"
    #: A signed margin where the check measures something (positive is
    #: inside the limit), ``None`` for a structural check.
    margin: float | None = None
    #: The parts and nets this clause is about, so a repair prompt and a
    #: receipt can name them without parsing ``detail``.
    refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "severity": self.severity,
            "margin": self.margin,
            "refs": list(self.refs),
        }


@dataclass(frozen=True)
class Verdict:
    verifier: str
    clauses: tuple[Clause, ...] = ()
    #: What the verifier looked at, in words: which nets it classed as
    #: ground, which pins it read from the library. A wrong classification
    #: must be visible in the receipt, not hidden behind ``ok``.
    evidence: dict[str, Any] = field(default_factory=dict)
    unverified_reason: str | None = None

    @property
    def status(self) -> Literal["ok", "blocked", "unverified"]:
        if self.unverified_reason is not None:
            return "unverified"
        return "ok" if all(c.passed for c in self.blockers) else "blocked"

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def blockers(self) -> tuple[Clause, ...]:
        return tuple(c for c in self.clauses if c.severity == "blocker")

    @property
    def failures(self) -> tuple[Clause, ...]:
        """Every failed clause, blockers first."""
        failed = [c for c in self.clauses if not c.passed]
        return tuple(sorted(failed, key=lambda c: c.severity != "blocker"))

    def repair_items(self) -> list[str]:
        """Failed blocking clauses as one-line repair items for a model."""
        return [f"{c.name}: {c.detail}" for c in self.blockers if not c.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "verifier": self.verifier,
            "status": self.status,
            "clauses": [c.as_dict() for c in self.clauses],
            "evidence": self.evidence,
            "unverified_reason": self.unverified_reason,
        }

    @classmethod
    def unverified(cls, verifier: str, reason: str) -> Verdict:
        return cls(verifier=verifier, unverified_reason=reason)
