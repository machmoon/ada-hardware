"""Deterministic verifiers: what the model may not claim without a check.

Every verifier here answers a :class:`~silkscreen.verify.verdict.Verdict`:
clauses with pass/fail and a reason, an ``ok`` that is the conjunction of the
blocking clauses, and an ``unverified`` state that is neither -- "could not
check" is never a green. The model never runs one; the engine does, and the
agents layer feeds red clauses back as repair items or refuses to finish.

Three verifiers ship:

* :func:`electrical_completeness` -- the ground and supply rules a hardware
  engineer applies before reading anything else (a pin that should carry
  power is wired or declared open, one ground, no rail shorted to ground,
  each IC decoupled). Rules follow tscircuit's ``checks`` package
  (``lib/check-no-ground-pin-defined.ts``, ``lib/check-pin-must-be-connected.ts``,
  ``lib/check-same-name-nets-are-connected.ts``) and azonenberg's
  ``pcb-checklist/schematic-checklist.md``.
* :func:`erc`, :func:`drc`, :func:`parity` -- KiCad's own checks, run through
  ``kicad-cli`` the way atopile's ``src/faebryk/libs/kicad/drc.py::run_drc``
  does (JSON report in a temporary directory, ``--severity-all``), classified
  by KiCad's own ``severity`` field against a pinned list of benign types.

:mod:`.ingest` turns everything else into the same shape: compiler output
(GNU text or SARIF), reviewdog's rdjsonl, JSON-lines and CloudWatch logs,
SPICE clauses and the enclosure and mechanism kernels.

The engine stays model-free; this package imports nothing from ``agents``.
"""

from __future__ import annotations

from . import ingest
from .circuit import electrical_completeness
from .kicad import drc, erc, erc_from_spec, kicad_cli_path, parity
from .verdict import Clause, Verdict

__all__ = [
    "Clause",
    "Verdict",
    "electrical_completeness",
    "erc",
    "erc_from_spec",
    "drc",
    "parity",
    "kicad_cli_path",
    "ingest",
]
