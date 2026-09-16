"""Emit a complete ``.kicad_pcb`` from a validated circuit.

This is the step the previous project never had. Its "layout engine" pasted a
board a human had already drawn, through the clipboard, into a running KiCad
window -- so it could only ever produce the one design it had been handed.

Here a :class:`~silkscreen.netlist.CircuitSpec` becomes real footprints with
real pads on real nets, placed by the CP-SAT solver, written as KiCad 8
s-expressions. No KiCad installation, no footprint library on disk, no GUI.

The output is verified by round-tripping through ``kiutils`` in the test suite:
if KiCad's own parser cannot read what we wrote, the tests fail.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .diffpair import CoupledRoute, PairFailure, route_pair
from .footprints import (
    BATTERY_PACKAGES,
    CONNECTOR_PACKAGES,
    SWITCH_PACKAGES,
    TESTPOINT_PACKAGES,
    Footprint,
    UnsupportedPackage,
    battery_holder,
    cathode_mark,
    connector,
    dual_row_header,
    esp32_wroom_32e,
    for_passive,
    lqfp,
    pin1_mark,
    probe_point,
    silk_segments,
    soic,
    sot223,
    switch,
    ti_powerpad_so8,
    tssop,
)
from .ids import stable_uuid
from .models3d import model_for
from .netlist import CircuitSpec
from .packing import Layer, Part, Placement, pack
from .packing import Net as PackNet
from .routing import (
    DEFAULT_EDGE_CLEARANCE_NM,
    DEFAULT_TRACK_WIDTH_NM,
    DEFAULT_VIA_DIAMETER_NM,
    DEFAULT_VIA_DRILL_NM,
    RoutePad,
    RouteResult,
    Track,
    Via,
    route,
)
from .units import DEFAULT_CLEARANCE_NM, NM_PER_MM, mm

__all__ = [
    "BoardResult",
    "PlacedPart",
    "DEFAULT_BOARD_MARGIN_NM",
    "PASSIVE_SPACING_NM",
    "IC_SPACING_NM",
    "REF_TEXT_SIZE_NM",
    "REF_TEXT_STROKE_NM",
    "REF_TEXT_CLEARANCE_NM",
    "REF_TEXT_BAND_NM",
    "ref_text_offset_nm",
    "build_board",
    "board_pads",
    "package_errors",
    "supported_packages_text",
    "part_anchor",
    "placed_half_extents",
    "rotate_offset",
    "route_board",
    "emit_kicad_pcb",
    "write_board",
]

#: Gap between the outermost courtyard and the board edge. Shared by the
#: emitter and the router: the router must know where the copper may go, and
#: the edge is drawn from the same number, so they cannot drift apart.
DEFAULT_BOARD_MARGIN_NM = mm(2.0)

#: Room a generated board reserves on every side of a part's courtyard, on top
#: of the solver clearance. Two neighbours end up ``clearance + a + b`` apart,
#: so with the 0.25 mm default clearance two passives sit 0.75 mm apart and
#: anything next to an IC sits at least 1.0 mm from it. The solver's own
#: clearance is a manufacturing minimum, and a board packed at the minimum
#: reads as compressed rather than placed: nowhere to probe, nowhere to rework,
#: nowhere for a designator to go. These are the generated-board defaults
#: only -- ``pack()`` still places a real board exactly as asked.
PASSIVE_SPACING_NM = mm(0.25)
IC_SPACING_NM = mm(0.5)

#: The reference designator: KiCad text size, pen width, and the gap kept
#: between its glyph box and the courtyard it labels (the same 0.2 mm the
#: silkscreen keeps from copper, see :data:`footprints.SILK_PAD_CLEARANCE_NM`).
REF_TEXT_SIZE_NM = mm(0.8)
REF_TEXT_STROKE_NM = mm(0.12)
REF_TEXT_CLEARANCE_NM = mm(0.2)

#: Height of the band above every courtyard that the designator occupies:
#: clearance plus the glyph box (size plus one pen width). The placer reserves
#: this band with the part, so the text can never land on a neighbour --
#: which is where it went when parts were packed at the bare clearance.
REF_TEXT_BAND_NM = REF_TEXT_CLEARANCE_NM + REF_TEXT_SIZE_NM + REF_TEXT_STROKE_NM

#: Horizontal room per glyph. KiCad's stroke font advances about 0.7 of the
#: size per character; reserving one full size per character over-reserves
#: rather than under, so a long designator on a narrow part widens the part's
#: reservation instead of hanging over the neighbour.
_REF_GLYPH_ADVANCE_NM = REF_TEXT_SIZE_NM


def ref_text_offset_nm(fp: Footprint) -> int:
    """Distance from the anchor to the designator's centre, straight up.

    Footprint-local, along the courtyard's short side: the text sits centred
    above the courtyard with :data:`REF_TEXT_CLEARANCE_NM` between its glyph
    box and the courtyard line. The emitter writes it (negated, since KiCad's
    footprint frame is Y-down) and :func:`build_board` reserves the band it
    needs, so the two cannot disagree about where the label is.
    """
    return (
        fp.courtyard_h_nm
        + REF_TEXT_CLEARANCE_NM
        + (REF_TEXT_SIZE_NM + REF_TEXT_STROKE_NM) // 2
    )


def _ref_text_width_nm(ref: str) -> int:
    """Conservative width of a designator's glyph box."""
    return len(ref) * _REF_GLYPH_ADVANCE_NM + REF_TEXT_STROKE_NM


@dataclass(frozen=True)
class _Reserve:
    """Room reserved around a courtyard in the solver's box, per side.

    ``top`` is the side the designator lives on (Y-up, above the courtyard);
    it is the larger of the spacing and the text band, since the band already
    exceeds any spacing this module defines. ``side`` grows when the designator
    is wider than the courtyard, so a long ref on an 0402 still stays inside
    the part's own reservation.
    """

    side: int
    bottom: int
    top: int


