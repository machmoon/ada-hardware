"""Buses the circuit carries, and the design checks each bus owns.

A netlist is loose wires; an engineer reads buses. The model is atopile's
(``src/faebryk/library/I2C.py``, ``requires_pulls.py``,
``implements_design_check.py``): a bus type knows what it requires -- I2C's
open-drain lines need pull-ups between 1 k and 10 k -- and a check is either
unfulfilled or maybe-unfulfilled. Here a bus is recognised from pin and net
names rather than declared, because the circuit comes from a model that names
pins ("SDA", "U0TXD") but declares no interfaces.

The two outcomes stay apart, as atopile's two exception types do:

* :func:`signal_errors` -- unfulfilled, returned as repair items for the
  propose repair round. Claimed only when the circuit is unambiguous: the pin
  names are the bus's own and the ends are ICs.
* :func:`signal_notes` -- maybe, for the engineer: differential pairs found,
  or an I2C line that leaves the board with no pull-up here.

:func:`net_classes` feeds the ``.kicad_pro`` writer. Deterministic and
network-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .netlist import CircuitSpec, PassiveType
from .schematic import net_class

__all__ = [
    "DiffPair",
    "diff_pairs",
    "net_classes",
    "resistance_ohms",
    "signal_errors",
    "signal_notes",
]

#: atopile ``I2C.requires_pulls``: 1 k to 10 k, each +/-10 %.
I2C_PULL_MIN_OHMS = 900.0
I2C_PULL_MAX_OHMS = 11_000.0

# Whole-token pin-name patterns. A token is a run of letters and digits, so
# "I2C_SDA" yields "SDA" while "SDAT" (an SD card line) and "TXEN" do not match.
_I2C_ROLES = {"SDA": re.compile(r"SDA\d?"), "SCL": re.compile(r"SCL\d?")}
_UART_ROLES = {"TX": re.compile(r"(U\d)?TXD?\d?"), "RX": re.compile(r"(U\d)?RXD?\d?")}
_TOKEN = re.compile(r"[A-Z0-9]+")


@dataclass(frozen=True)
class _End:
    part: str
    pin: str
    #: The device kind, or ``"passive"``.
    kind: str


@dataclass(frozen=True)
class DiffPair:
    name: str
    positive: str
    negative: str
    #: Target differential impedance in ohms (USB 2.0: 90, CAN: 120).
    impedance_ohms: float


def _role(pin: str, roles: dict[str, re.Pattern[str]]) -> str | None:
    for token in _TOKEN.findall(pin.upper()):
        for role, pattern in roles.items():
            if pattern.fullmatch(token):
                return role
    return None


def _ends_by_net(spec: CircuitSpec) -> dict[str, list[_End]]:
    kinds = {device.name: device.kind for device in spec.devices}
    ends: dict[str, list[_End]] = {}
    for conn in spec.connections:
        for endpoint in conn.endpoints:
            part, _, pin = endpoint.rpartition(".")
            kind = kinds.get(part, "passive")
            ends.setdefault(conn.net, []).append(_End(part, pin, kind))
    return ends


def _nets_by_part(spec: CircuitSpec) -> dict[str, set[str]]:
    nets: dict[str, set[str]] = {}
    for conn in spec.connections:
        for endpoint in conn.endpoints:
            nets.setdefault(endpoint.rpartition(".")[0], set()).add(conn.net)
    return nets


def resistance_ohms(value: str) -> float | None:
    """A resistor value as an engineer writes it, in ohms; None if unreadable.

    Handles the RKM code ("4k7", "2R2") and a bare "M" meaning mega, both of
    which SPICE syntax reads differently ("4k7" as 4 k, "M" as milli).
    """
    from .spice.values import parse_value

    text = value.strip().upper()
    for unit in ("Ω", "OHMS", "OHM"):
        text = text.replace(unit, "")
    rkm = re.fullmatch(r"(\d+)([RKM])(\d+)", text)
    if rkm:
        whole, multiplier, fraction = rkm.groups()
        text = f"{whole}.{fraction}{'' if multiplier == 'R' else multiplier}"
    if text.endswith("M"):
        text = text[:-1] + "MEG"
    try:
        return parse_value(text)[0]
    except Exception:  # noqa: BLE001 - unreadable is "unknown", never a pass
        return None


def _i2c_lines(ends_by_net: dict[str, list[_End]]) -> dict[str, str]:
    """Nets carrying an IC's SDA or SCL pin, with the role."""
    lines = {}
    for net, ends in ends_by_net.items():
        for end in ends:
            role = _role(end.pin, _I2C_ROLES) if end.kind == "ic" else None
            if role:
                lines[net] = role
    return lines


