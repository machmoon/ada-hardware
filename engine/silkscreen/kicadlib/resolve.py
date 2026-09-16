"""Resolve a proposed IC to its KiCad library symbol, and hold its pins to it.

The model writes an IC as ``"AMS1117-3.3": {"pins": {"VIN": "3", ...}}`` -- a
part number and a pin map it made up. KiCad already knows that part: its
``Regulator_Linear:AMS1117-3.3`` symbol says pin 1 is GND, 2 is VO and 3 is
VI. So the proposal is checked against the library:

* **Resolution is exact**, never fuzzy: the device key must equal a symbol
  name or match one of KiCad's ``x`` placeholder names ("STM32F103C8T6" ->
  ``STM32F103C8Tx``). A token-similar part is a different part, and binding
  the wrong pinout silently is the failure this exists to prevent.
* **A name that clearly belongs to another pin moves the number.** A pin whose
  name matches exactly one library pin (after case, overbar and punctuation
  are normalised, or one name is a prefix of the other -- "VIN"/"VI",
  "VOUT"/"VO") but whose number is a different, incompatible pin takes the
  library's number: "VIN"=1 on an AMS1117, whose pin 1 is GND, becomes 3.
  That is a correction, reported as a note.
* **Otherwise a number the part has is trusted.** Names are labels and
  spellings differ -- an NE555's "RESET"=4 is KiCad's ``~{RST}``, "CV"=5 is
  ``CONT`` -- so a real pin number with an unfamiliar name stands. (The
  first version rejected exactly that correct 555, measured 2026-09-14.)
* **A number the part does not have is a repair item** carrying the real
  pinout, so the one repair round the loop allows has the facts to fix it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .index import LibraryIndex, SymbolEntry

__all__ = ["PinCheck", "check_pins", "resolve_part"]


def resolve_part(index: LibraryIndex, part_number: str) -> SymbolEntry | None:
    """The library symbol for ``part_number``, or None. Exact matches only."""
    exact = index.by_name(part_number)
    if not exact:
        hits = index.search(part_number, 3)
        # search() scores an exact name 100+ and an x-placeholder match 90+;
        # anything lower is token similarity, which is not identity.
        exact = [
            h
            for h in hits
            if h.name.lower() == part_number.strip().lower()
            or _placeholder_match(h.name, part_number)
        ]
    if not exact:
        return None
    # The same name in two libraries: prefer the one that names a footprint.
    exact.sort(key=lambda e: (not e.footprint, e.lib_id))
    return exact[0]


def _placeholder_match(name: str, part_number: str) -> bool:
    if not re.search(r"(?<=[0-9A-Z])x", name):
        return False
    pattern = re.sub(r"(?<=[0-9A-Z])x", ".", re.escape(name))
    return re.fullmatch(pattern, part_number.strip(), re.IGNORECASE) is not None


def _norm(name: str) -> str:
    # KiCad writes an overbar as ~{RESET}; "~" alone is an unnamed pin.
    return re.sub(r"[^a-z0-9]", "", name.replace("~{", "").lower())


def _compatible(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    return len(short) >= 2 and long_.startswith(short)


@dataclass
class PinCheck:
    """The corrected pin map, what was corrected, and what could not be."""

    pins: dict[str, str]
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def check_pins(entry: SymbolEntry, part: str, pins: dict[str, str]) -> PinCheck:
    lib_by_number = {number: name for number, name, _ in entry.pins}
    result = PinCheck(pins=dict(pins))
    unknown: list[str] = []
    for name, number in pins.items():
        number = str(number)
        at_number = lib_by_number.get(number)
        if at_number is not None and _compatible(name, at_number):
            continue
        candidates = [
            n for n, lib_name, _ in entry.pins if _compatible(name, lib_name)
        ]
        if len(candidates) == 1 and candidates[0] != number:
            result.pins[name] = candidates[0]
            result.notes.append(
                f"{part}.{name}: pin {number} corrected to {candidates[0]} "
                f"({entry.lib_id} names it "
                f"{lib_by_number[candidates[0]]!r})"
            )
            continue
        if at_number is not None:
            continue  # a real pin under a different spelling
        unknown.append(f"{name}={number}")
    if unknown:
        pinout = ", ".join(
            f"{number} {name}" for number, name, _ in entry.pins if name != "~"
        )
        result.errors.append(
            f"{part!r} is KiCad's {entry.lib_id}, whose pins are: {pinout}. "
            f"These pins are numbered for pins the part does not have: "
            f"{unknown}. Use the library's pin numbers."
        )
    # Two names landing on one number after correction is a short the
    # correction would have made; say so rather than ship it.
    seen: dict[str, str] = {}
    for name, number in result.pins.items():
        if number in seen:
            result.errors.append(
                f"{part!r}: pins {seen[number]!r} and {name!r} both map to "
                f"{entry.lib_id} pin {number}"
            )
        seen[number] = name
    return result


def apply_library(
    spec, index: LibraryIndex, *, skip=lambda device: False
) -> tuple[object, list[str], list[str]]:
    """Bind every IC in ``spec`` that KiCad knows to its library symbol.

    Returns ``(spec, notes, errors)``: the spec with each resolved IC's
    ``symbol`` set and its pin numbers corrected to the library, one note per
    correction, and one repair item per IC whose pins could not be reconciled.
    Only plain ICs are touched -- connectors, batteries, switches and test
    points are chosen by package name, and ``skip`` excludes devices the caller
    already draws another way (the engine's hand-checked named chips).
    """
    from dataclasses import replace

    notes: list[str] = []
    errors: list[str] = []
    devices = []
    changed = False
    for device in spec.devices:
        if device.kind != "ic" or device.package or skip(device):
            devices.append(device)
            continue
        entry = resolve_part(index, device.name)
        if entry is None or not entry.pins:
            devices.append(device)
            continue
        check = check_pins(entry, device.name, dict(device.pins))
        notes.extend(check.notes)
        errors.extend(check.errors)
        devices.append(replace(device, pins=check.pins, symbol=entry.lib_id))
        changed = True
    if not changed:
        return spec, notes, errors
    return replace(spec, devices=devices), notes, errors