def _reserve_for(fp: Footprint, ref: str, spacing_nm: int) -> _Reserve:
    overhang = max(0, (_ref_text_width_nm(ref) - 2 * fp.courtyard_w_nm + 1) // 2)
    return _Reserve(
        side=max(spacing_nm, overhang),
        bottom=spacing_nm,
        top=max(spacing_nm, REF_TEXT_BAND_NM),
    )


_LAYERS = """    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)"""


@dataclass
class PlacedPart:
    """A footprint with its reference designator, position and value."""

    ref: str
    footprint: Footprint
    value: str = ""
    x_nm: int = 0
    y_nm: int = 0
    rotated: bool = False
    layer: Layer = Layer.TOP
    #: ``{pad number: net name}`` for pins the part declares and the circuit
    #: leaves unwired, named the way KiCad names them
    #: (:func:`kicad_unconnected_net`).
    #: Written into the ``.kicad_pcb`` only -- never routed, never counted as
    #: unrouted -- so the board agrees with the schematic's no-connect flags.
    no_connects: dict[str, str] = field(default_factory=dict)


def _schematic_pins(device) -> list[tuple[str, str]]:
    """``(pin name, number)`` for every pin the schematic will draw.

    For a library-bound IC that is the library symbol's pins -- all of them,
    under the library's names -- because that is the symbol the schematic
    embeds and the names KiCad builds its ``unconnected-(...)`` nets from. For
    everything else it is the pins the circuit declared. KiCad's unnamed pin
    ``~`` has no name in the net.
    """
    from .kicadlib.packages import PACKAGE_SYMBOLS, package_pads

    package = getattr(device, "package", None) or ""
    symbol = getattr(device, "symbol", None)
    pads = None
    if not symbol and package in PACKAGE_SYMBOLS:
        symbol = PACKAGE_SYMBOLS[package]
        pads = package_pads(package)
    elif not symbol and package:
        symbol = _connector_symbol(package, device)
    if symbol:
        from . import kicadlib
        from .kicadlib.symbol import load_symbol

        if kicadlib.enabled():
            loaded = load_symbol(symbol, pads=pads)
            if loaded is not None:
                return [
                    ("" if p.name == "~" else p.name, p.number) for p in loaded.pins
                ]
    return [(name, str(number)) for name, number in device.pins.items()]


def kicad_unconnected_net(ref: str, pin_name: str, number: str) -> str:
    """The net KiCad gives a no-connect pin when it updates a board.

    ``SCH_PIN::GetDefaultNetName`` in ``eeschema/sch_pin.cpp``: an unconnected
    pin is ``unconnected-(REF-NAME-PadNUMBER)`` when its name differs from its
    number and ``unconnected-(REF-PadNUMBER)`` otherwise, the name passed
    through ``EscapeString(..., CTX_NETNAME)`` (``common/string_utils.cpp``),
    which turns ``/`` into ``{slash}`` and drops line breaks. A pad left with
    no net instead reads to schematic parity as "Pad missing net given by
    schematic" -- 33 of those on the 2026-09-14 demo board.
    """

    def escape(text: str) -> str:
        return text.replace("/", "{slash}").replace("\n", "").replace("\r", "")

    number = str(number)
    if pin_name and pin_name != number:
        return f"unconnected-({ref}-{escape(pin_name)}-Pad{escape(number)})"
    return f"unconnected-({ref}-Pad{escape(number)})"


@dataclass
class BoardResult:
    parts: list[PlacedPart]
    nets: list[str]
    width_nm: int
    height_nm: int
    solver_status: str
    wirelength_nm: int | None = None
    warnings: list[str] = field(default_factory=list)
    #: Copper laid by :func:`route_board`. Empty means the board is placed but
    #: unrouted -- which is what KiCad draws as a ratsnest.
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    #: Nets the router could not finish, each mapped to the reason. Read this
    #: before telling anyone the board is routed.
    unrouted_nets: dict[str, str] = field(default_factory=dict)
    routed_nets: list[str] = field(default_factory=list)
    #: Nets carried by a copper pour on both layers rather than by tracks
    #: (the ground fill :func:`route_board` reserves and :func:`emit_kicad_pcb`
    #: draws as two zones). KiCad fills the zones; see :func:`write_board`.
    filled_nets: list[str] = field(default_factory=list)
    #: How many of ``vias`` are ground stitching (:func:`stitch_filled_nets`)
    #: rather than signal layer changes, so a via count stays readable.
    stitching_vias: int = 0
    #: Nets the router lays before any other: both legs of every
    #: differential pair :func:`silkscreen.signals.diff_pairs` recognises.
    priority_nets: list[str] = field(default_factory=list)
    #: One :meth:`CoupledRoute.as_dict` per pair laid with a constant gap.
    coupled_pairs: list[dict] = field(default_factory=list)

    @property
    def size_mm(self) -> tuple[float, float]:
        return (self.width_nm / NM_PER_MM, self.height_nm / NM_PER_MM)

    @property
    def is_routed(self) -> bool:
        """True only when every routable net came out fully connected."""
        return bool(self.tracks) and not self.unrouted_nets

    @property
    def route_completion(self) -> float:
        """Fraction of routable nets finished.

        1.0 with both lists empty means there was nothing to route, not that a
        refusal succeeded: :func:`route_board` names every net a refusal covers
        in ``unrouted_nets``, so a skipped route reads 0.0 rather than 100%.
        """
        total = len(self.routed_nets) + len(self.unrouted_nets)
        return 1.0 if total == 0 else len(self.routed_nets) / total


@dataclass(frozen=True)
class _ModulePackage:
    """A plug-in module's land pattern, keyed by name rather than pin count.

    A module is not a chip. Its pin count says nothing about its package -- a
    30-pin Arduino Nano and a 30-pin SSOP share a number and nothing else --
    so the count can never select the geometry the way it does for a SOIC. The
    name selects it and the count is then *checked*, which is what turns a
    mismatch into a refusal instead of a board with pads in the wrong place.
    """

    label: str
    pin_count: int
    row_spacing_mm: float
    body_w_mm: float
    body_h_mm: float


#: Modules this pipeline has a characterised land pattern for. Adding an entry
#: means someone has checked the dimensions against the module's own drawing;
#: it is deliberately not a guess from the pin count.
_MODULE_PACKAGES: dict[str, _ModulePackage] = {
    # Arduino Nano and its pin-compatible clones: two 15-pin 0.1" rows on a
    # 0.6" span, 17.78 x 43.18 mm board outline (Arduino Nano rev 3.2 drawing).
    "arduino_nano": _ModulePackage(
        label="Arduino Nano",
        pin_count=30,
        row_spacing_mm=15.24,
        body_w_mm=17.78,
        body_h_mm=43.18,
    ),
}


@dataclass(frozen=True)
class _NamedChip:
    """A chip whose land pattern is chosen by its part number, not its count.

    The pin-count rule answers "a 28-pin IC is a SOIC-28", which is false for
    every part that is never made in SOIC -- a PCA9685 comes in TSSOP-28 and
    HVQFN-28 and nothing else, so the count rule drew a package that does not
    exist and nothing raised. The IR already keys an IC by its manufacturer
    part number (propose rule 11), so the name is available to decide on, the
    same way :data:`_MODULE_PACKAGES` decides a module. The count is then
    checked, never trusted.
    """

    label: str
    pin_count: int
    build: Callable[[dict[str, str]], Footprint]


#: Part-number stems with a characterised land pattern. Matched as tokens of
#: the normalised device name, longest key first, so a suffix that names a
#: different package (``_CHIP_REFUSALS``) is seen before the stem it extends.
_NAMED_CHIPS: dict[str, _NamedChip] = {
    # NXP PCA9685PW: TSSOP-28, 4.4 mm body (NXP PCA9685 datasheet, package
    # outline SOT361-1; Adafruit's open servo-driver board uses this part).
    "pca9685": _NamedChip("PCA9685 (TSSOP-28)", 28, lambda nets: tssop(28, nets)),
    # Espressif ESP32-WROOM-32E: 38 castellated pins plus the GND slug as
    # pin 39, KiCad's RF_Module:ESP32-WROOM-32E symbol numbering.
    "esp32_wroom_32e": _NamedChip("ESP32-WROOM-32E module", 39, esp32_wroom_32e),
    # TI TPS5430 (3 A) and TPS5450 (5 A) buck converters: both ship only in
    # the 8-lead DDA PowerPAD SO, and KiCad's Regulator_Switching:TPS5430DDA
    # symbol numbers the exposed pad 9 (GNDPAD). A plain SOIC-8 would leave
    # the pad the datasheet requires grounded floating.
    "tps5430": _NamedChip("TPS5430 (TI SO PowerPAD-8, pad 9 GND)", 9, ti_powerpad_so8),
    "tps5450": _NamedChip("TPS5450 (TI SO PowerPAD-8, pad 9 GND)", 9, ti_powerpad_so8),
}

#: Part numbers a stem above would match that are a *different* package.
#: Refused by name rather than drawn as the stem's pattern.
_CHIP_REFUSALS: dict[str, str] = {
    "pca9685bs": "the PCA9685BS is the HVQFN-28 package, which is not drawn; "
    "use the PCA9685PW (TSSOP-28)",
}


def _normalised(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower())


def _named_chip_footprint(
    name: str, pin_count: int, nets: dict[str, str]
) -> Footprint | None:
    """The part-number rule, or None when the name names no known chip."""
    normalised = _normalised(name)
    for key, reason in _CHIP_REFUSALS.items():
        if key in normalised:
            raise UnsupportedPackage(f"{name!r}: {reason}.")
    for key in sorted(_NAMED_CHIPS, key=len, reverse=True):
        if key not in normalised:
            continue
        chip = _NAMED_CHIPS[key]
        if pin_count != chip.pin_count:
            raise UnsupportedPackage(
                f"{name!r} is the {chip.label}, which has {chip.pin_count} "
                f"pins, but the circuit declares {pin_count} (highest pin "
                f"number). Number its pins as the part's own pinout does."
            )
        return chip.build(nets)
    return None


def _module_key(name: str) -> str | None:
    """The module registry key this device name names, if any.

    Names arrive decorated ("U1_ARDUINO_NANO"), so the match is a token
    search over a normalised name rather than an equality test.
    """
    normalised = "".join(c if c.isalnum() else "_" for c in name.lower())
    for key in _MODULE_PACKAGES:
        if key in normalised:
            return key
    return None


#: The kinds whose land pattern is chosen by *name*. One tuple rather than a
#: literal repeated at each branch, so a kind added to the dispatch table below
#: and forgotten at the ``_footprint_for_device`` branch cannot happen -- which
#: would refuse every board carrying the new part while the table advertised it.
_NAMED_KINDS = ("connector", "battery", "switch", "testpoint")


def _named_package(
    kind: str, name: str, package: str | None, pin_count: int, nets: dict[str, str]
) -> Footprint:
    """Draw a connector or battery holder from its named land pattern.

    Split out because these two kinds are selected by *name*, not pin count:
    a 2-pin JST and a 2-pin screw terminal share a number and nothing else,
    exactly as with the plug-in modules above.
    """
    known, build = {
        "connector": (CONNECTOR_PACKAGES, connector),
        "battery": (BATTERY_PACKAGES, battery_holder),
        "switch": (SWITCH_PACKAGES, switch),
        "testpoint": (TESTPOINT_PACKAGES, probe_point),
    }[kind]
    if package is None:
        raise UnsupportedPackage(
            f"{kind} {name!r} names no package; its land pattern cannot be "
            f"guessed from the pin count. Supported: {sorted(known)}."
        )
    if kind == "connector" and package not in known:
        library = _library_connector(package, pin_count, nets)
        if library is not None:
            return library
    if package not in known:
        raise UnsupportedPackage(
            f"No {kind} land pattern named {package!r} (for {name!r}). "
            f"Supported: {sorted(known)}."
        )
    # A count *above* the package's is the module rule again: the name says
    # which part this is, and a circuit declaring more pins than it has means
    # one of the two is wrong. Fewer is legitimate -- a USB-C power receptacle
    # wired for VBUS and GND alone leaves its other pins unconnected.
    if pin_count > known[package]:
        raise UnsupportedPackage(
            f"{name!r} is a {package} with {known[package]} pins, but the "
            f"circuit declares {pin_count}. Refusing to place a connector on "
            f"a pattern that does not match its pinout."
        )
    try:
        return build(package, nets)
    except ValueError as exc:
        # ``footprints`` raises plain ``ValueError`` for an unknown package so
        # it need not import this module; converted here so every caller sees
        # the one refusal type. The membership test above should already have
        # caught it -- this is the belt for a table that drifts.
        raise UnsupportedPackage(str(exc)) from exc


def _library_connector(
    package: str, pin_count: int, nets: dict[str, str]
) -> Footprint | None:
    """A connector family or KiCad id (``kicadlib.connectors``) as a footprint.

    Raises when the family is known but KiCad has no part with that many pins,
    so the refusal names the family rather than the fixed package list.
    """
    from . import kicadlib
    from .kicadlib.connectors import is_connector_spec, resolve

    if not is_connector_spec(package):
        return None
    if not kicadlib.enabled():
        raise UnsupportedPackage(
            f"connector package {package!r} is a KiCad library family, and "
            f"KiCad's libraries are not available here; use one of "
            f"{sorted(CONNECTOR_PACKAGES)}."
        )
    found = resolve(package, pin_count)
    if found is None:
        raise UnsupportedPackage(
            f"no installed KiCad footprint for connector {package!r} with "
            f"{pin_count} pins; write the count the part really has, e.g. "
            f"{package.split('_')[0]}_4P."
        )
    if pin_count > found.pins:
        raise UnsupportedPackage(
            f"{package!r} resolves to {found.lib_id} with {found.pins} pins, but the "
            f"circuit declares pin {pin_count}."
        )
    return _library_footprint_by_id(found.lib_id, nets)


def _footprint_for_device(
    name: str,
    pin_count: int,
    nets: dict[str, str],
    *,
    kind: str = "ic",
    package: str | None = None,
    symbol: str | None = None,
) -> Footprint:
    """Pick a package from the device's kind, then its name, then the pin count.

    Deliberately conservative: it covers the shapes this pipeline generates and
    raises otherwise. Guessing a land pattern is how boards come back dead.

    ``kind`` is consulted first and that ordering is the whole point. While the
    pin count came first, a 2-pin power connector fell through to the SOIC rule,
    was padded to 4 pins, and was drawn as a surface-mount chip: the board had
    no power input, the schematic claimed one, and nothing raised.
    """
    if kind in _NAMED_KINDS:
        return _named_package(kind, name, package, pin_count, nets)
    if kind != "ic":
        raise UnsupportedPackage(
            f"device {name!r} has unknown kind {kind!r}; known kinds are "
            f"'ic' and {sorted(_NAMED_KINDS)}."
        )
    if package is not None:
        raise UnsupportedPackage(
            f"device {name!r} is an 'ic' but names package {package!r}; an "
            f"IC's land pattern follows from its pin count."
        )
    key = _module_key(name)
    if key is not None:
        module = _MODULE_PACKAGES[key]
        if pin_count != module.pin_count:
            # The name says which land pattern this is; a count that disagrees
            # means one of the two is wrong, and building either would put
            # copper where the part has no pins.
            raise UnsupportedPackage(
                f"{name!r} matches the {module.label} land pattern, which has "
                f"{module.pin_count} pins, but the circuit declares "
                f"{pin_count}. Refusing to place a module on a pattern that "
                f"does not match its pinout."
            )
        return dual_row_header(
            module.pin_count,
            row_spacing_mm=module.row_spacing_mm,
            body_w_mm=module.body_w_mm,
            body_h_mm=module.body_h_mm,
            nets=nets,
        )
    named = _named_chip_footprint(name, pin_count, nets)
    if named is not None:
        return named
    library = _library_footprint(symbol, nets)
    if library is not None:
        return library
    if pin_count == 3:
        return sot223(nets)
    if pin_count in SOIC_PINS:
        return soic(pin_count, nets=nets)
    if pin_count in LQFP_BODY_MM:
        return lqfp(pin_count, body_mm=LQFP_BODY_MM[pin_count], nets=nets)
    raise UnsupportedPackage(
        f"No package rule for {name!r} with {pin_count} pins. Supported: "
        f"{supported_packages_text()}."
    )


def _library_footprint(symbol: str | None, nets: dict[str, str]) -> Footprint | None:
    """The symbol's default KiCad footprint with ``nets`` on its pads, or None.

    Only for a device :func:`silkscreen.kicadlib.resolve.apply_library` bound
    to a library symbol, and only while the library is enabled. A symbol with no
    default footprint, or a footprint not installed, is None and the pin-count
    rule decides as before -- never a guessed land pattern.
    """
    if not symbol:
        return None
    from . import kicadlib

    index = kicadlib.library_index()
    entry = index.get(symbol) if index is not None else None
    if entry is None or not entry.footprint:
        return None
    return _library_footprint_by_id(entry.footprint, nets)


def _library_footprint_by_id(lib_id: str, nets: dict[str, str]) -> Footprint | None:
    try:
        loaded = _load_library_footprint(lib_id)
    except (OSError, ValueError):
        return None
    base = loaded.footprint
    return Footprint(
        name=base.name,
        pads=[replace(pad, net=_net_for(nets, pad.number)) for pad in base.pads],
        courtyard_w_nm=base.courtyard_w_nm,
        courtyard_h_nm=base.courtyard_h_nm,
        body_w_nm=base.body_w_nm,
        body_h_nm=base.body_h_nm,
        description=base.description,
        library=loaded,
    )


def _library_footprint_block(
    part: PlacedPart,
    anchor_x: int,
    anchor_y: int,
    net_index: dict[str, int],
    filled_nets: frozenset[str] = frozenset(),
) -> str:
    """One library footprint, placed, exactly as KiCad would embed it.

    KiCad's "Update PCB from Schematic" copies the library footprint into the
    board; this does the same from :class:`silkscreen.kicadlib.LibraryFootprint`
    text: every pad shape, silkscreen and fab line and the 3D model stay the
    library's, and only what placement decides is written -- position (the
    courtyard-centred anchor plus the recorded offset back to the library
    origin), reference, value and each pad's net (a no-connect pad takes
    KiCad's own ``unconnected-(...)`` name). Unrotated, top-side parts only;
    the builder does not rotate library parts.
    """
    from kiutils.footprint import Footprint as KiFootprint
    from kiutils.items.common import Net, Position
    from kiutils.utils.sexpr import parse_sexp

    fp = part.footprint
    lib = fp.library
    kfp = KiFootprint.from_sexpr(parse_sexp(lib.text))
    library, _, entry = lib.lib_id.partition(":")
    kfp.libraryNickname = library
    kfp.entryName = entry
    kfp.version = None
    kfp.generator = None
    kfp.tedit = None
    kfp.tstamp = _uuid(part.ref)
    kfp.layer = "F.Cu"
    ox, oy = lib.origin_offset_nm
    kfp.position = Position(
        X=(anchor_x + ox) / NM_PER_MM, Y=(anchor_y + oy) / NM_PER_MM
    )
    kfp.properties["Reference"] = part.ref
    kfp.properties["Value"] = part.value
    nets_by_pad = {pad.number: pad.net for pad in fp.pads}
    for pad in kfp.pads:
        name = nets_by_pad.get(str(pad.number)) or part.no_connects.get(
            str(pad.number), ""
        )
        pad.net = Net(number=net_index[name], name=name) if name else None
        # The same solid pour connection the generated pads get
        # (:func:`emit_kicad_pcb`), for the same reason.
        if name in filled_nets and pad.type == "smd":
            pad.zoneConnect = 2
    return "  " + kfp.to_sexpr(indent=2).strip()


def footprint_lib_id(part: PlacedPart) -> str:
    """The ``Lib:Name`` the board and the schematic both call this footprint.

    One definition because two places write it: the board's footprint and the
    schematic symbol's ``Footprint`` field. When they disagree, schematic
    parity reports ``footprint_symbol_mismatch`` -- measured 2026-09-14 on the
    first board with KiCad library footprints, whose symbols still said
    ``silkscreen:SOT-223-3_TabPin2``.
    """
    library = part.footprint.library
    if library is not None:
        return library.lib_id
    return f"silkscreen:{part.footprint.name}"


def _net_for(nets: dict[str, str], number: str) -> str:
    return (nets or {}).get(str(number), "")


@functools.lru_cache(maxsize=512)
def _load_library_footprint(lib_id: str):
    from .kicadlib import load_footprint

    return load_footprint(lib_id)


#: The pin counts each land-pattern generator actually covers. The prompt
#: text and the refusal message are both rendered from these, so a package
#: the builder does not have cannot be advertised to the model.
SOIC_PINS = (4, 6, 8, 10, 14, 16, 20, 24, 28)
LQFP_BODY_MM = {32: 5.0, 44: 10.0, 48: 7.0, 64: 10.0, 100: 14.0, 144: 20.0}


def supported_packages_text() -> str:
    """The package rule, in words, for prompts and error messages.

    One string in one place: the propose prompt tells the model what it may
    pick from, and the refusal above says the same thing, so the two cannot
    drift apart and leave the model designing against a list the builder no
    longer honours.
    """
    # Rendered from the same tuples the builder branches on. The old string
    # said "4-28 pins even (SOIC)", which advertises 12, 18, 22 and 26 --
    # none of which `_footprint_for_device` accepts. The whole point of this
    # function is that the prompt and the refusal cannot drift, and they had.
    soic = "/".join(str(n) for n in SOIC_PINS)
    lqfp_pins = "/".join(str(n) for n in sorted(LQFP_BODY_MM))
    # Connectors and batteries are listed by *name*, rendered straight from the
    # tables the builder branches on for the same anti-drift reason -- and
    # deliberately without their pin counts, which are implied by the name and
    # would otherwise read as pin counts an IC could be built at.
    return (
        f"kind 'ic': 3 pins (SOT-223), {soic} pins (SOIC), {lqfp_pins} pins "
        f"(LQFP), and the named modules {sorted(_MODULE_PACKAGES)}. "
        f"Chips drawn by PART NUMBER rather than pin count (the part number "
        f"must appear in the device key, and the pin count must match): "
        + "; ".join(
            f"{c.label} for a key containing {k!r}, {c.pin_count} pins"
            for k, c in sorted(_NAMED_CHIPS.items())
        )
        + ". "
        f"kind 'connector', by package name: {sorted(CONNECTOR_PACKAGES)}, or "
        f"(KiCad library) a real connector family written FAMILY_<N>P with N the "
        f"part's pin count: {', '.join(_connector_families())}. "
        f"kind 'battery', by package name: {sorted(BATTERY_PACKAGES)}. "
        f"kind 'switch', by package name: {sorted(SWITCH_PACKAGES)}. "
        f"kind 'testpoint', by package name: {sorted(TESTPOINT_PACKAGES)}"
    )


def package_errors(spec: CircuitSpec) -> list[str]:
    """Every device in ``spec`` that no land-pattern rule covers, as messages.

    This is the footprint rule run early, at proposal time, so an unsupported
    package goes back to the model as one more repair item instead of surfacing
    after the proposal was accepted, where nothing can fix it. Empty means
    :func:`build_board` will not raise :class:`UnsupportedPackage`.
    """
    errors: list[str] = []
    for device in spec.devices:
        nets = _device_pin_nets(spec, device)
        try:
            fp = _footprint_for_device(
                device.name,
                _package_pin_count(device.pins),
                nets,
                kind=device.kind,
                package=device.package,
                symbol=device.symbol,
            )
        except UnsupportedPackage as exc:
            errors.append(str(exc))
            continue
        errors.extend(_pad_errors(device, fp))
    for passive in spec.passives:
        errors.extend(_polarity_errors(spec, passive))
    return errors


def _passive_legs(spec: CircuitSpec, name: str) -> dict[str, str]:
    legs = {"1": "", "2": ""}
    for conn in spec.connections:
        for endpoint in conn.endpoints:
            part, _, leg = endpoint.rpartition(".")
            if part == name and leg in legs:
                legs[leg] = conn.net
    return legs


def _is_ground(net: str) -> bool:
    lowered = net.lower().lstrip("+-/")
    return lowered.startswith(("gnd", "vss", "agnd", "dgnd", "pgnd"))


def _polarity_errors(spec: CircuitSpec, passive) -> list[str]:
    """A capacitor drawn as an electrolytic with its + leg on ground.

    From 100 uF up ``for_passive`` draws an aluminium electrolytic, whose pad 1
    is +. The IR's capacitor legs carry no polarity, so the model's choice of
    which leg is "1" becomes the can's orientation on the board; an
    electrolytic fitted reversed across a 5 V servo rail vents. Refused in
    words so the repair round swaps the legs, never flipped silently (that
    would put the schematic's pin 1 on a different net from the board's pad 1,
    which KiCad's parity check reports).
    """
    if passive.type.value != "capacitor":
        return []
    fp = for_passive(passive.type.value, passive.value)
    if not fp.polarised:
        return []
    legs = _passive_legs(spec, passive.name)
    if _is_ground(legs["1"]) and not _is_ground(legs["2"]):
        return [
            f"capacitor {passive.name!r} ({passive.value}) is drawn as an "
            f"aluminium electrolytic, whose leg 1 is + and leg 2 is -; the "
            f"circuit puts leg 1 on {legs['1']} and leg 2 on {legs['2']}. "
            f"Put leg 1 on the positive net and leg 2 on ground."
        ]
    return []


def _device_pin_nets(spec: CircuitSpec, device) -> dict[str, str]:
    """``{pad number: net}`` for one device, from the spec's connections."""
    pin_nets: dict[str, str] = {}
    for conn in spec.connections:
        for endpoint in conn.endpoints:
            part, _, pin_name = endpoint.rpartition(".")
            if part == device.name:
                number = device.pins.get(pin_name)
                if number:
                    pin_nets[str(number)] = conn.net
    return pin_nets


#: Pads a connector ties together by function, per package. KiCad's own
#: ``Connector:USB_C_Receptacle_USB2.0_16P`` symbol gives every contact in a
#: group one pin name (four ``GND``, four ``VBUS``, two ``D+``, two ``D-``),
#: so a schematic that wires one of them has wired all of them. The IR lets the
#: model name pins one by one, and on 2026-09-14 a model declared GND on A1/B12
#: and VBUS on A4/B9 only: the other GND and VBUS lands (B1/A12, B4/A9) shipped
#: with no net -- half the receptacle unpowered, 33 parity conflicts and two
#: solder-mask bridges on the demo board. The groups follow the pin functions
#: ``footprints.py`` documents for each pattern.
TIED_PADS: dict[str, tuple[tuple[str, ...], ...]] = {
    "USB_C_Receptacle_USB2.0_16P": (
        ("A1", "A12", "B1", "B12"),  # GND
        ("A4", "A9", "B4", "B9"),  # VBUS
        ("A6", "B6"),  # D+ (a USB 2.0 device ties both contacts)
        ("A7", "B7"),  # D-
    ),
    "USB_C_Receptacle_Power": (
        ("A12", "B12"),  # GND
        ("A9", "B9"),  # VBUS
    ),
    # Named chips, keyed as in _NAMED_CHIPS: KiCad's RF_Module:ESP32-WROOM-32E
    # symbol stacks GND as one pin on pads [1,15,38,39] (the slug is 39), so a
    # circuit that wires "GND" wires all four, as KiCad's own netlist would.
    "esp32_wroom_32e": (("1", "15", "38", "39"),),
}


def _tie_key(device) -> str:
    """The :data:`TIED_PADS` key for a device: its package, or for an IC the
    named-chip stem its part number matches (refused variants match nothing)."""
    if device.package:
        return device.package
    normalised = _normalised(device.name)
    if any(key in normalised for key in _CHIP_REFUSALS):
        return ""
    stems = sorted(_NAMED_CHIPS, key=len, reverse=True)
    return next((stem for stem in stems if stem in normalised), "")


def tie_package_pins(spec: CircuitSpec) -> tuple[CircuitSpec, list[str]]:
    """Wire every contact of a tied group once any one of them is wired.

    Returns the completed spec and one note per pad it tied, so the change is
    reported rather than made quietly. A group whose wired members already
    disagree is left alone: that is a short :func:`_pad_errors` names, not
    something to guess a winner for. Runs on the accepted proposal, before
    either emitter, so the schematic and the board both carry the tie.
    """
    from dataclasses import replace

    notes: list[str] = []
    conns = {c.net: list(c.endpoints) for c in spec.connections}
    devices = []
    for device in spec.devices:
        groups = TIED_PADS.get(_tie_key(device))
        if not groups:
            devices.append(device)
            continue
        pins = dict(device.pins)
        name_of = {str(number): name for name, number in pins.items()}
        net_of_pin: dict[str, str] = {}
        for net, endpoints in conns.items():
            for endpoint in endpoints:
                part, _, pin = endpoint.rpartition(".")
                if part == device.name:
                    net_of_pin[pin] = net
        for group in groups:
            wired = {
                net_of_pin[name_of[n]]
                for n in group
                if n in name_of and name_of[n] in net_of_pin
            }
            if len(wired) != 1:
                continue
            (net,) = wired
            base = next(
                name_of[n].rstrip("0123456789_").rstrip("+-") or name_of[n]
                for n in group
                if n in name_of and name_of[n] in net_of_pin
            )
            for number in group:
                name = name_of.get(number)
                if name is None:
                    name = f"{base}_{number}"
                    while name in pins:
                        name += "_"
                    pins[name] = number
                    name_of[number] = name
                if name in net_of_pin:
                    continue
                conns[net].append(f"{device.name}.{name}")
                net_of_pin[name] = net
                notes.append(
                    f"{device.name} pad {number} tied to {net}: the "
                    f"{device.package} carries it on the same contact group"
                )
        devices.append(replace(device, pins=pins))
    if not notes:
        return spec, notes
    connections = [
        replace(c, endpoints=tuple(conns[c.net])) for c in spec.connections
    ]
    return replace(spec, devices=devices, connections=connections), notes


def _pad_errors(device, fp: Footprint) -> list[str]:
    """Pins the land pattern has no pad for, and stacked pads split across nets.

    A pin numbered for a pad the pattern does not have used to be dropped in
    silence: ``build_board`` looks the pad up by exact number, finds nothing,
    and the net simply never reaches the part. Measured 2026-09-13 on a
    robot-arm controller whose USB-C receptacle was declared with pins "1".."6"
    and D+/D- on a power-only part -- the board placed, the connector carried
    no copper, and the CH340's USB lines went nowhere.

    Pads that share a position but not a number (a USB-C receptacle's A1/B12,
    which are one land on the part) are one piece of copper, so the pins they
    carry must be on one net; anything else is a short the part itself makes.
    """
    errors: list[str] = []
    pads = {pad.number for pad in fp.pads}
    missing = sorted(
        f"{name}={number}"
        for name, number in device.pins.items()
        if str(number) not in pads
    )
    if missing:
        errors.append(
            f"{device.name!r} ({fp.name}) has no pad for pin(s) {missing}; its "
            f"pads are numbered {sorted(pads, key=_pad_sort_key)}. Number each "
            f"pin with the pad it lands on, and do not wire a signal to a part "
            f"that has no pin for it."
        )
    by_position: dict[tuple[int, int], set[str]] = {}
    for pad in fp.pads:
        by_position.setdefault((pad.x_nm, pad.y_nm), set()).add(pad.number)
    nets_of = {pad.number: pad.net for pad in fp.pads}
    for numbers in by_position.values():
        if len(numbers) < 2:
            continue
        nets = {nets_of[n] for n in numbers}
        if len(nets) > 1:
            errors.append(
                f"{device.name!r} ({fp.name}) pads {sorted(numbers)} are one "
                f"land on the part, so they must be on one net; the circuit "
                f"puts them on {sorted(n or '(unconnected)' for n in nets)}."
            )
    return errors


def _pad_sort_key(number: str) -> tuple[str, int]:
    head = number.rstrip("0123456789")
    tail = number[len(head):]
    return (head, int(tail) if tail else -1)


def _package_pin_count(pins: dict[str, str]) -> int:
    """Highest physical pin number, which is the package's pin count."""
    numbers = []
    for value in pins.values():
        try:
            numbers.append(int(str(value)))
        except (TypeError, ValueError):
            continue
    return max(numbers) if numbers else len(pins)


_POWER_TOKENS = ("gnd", "vss", "vcc", "vdd", "vbus", "vin", "vout", "agnd", "dgnd")


def _is_power(net: str) -> bool:
    lowered = net.lower().lstrip("+-/")
    return any(lowered.startswith(t) for t in _POWER_TOKENS)


#: Placer weight of the pull between a decoupling capacitor's rail leg and the
#: IC pin it serves, against 1.0 for a signal net and 0.25 for a whole rail.
#: The ground leg is pulled equally: what matters is the loop the cap closes,
#: and pulling only the rail leg just moved the length onto ground. Swept
#: 2026-09-14 over (rail, ground) = (4,1), (2,2), (4,4), (8,8) on the regulator,
#: via and two-chip fixtures; (2,2) cut total loop length 15-18 % and, on the
#: regulator, routed copper too (43.0 -> 39.5 mm); heavier weights bought no
#: shorter loop and cost copper.
DECOUPLING_WEIGHT = 2.0
DECOUPLING_GROUND_WEIGHT = 2.0


@dataclass(frozen=True)
class Decoupling:
    """One capacitor serving one IC supply pin (endpoint strings, spec names)."""

    cap: str
    rail_endpoint: str  # "C1.1"
    ground_endpoint: str  # "C1.2"
    pin_endpoint: str  # "U1.VDD"
    ground_pin_endpoint: str | None  # "U1.GND", when the IC has one


def decoupling_caps(spec: CircuitSpec) -> list[Decoupling]:
    """Each decoupling capacitor and the IC supply pin it belongs next to.

    The rule is tscircuit's (``rfcs/2026-07-31-automatic-decoupling-capacitor-
    detection-and-enforcement.md``): a capacitor joined between an IC's power
    pin and ground is a decoupling cap, and it belongs close to *that pin*.
    The placer only sees nets, and a rail is one down-weighted bounding box
    over every pad on it -- the cap can sit anywhere inside the box at no cost,
    which is how C1 on the ESP32 demo board ended up across the board from the
    module's 3V3 pin. So each detected cap gets its own two-terminal net to
    the pin.

    Only ``kind == "ic"`` devices count (a connector's VBUS pin is a source,
    not a load), and when one rail feeds several IC pins the caps are dealt
    out in turn so each pin gets its share rather than all of them crowding
    the first.
    """
    from .netlist import PassiveType
    from .schematic import net_class

    devices = {d.name: d for d in spec.devices}
    caps = {p.name for p in spec.passives if p.type is PassiveType.CAPACITOR}
    net_of: dict[str, str] = {}
    for conn in spec.connections:
        for endpoint in conn.endpoints:
            net_of[endpoint] = conn.net

    def ic_pins(net: str) -> list[str]:
        conn = next(c for c in spec.connections if c.net == net)
        out = []
        for endpoint in conn.endpoints:
            part, _, _ = endpoint.rpartition(".")
            device = devices.get(part)
            if device is not None and device.kind == "ic":
                out.append(endpoint)
        return out

    served: dict[str, int] = {}
    found: list[Decoupling] = []
    for cap in sorted(caps):
        legs = [(f"{cap}.{n}", net_of.get(f"{cap}.{n}")) for n in ("1", "2")]
        classes = [net_class(net) if net else None for _, net in legs]
        if sorted(map(str, classes)) != ["ground", "rail"]:
            continue
        rail_leg, rail = legs[classes.index("rail")]
        ground_leg, ground = legs[classes.index("ground")]
        pins = ic_pins(rail)
        if not pins:
            continue
        pin = min(pins, key=lambda e: (served.get(e, 0), pins.index(e)))
        served[pin] = served.get(pin, 0) + 1
        part = pin.rpartition(".")[0]
        ground_pin = next(
            (e for e in ic_pins(ground) if e.rpartition(".")[0] == part), None
        )
        found.append(Decoupling(cap, rail_leg, ground_leg, pin, ground_pin))
    return found


def solver_pad_offset(fp, pad, reserve) -> tuple[int, int]:
    """A pad as an offset from the bottom-left of the box the solver places.

    The one place `build_board` crosses frames, factored out so it can be
    checked against independent arithmetic rather than only through a whole
    solve.

    X runs the same way in both frames, so the pad's own offset is added. Y
    does not: ``pad.y_nm`` is the footprint frame's **Y-down** offset from the
    package centre (see footprints.py's "Frame" note), while the solver
    measures Y **up** from the reserved box's bottom-left corner. So the
    half-extent minus the pad, never plus it.

    Adding it mirrored every package vertically before CP-SAT ever saw it --
    pin 1 of a SOIC-8 was handed over as though it sat at the bottom of the
    part. Nothing overlapped and no test failed, because the geometry that is
    *written* is converted correctly elsewhere (``kicad.py``'s ``max_y - pad_y``
    and ``board_pads``' single flip). What was wrong was the netlist the
    placer optimised against -- and so the placement, and so the reported
    ``wirelength_nm``.
    """
    ox = (pad.x_nm if pad else 0) + fp.courtyard_w_nm + reserve.side
    oy = (fp.courtyard_h_nm - (pad.y_nm if pad else 0)) + reserve.bottom
    return ox, oy


#: Parts that only work at a board edge, and the side they must face with the
#: footprint unrotated, in the solver's Y-up frame.
#:
#: Connectors a cable plugs into from outside the board face their mouth. Both
#: patterns open toward KiCad +Y -- the USB-C receptacles' pads sit on the
#: -Y row and the shell runs to +Y (``footprints._USB_C16_PADS_MM``); the
#: barrel jack's pins are at the rear and its barrel at +Y
#: (``footprints._BARREL_PADS_MM``) -- and KiCad +Y is the solver's bottom
#: edge, which the enclosure calls the front. The 2026-09-14 demo board put
#: its USB-C receptacle in the middle of the board, mouth facing a resistor:
#: no plug could ever go in, and neither DRC nor parity can see that.
#:
#: A module with a PCB antenna faces its antenna. Espressif's ESP32 hardware
#: design guidelines put the WROOM antenna at the board edge with no copper
#: beneath it; the footprint's own docstring (``footprints.esp32_wroom_32e``)
#: notes the antenna is its y < 0 end -- KiCad -Y, the solver's top -- and
#: that nothing enforced it. On the demo board the module sat mid-board with
#: traces around its antenna. (The copper keep-out under the antenna is still
#: not enforced; the edge is.)
EDGE_FACING: dict[str, str] = {
    "USB_C_Receptacle_Power": "bottom",
    "USB_C_Receptacle_USB2.0_16P": "bottom",
    "Barrel_Jack_5.5x2.1mm": "bottom",
    "ESP32-WROOM-32E": "top",
}


def build_board(
    spec: CircuitSpec,
    *,
    clearance_nm: int = DEFAULT_CLEARANCE_NM,
    time_limit_s: float | None = 20.0,
    edge_refs: set[str] | None = None,
    rotatable_refs: set[str] | None = None,
    two_sided: bool = False,
) -> BoardResult:
    """Turn a validated circuit into footprints, nets and a placement.

    ``rotatable_refs`` names parts the solver may turn 90 degrees. Rotation is
    off by default because it only pays when a board is tight, and it costs
    determinism nothing either way.

    With ``two_sided``, passives may go on either side while ICs stay on top --
    an IC on the underside complicates assembly and rework for little area
    saved, whereas moving decoupling capacitors under their chip is standard
    practice and shortens the loop.
    """
    spec.validate()

    # The schematic emitter numbers parts from the same call, so R2 on the
    # drawing is R2 on the board. Numbering them separately would give two
    # self-consistent files that describe different circuits.
    ref_of = spec.assign_refs()

    # A ref that names nothing is a caller error, not a no-op -- the same
    # convention pack() and kicad.py already follow. Silently ignoring it means
    # a part the caller asked to rotate quietly does not, and the board looks
    # like the solver simply preferred it that way.
    known = set(ref_of.values())
    unknown = sorted(set(rotatable_refs or ()) - known)
    if unknown:
        raise ValueError(
            f"rotatable_refs names {unknown}, which are not on this board; "
            f"known refs: {sorted(known)}"
        )

    placed: list[PlacedPart] = []
    # How much room each part reserves beyond its courtyard. Devices are the
    # ICs; everything from the passives list is a chip passive.
    spacing_of: dict[str, int] = {}

    for device in spec.devices:
        pin_nets = _device_pin_nets(spec, device)
        fp = _footprint_for_device(
            device.name,
            _package_pin_count(device.pins),
            pin_nets,
            kind=device.kind,
            package=device.package,
            symbol=device.symbol,
        )
        pad_problems = _pad_errors(device, fp)
        if pad_problems:
            raise UnsupportedPackage(" ".join(pad_problems))
        ref = ref_of[device.name]
        pads = {pad.number for pad in fp.pads}
        no_connects = {
            str(number): kicad_unconnected_net(ref, name, str(number))
            for name, number in _schematic_pins(device)
            if str(number) not in pin_nets and str(number) in pads
        }
        placed.append(
            PlacedPart(
                ref=ref, footprint=fp, value=device.name, no_connects=no_connects
            )
        )
        spacing_of[ref] = IC_SPACING_NM

    for passive in spec.passives:
        legs = {"1": "", "2": ""}
        for conn in spec.connections:
            for endpoint in conn.endpoints:
                part, _, leg = endpoint.rpartition(".")
                if part == passive.name and leg in legs:
                    legs[leg] = conn.net
        fp = for_passive(
            passive.type.value, passive.value, net1=legs["1"], net2=legs["2"]
        )
        ref = ref_of[passive.name]
        placed.append(PlacedPart(ref=ref, footprint=fp, value=passive.value))
        spacing_of[ref] = PASSIVE_SPACING_NM

    # The solver places a box larger than the courtyard: the courtyard plus
    # the spacing on every side plus the designator's band on top. The solver
    # clearance then separates *boxes*, so courtyards end up spacing-plus-
    # clearance apart and the label above one part never reaches the next.
    # The courtyard's own position is recovered from the box after solving.
    reserve_of = {
        p.ref: _reserve_for(p.footprint, p.ref, spacing_of[p.ref]) for p in placed
    }
    parts = [
        Part(
            width_nm=p.footprint.courtyard_w_nm * 2 + 2 * reserve_of[p.ref].side,
            height_nm=(
                p.footprint.courtyard_h_nm * 2
                + reserve_of[p.ref].bottom
                + reserve_of[p.ref].top
            ),
            ref=p.ref,
            must_be_on_edge=p.ref in (edge_refs or set()),
            edge_side=(
                EDGE_FACING.get(p.footprint.name) or _library_edge_side(p.footprint)
            ),
            allow_rotation=p.ref in (rotatable_refs or set()),
            layer=Layer.EITHER if two_sided and p.ref.startswith(("C", "R"))
            else Layer.TOP,
        )
        for p in placed
    ]
    index_of = {p.ref: i for i, p in enumerate(placed)}

    def terminal(endpoint: str) -> tuple[int, tuple[int, int]] | None:
        part_name, _, pin = endpoint.rpartition(".")
        ref = ref_of.get(part_name)
        if ref is None:
            return None
        idx = index_of[ref]
        fp = placed[idx].footprint
        number = pin
        device = next((d for d in spec.devices if d.name == part_name), None)
        if device is not None:
            number = str(device.pins.get(pin, pin))
        pad = fp.pad_by_number(number)
        # Offsets are measured from the bottom-left corner of the box the
        # solver places, which is the courtyard's corner pushed out by the
        # reservation on that side.
        return idx, solver_pad_offset(fp, pad, reserve_of[ref])

    pack_nets: list[PackNet] = []
    for conn in spec.connections:
        terminals: list[tuple[int, tuple[int, int]]] = []
        seen: set[int] = set()
        for endpoint in conn.endpoints:
            term = terminal(endpoint)
            if term is None or term[0] in seen:
                continue
            seen.add(term[0])
            terminals.append(term)
        if len(terminals) >= 2:
            pack_nets.append(
                PackNet(
                    terminals=tuple(terminals),
                    name=conn.net,
                    weight=0.25 if _is_power(conn.net) else 1.0,
                )
            )
    # A rail's bounding box lets its decoupling caps sit anywhere inside it
    # for free; a pin-to-cap pair makes distance from the pin cost something.
    for dec in decoupling_caps(spec):
        pairs = [(dec.rail_endpoint, dec.pin_endpoint, DECOUPLING_WEIGHT)]
        if dec.ground_pin_endpoint is not None:
            pairs.append(
                (dec.ground_endpoint, dec.ground_pin_endpoint, DECOUPLING_GROUND_WEIGHT)
            )
        for a, b, weight in pairs:
            ta, tb = terminal(a), terminal(b)
            if ta is None or tb is None or ta[0] == tb[0]:
                continue
            pack_nets.append(
                PackNet(terminals=(ta, tb), name=f"decouple:{a}", weight=weight)
            )

    result = pack(
        parts, nets=pack_nets, clearance_nm=clearance_nm, time_limit_s=time_limit_s
    )
    by_ref: dict[str, Placement] = {p.ref: p for p in result.placements}
    for part in placed:
        placement = by_ref.get(part.ref)
        if placement:
            # The placement is the reserved box's bottom-left corner; the
            # part's own corner is inside it by the reservation. A rotated
            # box has turned with the part: pack maps a local (ox, oy) to
            # (H - oy, ox), which puts the top band on the left and the side
            # room underneath. That is the same turn rotate_offset applies
            # to the pads and the emitter applies to the label, so all three
            # agree on which side of the part the designator is on.
            reserve = reserve_of[part.ref]
            if placement.rotated:
                part.x_nm = placement.x_nm + reserve.top
                part.y_nm = placement.y_nm + reserve.side
            else:
                part.x_nm = placement.x_nm + reserve.side
                part.y_nm = placement.y_nm + reserve.bottom
            part.rotated = placement.rotated
            part.layer = placement.layer
            # Not flushed to the outline yet (_flush_to_edge): measured
            # 2026-09-14, a flush USB-C receptacle made the default case fail
            # `headroom` (the lid lip lands over the connector) and skewed the
            # enclosure's hole mapping (the keep-out overhangs the outline).
            # Flush has to land together with enclosure support for it.

    from .signals import diff_pairs

    return BoardResult(
        parts=placed,
        nets=[c.net for c in spec.connections],
        width_nm=result.board_width_nm,
        height_nm=result.board_height_nm,
        solver_status=result.status.value,
        wirelength_nm=result.wirelength_nm,
        warnings=list(result.warnings),
        priority_nets=[
            n for pair in diff_pairs(spec) for n in (pair.positive, pair.negative)
        ],
    )


#: How far silkscreen stops short of an edge-facing part's mating side, in nm.
EDGE_SILK_GAP_NM = mm(0.5)


def part_silk_segments(part: PlacedPart) -> list[tuple[int, int, int, int]]:
    """The part's body outline strokes, minus its mating edge when flush.

    An edge-facing part (:data:`EDGE_FACING`) now meets the board outline with
    its body, so the outline's stroke on that side would lie on -- or past --
    the board edge: KiCad reports "Silkscreen clipped by board edge". KiCad's
    own USB-C receptacle footprints draw no silkscreen across the mouth, so
    that edge is dropped and the two side strokes end ``EDGE_SILK_GAP_NM``
    short of it. Footprint-local and KiCad Y-down, like
    :func:`footprints.silk_segments`; both emitters (this module's
    ``.kicad_pcb`` and ``fab.py``'s legend Gerber) draw what this returns.
    """
    segments = silk_segments(part.footprint)
    side = EDGE_FACING.get(part.footprint.name)
    if side is None or part.rotated:
        return segments
    fp = part.footprint
    # Solver side -> the local axis and sign of the mating edge (Y-down).
    axis, edge = {
        "bottom": ("y", fp.body_h_nm),
        "top": ("y", -fp.body_h_nm),
        "left": ("x", -fp.body_w_nm),
        "right": ("x", fp.body_w_nm),
    }[side]
    limit = abs(edge) - EDGE_SILK_GAP_NM
    out = []
    for x0, y0, x1, y1 in segments:
        a0, a1 = (y0, y1) if axis == "y" else (x0, x1)
        if a0 == a1 == edge:
            continue  # the stroke along the mating edge itself
        if a0 != a1:  # a side stroke running towards the edge: stop short
            sign = 1 if edge > 0 else -1
            a0 = sign * min(sign * a0, limit)
            a1 = sign * min(sign * a1, limit)
            if a0 == a1:
                continue
            if axis == "y":
                y0, y1 = a0, a1
            else:
                x0, x1 = a0, a1
        out.append((x0, y0, x1, y1))
    return out


def _flush_to_edge(part: PlacedPart, side: str, width: int, height: int) -> None:
    """Move an edge-facing part outward until its *body* meets the outline.

    The placer puts the part's reserved box against the packing rectangle, but
    the outline is ``DEFAULT_BOARD_MARGIN_NM`` further out, the reserve adds
    room on that side, and the courtyard itself runs past the body (a USB-C
    receptacle's courtyard is 1.08 mm beyond its shell). Together that left
    about 3 mm of board in front of the receptacle's mouth -- enough for a
    plug's overmold, which sits below the top of the board, to hit the lip.
    Only the edge-facing part enters the margin, and only outward: nothing
    else was placed there, so nothing can collide. Measured from the body
    (``Footprint.body_*``), which is where the mating face is.
    """
    fp = part.footprint
    m = DEFAULT_BOARD_MARGIN_NM
    ch, bh = fp.courtyard_h_nm, fp.body_h_nm
    cw, bw = fp.courtyard_w_nm, fp.body_w_nm
    if side == "bottom":
        part.y_nm = min(part.y_nm, -m - (ch - bh))
    elif side == "top":
        part.y_nm = max(part.y_nm, height + m - ch - bh)
    elif side == "left":
        part.x_nm = min(part.x_nm, -m - (cw - bw))
    elif side == "right":
        part.x_nm = max(part.x_nm, width + m - cw - bw)


def placed_half_extents(part: PlacedPart) -> tuple[int, int]:
    """The part's courtyard half-extents **as placed**, not as drawn.

    A 90-degree rotation swaps them. The placer reserves the swapped box and
    reports its bottom-left corner, so anything deriving a position from that
    corner has to swap too. Not swapping is the recorded bug this function
    exists to end: the anchor came out short by exactly ``(ch-cw, cw-ch)``, the
    file still parsed, the courtyard still drew, and only the pads were wrong.
    """
    fp = part.footprint
    if part.rotated:
        return fp.courtyard_h_nm, fp.courtyard_w_nm
    return fp.courtyard_w_nm, fp.courtyard_h_nm


def part_anchor(part: PlacedPart) -> tuple[int, int]:
    """The footprint anchor in the solver's Y-up frame.

    One definition, used by the emitter and by the router's pad geometry. Two
    copies of this arithmetic is how the copper and the footprints ended up in
    different places while both looked right on their own.
    """
    half_w, half_h = placed_half_extents(part)
    return part.x_nm + half_w, part.y_nm + half_h


def rotate_offset(x_nm: int, y_nm: int, *, rotated: bool) -> tuple[int, int]:
    """A footprint-local offset mapped into the board frame.

    Only 0 and 90 degrees occur, and both are exact in integer nanometres, so
    this stays integer arithmetic rather than going through sin/cos. KiCad
    turns a footprint counter-clockwise on screen and screen Y points down, so
    the board-frame image of a local point is a rotation by ``-angle``: at 90
    degrees, ``(x, y) -> (y, -x)``. That is the same mapping
    :func:`silkscreen.kicad._rotate_mm` applies when reading a board back, and
    the two must agree or a board does not survive a round trip.
    """
    if not rotated:
        return x_nm, y_nm
    return y_nm, -x_nm


#: Copper keep-outs a footprint needs beyond its pads, footprint-local and in
#: the pads' frame (anchor-centred, KiCad Y-down): ``(centre x, centre y,
#: width, height)`` in nm. The ESP32-WROOM-32E antenna is the module's y < 0
#: end (``footprints.esp32_wroom_32e``): the whole 18 mm width, from the body's
#: top edge (y -12.75) down to just above the first castellated pad (pin 1 at
#: y -5.26, 0.9 mm tall). Espressif's hardware design guidelines want no
#: copper under the antenna on any layer.
ANTENNA_KEEPOUTS_NM: dict[str, tuple[int, int, int, int]] = {
    "ESP32-WROOM-32E": (0, -mm(9.35), mm(18.0), mm(6.8)),
}


def board_keepouts(board: BoardResult) -> list[RoutePad]:
    """Copper-free regions as netless through-hole obstacles for the router.

    A netless pad is an obstacle and nothing else to :func:`routing.route`, and
    a through-hole one is reserved on every copper layer with no via allowed in
    it -- exactly the antenna rule. Kept apart from :func:`board_pads`, which
    lists real pads that other code measures one by one.
    """
    keepouts: list[RoutePad] = []
    for part in board.parts:
        box = ANTENNA_KEEPOUTS_NM.get(part.footprint.name)
        if box is None:
            continue
        cx, cy, w, h = box
        anchor_x, anchor_y = part_anchor(part)
        ox, oy = rotate_offset(cx, cy, rotated=part.rotated)
        w, h = (h, w) if part.rotated else (w, h)
        keepouts.append(
            RoutePad(
                net="",
                x_nm=anchor_x + ox,
                y_nm=anchor_y - oy,
                w_nm=w,
                h_nm=h,
                layer=part.layer,
                through_hole=True,
                ref=part.ref,
                number="antenna keep-out",
            )
        )
    return keepouts


def board_pads(board: BoardResult) -> list[RoutePad]:
    """Every pad on the board, absolute, in the solver's Y-up frame.

    The placer hands back a courtyard's bottom-left corner with Y up; a pad
    offset inside a footprint is measured from the anchor with Y **down**,
    because that is the frame it is written to the file in. Composing the two
    is the single place those frames meet outside the emitter, and getting it
    wrong is the silent-geometry bug class: the router would lay copper to
    coordinates no pad occupies, the run would report success, and the board
    would come back with tracks ending in bare laminate.

    A rotated part turns its pads with it: the offset is mapped through
    :func:`rotate_offset` and the pad's own width and height swap, because a
    1.95 x 0.6 mm pad laid on its side is 0.6 x 1.95 mm of copper for the
    router to keep clear of.
    """
    pads: list[RoutePad] = []
    for part in board.parts:
        anchor_x, anchor_y = part_anchor(part)
        for pad in part.footprint.pads:
            ox, oy = rotate_offset(pad.x_nm, pad.y_nm, rotated=part.rotated)
            w_nm, h_nm = (
                (pad.h_nm, pad.w_nm) if part.rotated else (pad.w_nm, pad.h_nm)
            )
            pads.append(
                RoutePad(
                    net=pad.net,
                    x_nm=anchor_x + ox,
                    # The one Y flip: pad offsets are written Y-down, the
                    # router works Y-up.
                    y_nm=anchor_y - oy,
                    w_nm=w_nm,
                    h_nm=h_nm,
                    layer=part.layer,
                    # A plated hole is copper on both faces whichever side
                    # the part sits on; the emitter writes it on "*.Cu" and
                    # the router has to keep both layers clear of it.
                    through_hole=pad.is_tht,
                    ref=part.ref,
                    number=pad.number,
                )
            )
    return pads


def route_board(
    board: BoardResult,
    *,
    margin_nm: int = DEFAULT_BOARD_MARGIN_NM,
    two_layer: bool = True,
    ground_fill: bool = True,
    **kwargs,
) -> RouteResult:
    """Route ``board`` in place, filling its tracks, vias and unrouted nets.

    ``ground_fill`` (the default) gives every ground-class net a copper pour
    on both layers instead of tracks -- what nearly every two-layer board
    does, and what the 2026-09-15 ESP32 demo showed the maze router cannot
    do on its own (GND ran out of search budget behind 36 signal nets). The
    pour is drawn by :func:`emit_kicad_pcb` and filled by KiCad
    (:func:`write_board`); the router treats those nets as done and lists
    them in ``RouteResult.filled``.

    Placement and routing are separate calls, not one, so the placed board can
    be written out and inspected before any copper exists. That is the step the
    pipeline used to skip past.

    Rotated parts route like any other. They used to abort here, because the
    emitter misplaced a rotated footprint's anchor and routing to those
    coordinates would have turned a latent placement bug into copper landing on
    bare laminate. That bug is fixed: :func:`part_anchor` is now the one
    definition of where a part sits, and the emitter and this function both
    read it.
    """
    ground = (
        [n for n in dict.fromkeys(board.nets) if n and _is_ground(n)]
        if ground_fill
        else []
    )
    obstacles = board_pads(board) + board_keepouts(board)
    # Fan the ground pads out to vias *before* any signal is routed, and hand
    # the fan-outs to the router as copper it must keep clear of. That is
    # FreeRouting's order (``AutorouteBatchLoop.java`` calls
    # ``BatchFanout.fanoutBoard`` before the routing passes, 60336be; GPL,
    # read for the design only): fanned out after routing, a ground pad the
    # signals had boxed in had nowhere left to put its via (measured
    # 2026-09-16, two isolated pour islands on the ESP32 eval board).
    fanout_vias: list[Via] = []
    fanout_tracks: list[Track] = []
    if ground:
        fanout_vias, fanout_tracks = plan_fanout(board, ground[0], obstacles)
    nets = board.priority_nets
    pairs = list(zip(nets[0::2], nets[1::2], strict=False))

    bounds = {
        "min_x_nm": -margin_nm,
        "min_y_nm": -margin_nm,
        "max_x_nm": board.width_nm + margin_nm,
        "max_y_nm": board.height_nm + margin_nm,
    }

    def attempt(flip: bool) -> tuple[RouteResult, list[Track], list[CoupledRoute]]:
        joins, seen_by_router = plan_pair_joins(obstacles, pairs, flip=flip)
        # Each pair is laid as one coupled route before any other signal:
        # constant gap from breakout to breakout, the way a layout engineer
        # routes USB, and its copper is fixed for everything after it.
        coupled, seen_by_router, notes = plan_coupled_pairs(
            seen_by_router, pairs,
            tracks=fanout_tracks + joins, vias=fanout_vias, **bounds,
        )
        laid = [t for c in coupled for t in c.tracks]
        done = [n for c in coupled for n in (c.positive, c.negative)]
        # A live view hears the coupled copper first, as it was laid.
        if kwargs.get("on_net") is not None:
            for net in done:
                kwargs["on_net"]("committed", net,
                                 [t for t in laid if t.net == net], [])
        result = route(
            seen_by_router
            + _as_obstacles(fanout_vias, fanout_tracks)
            + _as_obstacles([], _off_pads(joins, obstacles), net="")
            + _as_obstacles([], laid, net=""),
            filled_nets=ground,
            first_nets=[n for n in board.priority_nets if n not in done],
            two_layer=two_layer,
            **bounds,
            **kwargs,
        )
        result.tracks = list(result.tracks) + laid
        result.routed = done + [n for n in result.routed if n not in done]
        result.warnings.extend(notes)
        return result, joins, coupled

    result, joins, coupled = attempt(False)
    if joins and (set(result.unrouted) & set(board.priority_nets)
                  or len(coupled) * 2 < len(board.priority_nets)):
        # The other side for each loop; keep whichever couples more pairs,
        # then whichever leaves fewer nets.
        flipped, flipped_joins, flipped_coupled = attempt(True)
        if (len(flipped_coupled), -len(flipped.unrouted)) > (
            len(coupled), -len(result.unrouted)
        ):
            result, joins, coupled = flipped, flipped_joins, flipped_coupled
    board.coupled_pairs = [c.as_dict() for c in coupled]
    board.tracks = list(result.tracks) + fanout_tracks + joins
    board.vias = list(result.vias) + fanout_vias
    board.unrouted_nets = dict(result.unrouted)
    board.routed_nets = list(result.routed)
    board.filled_nets = list(result.filled)
    board.warnings.extend(result.warnings)
    if board.filled_nets:
        stitch_filled_nets(board)
        board.stitching_vias += len(fanout_vias)
    return result


def plan_fanout(
    board: BoardResult,
    net: str,
    obstacles: list[RoutePad],
    *,
    clearance_nm: int | None = None,
    diameter_nm: int = DEFAULT_VIA_DIAMETER_NM,
    drill_nm: int = DEFAULT_VIA_DRILL_NM,
    track_width_nm: int = DEFAULT_TRACK_WIDTH_NM,
) -> tuple[list[Via], list[Track]]:
    """A via and a short straight track for every surface pad of ``net``.

    Run before routing, so only pads, keep-outs and earlier fan-outs are in
    the way. Orthogonal moves only: the router takes the result as
    rectangular obstacles, and an axis-aligned track is exactly one
    rectangle. A pad stacked on another of the same net (a USB-C GND pair
    shares one land) fans out once.
    """
    clearance = STITCH_CLEARANCE_NM if clearance_nm is None else clearance_nm
    radius = diameter_nm / 2
    edge = DEFAULT_EDGE_CLEARANCE_NM + radius
    probe = replace(board, tracks=[], vias=[])
    vias: list[Via] = []
    tracks: list[Track] = []
    seen: set[tuple[int, int]] = set()
    for pad in obstacles:
        if pad.net != net or pad.through_hole or (pad.x_nm, pad.y_nm) in seen:
            continue
        seen.add((pad.x_nm, pad.y_nm))
        spot = _fanout_spot(probe, obstacles, vias, tracks, pad, radius, clearance,
                            edge, track_width_nm, directions=_FANOUT_DIRECTIONS[:4])
        if spot is None:
            continue
        vx, vy = spot
        vias.append(Via(vx, vy, net, diameter_nm, drill_nm))
        tracks.append(Track(pad.x_nm, pad.y_nm, vx, vy, pad.layer, net, track_width_nm))
    return vias, tracks


def plan_pair_joins(
    pads: list[RoutePad],
    pairs: list[tuple[str, str]],
    *,
    flip: bool = False,
    clearance_nm: int | None = None,
    track_width_nm: int = DEFAULT_TRACK_WIDTH_NM,
) -> tuple[list[Track], list[RoutePad]]:
    """Join a reversible connector's duplicated pair pins at the connector.

    A USB-C receptacle carries D+ and D- twice, interleaved in one row
    (``B7 A6 A7 B6``: D-, D+, D-, D+), and the two copies of each must be
    tied together. Left to the router, the first leg routed took the only
    channel through the row and the other could never reach its pins
    (measured 2026-09-16 on the ESP32 eval board, whichever leg went first).
    Layouts do it the same way every time: one net loops round the pin
    ends on one side of the row, the other net on the opposite side. This
    draws those two loops before routing; the router then connects each net
    once, from the copy whose open side the other loop leaves free, and
    sees the other copy and both loops as obstacles.

    Returns the loop tracks (full copper, pad centre to pad centre) and the
    pad list the router should use. A pair whose pins are not two-and-two in
    one interleaved row, or whose loop would pass too close to another pad,
    is left to the router untouched. ``flip`` swaps which net takes which
    side.
    """
    clearance = STITCH_CLEARANCE_NM if clearance_nm is None else clearance_nm
    tracks: list[Track] = []
    hidden: set[int] = set()
    by_ref: dict[str, list[RoutePad]] = {}
    for pad in pads:
        if pad.ref and not pad.through_hole:
            by_ref.setdefault(pad.ref, []).append(pad)
    for pos_net, neg_net in pairs:
        for members in by_ref.values():
            legs = {}
            for net in (pos_net, neg_net):
                spots = {}
                for pad in members:
                    if pad.net == net:
                        spots.setdefault((pad.x_nm, pad.y_nm), pad)
                legs[net] = sorted(spots.values(), key=lambda p: p.x_nm)
            row = legs[pos_net] + legs[neg_net]
            if len(legs[pos_net]) != 2 or len(legs[neg_net]) != 2:
                continue
            if len({(p.y_nm, p.h_nm) for p in row}) != 1:
                continue
            xs = sorted(row, key=lambda p: p.x_nm)
            if [p.net for p in xs][0::2] != [xs[0].net] * 2:
                continue  # not interleaved: the router needs no help
            up, down = (neg_net, pos_net) if not flip else (pos_net, neg_net)
            plan = _pair_loops(members, legs[up], legs[down], clearance, track_width_nm)
            if plan is None:
                continue
            loop_tracks, hide = plan
            tracks.extend(loop_tracks)
            hidden.update(id(p) for p in hide)
    seen = [replace(p, net="") if id(p) in hidden else p for p in pads]
    return tracks, seen


def _pair_loops(
    members: list[RoutePad],
    up_pads: list[RoutePad],
    down_pads: list[RoutePad],
    clearance_nm: int,
    track_width_nm: int,
) -> tuple[list[Track], list[RoutePad]] | None:
    y, half = up_pads[0].y_nm, up_pads[0].h_nm // 2
    reach = half + clearance_nm + track_width_nm // 2
    up_y, down_y = y + reach, y - reach
    ends = {id(p) for p in (*up_pads, *down_pads)}
    need = clearance_nm + track_width_nm / 2
    loops: list[Track] = []
    for pads, loop_y in ((up_pads, up_y), (down_pads, down_y)):
        a, b = pads
        layer = a.layer
        net = a.net
        for x in range(a.x_nm, b.x_nm + 1, mm(0.05)):
            for other in members:
                if id(other) in ends or other.net == net:
                    continue
                if _box_gap_nm(x, loop_y, other) < need:
                    return None
        loops += [
            Track(a.x_nm, y, a.x_nm, loop_y, layer, net, track_width_nm),
            Track(a.x_nm, loop_y, b.x_nm, loop_y, layer, net, track_width_nm),
            Track(b.x_nm, loop_y, b.x_nm, y, layer, net, track_width_nm),
        ]
    # Each net keeps, as its one terminal, the copy the other loop does not
    # cover; the other copy is already joined and becomes an obstacle.
    up_span = (up_pads[0].x_nm, up_pads[1].x_nm)
    down_span = (down_pads[0].x_nm, down_pads[1].x_nm)
    up_open = [p for p in up_pads if not down_span[0] <= p.x_nm <= down_span[1]]
    down_open = [p for p in down_pads if not up_span[0] <= p.x_nm <= up_span[1]]
    if not up_open or not down_open:
        return None
    hide = [p for p in up_pads if p is not up_open[0]]
    hide += [p for p in down_pads if p is not down_open[0]]
    # The pads that share a land with a hidden copy are hidden with it.
    spots = {(p.x_nm, p.y_nm) for p in hide}
    hide += [p for p in members if (p.x_nm, p.y_nm) in spots and p not in hide
             and p.net in (up_pads[0].net, down_pads[0].net)]
    return loops, hide


def _off_pads(tracks: list[Track], pads: list[RoutePad]) -> list[Track]:
    """``tracks`` with every end that sits on a pad pulled back to the pad's edge.

    What the router must avoid is the copper *outside* the pad: modelled from
    the pad centre, a join's leg covered the very pad the router was meant to
    start from, and the pad could not reach the lattice at all.
    """
    out: list[Track] = []
    for t in tracks:
        sx, sy, ex, ey = t.start_x_nm, t.start_y_nm, t.end_x_nm, t.end_y_nm
        for pad in pads:
            if pad.net != t.net:
                continue
            half = pad.h_nm // 2
            if (sx, sy) == (pad.x_nm, pad.y_nm) and sx == ex:
                sy += half if ey > sy else -half
            if (ex, ey) == (pad.x_nm, pad.y_nm) and sx == ex:
                ey += half if sy > ey else -half
        if (sx, sy) != (ex, ey):
            out.append(
                replace(t, start_x_nm=sx, start_y_nm=sy, end_x_nm=ex, end_y_nm=ey)
            )
    return out


def plan_coupled_pairs(
    pads: list[RoutePad],
    pairs: list[tuple[str, str]],
    *,
    tracks: list[Track],
    vias: list[Via],
    **bounds: int,
) -> tuple[list[CoupledRoute], list[RoutePad], list[str]]:
    """Route every pair whose nets each have two terminals as a coupled pair.

    Returns the routes, the pad list with each coupled pair's terminals made
    plain obstacles (the router must not route those nets again), and one
    note per pair that could not be coupled, saying why -- such a pair falls
    back to the ordinary router, routed first but not coupled.
    """
    routes: list[CoupledRoute] = []
    notes: list[str] = []
    hidden: set[int] = set()
    laid: list[Track] = list(tracks)
    for pos_net, neg_net in pairs:
        ends = {}
        for net in (pos_net, neg_net):
            spots: dict[tuple[int, int], RoutePad] = {}
            for pad in pads:
                if pad.net == net:
                    spots.setdefault((pad.x_nm, pad.y_nm), pad)
            ends[net] = list(spots.values())
        p, n = ends[pos_net], ends[neg_net]
        if len(p) != 2 or len(n) != 2:
            notes.append(
                f"{pos_net}/{neg_net} not routed as a coupled pair: each net needs "
                f"exactly two terminals, found {len(p)} and {len(n)}"
            )
            continue
        # Match the ends by part: P's first terminal sits on the same part as
        # N's first, so the pair leaves one part together and arrives at the
        # other together.
        if p[0].ref != n[0].ref:
            n = n[::-1]
        if p[0].ref != n[0].ref or p[1].ref != n[1].ref:
            notes.append(
                f"{pos_net}/{neg_net} not routed as a coupled pair: its two nets "
                "do not run between the same two parts"
            )
            continue
        found = route_pair(
            (p[0], p[1]), (n[0], n[1]),
            pads=[x for x in pads if id(x) not in hidden],
            tracks=laid, vias=vias, **bounds,
        )
        if isinstance(found, PairFailure):
            notes.append(
                f"{pos_net}/{neg_net} not routed as a coupled pair: {found.reason}"
            )
            continue
        routes.append(found)
        laid.extend(found.tracks)
        hidden.update(id(x) for x in pads if x.net in (pos_net, neg_net))
    seen = [replace(x, net="") if id(x) in hidden else x for x in pads]
    return routes, seen, notes


#: Step between the square obstacles a diagonal track is modelled by.
_DIAGONAL_SAMPLE_NM = mm(0.05)


def _as_obstacles(
    vias: list[Via], tracks: list[Track], *, net: str | None = None
) -> list[RoutePad]:
    """Pre-planned copper as the rectangles the router already knows how to
    avoid. ``net`` overrides each item's own net (``""``: a plain obstacle).

    An axis-aligned track is exactly one rectangle. A diagonal one is a row
    of track-width squares along its centreline: its bounding box would
    fence off a whole triangle of free board.
    """
    out = [
        RoutePad(net=v.net if net is None else net, x_nm=v.x_nm, y_nm=v.y_nm,
                 w_nm=v.diameter_nm, h_nm=v.diameter_nm, through_hole=True,
                 ref="fanout")
        for v in vias
    ]
    for t in tracks:
        if t.start_x_nm != t.end_x_nm and t.start_y_nm != t.end_y_nm:
            steps = max(1, math.ceil(t.length_nm / _DIAGONAL_SAMPLE_NM))
            for k in range(steps + 1):
                out.append(
                    RoutePad(
                        net=t.net if net is None else net,
                        x_nm=t.start_x_nm + (t.end_x_nm - t.start_x_nm) * k // steps,
                        y_nm=t.start_y_nm + (t.end_y_nm - t.start_y_nm) * k // steps,
                        w_nm=t.width_nm,
                        h_nm=t.width_nm,
                        layer=t.layer,
                        ref="fanout",
                    )
                )
            continue
        out.append(
            RoutePad(
                net=t.net if net is None else net,
                x_nm=(t.start_x_nm + t.end_x_nm) // 2,
                y_nm=(t.start_y_nm + t.end_y_nm) // 2,
                w_nm=abs(t.end_x_nm - t.start_x_nm) + t.width_nm,
                h_nm=abs(t.end_y_nm - t.start_y_nm) + t.width_nm,
                layer=t.layer,
                ref="fanout",
            )
        )
    return out


#: Pitch of the ground stitching grid. 2.54 mm is the default of the most
#: used KiCad stitching plugin (jsreynaud/kicad-action-scripts
#: ``ViaStitching/FillArea.py`` ``SetStepMM(2.54)`` at 5743b9f; GPL, read for
#: the design only). Dense enough that a pour island on one layer almost
#: always sits over a via to the other layer's pour.
STITCH_PITCH_NM = mm(2.54)

#: Copper-to-copper spacing a stitching via keeps from anything that is not
#: its own pour: the pour's own clearance (``connect_pads (clearance 0.3)``
#: in :func:`emit_kicad_pcb`), which is the larger of it and the router's.
STITCH_CLEARANCE_NM = mm(0.3)


#: Where a fan-out via is tried around a pad, in order: the four sides,
#: then the four corners.
_FANOUT_DIRECTIONS: tuple[tuple[float, float], ...] = (
    (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1), (1, -1), (-1, -1),
)


def _box_gap_nm(px: int, py: int, pad: RoutePad) -> float:
    """Distance from a point to a pad's rectangle (0 inside it)."""
    dx = max(abs(px - pad.x_nm) - pad.w_nm / 2, 0)
    dy = max(abs(py - pad.y_nm) - pad.h_nm / 2, 0)
    return (dx * dx + dy * dy) ** 0.5


def _segment_gap_nm(px: int, py: int, t: Track) -> float:
    """Distance from a point to a track's centreline."""
    ax, ay, bx, by = t.start_x_nm, t.start_y_nm, t.end_x_nm, t.end_y_nm
    vx, vy = bx - ax, by - ay
    span = vx * vx + vy * vy
    along = ((px - ax) * vx + (py - ay) * vy) / span if span else 0.0
    u = max(0.0, min(1.0, along))
    cx, cy = ax + u * vx, ay + u * vy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def stitch_filled_nets(
    board: BoardResult,
    *,
    pitch_nm: int = STITCH_PITCH_NM,
    clearance_nm: int = STITCH_CLEARANCE_NM,
    diameter_nm: int = DEFAULT_VIA_DIAMETER_NM,
    drill_nm: int = DEFAULT_VIA_DRILL_NM,
    track_width_nm: int = DEFAULT_TRACK_WIDTH_NM,
) -> int:
    """Drop a grid of through vias on the first filled net, tying its two pours.

    A two-layer ground pour is only one net if the two layers meet: signal
    copper splits the top pour into islands, and KiCad reports each island
    as unconnected (measured 2026-09-16 on the ESP32 eval board: three
    ``GND_F`` islands). Stitching vias are how two-layer boards join them --
    the grid-and-clearance design of ViaStitching's ``FillArea.py``: a via
    on every grid point whose copper keeps ``clearance_nm`` from every pad,
    track, other via and the board edge. Runs after routing, so it never
    takes a channel a signal wanted. Returns how many vias it added and
    records the count on ``board.stitching_vias``.
    """
    if not board.filled_nets:
        return 0
    net = board.filled_nets[0]
    radius = diameter_nm / 2
    edge = DEFAULT_EDGE_CLEARANCE_NM + radius
    obstacles = board_pads(board) + board_keepouts(board)
    added: list[Via] = []
    fanout: list[Track] = []
    # First, a fan-out at every surface pad of the net: a short track from
    # the pad to a via just outside it. A pad whose pour island is cut off by
    # signal copper (the 2026-09-16 ESP32 board had two such slivers, around
    # the USB-C shield pins and a decoupling cap) then still reaches the
    # other layer's pour through its own copper, not through the island.
    # Candidates ring the pad at growing distances, sides before corners,
    # and the first whose via and track are both clear wins.
    fanned = {(t.start_x_nm, t.start_y_nm) for t in board.tracks if t.net == net}
    for pad in obstacles:
        if pad.net != net or pad.through_hole or (pad.x_nm, pad.y_nm) in fanned:
            continue
        fanned.add((pad.x_nm, pad.y_nm))
        spot = _fanout_spot(board, obstacles, added, fanout, pad, radius,
                            clearance_nm, edge, track_width_nm)
        if spot is None:
            continue
        vx, vy = spot
        added.append(Via(vx, vy, net, diameter_nm, drill_nm))
        fanout.append(Track(pad.x_nm, pad.y_nm, vx, vy, pad.layer, net, track_width_nm))
    board.tracks.extend(fanout)
    y = pitch_nm // 2
    while y <= board.height_nm - edge:
        x = pitch_nm // 2
        while x <= board.width_nm - edge:
            if x >= edge and y >= edge and _stitch_clear(
                board, obstacles, added, x, y, radius, clearance_nm
            ):
                added.append(Via(x, y, net, diameter_nm, drill_nm))
            x += pitch_nm
        y += pitch_nm
    board.vias.extend(added)
    board.stitching_vias = len(added)
    return len(added)


#: How far out, in multiples of the tightest ring, a fan-out via is tried.
_FANOUT_RINGS: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0)


