"""Real connector families, resolved to KiCad's own library footprints.

The engine drew a handful of connectors itself (``footprints.CONNECTOR_PACKAGES``:
pin headers, a JST PH, USB-C, a barrel jack), so a design that needed a
JST-GH servo lead, a Molex Micro-Fit power input or an XT60 battery lead had
nothing to use. KiCad installs all of those, with verified pads and 3D models.

The shape is tscircuit's ``<connector standard="jst_ph" pinCount={4}>``
(``tscircuit/props lib/components/connector.ts``): a family plus a pin count,
never a hand-typed footprint name. The family resolves by KiCad's library
naming convention (KLC F2/F3: ``<Family>_<MPN>_<rows>x<NN>[-<k>MP]_P<pitch>mm
_<Orientation>``), read from the installed ``.pretty`` directory, so nothing
here is a copied table of names that can drift from the library.

A package is written ``"JST_GH_4P"`` (explicit count, preferred: an unwired
pin still exists on the part) or ``"JST_GH"`` (count from the highest pin the
circuit declares), or as an exact KiCad id ``"Connector_JST:JST_GH_..."``.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from .paths import footprint_dir, model_dir

__all__ = [
    "FAMILIES",
    "ConnectorFootprint",
    "families_text",
    "is_connector_spec",
    "resolve",
]


@dataclass(frozen=True)
class Family:
    library: str
    #: Filename prefix within the library that selects the part series.
    prefix: str
    rows: int
    #: Acceptable orientations, preferred first. A later one is used only
    #: when it carries an installed 3D model and the earlier ones do not.
    orientation: str | tuple[str, ...]
    what: str
    #: Pin pitch in mm as KiCad writes it, when the prefix alone admits two.
    pitch: str = r"[\d.,]+"


#: tscircuit's standards (jst_sh/gh/zh/ph/xh/vh) plus the power, wire-to-board
#: and header families a mechatronic system needs. The prefix picks the
#: through-hole or standard variant KiCad lists first for the series.
FAMILIES: dict[str, Family] = {
    "JST_SH": Family(
        "Connector_JST",
        "JST_SH_BM",
        1,
        "Vertical",
        "JST SH 1.0 mm, small signal (Qwiic/STEMMA QT)",
    ),
    "JST_GH": Family(
        "Connector_JST",
        "JST_GH_BM",
        1,
        "Vertical",
        "JST GH 1.25 mm, latching signal (Pixhawk, servos, sensors)",
    ),
    "JST_ZH": Family(
        "Connector_JST", "JST_ZH_B", 1, "Vertical", "JST ZH 1.5 mm, small wire-to-board"
    ),
    "JST_PH": Family(
        "Connector_JST",
        "JST_PH_B",
        1,
        "Vertical",
        "JST PH 2.0 mm, LiPo cells and general wire-to-board",
    ),
    "JST_XH": Family(
        "Connector_JST",
        "JST_XH_B",
        1,
        "Vertical",
        "JST XH 2.5 mm, balance leads, up to 3 A",
    ),
    "JST_VH": Family(
        "Connector_JST", "JST_VH_B", 1, "Vertical", "JST VH 3.96 mm, power up to 10 A"
    ),
    "MOLEX_PICOBLADE": Family(
        "Connector_Molex",
        "Molex_PicoBlade_53047",
        1,
        "Vertical",
        "Molex PicoBlade 1.25 mm signal",
    ),
    "MOLEX_MICROFIT": Family(
        "Connector_Molex",
        "Molex_Micro-Fit_3.0_43045",
        2,
        "Vertical",
        "Molex Micro-Fit 3.0, dual row power up to 5 A per pin",
    ),
    "XT30": Family(
        "Connector_AMASS",
        "AMASS_XT30",
        1,
        ("Horizontal", "Vertical"),
        "AMASS XT30, 2-pin battery/power 15 A",
    ),
    "XT60": Family(
        "Connector_AMASS",
        "AMASS_XT60",
        1,
        ("Horizontal", "Vertical"),
        "AMASS XT60, 2-pin battery/power 30 A",
    ),
    "TERMINAL_5.08": Family(
        "Connector_Phoenix_MSTB",
        "PhoenixContact_MSTBA_2,5_",
        1,
        "Horizontal",
        "Phoenix MSTBA 5.08 mm pluggable terminal block",
        pitch="5.08",
    ),
    "HEADER_2.54": Family(
        "Connector_PinHeader_2.54mm",
        "PinHeader",
        1,
        "Vertical",
        "2.54 mm single-row pin header",
    ),
    "HEADER_2X_2.54": Family(
        "Connector_PinHeader_2.54mm",
        "PinHeader",
        2,
        "Vertical",
        "2.54 mm dual-row pin header",
    ),
    "IDC_2.54": Family(
        "Connector_IDC",
        "IDC-Header",
        2,
        "Vertical",
        "2.54 mm shrouded IDC box header (ribbon cable)",
    ),
}

_SPEC = re.compile(r"^(?P<family>[A-Z0-9_.]+?)(?:_(?P<count>\d{1,2})P)?$")


@dataclass(frozen=True)
class ConnectorFootprint:
    lib_id: str
    family: str
    pins: int
    #: KiCad's generic symbol with the same pin numbering as the footprint.
    symbol: str


def _parse(package: str) -> tuple[str, int | None] | None:
    match = _SPEC.match(package.strip().upper())
    if not match or match["family"] not in FAMILIES:
        return None
    return match["family"], int(match["count"]) if match["count"] else None


def is_connector_spec(package: str) -> bool:
    """Whether ``package`` names a family here or an exact KiCad connector id.

    Syntax only, so the netlist can accept it without KiCad installed; whether
    the footprint exists is :func:`resolve`'s question.
    """
    return _parse(package) is not None or (
        ":" in package and package.split(":", 1)[0].startswith("Connector")
    )


@functools.lru_cache(maxsize=64)
def _names(library: str) -> tuple[str, ...]:
    root = footprint_dir()
    if root is None or not (root / f"{library}.pretty").is_dir():
        return ()
    return tuple(
        sorted(p.stem for p in (root / f"{library}.pretty").glob("*.kicad_mod"))
    )


_MODEL = re.compile(r'\(model\s+"?\$\{[A-Z0-9_]+\}/([^"\s)]+)')


@functools.lru_cache(maxsize=512)
def _has_model(library: str, name: str) -> bool:
    """Whether the footprint's own ``(model ...)`` file is installed."""
    root, models = footprint_dir(), model_dir()
    if root is None or models is None:
        return False
    try:
        text = (root / f"{library}.pretty" / f"{name}.kicad_mod").read_text(
            errors="replace"
        )
    except OSError:
        return False
    return any((models / rel).is_file() for rel in _MODEL.findall(text))