def _pull_ups(
    net: str,
    ends: list[_End],
    spec: CircuitSpec,
    nets_by_part: dict[str, set[str]],
) -> list[tuple[str, str]]:
    """``(resistor name, value)`` for each resistor from ``net`` to a rail."""
    values = {p.name: p.value for p in spec.passives if p.type == PassiveType.RESISTOR}
    pulls = []
    for end in ends:
        if end.part not in values:
            continue
        others = nets_by_part.get(end.part, set()) - {net}
        if any(net_class(other) == "rail" for other in others):
            pulls.append((end.part, values[end.part]))
    return pulls


def _i2c_errors(spec: CircuitSpec, ends_by_net, nets_by_part) -> list[str]:
    errors = []
    for net, role in sorted(_i2c_lines(ends_by_net).items()):
        ends = ends_by_net[net]
        pulls = _pull_ups(net, ends, spec, nets_by_part)
        leaves_board = any(end.kind == "connector" for end in ends)
        if not pulls and not leaves_board:
            errors.append(
                f"I2C {role} net {net!r} has no pull-up. I2C lines are "
                f"open-drain: add a 1k-10k resistor (4.7k is usual) from "
                f"{net!r} to the logic rail."
            )
        for resistor, value in pulls:
            ohms = resistance_ohms(value)
            if ohms is not None and not I2C_PULL_MIN_OHMS <= ohms <= I2C_PULL_MAX_OHMS:
                errors.append(
                    f"I2C {role} pull-up {resistor} on {net!r} is {value}; an "
                    f"I2C pull-up must be 1k-10k (4.7k is usual at 100-400 kHz)."
                )
    return errors


def _uart_errors(ends_by_net: dict[str, list[_End]]) -> list[str]:
    errors = []
    for net, ends in sorted(ends_by_net.items()):
        pins_by_role: dict[str, list[str]] = {}
        for end in ends:
            role = _role(end.pin, _UART_ROLES) if end.kind == "ic" else None
            if role:
                pins_by_role.setdefault(role, []).append(f"{end.part}.{end.pin}")
        for role, pins in sorted(pins_by_role.items()):
            if len({pin.rpartition(".")[0] for pin in pins}) >= 2:
                errors.append(
                    f"UART net {net!r} joins {role} to {role} ({', '.join(pins)}). "
                    f"A UART crosses over: each device's TX goes to the other's RX."
                )
    return errors


def signal_errors(spec: CircuitSpec) -> list[str]:
    """Unfulfilled bus requirements, as repair items."""
    ends_by_net = _ends_by_net(spec)
    return _i2c_errors(spec, ends_by_net, _nets_by_part(spec)) + _uart_errors(
        ends_by_net
    )


#: ``(bus, impedance, positive suffix, negative suffix)`` on upper-cased nets.
_PAIR_RULES = (
    ("USB", 90.0, r"(D\+|DP|D_P|UDP)", r"(D-|DM|DN|D_N|UDM)"),
    ("CAN", 120.0, r"CANH", r"CANL"),
)


def diff_pairs(spec: CircuitSpec) -> list[DiffPair]:
    """Differential pairs recognised by net name: USB D+/D-, CAN H/L."""
    nets = {conn.net.upper(): conn.net for conn in spec.connections}
    pairs = []
    for bus, ohms, positive, negative in _PAIR_RULES:
        for upper, net in nets.items():
            match = re.fullmatch(rf"(?P<base>.*?){positive}", upper)
            if not match:
                continue
            base = match["base"]
            partner = next(
                (
                    nets[other]
                    for other in nets
                    if re.fullmatch(rf"{re.escape(base)}{negative}", other)
                ),
                None,
            )
            if partner is None:
                continue
            tag = base.strip("_")
            name = bus if tag in ("", bus) else f"{bus}_{tag}"
            pairs.append(DiffPair(name, net, partner, ohms))
    return pairs


def signal_notes(spec: CircuitSpec) -> list[str]:
    """Maybe-unfulfilled findings and recognised pairs, for the engineer."""
    ends_by_net = _ends_by_net(spec)
    nets_by_part = _nets_by_part(spec)
    notes = [
        f"{pair.name}: {pair.positive}/{pair.negative} is a "
        f"{pair.impedance_ohms:.0f} ohm differential pair; route it coupled "
        "and length-matched."
        for pair in diff_pairs(spec)
    ]
    for net, role in sorted(_i2c_lines(ends_by_net).items()):
        ends = ends_by_net[net]
        leaves_board = any(end.kind == "connector" for end in ends)
        if leaves_board and not _pull_ups(net, ends, spec, nets_by_part):
            notes.append(
                f"I2C {role} net {net!r} leaves the board with no pull-up here; "
                "confirm the far end pulls it up (1k-10k)."
            )
    return notes


def net_classes(spec: CircuitSpec) -> list[dict]:
    """One class per differential pair, for ``schematic.emit_kicad_pro``."""
    return [
        {
            "name": pair.name,
            "nets": [pair.positive, pair.negative],
            "impedance_ohms": pair.impedance_ohms,
        }
        for pair in diff_pairs(spec)
    ]