def _fanout_spot(
    board: BoardResult,
    pads: list[RoutePad],
    added: list[Via],
    fanout: list[Track],
    pad: RoutePad,
    radius: float,
    clearance_nm: int,
    edge: float,
    track_width_nm: int,
    directions: tuple[tuple[float, float], ...] = _FANOUT_DIRECTIONS,
) -> tuple[int, int] | None:
    base_x = pad.w_nm / 2 + clearance_nm + radius
    base_y = pad.h_nm / 2 + clearance_nm + radius
    for ring in _FANOUT_RINGS:
        for dx, dy in directions:
            vx = round(pad.x_nm + dx * base_x * ring)
            vy = round(pad.y_nm + dy * base_y * ring)
            if not (edge <= vx <= board.width_nm - edge
                    and edge <= vy <= board.height_nm - edge):
                continue
            if not _stitch_clear(board, pads, added, vx, vy, radius, clearance_nm,
                                 extra_tracks=fanout):
                continue
            if _fanout_track_clear(board, pads, added, fanout, pad, vx, vy,
                                   track_width_nm, clearance_nm):
                return vx, vy
    return None


def _fanout_track_clear(
    board: BoardResult,
    pads: list[RoutePad],
    added: list[Via],
    fanout: list[Track],
    origin: RoutePad,
    vx: int,
    vy: int,
    track_width_nm: int,
    clearance_nm: int,
) -> bool:
    """The pad-to-via track keeps ``clearance_nm`` from foreign copper.

    Checked by sampling the centreline every 0.1 mm against every pad but
    its own (the track starts inside it), every track and every via but the
    one it ends on; copper of the same net may touch, as KiCad allows.
    """
    need = track_width_nm / 2 + clearance_nm
    length = ((vx - origin.x_nm) ** 2 + (vy - origin.y_nm) ** 2) ** 0.5
    steps = max(1, int(length // mm(0.1)))
    for k in range(steps + 1):
        px = origin.x_nm + (vx - origin.x_nm) * k / steps
        py = origin.y_nm + (vy - origin.y_nm) * k / steps
        for p in pads:
            if p is origin or p.net == origin.net:
                continue
            if _box_gap_nm(round(px), round(py), p) < need:
                return False
        for t in (*board.tracks, *fanout):
            if t.net == origin.net:
                continue
            if _segment_gap_nm(round(px), round(py), t) < need + t.width_nm / 2:
                return False
        for v in (*board.vias, *added):
            if v.net == origin.net:
                continue
            gap = ((px - v.x_nm) ** 2 + (py - v.y_nm) ** 2) ** 0.5
            if gap < need + v.diameter_nm / 2:
                return False
    return True


def _stitch_clear(
    board: BoardResult,
    pads: list[RoutePad],
    added: list[Via],
    x: int,
    y: int,
    radius: float,
    clearance_nm: int,
    extra_tracks: list[Track] = (),
) -> bool:
    need = radius + clearance_nm
    for pad in pads:
        # Even a pad of the stitched net: a via in a pad wicks solder away.
        if _box_gap_nm(x, y, pad) < need:
            return False
    for t in (*board.tracks, *extra_tracks):
        if _segment_gap_nm(x, y, t) < need + t.width_nm / 2:
            return False
    for v in (*board.vias, *added):
        if ((x - v.x_nm) ** 2 + (y - v.y_nm) ** 2) ** 0.5 < need + v.diameter_nm / 2:
            return False
    return True


def _uuid(seed: str) -> str:
    """Stable UUID from a seed, so output is byte-identical across runs."""
    return stable_uuid(seed)


def emit_kicad_pcb(
    board: BoardResult, *, margin_nm: int = DEFAULT_BOARD_MARGIN_NM
) -> str:
    """Render a :class:`BoardResult` as KiCad 8 ``.kicad_pcb`` source."""

    def f(nm: int) -> str:
        return f"{nm / NM_PER_MM:.4f}".rstrip("0").rstrip(".") or "0"

    # Net 0 is KiCad's unconnected net and must exist.
    net_names: list[str] = [""]
    for name in dict.fromkeys(board.nets):
        if name and name not in net_names:
            net_names.append(name)
    # No-connect pads last, so every real net keeps the index it always had.
    for part in board.parts:
        for name in part.no_connects.values():
            if name not in net_names:
                net_names.append(name)
    net_index = {name: i for i, name in enumerate(net_names)}

    out: list[str] = []
    out.append('(kicad_pcb (version 20240108) (generator "silkscreen")')
    out.append("  (general (thickness 1.6))")
    out.append('  (paper "A4")')
    out.append("  (layers")
    out.append(_LAYERS)
    out.append("  )")
    for i, name in enumerate(net_names):
        out.append(f'  (net {i} "{name}")')

    m = margin_nm
    corners = [
        (-m, -m),
        (board.width_nm + m, -m),
        (board.width_nm + m, board.height_nm + m),
        (-m, board.height_nm + m),
    ]
    for i in range(4):
        sx, sy = corners[i]
        ex, ey = corners[(i + 1) % 4]
        out.append(
            f"  (gr_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
            f' (stroke (width 0.1) (type solid)) (layer "Edge.Cuts")'
            f' (uuid "{_uuid(f"edge{i}")}"))'
        )

    for part in board.parts:
        fp = part.footprint
        # The placer gives a bottom-left corner with Y up; KiCad wants the
        # anchor with Y down, and the courtyard is centred on the anchor.
        # part_anchor owns the rotation swap, so this cannot drift from the
        # pad geometry the router was handed.
        anchor_up_x, anchor_up_y = part_anchor(part)
        anchor_x = anchor_up_x
        anchor_y = board.height_nm - anchor_up_y
        angle = 90 if part.rotated else 0
        at = f"{f(anchor_x)} {f(anchor_y)}" + (f" {angle}" if angle else "")

        # A bottom-side footprint lives on B.Cu with its pads and graphics on
        # the B layers; KiCad renders it mirrored from the same coordinates.
        bottom = part.layer is Layer.BOTTOM
        side = "B" if bottom else "F"
        if fp.library is not None and not part.rotated and not bottom:
            out.append(
                _library_footprint_block(
                    part, anchor_x, anchor_y, net_index, frozenset(board.filled_nets)
                )
            )
            continue
        out.append(f'  (footprint "silkscreen:{fp.name}"')
        out.append(f'    (layer "{side}.Cu")')
        out.append(f'    (uuid "{_uuid(part.ref)}")')
        out.append(f"    (at {at})")
        out.append(f'    (descr "{fp.description}")')
        # The library 3D model, when the land pattern has an honest one: the
        # 3D viewer and ``kicad-cli pcb export glb`` draw a part only from a
        # model line, and a board without any exports as a bare substrate.
        # The offset is the model's own claim about where it sits relative to
        # this land pattern's origin, and it is zero only because KiCad and
        # this pipeline agree on centring for ICs and chip packages. A
        # through-hole connector is anchored on pin 1 in KiCad's library and
        # on the body here, so its model carries the difference; writing zero
        # for those put the plastic beside the part. The rotation is the
        # model's claim about whose pin-1 convention it was drawn for
        # (models3d.Model3D).
        model = model_for(fp.name, part.ref)
        if model is not None:
            ox, oy, oz = model.offset_mm
            out.append(
                f'    (model "{model.path}" (offset (xyz {ox} {oy} {oz}))'
                f" (scale (xyz 1 1 1)) (rotate (xyz 0 0 {model.rotate_deg})))"
            )
        # The designator sits centred just above the courtyard, in the band
        # build_board reserved for it. Its angle is written as the part's own:
        # KiCad stores footprint text angles as absolute on-screen angles, so
        # a rotated part's label written at 0 would stay upright and lie
        # across the part (checked against kicad-cli 10), while one written
        # at the part's angle turns with it and stays in the band.
        text_size = f(REF_TEXT_SIZE_NM)
        text_stroke = f(REF_TEXT_STROKE_NM)
        out.append(
            f'    (property "Reference" "{part.ref}"'
            f" (at 0 {f(-ref_text_offset_nm(fp))} {angle})"
            f' (layer "{side}.SilkS") (uuid "{_uuid(part.ref + "ref")}")'
            f" (effects (font (size {text_size} {text_size})"
            f" (thickness {text_stroke}))))"
        )
        out.append(
            f'    (property "Value" "{part.value}"'
            f" (at 0 {f(ref_text_offset_nm(fp))} {angle})"
            f' (layer "{side}.Fab") (uuid "{_uuid(part.ref + "val")}")'
            f" (effects (font (size {text_size} {text_size})"
            f" (thickness {text_stroke}))))"
        )

        cw, ch = fp.courtyard_w_nm, fp.courtyard_h_nm
        cpts = [(-cw, -ch), (cw, -ch), (cw, ch), (-cw, ch)]
        for i in range(4):
            sx, sy = cpts[i]
            ex, ey = cpts[(i + 1) % 4]
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.05) (type solid)) (layer "{side}.CrtYd")'
                f' (uuid "{_uuid(part.ref + f"crt{i}")}"))'
            )

        # The body outline, clipped clear of the pads: stroking the raw body
        # rectangle put ink on every pad the body edge touches (see
        # footprints.silk_segments), and ink on a pad is a solderability
        # defect a fab will clip or flag.
        for i, (sx, sy, ex, ey) in enumerate(part_silk_segments(part)):
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.12) (type solid)) (layer "{side}.SilkS")'
                f' (uuid "{_uuid(part.ref + f"silk{i}")}"))'
            )

        # Orientation on the legend itself: the outline is symmetric, so a
        # pin-1 dot (ICs) or cathode bar (diodes) is the only mark the fab
        # and the assembler read. Footprint-local like the pads, so a rotated
        # or bottom-side part carries it round.
        dot = pin1_mark(fp)
        if dot is not None:
            dx, dy, r = dot
            out.append(
                f"    (fp_circle (center {f(dx)} {f(dy)}) (end {f(dx + r)} {f(dy)})"
                f' (stroke (width 0.12) (type solid)) (fill solid)'
                f' (layer "{side}.SilkS") (uuid "{_uuid(part.ref + "pin1")}"))'
            )
        bar = cathode_mark(fp)
        if bar is not None:
            sx, sy, ex, ey = bar
            out.append(
                f"    (fp_line (start {f(sx)} {f(sy)}) (end {f(ex)} {f(ey)})"
                f' (stroke (width 0.12) (type solid)) (layer "{side}.SilkS")'
                f' (uuid "{_uuid(part.ref + "cathode")}"))'
            )

        for pad_index, pad in enumerate(fp.pads):
            pad_net = pad.net or part.no_connects.get(pad.number, "")
            idx = net_index.get(pad_net, 0)
            net_decl = f' (net {idx} "{pad_net}")' if idx else ""
            # Seeded by position, not number: a SOT-223 tab shares pin 2's
            # number, and two pads with one uuid is a file KiCad repairs
            # silently on load.
            uuid = _uuid(f"{part.ref}p{pad_index}:{pad.number}")
            if pad.is_tht:
                # A plated hole is on every copper layer, so it is emitted
                # with "*.Cu" rather than the placed side -- a through-hole
                # pin lands on both faces whichever side the module sits on.
                # It gets no F.Paste: a stencil aperture over an open hole
                # drops paste through it instead of forming a joint.
                # Pin 1 is square so the polarity mark survives assembly;
                # every other pin is round, the usual header convention.
                shape = "rect" if pad.number == "1" else "circle"
                out.append(
                    f'    (pad "{pad.number}" thru_hole {shape}'
                    f" (at {f(pad.x_nm)} {f(pad.y_nm)})"
                    f" (size {f(pad.w_nm)} {f(pad.h_nm)})"
                    f" (drill {f(pad.drill_nm)})"
                    f' (layers "*.Cu" "*.Mask"){net_decl}'
                    f' (uuid "{uuid}"))'
                )
            else:
                # A surface pad on a poured net joins the pour solidly
                # (KiCad's pad ``zone_connect`` 2). A thermal relief on a
                # small SMD pad leaves one spoke where KiCad's DRC asks for
                # two (``starved_thermal``, five of them on the 2026-09-16
                # ESP32 eval board); reflow does not need the relief that
                # hand-soldering a through-hole pin does, so holes keep it.
                solid = " (zone_connect 2)" if pad_net in board.filled_nets else ""
                out.append(
                    f'    (pad "{pad.number}" smd roundrect'
                    f" (at {f(pad.x_nm)} {f(pad.y_nm)})"
                    f" (size {f(pad.w_nm)} {f(pad.h_nm)})"
                    f' (layers "{side}.Cu" "{side}.Paste" "{side}.Mask")'
                    f" (roundrect_rratio 0.25){net_decl}{solid}"
                    f' (uuid "{uuid}"))'
                )
        out.append("  )")

    # Copper last, after every footprint, matching KiCad's own ordering.
    # The Y flip here is the same one applied to footprint anchors above: the
    # router works in the placer's Y-up frame and this is the only place its
    # output crosses into KiCad's Y-down frame.
    def flip_y(y_nm: int) -> int:
        return board.height_nm - y_nm

    for index, track in enumerate(board.tracks):
        layer = "B.Cu" if track.layer is Layer.BOTTOM else "F.Cu"
        idx = net_index.get(track.net, 0)
        out.append(
            f"  (segment (start {f(track.start_x_nm)} {f(flip_y(track.start_y_nm))})"
            f" (end {f(track.end_x_nm)} {f(flip_y(track.end_y_nm))})"
            f" (width {f(track.width_nm)}) (layer \"{layer}\") (net {idx})"
            f' (uuid "{_uuid(f"seg{index}:{track.net}")}"))'
        )

    for index, via in enumerate(board.vias):
        idx = net_index.get(via.net, 0)
        out.append(
            f"  (via (at {f(via.x_nm)} {f(flip_y(via.y_nm))})"
            f" (size {f(via.diameter_nm)}) (drill {f(via.drill_nm)})"
            f' (layers "F.Cu" "B.Cu") (net {idx})'
            f' (uuid "{_uuid(f"via{index}:{via.net}")}"))'
        )

    # Copper pours for the filled nets: one zone per layer over the whole
    # board, thermal reliefs to pads, KiCad's default 0.3 mm clearance and
    # 0.25 mm minimum width. Unfilled here on purpose -- a fill polygon is
    # KiCad's to compute (write_board asks kicad-cli to refill and save).
    for net in board.filled_nets:
        idx = net_index.get(net)
        if not idx:
            continue
        pts = " ".join(f"(xy {f(x)} {f(y)})" for x, y in corners)
        for layer in ("F.Cu", "B.Cu"):
            out.append(
                f'  (zone (net {idx}) (net_name "{net}") (layer "{layer}")'
                f' (uuid "{_uuid(f"zone:{net}:{layer}")}") (name "{net}_{layer[0]}")'
                f" (hatch edge 0.5) (connect_pads (clearance 0.3))"
                f" (min_thickness 0.25) (filled_areas_thickness no)"
                f" (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))"
                f" (polygon (pts {pts})))"
            )

    out.append(")")
    return "\n".join(out) + "\n"


