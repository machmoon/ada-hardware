"""Load a KiCad library footprint as a :class:`silkscreen.footprints.Footprint`.

The placer and router work in rectangles around an anchor at the courtyard's
centre; a library ``.kicad_mod`` is drawn around its own origin (often pin 1)
with rounded-rectangle, oval and custom pads. So a loaded footprint is two
things kept together:

* a :class:`~silkscreen.footprints.Footprint` the engine can place and route --
  every copper pad as its axis-aligned bounding rectangle (conservative: the
  router keeps clear of the corners a ``roundrect`` does not actually have),
  re-centred on the courtyard, and the courtyard from ``F.CrtYd`` bounded by
  :func:`silkscreen.kicad._courtyard_points`, the same shape-aware bound the
  board reader uses;
* the library's own text and the offset from that anchor back to the library
  origin, so an emitter can write the footprint KiCad itself would have put
  on the board -- exact pad shapes, silkscreen, fab layer and 3D model --
  instead of a redrawn approximation.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..footprints import Footprint, Pad
from ..units import mm
from .paths import footprint_dir

__all__ = ["LibraryFootprint", "load_footprint"]

#: Courtyard excess used when a library footprint draws no ``F.CrtYd`` (rare;
#: KLC requires one): IPC-7351 nominal, the :func:`fit_courtyard` default.
_FALLBACK_EXCESS_MM = 0.25


@dataclass(frozen=True)
class LibraryFootprint:
    lib_id: str
    footprint: Footprint
    #: Where the library origin sits relative to the courtyard-centred anchor,
    #: in nm, KiCad frame (Y down): place the library footprint at
    #: ``anchor + origin_offset_nm``.
    origin_offset_nm: tuple[int, int]
    #: The ``.kicad_mod`` source, verbatim.
    text: str
    models: tuple[str, ...] = field(default_factory=tuple)


def _pad_rect(pad) -> tuple[float, float, float, float]:
    """Axis-aligned (cx, cy, w, h) in mm, footprint frame, for one pad."""
    w, h = float(pad.size.X), float(pad.size.Y or pad.size.X)
    angle = float(pad.position.angle or 0.0) % 180.0
    if angle:
        r = math.radians(angle)
        c, s = abs(math.cos(r)), abs(math.sin(r))
        w, h = w * c + h * s, w * s + h * c
    return float(pad.position.X), float(pad.position.Y), w, h


def _bbox(
    points: list[tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def load_footprint(lib_id: str, root: Path | None = None) -> LibraryFootprint:
    """``"Package_TO_SOT_SMD:SOT-223-3_TabPin2"`` -> a placeable footprint.

    Raises ``FileNotFoundError`` naming the path when the library or the
    footprint is not installed, and ``ValueError`` for an id without a colon --
    a footprint that cannot be found is a stated refusal, never an empty part.
    """
    from kiutils.footprint import Footprint as KiFootprint

    from ..kicad import _courtyard_points

    if ":" not in lib_id:
        raise ValueError(f"footprint id {lib_id!r} must be 'Library:Name'")
    root = root or footprint_dir()
    if root is None:
        raise FileNotFoundError("no KiCad footprint library is installed")
    library, name = lib_id.split(":", 1)
    path = root / f"{library}.pretty" / f"{name}.kicad_mod"
    if not path.is_file():
        raise FileNotFoundError(f"{lib_id}: {path} does not exist")
    text = path.read_text(encoding="utf-8")
    kfp = KiFootprint().from_file(str(path))

    copper = [p for p in kfp.pads if p.type in ("smd", "thru_hole")]
    rects = [(p, *_pad_rect(p)) for p in copper]

    court: list[tuple[float, float]] = []
    body: list[tuple[float, float]] = []
    for item in kfp.graphicItems:
        layer = getattr(item, "layer", "")
        if layer == "F.CrtYd":
            court.extend(_courtyard_points(item))
        elif layer == "F.Fab":
            # Fab text and the like have no bound; only shapes count.
            with contextlib.suppress(Exception):
                body.extend(_courtyard_points(item))
    pad_points = [
        pt
        for _, cx, cy, w, h in rects
        for pt in ((cx - w / 2, cy - h / 2), (cx + w / 2, cy + h / 2))
    ]
    box = _bbox(court)
    if box is None:
        pb = _bbox(pad_points)
        if pb is None:
            raise ValueError(f"{lib_id}: no courtyard and no copper pads to bound")
        e = _FALLBACK_EXCESS_MM
        box = (pb[0] - e, pb[1] - e, pb[2] + e, pb[3] + e)
    x0, y0, x1, y1 = box
    cx0, cy0 = (x0 + x1) / 2, (y0 + y1) / 2

    pads = [
        Pad(
            str(p.number),
            mm(cx - cx0),
            mm(cy - cy0),
            mm(w),
            mm(h),
            "",
            mm(float(p.drill.diameter)) if p.type == "thru_hole" and p.drill else 0,
        )
        for p, cx, cy, w, h in rects
    ]
    bb = _bbox(body)
    if bb is not None:
        body_w = max(abs(bb[0] - cx0), abs(bb[2] - cx0))
        body_h = max(abs(bb[1] - cy0), abs(bb[3] - cy0))
    else:
        body_w, body_h = (x1 - x0) / 2, (y1 - y0) / 2
    fp = Footprint(
        name=name,
        pads=pads,
        courtyard_w_nm=mm((x1 - x0) / 2),
        courtyard_h_nm=mm((y1 - y0) / 2),
        body_w_nm=mm(body_w),
        body_h_nm=mm(body_h),
        description=f"KiCad library {lib_id}",
    )
    return LibraryFootprint(
        lib_id=lib_id,
        footprint=fp,
        origin_offset_nm=(mm(-cx0), mm(-cy0)),
        text=text,
        models=tuple(m.path for m in kfp.models),
    )