def _symbol(rows: int, pins: int) -> str:
    if rows == 2:
        return f"Connector_Generic:Conn_02x{pins // 2:02d}_Odd_Even"
    return f"Connector_Generic:Conn_01x{pins:02d}"


def resolve(package: str, declared_pins: int) -> ConnectorFootprint | None:
    """The installed KiCad footprint for a connector spec, or None.

    None means "not a family spec" or "this family has no part with that pin
    count installed" -- the caller refuses in words, never draws a guess.
    """
    if ":" in package:
        library, name = package.split(":", 1)
        if name not in _names(library):
            return None
        match = re.search(r"_(\d)x(\d{2})", name)
        rows, per_row = (int(match[1]), int(match[2])) if match else (1, declared_pins)
        return ConnectorFootprint(
            package, library, rows * per_row, _symbol(rows, rows * per_row)
        )
    parsed = _parse(package)
    if parsed is None:
        return None
    family_name, count = parsed
    family = FAMILIES[family_name]
    pins = count or declared_pins
    if family.rows == 2 and pins % 2:
        pins += 1  # a dual-row part has an even count; the spare pin is unwired
    per_row = pins // family.rows
    orientations = (
        (family.orientation,)
        if isinstance(family.orientation, str)
        else family.orientation
    )
    pattern = re.compile(
        rf"^{re.escape(family.prefix)}.*_{family.rows}x{per_row:02d}(-\d+MP)?_P{family.pitch}mm_"
        rf"({'|'.join(orientations)})$"
    )
    candidates = [n for n in _names(family.library) if pattern.match(n)]
    if not candidates:
        return None

    # A connector with no 3D body breaks the case fit and the system assembly,
    # and KiCad ships footprints whose model it does not (XT60PW horizontal).
    # So: a variant with an installed model first, then orientation preference,
    # then male (board-side "-M") over female, then the plainest (shortest) name.
    def rank(n: str) -> tuple:
        orientation = next(i for i, o in enumerate(orientations) if n.endswith(o))
        return (not _has_model(family.library, n), orientation, "-F_" in n, len(n), n)

    name = min(candidates, key=rank)
    return ConnectorFootprint(
        f"{family.library}:{name}", family_name, pins, _symbol(family.rows, pins)
    )


def families_text() -> str:
    """The families for a prompt, one line each."""
    return "\n".join(
        f"  - {name}_<N>P: {family.what}" for name, family in FAMILIES.items()
    )