def live_copper(board: BoardResult, tracks, vias) -> dict[str, list[dict[str, Any]]]:
    """``tracks`` and ``vias`` as the KiCad file would carry them, for a
    watcher drawing them into an open editor while the router runs.

    Millimetres in KiCad's Y-down frame with the layer named the KiCad way --
    the same numbers :func:`emit_kicad_pcb` writes, through the same flip,
    which is the only place the router's Y-up frame crosses into KiCad's.
    Sending a watcher solver-frame copper and letting it flip would be a
    second flip, and two flips is how a mirrored board happens.
    """

    def f(nm: int) -> float:
        return round(nm / NM_PER_MM, 6)

    def flip_y(y_nm: int) -> int:
        return board.height_nm - y_nm

    return {
        "segments": [
            {
                "layer": "B.Cu" if t.layer is Layer.BOTTOM else "F.Cu",
                "x0_mm": f(t.start_x_nm), "y0_mm": f(flip_y(t.start_y_nm)),
                "x1_mm": f(t.end_x_nm), "y1_mm": f(flip_y(t.end_y_nm)),
                "width_mm": f(t.width_nm), "net": t.net,
            }
            for t in tracks
        ],
        "vias": [
            {
                "x_mm": f(v.x_nm), "y_mm": f(flip_y(v.y_nm)),
                "size_mm": f(v.diameter_nm), "drill_mm": f(v.drill_nm), "net": v.net,
            }
            for v in vias
        ],
    }


