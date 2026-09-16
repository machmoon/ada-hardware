"""KiCad's own symbol and footprint libraries, as a catalog the engine can use.

Until 2026-09-14 every land pattern here was generated in
:mod:`silkscreen.footprints` and every symbol in :mod:`silkscreen.schematic`: a
few dozen packages, with pin maps the model had to invent. KiCad installs
22,860 symbols across 224 libraries -- each with verified pin names and numbers
and usually a default footprint (``Regulator_Linear:AMS1117-3.3`` ->
``Package_TO_SOT_SMD:SOT-223-3_TabPin2``) -- plus 155 footprint libraries and
3 GB of 3D models, on every machine that has KiCad. This package reads them.

It follows the tools that already solved this in the open. SKiDL
(``skidl/tools/kicad*/lib.py``) and KiCad's own "Update PCB from Schematic"
both take the part from the installed library rather than describing it anew;
KiCad copies the library footprint into the board verbatim, which is what
:class:`~silkscreen.kicadlib.footprint.LibraryFootprint` keeps the text for.
Parsing is ``kiutils`` (already a dependency), and courtyards are bounded by
:func:`silkscreen.kicad._courtyard_points`, the shape-aware bound the board
reader already trusts.

The datasheet retrieval pipeline (``agents/retrieval.py``, ``agents/grounding.py``)
is a different lane and is not touched here.
"""

import os
import threading

from .footprint import LibraryFootprint, load_footprint
from .index import LibraryIndex, SymbolEntry, load_index
from .paths import footprint_dir, symbol_dir

__all__ = [
    "LibraryFootprint",
    "LibraryIndex",
    "SymbolEntry",
    "enabled",
    "footprint_dir",
    "library_index",
    "load_footprint",
    "load_index",
    "symbol_dir",
]

_lock = threading.Lock()
_index: LibraryIndex | None = None


def enabled() -> bool:
    """Whether designs use KiCad's installed libraries.

    On when both libraries are installed, unless ``SILKSCREEN_KICAD_LIBRARY``
    is ``0`` (the test suite's default, see the root ``conftest.py``). Off means
    exactly the engine's own generated land patterns, as before.
    """
    if os.environ.get("SILKSCREEN_KICAD_LIBRARY", "").strip() == "0":
        return False
    return symbol_dir() is not None and footprint_dir() is not None


def library_index() -> LibraryIndex | None:
    """The process-wide catalog, loaded once; None when not enabled."""
    global _index
    if not enabled():
        return None
    with _lock:
        if _index is None:
            _index = load_index()
        return _index
