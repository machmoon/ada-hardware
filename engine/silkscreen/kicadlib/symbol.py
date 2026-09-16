"""Load a KiCad library symbol for embedding in a generated schematic.

KiCad itself flattens a placed symbol into the schematic's ``lib_symbols``:
a derived symbol (``(extends "AP1117-15")``) is written as its parent's
graphics and pins under its own name, with its own property values. This does
the same, through ``kiutils`` -- which writes the KiCad 8 syntax the schematic
emitter's ``(version 20231120)`` promises, so the file still opens in KiCad 8.

Two normalisations and two refusals, each measured or reasoned rather than
assumed:

* ``kiutils`` reads KiCad 10's ``(show_name no)`` as "show" and writes a bare
  ``(show_name)``, and it drops KiCad 10's ``(hide yes)`` line; left alone,
  every placed part would print "Reference U" labels, its datasheet URL and
  its footprint name. Names are set back to hidden, and every property but
  Reference and Value is hidden, as the library has them.
* Units are renamed with the symbol (``AP1117-15_1_1`` -> ``AMS1117-3.3_1_1``)
  because KiCad matches unit sub-symbols to the outer name.
* **Multi-unit symbols are refused** (None): a sheet that places one instance of
  a dual op-amp places one unit, and the other unit's pins would exist on the
  board and nowhere on the schematic.
* **Symbols with hidden pins are refused**: a hidden ``power_in`` pin connects
  itself to a global net named after the pin, silently, which would contradict
  the net the circuit put it on.

A refusal means the schematic keeps drawing its own generated symbol for that
part -- the board still uses the library footprint.
"""

from __future__ import annotations

import copy
import functools
from dataclasses import dataclass
from pathlib import Path

from ..units import mm
from .paths import symbol_dir

__all__ = ["LibraryPin", "LibrarySymbol", "load_symbol"]


@dataclass(frozen=True)
class LibraryPin:
    number: str
    name: str
    #: Connection point, symbol-local, Y up, nm.
    x_nm: int
    y_nm: int
    #: KiCad's orientation: the direction the pin runs from its connection
    #: point toward the body (0 right, 90 up, 180 left, 270 down).
    angle: int
    length_nm: int
    #: KiCad's electrical type ("power_out", "input", "passive", ...).
    electrical: str = "passive"


@dataclass(frozen=True)
class LibrarySymbol:
    lib_id: str
    #: The flattened ``(symbol "Lib:Name" ...)`` block, ready for lib_symbols.
    text: str
    pins: tuple[LibraryPin, ...]
    #: Half-extents of body and pins about the symbol origin, nm.
    extent_w_nm: int
    extent_h_nm: int


@functools.lru_cache(maxsize=64)
def _library(path: str):
    from kiutils.symbol import SymbolLib

    return SymbolLib().from_file(path)


def _points(item) -> list[tuple[float, float]]:
    name = type(item).__name__
    pts: list[tuple[float, float]] = []
    if name == "SyRect":
        pts += [(item.start.X, item.start.Y), (item.end.X, item.end.Y)]
    elif name == "SyPolyLine":
        pts += [(p.X, p.Y) for p in item.points]
    elif name == "SyCircle":
        r = float(item.radius)
        pts += [
            (item.center.X - r, item.center.Y - r),
            (item.center.X + r, item.center.Y + r),
        ]
    elif name == "SyArc":
        pts += [(p.X, p.Y) for p in (item.start, item.mid, item.end) if p is not None]
    return pts


@functools.lru_cache(maxsize=512)
def load_symbol(
    lib_id: str,
    root: str | None = None,
    *,
    pads: frozenset[str] | None = None,
) -> LibrarySymbol | None:
    """The flattened library symbol for ``lib_id``, or None when it cannot be
    used: not installed, not found, multi-unit, or carrying hidden pins.

    ``pads`` names the pad numbers the land pattern actually has. A symbol pin
    with no pad is left out of the embedded copy: KiCad's USB-C symbols carry
    a shield pin ``SH`` for tabs the engine's pattern does not draw, and
    schematic parity reports every such pin ("No pad found for pin SH"). The
    embedded symbol is a copy, and saying less than the library is honest
    where saying more would describe copper that is not there.
    """
    base = Path(root) if root else symbol_dir()
    if base is None or ":" not in lib_id:
        return None
    library, name = lib_id.split(":", 1)
    path = base / f"{library}.kicad_sym"
    if not path.is_file():
        return None
    lib = _library(str(path))
    by_name = {s.entryName: s for s in lib.symbols}
    symbol = by_name.get(name)
    if symbol is None:
        return None
    source = symbol
    for _ in range(8):  # extends chain, bounded against a malformed cycle
        if not source.extends or source.extends not in by_name:
            break
        source = by_name[source.extends]
    flat = copy.deepcopy(source)
    if source is not symbol:
        # The derived symbol's own fields win; the geometry is the parent's.
        own = {p.key: p.value for p in symbol.properties}
        for prop in flat.properties:
            if prop.key in own:
                prop.value = own[prop.key]
        flat.extends = None
    flat.libraryNickname = library
    flat.entryName = name
    for prop in flat.properties:
        prop.showName = False
        # KiCad 10 writes hiding as its own ``(hide yes)`` line, which kiutils
        # drops: every datasheet URL and footprint name would be printed on
        # the sheet. The library hides everything but Reference and Value.
        if prop.key not in ("Reference", "Value") and prop.effects is not None:
            prop.effects.hide = True

    units = [u for u in flat.units if u.unitId and int(u.unitId) > 1]
    if units:
        return None  # multi-unit: one placed instance would lose pins
    pins: list[LibraryPin] = []
    points: list[tuple[float, float]] = []
    for unit in flat.units:
        unit.entryName = name
        if pads is not None:
            unit.pins = [p for p in unit.pins if str(p.number) in pads]
        for item in unit.graphicItems:
            points += _points(item)
        for pin in unit.pins:
            if pin.hide:
                return None  # a hidden pin wires itself by name; see module doc
            x, y = float(pin.position.X), float(pin.position.Y)
            angle = int(float(pin.position.angle or 0)) % 360
            pins.append(
                LibraryPin(
                    number=str(pin.number),
                    name=str(pin.name),
                    x_nm=mm(x),
                    y_nm=mm(y),
                    angle=angle,
                    length_nm=mm(float(pin.length)),
                    electrical=str(pin.electricalType or "passive"),
                )
            )
            points.append((x, y))
    if not pins:
        return None
    ext_w = max(abs(x) for x, _ in points)
    ext_h = max(abs(y) for _, y in points)
    return LibrarySymbol(
        lib_id=lib_id,
        text=flat.to_sexpr(),
        pins=tuple(pins),
        extent_w_nm=mm(ext_w),
        extent_h_nm=mm(ext_h),
    )