def write_board(board: BoardResult, path: str | Path) -> Path:
    """Write the board to ``path`` and return it.

    A board with ``filled_nets`` carries its ground pours as *unfilled*
    zones, on purpose: KiCad fills a zone the moment it opens the file (or on
    ``B``), and ``kicad-cli pcb drc --refill-zones`` checks connectivity with
    the fill computed, which is how :func:`silkscreen.verify.kicad.drc` runs
    it. Asking KiCad to fill and *save* here was tried (2026-09-15) and
    reverted: the save rewrites the file in the installed KiCad's own format
    version, which this engine's ``kiutils`` readers, the KiCad bridge and
    the two-driver byte-parity test can no longer read. The 20240108 file
    this writes stays the one every tool in the tree understands.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(emit_kicad_pcb(board), encoding="utf-8")
    return path


def _connector_families() -> list[str]:
    from .kicadlib.connectors import FAMILIES

    return list(FAMILIES)


def _connector_symbol(package: str, device) -> str | None:
    """KiCad's generic connector symbol for a library connector family, so the
    schematic draws Conn_01xNN with the footprint's own pin numbers."""
    from .kicadlib.connectors import is_connector_spec, resolve

    if getattr(device, "kind", "") != "connector" or not is_connector_spec(package):
        return None
    found = resolve(package, _package_pin_count(device.pins))
    return found.symbol if found is not None else None


def _library_edge_side(fp: Footprint) -> str | None:
    """The edge a horizontal KiCad library connector must face, or None.

    A ``*_Horizontal`` wire-to-board part takes its plug parallel to the board,
    so it only works at an edge with its mouth outward -- the rule
    :data:`EDGE_FACING` states for the engine's USB-C and barrel jack. Which
    side is read from the footprint: the pads sit at the back and the body runs
    toward the mouth (Phoenix MSTBA: pads at local -Y, mouth +Y, the solver's
    bottom; AMASS XT60PW: pads at +Y, mouth -Y, the solver's top).
    """
    wire_to_board = ("PhoenixContact", "AMASS", "JST", "Molex", "TerminalBlock")
    if (
        fp.library is None
        or not fp.name.endswith("_Horizontal")
        or not fp.name.startswith(wire_to_board)
    ):
        return None
    pads = [p for p in fp.pads if not str(p.number).startswith("MP")]
    if not pads:
        return None
    mean_y = sum(p.y_nm for p in pads) / len(pads)
    return "bottom" if mean_y < 0 else "top"
