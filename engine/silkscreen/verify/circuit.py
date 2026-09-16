"""Electrical completeness: the ground and supply questions, answered from the IR.

Nothing here reads a ``.kicad_sch``: KiCad power symbols and ``PWR_FLAG``
never enter, so a verdict is about the circuit the model proposed, not about
how the emitter drew it. Net classification is delegated to
:func:`silkscreen.schematic.net_class` -- the tree already had four ground
vocabularies (``kicad.POWER_NET_PATTERNS``, ``schematic._GROUND_TOKENS``,
``board._is_ground``, ``spice.deck.GROUND_NAMES``) and this module adds none;
every net's class is listed in the verdict's evidence so a misclassification
is visible rather than silently green.

The rules, and where each comes from:

R1  **Every power-class pin is wired or declared open.** A pin is power-class
    when its KiCad library symbol types it ``power_in``/``power_out``
    (``kicadlib/index.py`` carries ``(number, name, electrical)``), or, with no
    library binding, when its *name* classifies as a ground or rail
    (``net_class("VDD")``). Wired: passes. Under ``Device.no_connect``: a
    warning, because a power pin deliberately open is unusual but stated.
    Neither: a blocker naming the pin. A library ``power_in`` pin the model
    did not even declare is the same blocker -- that is the two-GND TSSOP
    case where the model wired one and forgot the other. tscircuit's
    ``check-pin-must-be-connected.ts`` and ``check-no-ground-pin-defined.ts``
    are the same two questions asked of circuit-json.
R2  **One ground.** No ground-class net while ground-class pins exist is a
    blocker. Two or more ground-class nets are joined when a resistor or
    inductor bridges them (``AGND`` to ``DGND`` through a 0 R is one ground);
    every connected component past the first is an island, a blocker naming
    its nets. Downgraded to a warning when one IC has ground pins on two
    components, since that is what an isolator looks like and the IR cannot
    yet say ``isolated``.
R3  **Rails do not meet ground, outputs do not meet outputs.** A net carrying
    a ground-class pin and a rail-class pin of any device is a short, a
    blocker. Two different devices' library ``power_out`` pins on one net is
    a blocker (a regulator output tied to VBUS). A power pin on a net whose
    name is not a rail is only a warning: ``CH340C.V3`` on ``CH_V3`` is
    legitimate and the repo's own eval board does it.
R4  **Each IC is decoupled** (warning; the checklist's "decoupling present
    for all ICs"): per IC, per distinct rail-class net on its supply pins,
    at least one capacitor with one leg on that net and the other on a
    ground-class net. Per-pin placement is the board's job
    (``board.decoupling_caps``); this asks only whether a cap exists.

What this deliberately does not decide: whether the voltage on a rail is
the right one, or a capacitor's value. Those are datasheet numbers, and
they belong to SPICE's ``abs_max`` and the critic.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..netlist import CircuitSpec, PassiveType
from ..schematic import net_class
from .verdict import Clause, Verdict

__all__ = ["electrical_completeness", "VERIFIER"]

VERIFIER = "electrical_completeness"

#: Passive types that join two ground nets into one ground (a 0 R link, a
#: ferrite bead drawn as an inductor).
_BRIDGING = frozenset({PassiveType.RESISTOR, PassiveType.INDUCTOR})


def _library_pins(device, index) -> list[tuple[str, str, str]] | None:
    """``(number, name, electrical)`` from the bound library symbol, or None."""
    if index is None or not getattr(device, "symbol", None):
        return None
    entry = index.get(device.symbol)
    if entry is None:
        return None
    return list(entry.pins)


class _Pins:
    """Per-device power-class pins, from the library or from names."""

    def __init__(self, spec: CircuitSpec, index) -> None:
        self.source: dict[str, str] = {}
        #: device -> {pin name: "ground" | "rail"}
        self.classes: dict[str, dict[str, str]] = {}
        #: device -> {pin name: "power_in" | "power_out"} (library only)
        self.electrical: dict[str, dict[str, str]] = {}
        #: device -> library pins the model never declared, by number
        self.undeclared: dict[str, list[tuple[str, str, str]]] = {}
        for device in spec.devices:
            lib = _library_pins(device, index)
            classes: dict[str, str] = {}
            electrical: dict[str, str] = {}
            if lib is not None:
                self.source[device.name] = "library"
                by_number = {str(n): name for name, n in device.pins.items()}
                for number, lib_name, etype in lib:
                    if etype not in ("power_in", "power_out"):
                        continue
                    name = by_number.get(str(number))
                    if name is None:
                        self.undeclared.setdefault(device.name, []).append(
                            (str(number), lib_name, etype)
                        )
                        continue
                    electrical[name] = etype
                    classes[name] = (
                        net_class(lib_name)
                        or net_class(name)
                        or ("rail" if etype == "power_out" else "rail")
                    )
            else:
                self.source[device.name] = "name"
                for name in device.pins:
                    cls = net_class(name)
                    if cls is not None:
                        classes[name] = cls
            self.classes[device.name] = classes
            self.electrical[device.name] = electrical


def electrical_completeness(spec: CircuitSpec, *, index=None) -> Verdict:
    """Run R1-R4 over a validated spec. Never raises on a wrong circuit."""
    if index is None:
        try:
            from .. import kicadlib

            index = kicadlib.library_index() if kicadlib.enabled() else None
        except Exception:  # pragma: no cover - a broken library is not a verdict
            index = None

    pins = _Pins(spec, index)
    net_of: dict[str, str] = {}
    for conn in spec.connections:
        for ep in conn.endpoints:
            net_of[ep] = conn.net
    nets = [c.net for c in spec.connections]
    ground_nets = [n for n in nets if net_class(n) == "ground"]
    rail_nets = [n for n in nets if net_class(n) == "rail"]
    clauses: list[Clause] = []
    evidence: dict[str, Any] = {
        "ground_nets": ground_nets,
        "rail_nets": rail_nets,
        "devices": {
            d.name: {
                "source": pins.source[d.name],
                "power_pins": dict(sorted(pins.classes[d.name].items())),
            }
            for d in spec.devices
        },
        "not_checked": [],
    }

    # ---- R1: every power-class pin is wired or declared open
    any_ground_pin = False
    for device in spec.devices:
        classes = pins.classes[device.name]
        if not classes and device.name not in pins.undeclared:
            evidence["not_checked"].append(device.name)
        for name, cls in sorted(classes.items()):
            any_ground_pin |= cls == "ground"
            ep = f"{device.name}.{name}"
            if ep in net_of:
                continue
            etype = pins.electrical[device.name].get(name)
            if name in device.no_connect:
                clauses.append(
                    Clause(
                        f"power_pin_open:{ep}",
                        True,
                        f"{ep} is a {cls} pin declared no_connect; unusual for a "
                        f"power pin, stated by the design",
                        severity="warning",
                        refs=(device.name,),
                    )
                )
                continue
            clauses.append(
                Clause(
                    f"power_pin_wired:{ep}",
                    False,
                    f"{ep} is a {cls} pin ({etype or 'by name'}) on no net and not "
                    f"under no_connect; put it on the "
                    f"{'ground net' if cls == 'ground' else 'rail that feeds it'}",
                    severity="warning" if etype == "power_out" else "blocker",
                    refs=(device.name,),
                )
            )
        for number, lib_name, etype in pins.undeclared.get(device.name, []):
            clauses.append(
                Clause(
                    f"power_pin_declared:{device.name}.{lib_name}",
                    False,
                    f"{device.name} pin {number} ({lib_name}) is {etype} in the "
                    f"KiCad library symbol and the proposal neither declares nor "
                    f"wires it; add it to pins and put it on its net",
                    severity="warning" if etype == "power_out" else "blocker",
                    refs=(device.name,),
                )
            )
    if not any(not c.passed and c.name.startswith("power_pin") for c in clauses):
        clauses.append(
            Clause(
                "power_pins_wired",
                True,
                "every power-class pin is wired or declared open",
            )
        )

    # ---- R2: one ground
    if not ground_nets:
        clauses.append(
            Clause(
                "one_ground",
                not any_ground_pin,
                "no ground-class net (GND, VSS, AGND...) while parts have ground pins"
                if any_ground_pin
                else "no ground net and no part asks for one",
            )
        )
    else:
        parent = {n: n for n in ground_nets}

        def find(n: str) -> str:
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n

        for passive in spec.passives:
            if passive.type not in _BRIDGING:
                continue
            legs = [net_of.get(f"{passive.name}.1"), net_of.get(f"{passive.name}.2")]
            if all(leg in parent for leg in legs) and legs[0] != legs[1]:
                parent[find(legs[0])] = find(legs[1])
        components: dict[str, list[str]] = defaultdict(list)
        for n in ground_nets:
            components[find(n)].append(n)
        groups = sorted(components.values())
        if len(groups) == 1:
            clauses.append(
                Clause("one_ground", True, f"one ground: {', '.join(groups[0])}")
            )
        else:
            # An IC with ground pins on two components is what an isolator
            # looks like; the IR cannot say "isolated", so only warn.
            isolator = False
            for device in spec.devices:
                seen = {
                    find(net_of[f"{device.name}.{p}"])
                    for p, cls in pins.classes[device.name].items()
                    if cls == "ground" and net_of.get(f"{device.name}.{p}") in parent
                }
                if len(seen) > 1:
                    isolator = True
            clauses.append(
                Clause(
                    "one_ground",
                    False,
                    f"{len(groups)} separate grounds: "
                    + "; ".join("{" + ", ".join(g) + "}" for g in groups)
                    + " -- merge them into one net, or bridge them with a 0 R "
                    "if they are meant to be one return",
                    severity="warning" if isolator else "blocker",
                    refs=tuple(n for g in groups for n in g),
                )
            )

    # ---- R3: rails do not meet ground; outputs do not meet outputs
    shorts: list[Clause] = []
    for conn in spec.connections:
        ground_pins, rail_pins, outs = [], [], []
        for ep in conn.endpoints:
            part, _, pin = ep.rpartition(".")
            cls = pins.classes.get(part, {}).get(pin)
            if cls == "ground":
                ground_pins.append(ep)
            elif cls == "rail":
                rail_pins.append(ep)
                if pins.electrical.get(part, {}).get(pin) == "power_out":
                    outs.append(ep)
                if net_class(conn.net) is None:
                    clauses.append(
                        Clause(
                            f"rail_pin_on_signal_net:{ep}",
                            True,
                            f"{ep} is a supply pin on net {conn.net!r}, which is not "
                            f"named like a rail; fine if intended",
                            severity="warning",
                            refs=(part,),
                        )
                    )
        if ground_pins and rail_pins:
            shorts.append(
                Clause(
                    f"rail_to_ground:{conn.net}",
                    False,
                    f"net {conn.net!r} joins ground pin(s) {', '.join(ground_pins)} "
                    f"to supply pin(s) {', '.join(rail_pins)}: a short",
                    refs=tuple(ep.rpartition(".")[0] for ep in ground_pins + rail_pins),
                )
            )
        if len({ep.rpartition(".")[0] for ep in outs}) > 1:
            shorts.append(
                Clause(
                    f"outputs_tied:{conn.net}",
                    False,
                    f"net {conn.net!r} ties power outputs of different parts "
                    f"together: {', '.join(outs)}",
                    refs=tuple(ep.rpartition(".")[0] for ep in outs),
                )
            )
    clauses.extend(shorts or [Clause("no_supply_shorts", True, "no rail meets ground")])

    # ---- R4: each IC decoupled (warning)
    ground_set = set(ground_nets)
    caps_by_net: dict[str, int] = defaultdict(int)
    for passive in spec.passives:
        if passive.type != PassiveType.CAPACITOR:
            continue
        legs = {net_of.get(f"{passive.name}.1"), net_of.get(f"{passive.name}.2")}
        if legs & ground_set:
            for leg in legs - ground_set:
                if leg is not None:
                    caps_by_net[leg] += 1
    for device in spec.devices:
        if device.kind != "ic":
            continue
        supply_nets = sorted(
            {
                net_of[f"{device.name}.{p}"]
                for p, cls in pins.classes[device.name].items()
                if cls == "rail"
                and f"{device.name}.{p}" in net_of
                and pins.electrical[device.name].get(p) != "power_out"
            }
        )
        for net in supply_nets:
            clauses.append(
                Clause(
                    f"decoupled:{device.name}:{net}",
                    caps_by_net[net] > 0,
                    f"{device.name} draws from {net!r} "
                    + (
                        f"with {caps_by_net[net]} capacitor(s) to ground"
                        if caps_by_net[net]
                        else "with no capacitor from that net to ground"
                    ),
                    severity="warning",
                    refs=(device.name,),
                )
            )

    return Verdict(VERIFIER, tuple(clauses), evidence)
