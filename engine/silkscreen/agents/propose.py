"""Propose a circuit, then make the model fix its own mistakes.

The model never gets to hand its output straight to the board builder. Its
proposal goes through :func:`silkscreen.netlist.parse_circuit_spec`, and every
validation error is fed back as a repair prompt. The loop is bounded and each
round is strictly informed by the last, so it converges or gives up loudly --
rather than the previous project's approach, which was to ``json.loads`` raw
model text inside a worker thread with no handler and let the thread die.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..board import package_errors, supported_packages_text, tie_package_pins
from ..netlist import CircuitSpec, ValidationError, parse_circuit_spec
from .datasheet import PartFacts
from .model import Model

__all__ = [
    "propose_circuit",
    "ProposalError",
    "ProposalAttempt",
    "PROPOSE_PROMPT",
    "part_number_errors",
]


class ProposalError(RuntimeError):
    """The model could not produce a valid circuit within the repair budget.

    It is the requester's problem to act on, not an internal fault, so it
    carries what a person needs to edit the request: :attr:`errors` (the final
    round's validation errors), :attr:`unsupported` (the subset that are parts
    the board builder has no land pattern for) and :attr:`supported_packages`
    (what it can draw, the same :func:`~silkscreen.board.supported_packages_text`
    the prompt showed the model). ``service/app.py::_error_response`` turns
    these into a named 422 rather than a 500 "internal error".
    """

    def __init__(
        self,
        message: str,
        attempts: list[ProposalAttempt],
        *,
        unsupported: list[str] | None = None,
    ):
        self.attempts = attempts
        last = attempts[-1] if attempts else None
        self.errors: list[str] = list(last.errors) if last else []
        self.unsupported: list[str] = list(unsupported or [])
        self.supported_packages: str = supported_packages_text()
        super().__init__(message)


#: The most repair rounds an escalation may reach, counting the level's own.
#: Balanced's budget: a ``fast`` run whose proposal is still failing *on parts
#: the builder cannot draw* gets up to two more rounds rather than dying with
#: one, because that is the failure a further round demonstrably fixes (the
#: repair prompt names every supported package) and the one a complex request
#: -- six servos, a PWM driver, a module and a buck -- hits first. Measured on
#: the 2026-09-13 robotic-arm demo: one repair round, ``ProposalError`` on an
#: invented ``arduino_nano`` connector.
ESCALATED_MAX_REPAIRS = 3

#: The three refusals ``netlist.py`` words as "this part is not one we have"
#: (``CircuitSpec`` validation: unsupported package, unknown kind, unsupported
#: passive type). Pinned by ``engine/tests/test_propose_escalation.py`` so a
#: rewording there cannot silently switch escalation off.
_UNSUPPORTED_PART = re.compile(
    r"has unsupported package|has unknown kind|has unsupported type"
)


@dataclass
class ProposalAttempt:
    round: int
    raw: str
    errors: list[str] = field(default_factory=list)
    accepted: bool = False


PROPOSE_PROMPT = """\
You are designing a printed circuit board. Produce a complete, buildable
circuit as ONE JSON object -- no prose, no code fence.

{
  "devices": {
    "<MANUFACTURER PART NUMBER>": {"pins": {"<pin name>": "<pin number>", ...},
                                   "no_connect": ["<pin name>", ...]},
    "<connector id>": {"kind": "connector", "package": "<package name>",
                       "pins": {"<pin name>": "<pad number>", ...}},
    "<battery id>":   {"kind": "battery", "package": "<package name>",
                       "pins": {"P": "1", "N": "2"}}
  },
  "passives": {
    "<descriptive id>": {"type": "capacitor|resistor|inductor|diode|crystal",
                         "value": "<e.g. 100nF>"}
  },
  "nets": {
    "<net name>": ["<part>.<pin>", "<part>.<pin>", ...]
  }
}

The first "devices" form is an IC: it carries "pins" and an optional
"no_connect" list. Only the "connector" and "battery" forms take a "package".

Hard rules -- a proposal breaking any of these is rejected automatically:

