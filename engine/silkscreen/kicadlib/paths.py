"""Where KiCad's libraries are installed on this machine.

An environment variable wins (KiCad's own ``KICADn_SYMBOL_DIR`` /
``KICADn_FOOTPRINT_DIR`` spellings, newest first, or this repo's
``KICAD_SYMBOL_DIR`` / ``KICAD_FOOTPRINT_DIR``), then the platform install
locations -- the same candidates ``engine/tests/test_models3d.py`` uses. None
means "not installed here", never a guess.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

__all__ = ["footprint_dir", "model_dir", "symbol_dir"]

_ROOTS = (
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport",
    "/usr/share/kicad",
    "/usr/local/share/kicad",
    "C:/Program Files/KiCad/*/share/kicad",
)


def _find(kind: str, env_suffix: str) -> Path | None:
    names = [f"KICAD{n}_{env_suffix}" for n in range(12, 5, -1)]
    names.append(f"KICAD_{env_suffix}")
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value) if Path(value).is_dir() else None
    for root in _ROOTS:
        for hit in sorted(glob.glob(root), reverse=True):
            candidate = Path(hit) / kind
            if candidate.is_dir():
                return candidate
    return None


def symbol_dir() -> Path | None:
    """The directory holding ``*.kicad_sym`` files, or None."""
    return _find("symbols", "SYMBOL_DIR")


def model_dir() -> Path | None:
    """The directory holding ``*.3dshapes`` STEP models, or None."""
    return _find("3dmodels", "3DMODEL_DIR")


def footprint_dir() -> Path | None:
    """The directory holding ``*.pretty`` footprint libraries, or None."""
    return _find("footprints", "FOOTPRINT_DIR")
