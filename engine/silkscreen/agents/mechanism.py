"""Propose a mechanism (a robot arm), build it, and let the kernel judge it.

The :mod:`silkscreen.agents.enclosure` loop applied to joints: the model
answers MECHANISM-SPEC v1 JSON;
:func:`~silkscreen.mechanism.ir.parse_mechanism_spec` batches every
validation failure; :func:`~silkscreen.mechanism.cad.build_mechanism` builds
real B-rep (a link too short for its housings is a
:class:`~silkscreen.mechanism.errors.MechanismBuildError` naming
``LINK_TOO_SHORT``); :func:`~silkscreen.mechanism.kernel.verify_mechanism`
measures every clause. Validation failures, build failures and failing
clauses all go back in **one** repair prompt, **one** repair round by
default; after that the last model that built ships with its report (the
honest receipt: a failing torque clause rides it in words), and a spec that
never built raises :class:`MechanismProposalError`.

The prompt carries what the model needs to choose well and nothing it could
transcribe wrongly: the actuator catalogue with *derated* torque, mass and
control interface, the bearing catalogue, the minimum link length for each
housing pair (computed by :func:`~silkscreen.mechanism.layout.min_link_table`,
so the prompt and the builder cannot disagree), the controller board when
one was designed first (so an arm on a PCA9685 gets PWM servos), and --
optionally -- the cited prior art.

A :class:`~silkscreen.agents.model.ModelError` is deliberately not wrapped
(the :func:`~silkscreen.agents.propose.propose_circuit` convention).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from ..enclosure.cad import KERNEL_MISSING, kernel_available
from ..enclosure.errors import KernelUnavailable
from ..mechanism import rules
from ..mechanism.errors import (
    MechanismBuildError,
    MechanismError,
    MechanismValidationError,
)
from ..mechanism.ir import MECHANISM_SPEC_VERSION, MechanismSpec, parse_mechanism_spec
from ..mechanism.layout import min_link_table
from .model import Model

__all__ = [
    "MECHANISM_MARKER",
    "MECHANISM_PROMPT",
    "MECHANISM_STATUSES",
    "MechanismProposal",
    "MechanismProposalError",
    "MechanismResult",
    "electronics_block",
    "interface_warnings",
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
6. When a controller board is named below, every actuator must use the control
   interface its servo driver makes.
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
            f"{a.body_w_nm / 1e6:.1f} x {a.body_h_nm / 1e6:.1f} mm, "
            f"{a.interface} control"
        )
    lines.append("Bearing catalogue (deep-groove, pressed into the idler cheek):")
    for b in rules.BEARINGS.values():
        lines.append(f"  {b.name}: {b.bore_nm / 1e6:g}x{b.od_nm / 1e6:g}x"
                     f"{b.width_nm / 1e6:g} mm, {b.mass_mg / 1000:g} g")
    flange_mm = (
        rules.FLANGE_INSERT.length_nm + rules.INSERT_BORE_EXTRA_NM + rules.MIN_WALL_NM
    ) / 1e6
    lines.append("Minimum link_mm by (this joint kind + actuator -> next joint kind + "
                 "actuator); the last link only needs its coupling plus a "
                 f"{flange_mm:.1f} mm tool flange (the builder names the minimum "
                 "if it is short):")
    table = min_link_table()
    for (kind, a, nkind, b), v in sorted(table.items()):
        if a == "STS3215_12V" or b == "STS3215_12V":
            continue  # same body as STS3215
        lines.append(f"  {kind} {a} -> {nkind} {b}: {v / 1e6:.0f} mm")
    lines.append("(STS3215_12V has the STS3215 body, so the STS3215 rows apply.)")
    return "\n".join(lines)


def prior_art_block(prior_art: Any) -> str:
    """Open-source arms, bounded. Returns "" for nothing usable -- no
    invented precedent.

    Takes a :class:`~silkscreen.prior_art.PriorArtResult` -- its own
    ``brief_text``, which carries only cited facts, so this lane and the
    board's propose stage read the same text -- or, loosely, any
    ``{"projects": [...]}`` dict.
    """
    if not prior_art:
        return ""
    brief_text = getattr(prior_art, "brief_text", None)
    if callable(brief_text):
        return (brief_text() or "")[:_PRIOR_ART_MAX_CHARS]
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


#: Servo drivers the board generator draws by part number, and the actuator
#: interface and channel count each one has. A PCA9685 is a 16-channel PWM
#: generator (adafruit/Adafruit-PWM-Servo-Driver-Library ``README.md``:
#: "16-channel PWM & Servo driver"), so it can move an SG90 or an MG996R and
#: cannot move an STS3215 bus servo at all.
_DRIVER_INTERFACES: dict[str, tuple[str, int]] = {
    "pca9685": ("pwm", 16),
}


def _board_drivers(board_spec: Any) -> list[tuple[str, str, int]]:
    """``(device name, interface, channels)`` for every recognised driver."""
    out: list[tuple[str, str, int]] = []
    for device in getattr(board_spec, "devices", ()) or ():
        key = "".join(c if c.isalnum() else "_" for c in str(device.name).lower())
        for stem, (interface, channels) in _DRIVER_INTERFACES.items():
            if stem in key:
                out.append((str(device.name), interface, channels))
    return out


def electronics_block(board_spec: Any) -> str:
    """The controller board this arm will be wired to, for the prompt.

    "" when there is no board (the stage can run from the intent alone).
    Names the servo driver's interface so the model picks actuators that
    board can command.
    """
    if board_spec is None:
        return ""
    names = [str(d.name) for d in getattr(board_spec, "devices", ()) or ()]
    if not names:
        return ""
    lines = ["The controller board already designed for this arm carries: "
             + ", ".join(names[:20]) + "."]
    drivers = _board_drivers(board_spec)
    for name, interface, channels in drivers:
        lines.append(
            f"{name} is a {channels}-channel {interface} servo driver: every "
            f"actuator you choose must be {interface} control, and the arm may "
            f"have at most {channels} joints."
        )
    if not drivers:
        lines.append("No servo driver on it is one this catalogue recognises, so "
                     "choose actuators by torque; the pairing will be reported.")
    return "\n".join(lines)


def interface_warnings(spec: MechanismSpec, board_spec: Any) -> list[str]:
    """Where the arm and the board disagree, in words.

    Empty when they agree or when there is no board to check against; a
    board with no recognised driver says the check was not made rather than
    passing it.
    """
    if board_spec is None:
        return []
    drivers = _board_drivers(board_spec)
    if not drivers:
        return [
            "the controller board has no servo driver this check recognises "
            f"(known: {sorted(_DRIVER_INTERFACES)}), so whether it can command "
            "the arm's actuators was not checked"
        ]
    return _mismatches(spec, drivers)


def _mismatches(spec: MechanismSpec, drivers: list[tuple[str, str, int]]) -> list[str]:
    out: list[str] = []
    interfaces = {interface for _, interface, _ in drivers}
    channels = sum(c for _, _, c in drivers)
    names = ", ".join(n for n, _, _ in drivers)
    for joint in spec.joints:
        act = rules.ACTUATORS[joint.actuator]
        if act.interface not in interfaces:
            out.append(
                f"joint {joint.id!r} uses {act.name}, a {act.interface} servo, but "
                f"the board's driver ({names}) only makes "
                f"{'/'.join(sorted(interfaces))}: the board cannot move it"
            )
    if len(spec.joints) > channels:
        out.append(
            f"the arm has {len(spec.joints)} joints and the board's driver has "
            f"{channels} channels"
        )
    return out


def _board_rejections(spec: MechanismSpec, board_spec: Any) -> list[str]:
    """The mismatches that reject a proposal (not the "not checked" note)."""
    drivers = _board_drivers(board_spec) if board_spec is not None else []
    return _mismatches(spec, drivers) if drivers else []


#: What a mechanism run ended as. ``passed`` -- built and every kernel clause
#: passed; ``kernel_failed`` -- built and exported, at least one clause failed
#: (named on ``kernel``); ``failed`` -- nothing built within the repair
#: budget, or the export failed; ``unavailable`` -- the ``cad`` extra is not
#: installed, decided before any model call.
MECHANISM_STATUSES: tuple[str, ...] = (
    "passed", "kernel_failed", "failed", "unavailable",
)


@dataclass
class MechanismResult:
    """The mechanism stage's receipt.

    Never ``None`` when the stage was asked for: every way it can end is a
    :data:`MECHANISM_STATUSES` word plus a sentence in ``detail`` (the
    ``agents/simulate.py`` rule -- a run that built nothing must not look
    like a run that was never asked).
    """

    status: str
    detail: str
    spec: MechanismSpec | None = None
    #: The STEP assembly at the home pose; "" when nothing was built.
    step_text: str = ""
    repair_rounds: int = 0
    kernel: Any = None       # silkscreen.mechanism.kernel.MechanismReport
    exports: Any = None      # silkscreen.mechanism.cad.MechanismExports
    brief: str = ""
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in MECHANISM_STATUSES:
            raise ValueError(
                f"mechanism status {self.status!r} is not one of {MECHANISM_STATUSES}"
            )

    @property
    def built(self) -> bool:
        return self.spec is not None and bool(self.step_text)

    def note(self) -> str:
        """One line: what came out, or why nothing did."""
        if self.status == "passed" and self.spec is not None:
            return (f"Mechanism {self.spec.name!r}: {len(self.spec.joints)} joints, "
                    f"every kernel clause passed")
        if self.status == "kernel_failed":
            return f"Mechanism built but failed its kernel: {self.detail}"
        return f"Mechanism {self.status}: {self.detail}"

    def as_dict(self) -> dict[str, Any]:
        exports = self.exports
        return {
            "status": self.status,
            "detail": self.detail,
            "spec": None if self.spec is None else self.spec.as_dict(),
            "step": self.step_text or None,
            "repair_rounds": self.repair_rounds,
            "kernel": None if self.kernel is None else self.kernel.as_dict(),
            "exports": (
                None if exports is None
                else {"step": str(exports.step), "stls": [str(p) for p in exports.stls]}
            ),
            "warnings": list(self.warnings),
        }


def propose_mechanism(
    model: Model,
    intent: str,
    *,
    prior_art: Any = None,
    board_spec: Any = None,
    max_repairs: int = 1,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> MechanismProposal:
    """Ask for a MECHANISM-SPEC, build it, verify it, repair once.

    Events: ``mechanism.round`` per rejected answer, ``mechanism.kernel`` per
    verification. Raises :class:`KernelUnavailable` before any model call when
    build123d is missing, :class:`MechanismProposalError` when nothing built,
    and lets :class:`ModelError` through unwrapped.

    ``board_spec`` (a validated :class:`~silkscreen.netlist.CircuitSpec`)
    puts the controller board into the brief; an actuator that board's servo
    driver cannot command is a *rejection* the repair round sees, like a
    validation error, and is never built or shipped -- an arm its own board
    cannot move is not a design. A kernel failure, by contrast, ships with
    its report once the budget is spent (the enclosure's fast-mode rule).
    """
    if not kernel_available():
        raise KernelUnavailable(KERNEL_MISSING)
    from ..mechanism.cad import build_mechanism
    from ..mechanism.kernel import verify_mechanism

    art = prior_art_block(prior_art)
    brief = f"Request:\n{intent.strip()}\n\n{_catalogue_block()}"
    electronics = electronics_block(board_spec)
    if electronics:
        brief += f"\n\n{electronics}"
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
            # Checked before the build, and it skips the build: an arm its own
            # board cannot move is not a design worth kernel time, and it is
            # never shipped as one.
            board = _board_rejections(spec, board_spec)
            if board:
                errors = [f"BOARD {w}" for w in board]
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
                    errors = [ln for ln in report.text().splitlines()
                              if ln.startswith("FAIL")]
        last_errors = errors
        if on_event is not None:
            on_event({"event": "mechanism.round", "round": round_no + 1,
                      "errors": len(errors),
                      "first_error": errors[0][:160] if errors else ""})
        if round_no == max_repairs:
            break
        problems = "\n".join(f"  - {e}" for e in errors)
        prompt = (
            f"{MECHANISM_PROMPT}\n{brief}\n\nYour previous proposal was rejected. "
            f"Fix ALL of these and return the corrected JSON object:\n{problems}\n\n"
            f"Your previous proposal was:\n{raw}\n"
        )
    if shipped is not None:
        return shipped
    detail = "\n".join(f"  - {e}" for e in last_errors)
    raise MechanismProposalError(
        f"No buildable mechanism after {max_repairs + 1} attempts. "
        f"Final errors:\n{detail}",
        attempts=max_repairs + 1,
    )
