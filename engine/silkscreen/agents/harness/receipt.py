"""The receipt: what the run may claim, and the evidence for each claim.

Prose is not filtered -- the review of 2026-09-15 showed that deleting
unproven *sentences* removes the honest ones ("ERC was not run") and keeps
the confident ones. Instead the final output is structured, the way
``audit/judgment.py::_parse_findings`` fixes provenance in the constructor
so nothing a model returns can enter as proven: a :class:`Summary` is a list
of :class:`Claim`, each naming the verifier that proves it, plus ``notes``
that render verbatim under "not verified". :func:`summary_guardrail` is the
output tripwire: a claim whose verifier did not return ``ok`` in this run,
or whose evidence id was never issued, fails the whole output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ...verify import Verdict
from ..model import parse_json
from .guardrails import GuardrailFunctionOutput, OutputGuardrail

__all__ = [
    "Claim",
    "Summary",
    "Receipt",
    "parse_summary",
    "unproven",
    "summary_guardrail",
]


@dataclass(frozen=True)
class Claim:
    text: str
    verifier: str
    evidence_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "verifier": self.verifier,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True)
class Summary:
    claims: tuple[Claim, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"claims": [c.as_dict() for c in self.claims], "notes": list(self.notes)}


@dataclass
class Receipt:
    """Verdicts by verifier, and the evidence ids this run issued."""

    verdicts: dict[str, Verdict] = field(default_factory=dict)
    evidence_ids: set[str] = field(default_factory=set)
    #: Tool calls, retries and rounds the run spent, by name.
    spent: dict[str, int] = field(default_factory=dict)

    def record(self, verdict: Verdict, evidence_id: str) -> None:
        self.verdicts[verdict.verifier] = verdict
        self.evidence_ids.add(evidence_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdicts": {k: v.as_dict() for k, v in self.verdicts.items()},
            "evidence_ids": sorted(self.evidence_ids),
            "spent": dict(self.spent),
        }


def parse_summary(raw: Any) -> Summary:
    """A :class:`Summary` from model output; every shape failure is a ValueError."""
    data = parse_json(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("summary must be a JSON object with 'claims' and 'notes'")
    claims_raw = data.get("claims", [])
    notes_raw = data.get("notes", [])
    problems: list[str] = []
    claims: list[Claim] = []
    if not isinstance(claims_raw, list):
        problems.append("'claims' must be a list")
        claims_raw = []
    for i, c in enumerate(claims_raw):
        if (
            not isinstance(c, dict)
            or not isinstance(c.get("text"), str)
            or not isinstance(c.get("verifier"), str)
        ):
            problems.append(f"claims[{i}] needs string 'text' and 'verifier'")
            continue
        ev = c.get("evidence_id")
        claims.append(Claim(c["text"], c["verifier"], None if ev is None else str(ev)))
    if not isinstance(notes_raw, list) or not all(
        isinstance(n, str) for n in notes_raw
    ):
        problems.append("'notes' must be a list of strings")
        notes_raw = []
    if problems:
        raise ValueError("; ".join(problems))
    return Summary(tuple(claims), tuple(notes_raw))


def unproven(summary: Summary, receipt: Receipt) -> list[str]:
    """Each claim the receipt does not back, in words."""
    out: list[str] = []
    for claim in summary.claims:
        verdict = receipt.verdicts.get(claim.verifier)
        if verdict is None:
            out.append(f"{claim.text!r} cites {claim.verifier!r}, which did not run")
        elif not verdict.ok:
            out.append(
                f"{claim.text!r} cites {claim.verifier!r}, which is {verdict.status}"
            )
        elif (
            claim.evidence_id is not None
            and claim.evidence_id not in receipt.evidence_ids
        ):
            out.append(
                f"{claim.text!r} cites evidence {claim.evidence_id!r} "
                f"this run never issued"
            )
    return out


def summary_guardrail(receipt: Receipt) -> OutputGuardrail:
    def check(output: Any, context: dict[str, Any]) -> GuardrailFunctionOutput:
        del context
        try:
            summary = output if isinstance(output, Summary) else parse_summary(output)
        except ValueError as exc:
            return GuardrailFunctionOutput(True, f"summary is not well-formed: {exc}")
        bad = unproven(summary, receipt)
        return GuardrailFunctionOutput(bool(bad), bad or json.dumps(summary.as_dict()))

    return OutputGuardrail("summary_is_proven", check)
