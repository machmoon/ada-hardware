"""Propose a mechanism (a robot arm), build it, and let the kernel judge it.

The :mod:`silkscreen.agents.enclosure` loop applied to joints: the model
answers MECHANISM-SPEC v1 JSON; :func:`~silkscreen.mechanism.ir.parse_mechanism_spec`
batches every validation failure; :func:`~silkscreen.mechanism.cad.build_mechanism`
builds real B-rep (a link too short for its housings is a
:class:`~silkscreen.mechanism.errors.MechanismBuildError` naming
``LINK_TOO_SHORT``); :func:`~silkscreen.mechanism.kernel.verify_mechanism`
measures every clause. Validation failures, build failures and failing
clauses all go back in **one** repair prompt, **one** repair round by
default; after that the last model that built ships with its report (the
honest receipt: a failing torque clause rides it in words), and a spec that
never built raises :class:`MechanismProposalError`.

The prompt carries what the model needs to choose well and nothing it could
transcribe wrongly: the actuator catalogue with *derated* torque and mass,
the bearing catalogue, the minimum link length for each housing pair (computed
by :func:`~silkscreen.mechanism.layout.min_link_table`, so the prompt and the
builder cannot disagree), and -- optionally -- facts about open-source arms
from the prior-art agent, rendered loosely because that contract belongs to a
different lane.

A :class:`~silkscreen.agents.model.ModelError` is deliberately not wrapped
(the :func:`~silkscreen.agents.propose.propose_circuit` convention).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, NamedTuple

from ..enclosure.cad import KERNEL_MISSING, kernel_available
from ..enclosure.errors import KernelUnavailable
from ..mechanism import rules
from ..mechanism.errors import MechanismBuildError, MechanismError, MechanismValidationError
from ..mechanism.ir import MECHANISM_SPEC_VERSION, MechanismSpec, parse_mechanism_spec
from ..mechanism.layout import min_link_table
from .model import Model

__all__ = [
    "MECHANISM_MARKER",
    "MECHANISM_PROMPT",
    "MechanismProposal",
    "MechanismProposalError",
    "prior_art_block",
    "propose_mechanism",
]

MECHANISM_MARKER = MECHANISM_SPEC_VERSION
_PRIOR_ART_MAX_CHARS = 3000

MECHANISM_PROMPT = f"""\
You are the mechanical engineer designing a 3D-printed serial robot arm
({MECHANISM_MARKER}). Respond with ONE JSON object -- no prose, no code fence.

{{
  "name": "<short name>",
  "base": {{"type": "bolt_down" | "freestanding", "footprint_mm": <optional number>}},
  "joints": [
    {{"id": "<identifier, e.g. shoulder_lift>",
     "axis": "yaw" | "pitch" | "roll",
     "range_deg": [<min>, <max>],
     "actuator": "<catalogue name>",
     "bearing": "none" | "<catalogue name>",
     "link_mm": <distance from this joint to the next joint, or to the tool flange>}}
  ],
  "payload_g": <mass the arm must hold at the tool>,
  "reach_mm": <horizontal reach the arm must achieve>,
  "tool": {{"length_mm": <flange to payload>, "mass_g": <gripper mass>}},
  "material": "PLA" | "PETG" | "ABS",
  "mass_budget_g": <optional>
}}

Frames: the home pose is every joint at 0 with the arm pointing straight up.
"yaw"/"roll" turn about the link's own long axis; "pitch" is a hinge across it.

Hard rules -- a proposal breaking any is rejected automatically:
1. The first joint is "yaw" (a turntable on the base). Every range includes 0.
2. "actuator" and "bearing" come from the catalogues below; you never state a
   dimension of a servo, bearing, bracket or wall -- the builder looks them up.
3. A "bearing" is only allowed on a "pitch" joint (it sits in the idler cheek).
4. Every link_mm must be at least the minimum listed for its pair of housings.
5. The kernel checks, with signed margins: every joint's gravity torque with the
   arm stretched horizontal (payload + tool + every link, servo and bearing
   beyond it) against the derated torque below; reach >= reach_mm; no parts
   colliding at home, outstretched and every range limit (a hinge folds past
   about 100 deg before its bridge hits the housing); walls, overhangs,
   pockets and bearing seats. Put strong servos near the base, light ones at
   the wrist, and keep links no longer than the reach needs -- torque grows
   with every millimetre.