1. Every endpoint is "<part>.<pin>". For a device the pin is its NAME from the
   pins map. For a passive it is "1" or "2" -- passives have exactly two legs.
   A diode or LED is polarised: leg "1" is the ANODE and leg "2" the CATHODE,
   so forward current flows from 1 to 2 (the schematic symbol and the
   footprint's cathode bar both follow this).
2. Every passive must have BOTH legs on a net. A part connected on one leg is
   floating and will be rejected.
3. Every net needs at least two endpoints. A signal you name but wire to only
   one pin is not a connection -- take it to a connector pin, or drop it.
   For a device pin you are deliberately leaving unused, declare it in the
   pins map and LIST ITS NAME UNDER "no_connect". Do not invent a net with
   that one pin on it (a "NC", "UNUSED_OUT" or "CTRL" net with a single
   endpoint is the second most common rejection). A pin that is neither on a
   net nor under "no_connect" is reported as forgotten. A ground or supply
   pin is never unused: every GND/VSS pin goes on the ground net and every
   VCC/VDD/VIN pin on its rail, thermal pads included when the datasheet
   ties them. If a pin must be held at a level, put it on the real net that
   holds it -- GND or the rail -- which gives that net its second endpoint
   honestly.
4. A PIN JOINS EXACTLY ONE NET. Listing "<part>.<pin>" under two different net
   names is the most common rejection: it describes one physical node under
   two names, and every file downstream then disagrees about which name it
   has. If two nets are really the same node, merge them into one net with
   one name. If they are not, the pin belongs to only one of them.
5. Each pin NUMBER in a device's pins map names one physical pin, so two pin
   names must never share a number.
6. Only reference devices and passives you declared, and pin names you declared.
7. Use only the five passive types listed.
8. Name power nets conventionally: GND, +3V3, +5V, VIN.
9. The board builder can only lay out these packages: {packages}. An "ic" is
   judged by its highest pin number, so choose the variant of each part that
   comes in one of those (most parts offer a SOIC option) and number its pins
   to match. AN "ic" MUST NOT CARRY A "package" KEY -- this is the single most
   common rejection. When the request names a package for a chip ("an
   AMS1117-3.3 in SOT-223", "an NE555D in SOIC-8"), you satisfy it by giving
   the device the pin COUNT of that package and nothing else; writing
   "package": "SOT-223" on an ic is rejected, because a land pattern the
   builder derives from pin count and a name it would ignore are two
   different things and only one of them gets drawn. A "connector" or
   "battery" is the opposite case: it is chosen by NAME from the lists above,
   so it requires "package", and its pins are numbered with that package's
   real pad numbers.
10. THE BOARD MUST HAVE A POWER INPUT. Every design says where its energy
    comes from, as a real part: a "connector" (a barrel jack, a USB-C
    receptacle, a terminal block, a JST lead or a header) or a "battery".
    A board whose supply nets appear from nowhere cannot be built or powered.
    Do not model a connector as an "ic" -- an IC package has no plug and no
    wire entry, so a power input drawn that way is unbuildable.
11. AN "ic" IS KEYED BY ITS MANUFACTURER PART NUMBER, never by a reference
    designator. "U1", "IC1", "REG", "opamp" and "timer" are all rejected.
    Write "AMS1117-3.3", "NE555D", "LM358D", "TL072", "ATtiny85-20SU" -- the
    thing a person could order. The designator (U1, U2) is assigned later by
    the board itself, and the key you write here is what reaches the bill of
    materials, the distributor lookup, the 3D model, the simulation model and
    the schematic symbol. A board whose regulator is called "U1" loses all
    five and nobody is told. If the request names a part, use exactly that
    part number. If it does not, choose a specific, common, orderable part
    and name it. Connectors, batteries, switches and test points are the
    opposite case and are keyed however you like, because rule 9's "package"
    is what identifies them.

Design rules to follow:
- Give every IC supply pin its own decoupling capacitor, and say which supply
  pin each one bypasses in its id (e.g. c_dec_vdd_u1).
- Include bulk capacitance on each supply rail.
- Tie mode/boot pins that must not float to a defined level through a resistor.
- A pin the datasheet says to bypass rather than drive -- a 555's CTRL, a
  reference or compensation pin -- gets a small capacitor from that pin to
  GND. That is both the right circuit and a legal net: the pin and the
  capacitor leg give the net its two endpoints. Naming the pin as a net on
  its own instead is the rejection rule 3 describes.
- Prefer parts you were given datasheet facts for. If you must add a part with
  no datasheet, keep it to a common, unambiguous one.
- Bring every external signal to a connector rather than to a bare pad: a
  sensor, a display or a debug header is something a person has to plug into.
- A battery-powered design still needs its rail regulated or its cell voltage
  stated on the net name, so the rest of the board is designed against a
  known supply.
""".replace("{packages}", supported_packages_text())


# ==========================================================================
# The part-number rule
# ==========================================================================
#
# Measured 2026-09-08 with ``scripts/design_quality.py``: in 18 proposals out
# of 18, across nine prompts and two models, the model named its integrated
# circuits by *reference designator* -- "U1" -- rather than by part number,
# even when the request said "an AMS1117-3.3 in SOT-223". The IR accepted every
# one, because a device key is only ever required to be a unique string.
#
# The key is not a label. It is the part number everywhere downstream:
# ``sourcing.bom_rows`` puts it in the BOM and ``distributor.verify_mpns``
# looks it up; ``models3d.model_for`` matches it to a KiCad 3D model;
# ``spice/registry.ModelRegistry._builtin`` matches it against the op-amp,
# 555 and regulator families; ``schematic.py`` draws it on the symbol; and
# ``review.py``'s critic checks it against a datasheet. A board whose
# regulator is called "U1" silently loses all five, and the run still reports
# success -- exactly the class of bug ``kicad.py``'s identity rules exist to
# refuse rather than no-op.
#
# So it goes back to the model as one more repair item, in the same batch as
# ``board.package_errors``. That placement is the point: a rule the proposal
# stage can still fix belongs in the proposal stage's own loop, and after the
# proposal is accepted nobody can fix it.
#
# Prior art for putting a semantic rule (not a schema rule) into the same
# batch that drives the retry: instructor's
# ``instructor/v2/core/validators.py::Validator``, whose fields are
# ``is_valid`` / ``reason`` / ``fixed_value`` -- the error carries a suggested
# corrected value, not just a complaint -- and its reask path
# ``instructor/v2/providers/genai/handlers.py::reask_genai_structured_outputs``,
# which appends "Validation Error found:\n{exception}\nRecall the function
# correctly, fix the errors in the following attempt:\n{...}". LangChain's
# ``langchain_classic/output_parsers/prompts.py::NAIVE_FIX`` has the same three
# parts (instructions, completion, error) under delimiters. The loop below
# already had that shape; what it lacked was this clause, and the
# ``fixed_value`` half -- so each message here names what to write instead.

#: Reference designator prefixes an IC is given on a schematic. A part number
#: never looks like one of these followed by at most three digits: the closest
#: real collisions (``A4988``, ``ICL7660``, ``X9C103``) all carry either a
#: fourth digit or a letter after the prefix.
_DESIGNATOR_RE = re.compile(
    r"^(U|IC|Q|K|X|A|REF|REG|OA|AMP|VR)[\s_-]?\d{0,3}$", re.IGNORECASE
)

#: Category words. A model that cannot recall a part number reaches for one of
#: these; none of them is orderable.
_GENERIC_NAMES = frozenset(
    {
        "IC", "CHIP", "PART", "DEVICE", "MAIN", "MCU", "MICROCONTROLLER",
        "PROCESSOR", "REGULATOR", "LDO", "VREG", "OPAMP", "OPAMPS",
        "AMPLIFIER", "TIMER", "COMPARATOR", "DRIVER", "SENSOR", "MOSFET",
        "TRANSISTOR", "LOGIC", "GATE", "BUFFER", "CONVERTER", "MEMORY",
        "EEPROM", "FLASH", "DAC", "ADC", "PMIC", "SOC",
    }
)


#: Set to ``0`` to keep KiCad ERC out of the repair loop. On by default when
#: ``kicad-cli`` is installed; the root ``conftest.py`` turns it off for the
#: suite so a test's scripted circuit behaves the same with and without KiCad,
#: and the gated tests in ``test_verify.py`` turn it back on deliberately.
ERC_IN_LOOP_ENV = "SILKSCREEN_ERC_IN_LOOP"


def verifier_errors(spec: CircuitSpec, on_event=None) -> list[str]:
    """Repair items from the deterministic verifiers, and one event per verdict.

    ``electrical_completeness`` always runs (it is pure Python over the IR).
    ``erc`` runs when :data:`ERC_IN_LOOP_ENV` is not ``0`` and ``kicad-cli`` is
    found; without it the verdict is ``unverified`` and says so in the event,
    which is not an error -- "could not check" must not read as a failure any
    more than as a pass. Only blocking clauses become repair items; warnings
    ride the event for the receipt.
    """
    from ..verify import electrical_completeness, erc_from_spec

    verdicts = [electrical_completeness(spec)]
    if os.getenv(ERC_IN_LOOP_ENV, "1").strip() != "0":
        verdicts.append(erc_from_spec(spec))
    items: list[str] = []
    for verdict in verdicts:
        items.extend(verdict.repair_items())
        if on_event is not None:
            on_event(
                {
                    "event": "propose.verdict",
                    "verifier": verdict.verifier,
                    "status": verdict.status,
                    "failed": len(verdict.failures),
                    "blocking": len(verdict.repair_items()),
                    "first": (
                        verdict.failures[0].detail[:160] if verdict.failures else None
                    ),
                    "unverified_reason": verdict.unverified_reason,
                }
            )
    return items


def part_number_errors(spec: CircuitSpec) -> list[str]:
    """Every IC whose key is not a manufacturer part number, as messages.

    Only ``kind == "ic"`` is checked. A connector, battery holder, switch or
    test point is identified by its ``package`` name -- ``propose.py`` rule 9
    says so and ``board._footprint_for_device`` reads it that way -- so ``J1``
    is the correct name for a barrel jack and would be wrong to reject.

    The three rejections, narrowest first:

    * the name is a bare reference designator (``U1``, ``IC2``, ``REG``);
    * the name is a category word (``opamp``, ``regulator``);
    * the name contains no digit at all, which no orderable IC part number
      manages (``LM358``, ``NE555D``, ``AMS1117-3.3``, ``ATtiny85-20SU``).

    Empty means every IC carries something that can be ordered, looked up in
    ``spice/registry.py`` and matched in ``models3d.py``. It does **not** mean
    the part number is real -- nothing offline can know that, and
    ``sourcing.mpn_status`` is where "nobody checked" is said out loud.
    """
    errors: list[str] = []
    for device in spec.devices:
        if device.kind != "ic":
            continue
        name = device.name.strip()
        flat = re.sub(r"[^A-Za-z0-9]", "", name).upper()
        why: str | None = None
        if _DESIGNATOR_RE.match(name):
            why = "a reference designator, not a part number"
        elif flat in _GENERIC_NAMES:
            why = "a category of part, not a part number"
        elif not any(ch.isdigit() for ch in name):
            why = "has no digits in it, and no orderable IC part number does"
        if why is None:
            continue
        errors.append(
            f"device {name!r} is {why}. The key of an entry in \"devices\" is "
            f"the manufacturer part number and nothing else -- it is what "
            f"reaches the bill of materials, the distributor lookup, the "
            f"3D model, the simulation model and the schematic symbol, and "
            f"the reference designator ({name if _DESIGNATOR_RE.match(name) else 'U1'}"
            f") is assigned later by the board itself. Rename it to the part "
            f"number the request names, or if the request named no part, to a "
            f"specific orderable part you are choosing (for example "
            f"\"LM358D\", \"NE555D\", \"AMS1117-3.3\", \"ATtiny85-20SU\") and "
            f"keep its pin map."
        )
    return errors


def _facts_block(facts: list[PartFacts]) -> str:
    if not facts:
        return "No datasheets were supplied. Use widely-known pinouts only."
    chunks = []
    for f in facts:
        pins = ", ".join(f"{p.name}={p.number}" for p in f.pins)
        reqs = "\n".join(
            f"      - {r.get('requirement','')} (p.{r.get('page','?')})"
            for r in f.requirements
        )
        auxes = "\n".join(
            f"      - {a.get('type','?')} {a.get('value','')} "
            f"{a.get('connects','')} :: {a.get('why','')} (p.{a.get('page','?')})"
            for a in f.auxiliaries
        )
        chunks.append(
            f"  {f.part_number} [{f.package or 'package unknown'}, "
            f"{f.pin_count} pins]\n"
            f"    pins: {pins}\n"
            + (f"    datasheet requirements:\n{reqs}\n" if reqs else "")
            + (f"    recommended auxiliaries:\n{auxes}\n" if auxes else "")
            + (f"    notes: {f.notes}\n" if f.notes else "")
        )
    return "\n".join(chunks)


#: A token that looks like a part number: letters and digits together, at
#: least four characters, optionally with "-", "." or "/" inside.
_PART_TOKEN = re.compile(
    r"\b(?=[A-Za-z0-9.\-/]*\d)(?=[A-Za-z0-9.\-/]*[A-Za-z])"
    r"[A-Za-z0-9][A-Za-z0-9.\-/]{3,}\b"
)

#: Most library parts listed in one prompt.
MAX_LIBRARY_PARTS = 12


def library_block(text: str) -> str:
    """KiCad's real pinout and footprint for every part number in ``text``.

    Empty when the library is off or nothing named resolves. What the model
    reads here is what :func:`_apply_library` will hold its answer to, so the
    two cannot disagree: rule 9's pin-count packages apply only to ICs that are
    *not* listed.
    """
    from .. import kicadlib
    from ..kicadlib.resolve import resolve_part

    index = kicadlib.library_index()
    if index is None:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for token in _PART_TOKEN.findall(text):
        entry = resolve_part(index, token.strip(".-/"))
        if entry is None or entry.lib_id in seen or not entry.footprint:
            continue
        seen.add(entry.lib_id)
        pins = ", ".join(f"{n} {name}" for n, name, _ in entry.pins if name != "~")
        lines.append(
            f"- {token}: KiCad {entry.lib_id}, footprint {entry.footprint}, "
            f"pins: {pins}"
        )
        if len(lines) >= MAX_LIBRARY_PARTS:
            break
    if not lines:
        return ""
    return (
        "Parts found in KiCad's installed library. Use each one's pin NUMBERS "
        "exactly as listed (you may keep your own pin names); it is drawn with "
        "that real footprint, so rule 9's pin-count packages do not apply to "
        "these parts and the key must still be the part number:\n"
        + "\n".join(lines)
        + "\n\n"
    )


@dataclass
class _LibraryResult:
    spec: CircuitSpec
    errors: list[str]


def _apply_library(spec: CircuitSpec, on_event) -> _LibraryResult:
    """Bind ICs to KiCad library symbols when the library is enabled."""
    from .. import kicadlib
    from ..board import _CHIP_REFUSALS, _MODULE_PACKAGES, _NAMED_CHIPS, _normalised
    from ..kicadlib.resolve import apply_library

    def drawn_by_engine(device) -> bool:
        # The hand-checked named chips, modules and refused chips keep the
        # engine's own rule; the library does not override a verified pattern.
        name = _normalised(device.name)
        keys = (*_NAMED_CHIPS, *_MODULE_PACKAGES, *_CHIP_REFUSALS)
        return any(key in name for key in keys)


    index = kicadlib.library_index()
    if index is None:
        return _LibraryResult(spec, [])
    bound, notes, errors = apply_library(
        spec,
        index,
        skip=drawn_by_engine,
    )
    if on_event is not None:
        symbols = sum(1 for d in bound.devices if d.symbol)
        if symbols or notes:
            on_event(
                {
                    "event": "propose.library",
                    "symbols": symbols,
                    "corrected": len(notes),
                    "first": notes[0][:160] if notes else "",
                }
            )
    return _LibraryResult(bound, errors)


def propose_circuit(
    model: Model,
    intent: str,
    *,
    facts: list[PartFacts] | None = None,
    brief: str | None = None,
    max_repairs: int = 3,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[CircuitSpec, list[ProposalAttempt]]:
    """Ask for a circuit and repair it until it validates.

    Returns the accepted spec and the full attempt history, so a caller can show
    how many rounds it took -- which is a genuinely useful quality signal.

    ``on_event`` receives one ``propose.round`` event per rejected round, so a
    caller can watch the repair loop while it runs; see
    :func:`silkscreen.agents.pipeline.generate_pcb` for the event contract.

    Raises:
        ProposalError: the model answered, but never with a valid circuit.
        ModelError: the model could not be reached at all. Deliberately not
            wrapped -- an upstream outage is a different condition from a bad
            proposal, and callers route them differently.
    """
    facts = facts or []
    attempts: list[ProposalAttempt] = []

    # The plan, when one was made, sits between the intent and the facts: it
    # is what the bare intent MEANT, decided once and already checked against
    # the packages this builder can draw. Without it a handful of words --
    # "a home security camera system" -- became a netlist with nothing having
    # reasoned about where power enters.
    plan_block = f"The plan for this board:\n{brief}\n\n" if brief else ""
    plan_block += library_block(f"{intent}\n{brief or ''}")
    prompt = (
        f"{PROPOSE_PROMPT}\n\n"
        f"What to build:\n{intent}\n\n"
        f"{plan_block}"
        f"Datasheet facts you must design against:\n{_facts_block(facts)}\n"
    )

    budget = max_repairs
    unsupported: list[str] = []
    previous_errors: list[str] | None = None
    round_no = -1
    while round_no < budget:
        round_no += 1
        # A transport failure is deliberately NOT wrapped in ProposalError.
        # "the model was unreachable" and "the model could not produce a valid
        # circuit" are different conditions with different remedies -- retry
        # versus give up -- and a caller (an HTTP service deciding between 502
        # and 500) has to be able to tell them apart. ModelError propagates.
        # 65536, Gemini 3.5 Flash's output ceiling. Reasoning tokens share
        # this budget: a robot-arm controller (ESP32 module, PCA9685, buck,
        # USB-C, six servo headers) spent ~15k of 16384 thinking and was cut
        # off 1629 characters into the JSON (measured 2026-09-13).
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=65536)

        attempt = ProposalAttempt(round=round_no, raw=raw)
        attempts.append(attempt)

        errors: list[str] = []
        # Per round: an unsupported part named two rounds ago and since fixed
        # must not keep the escalation going or reach the final error.
        unsupported = []
        spec: CircuitSpec | None = None
        try:
            spec = parse_circuit_spec(raw)
        except ValidationError as exc:
            errors = [str(e) for e in exc.errors]
            # netlist.py refuses a package or kind outside its tables before
            # the builder ever sees the spec; those are unsupported parts too.
            unsupported = [e for e in errors if _UNSUPPORTED_PART.search(e)]
        else:
            # The footprint rule and the part-number rule, run now rather than
            # downstream: a device the builder has no land pattern for, or one
            # named "U1" instead of by part number, is the model's mistake to
            # fix, and after the proposal is accepted nobody can. Both lists
            # are collected together so one repair round addresses both -- the
            # netlist.py convention, applied past the IR's own edge.
            # Complete the contact groups a connector ties by function (a USB-C
            # receptacle's four GND and four VBUS pads) *before* the pad rule
            # runs: wiring A4 and not its stacked B9 used to be refused and
            # cost a repair round, and wiring one stacked land but not the
            # other shipped half a receptacle unpowered. Reported, not quiet.
            spec, tied = tie_package_pins(spec)
            if tied and on_event is not None:
                on_event(
                    {"event": "propose.tied", "pads": len(tied), "first": tied[0][:160]}
                )
            # Hold every IC KiCad knows to its library symbol: the model's pin
            # numbers are checked against the real pinout and corrected where
            # the names agree, and anything else goes back as a repair item
            # carrying that pinout (silkscreen.kicadlib.resolve).
            library_errors = _apply_library(spec, on_event)
            spec = library_errors.spec
            unsupported = package_errors(spec)
            # Bus design checks (signals.py, atopile's requires_pulls shape):
            # a missing I2C pull-up or a TX-to-TX UART goes back in this same
            # repair round rather than reaching layout.
            from ..signals import signal_errors

            errors = (
                library_errors.errors
                + unsupported
                + part_number_errors(spec)
                + signal_errors(spec)
            )
            # The deterministic verifiers (silkscreen.verify): a ground or
            # supply pin on no net, two grounds, a rail shorted to ground go
            # back as repair items in this same round, and, when kicad-cli is
            # installed, so does anything KiCad's own ERC reports on the
            # schematic this spec would draw. aider's shape
            # (aider/coders/base_coder.py: lint and test output become
            # ``reflected_message`` and the model repairs against it), and
            # the reason the model can no longer leave a regulator's GND
            # off and have the run report success (measured 2026-09-15).
            errors += verifier_errors(spec, on_event)

        if errors:
            attempt.errors = errors
            if on_event is not None:
                # Validation errors are engine-generated, not model text, so
                # they are safe to put on the wire -- truncated all the same.
                on_event(
                    {
                        "event": "propose.round",
                        "round": round_no + 1,
                        "errors": len(errors),
                        "first_error": errors[0][:160],
                    }
                )
            if round_no == budget and _should_escalate(
                budget, unsupported, errors, previous_errors
            ):
                budget += 1
                if on_event is not None:
                    on_event(
                        {
                            "event": "propose.escalated",
                            "round": round_no + 1,
                            "max_repairs": budget,
                            "unsupported": len(unsupported),
                            "reason": (
                                "the proposal still names parts the board "
                                "builder cannot draw; one more repair round"
                            ),
                        }
                    )
            previous_errors = errors
            if round_no == budget:
                break
            # Feed every problem back at once so one round fixes all of them.
            problems = "\n".join(f"  - {e}" for e in errors)
            prompt = (
                f"{PROPOSE_PROMPT}\n\n"
                f"What to build:\n{intent}\n\n"
                f"{plan_block}"
                f"Datasheet facts you must design against:\n{_facts_block(facts)}\n\n"
                f"Your previous proposal was rejected. Fix ALL of these and "
                f"return the corrected JSON object:\n{problems}\n\n"
                f"Your previous proposal was:\n{raw}\n"
            )
            continue

        assert spec is not None
        attempt.accepted = True
        return spec, attempts

    last = attempts[-1] if attempts else None
    detail = "\n".join(f"  - {e}" for e in (last.errors if last else []))
    escalated = (
        f" ({budget - max_repairs} more than the {max_repairs} repair "
        f"round(s) budgeted, because unsupported parts remained)"
        if budget > max_repairs
        else ""
    )
    supported = (
        f"\nThe board builder can draw: {supported_packages_text()}"
        if unsupported
        else ""
    )
    raise ProposalError(
        f"No valid circuit after {len(attempts)} attempts{escalated}. "
        f"Final errors:\n{detail}{supported}",
        attempts,
        unsupported=unsupported,
    )


def _should_escalate(
    budget: int,
    unsupported: list[str],
    errors: list[str],
    previous: list[str] | None,
) -> bool:
    """One more round only for unsupported parts, only while it is moving.

    Never past :data:`ESCALATED_MAX_REPAIRS`; never on a round whose errors are
    identical to the last one's (a model repeating itself will repeat itself
    again); and never for plain netlist errors, which is what keeps a
    never-validating proposal at exactly the level's budget.
    """
    if budget >= ESCALATED_MAX_REPAIRS or not unsupported:
        return False
    return previous is None or sorted(previous) != sorted(errors)