"""


class MechanismProposalError(MechanismError):
    """No spec validated and built within the repair budget."""

    def __init__(self, message: str, attempts: int):
        self.attempts = attempts
        super().__init__(message)


class MechanismProposal(NamedTuple):
    spec: MechanismSpec
    repair_rounds: int
    model: Any = None        # silkscreen.mechanism.cad.MechanismModel
    kernel: Any = None       # silkscreen.mechanism.kernel.MechanismReport
    brief: str = ""


def _catalogue_block() -> str:
    lines = ["Actuator catalogue (usable torque is stall x "
             f"{rules.STALL_DERATING_PPM / 1e6:.2f}):"]
    for a in rules.ACTUATORS.values():
        usable = a.stall_unmm * rules.STALL_DERATING_PPM / 1e12
        lines.append(
            f"  {a.name}: usable {usable:.0f} N-mm ({a.stall_note} stall), "
            f"{a.mass_mg / 1000:.0f} g, body {a.body_l_nm / 1e6:.1f} x "
            f"{a.body_w_nm / 1e6:.1f} x {a.body_h_nm / 1e6:.1f} mm"
        )
    lines.append("Bearing catalogue (deep-groove, pressed into the idler cheek):")
    for b in rules.BEARINGS.values():
        lines.append(f"  {b.name}: {b.bore_nm / 1e6:g}x{b.od_nm / 1e6:g}x"
                     f"{b.width_nm / 1e6:g} mm, {b.mass_mg / 1000:g} g")
    lines.append("Minimum link_mm by (this joint kind + actuator -> next joint kind + "
                 "actuator); the last link only needs its coupling plus a "
                 f"{(rules.FLANGE_INSERT.length_nm + rules.INSERT_BORE_EXTRA_NM + rules.MIN_WALL_NM) / 1e6:.1f}"
                 " mm tool flange (the builder names the minimum if it is short):")
    table = min_link_table()
    for (kind, a, nkind, b), v in sorted(table.items()):
        if a == "STS3215_12V" or b == "STS3215_12V":
            continue  # same body as STS3215
        lines.append(f"  {kind} {a} -> {nkind} {b}: {v / 1e6:.0f} mm")
    lines.append("(STS3215_12V has the STS3215 body, so the STS3215 rows apply.)")
    return "\n".join(lines)


def prior_art_block(prior_art: Any) -> str:
    """Open-source arms, loosely: whatever ``{"projects": [...]}`` carries,
    bounded. Returns "" for nothing usable -- no invented precedent."""
    if not prior_art:
        return ""
    projects = prior_art.get("projects") if isinstance(prior_art, dict) else None
    if not isinstance(projects, list) or not projects:
        return ""
    lines = ["Open-source arms that already solve similar requests (facts read "
             "from their repositories; use them as precedent, not as numbers "
             "to copy into fields the catalogue owns):"]
    for p in projects[:5]:
        if not isinstance(p, dict):
            continue
        keep = {k: v for k, v in p.items()
                if isinstance(v, (str, int, float, list)) and k != "snippets"}
        lines.append("  " + json.dumps(keep, default=str)[:600])
    text = "\n".join(lines)
    return text[:_PRIOR_ART_MAX_CHARS]


def propose_mechanism(
    model: Model,
    intent: str,
    *,
    prior_art: Any = None,
    max_repairs: int = 1,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> MechanismProposal:
    """Ask for a MECHANISM-SPEC, build it, verify it, repair once.

    Events: ``mechanism.round`` per rejected answer, ``mechanism.kernel`` per
    verification. Raises :class:`KernelUnavailable` before any model call when
    build123d is missing, :class:`MechanismProposalError` when nothing built,
    and lets :class:`ModelError` through unwrapped.
    """
    if not kernel_available():
        raise KernelUnavailable(KERNEL_MISSING)
    from ..mechanism.cad import build_mechanism
    from ..mechanism.kernel import verify_mechanism

    art = prior_art_block(prior_art)
    brief = f"Request:\n{intent.strip()}\n\n{_catalogue_block()}"
    if art:
        brief += f"\n\n{art}"
    prompt = f"{MECHANISM_PROMPT}\n{brief}\n"

    last_errors: list[str] = []
    shipped: MechanismProposal | None = None
    for round_no in range(max_repairs + 1):
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=4096)
        errors: list[str] = []
        try:
            spec = parse_mechanism_spec(raw)
        except MechanismValidationError as exc:
            errors = list(exc.errors)
        else:
            try:
                built = build_mechanism(spec)
            except MechanismBuildError as exc:
                errors = [str(exc)]
            else:
                report = verify_mechanism(built)
                shipped = MechanismProposal(spec, round_no, built, report, brief)
                if on_event is not None:
                    on_event({"event": "mechanism.kernel", "round": round_no + 1,
                              "passed": report.passed, "failed": report.failed})
                if report.passed:
                    return shipped
                errors = [ln for ln in report.text().splitlines() if ln.startswith("FAIL")]
        last_errors = errors
        if on_event is not None:
            on_event({"event": "mechanism.round", "round": round_no + 1,
                      "errors": len(errors), "first_error": errors[0][:160] if errors else ""})
        if round_no == max_repairs:
            break
        problems = "\n".join(f"  - {e}" for e in errors)
        prompt = (
            f"{MECHANISM_PROMPT}\n{brief}\n\nYour previous proposal was rejected. Fix ALL "
            f"of these and return the corrected JSON object:\n{problems}\n\n"
            f"Your previous proposal was:\n{raw}\n"
        )
    if shipped is not None:
        return shipped
    detail = "\n".join(f"  - {e}" for e in last_errors)
    raise MechanismProposalError(
        f"No buildable mechanism after {max_repairs + 1} attempts. Final errors:\n{detail}",
        attempts=max_repairs + 1,
    )
