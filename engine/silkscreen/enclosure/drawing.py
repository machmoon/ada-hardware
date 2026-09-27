"""Engineering drawings of the printed case parts, measured from the solids.

One SVG sheet per printed part (``<stem>-base.svg``, ``<stem>-lid.svg``): plan,
front and side views with hidden lines, an isometric view, overall dimensions,
a title block, and a schedule of the features the solid actually has -- every
round hole or boss with its diameter and centre, and every outline cut in a
flat face (a wall opening, a pocket, a lip step) with its width and height.

Two sources, both read as code:

- The sheet is build123d's own technical-drawing recipe (gumyr/build123d
  ``279f7b1`` ``docs/technical_drawing.py``, Apache-2.0): ``project_to_viewport``
  for visible and hidden edges, a ``TechnicalDrawing`` border, ``ExtensionLine``
  dimensions, ``ExportSVG`` with a dotted "Hidden" layer.
- The rule that **every number on the sheet is measured from the B-rep, never
  copied from the spec** is CADEX's (Hack the North 2026 finalist,
  justinbaduaa/htn2026 ``cad/measure.py``: "No scan inputs, generated Python, or
  inferred dimensions"), including its test for a full hole -- a cylindrical
  face spanning a whole turn, inside or outside by its normal -- and its
  treatment of a partial cylinder as a radius, never a hole. That repository has
  no licence, so the design is followed and none of its code is used.

The measurement is deliberately independent of ``cad.py``'s own numbers: a
drawing that printed ``spec.wall`` would agree with the builder by construction
and could never show a case that came out wrong.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .cad import EnclosureModel, require_kernel

#: Axis-alignment tolerance for a face normal or cylinder axis.
_AXIS_TOL = 1e-6
#: Schedule rows printed on the sheet; the rest are in the receipt.
_SCHEDULE_ROWS = 9
#: Drawing scales tried in order until the views fit the page (printed 1:N).
_SCALES = (1.0, 0.5, 0.4, 0.25, 0.2, 0.1)


@dataclass(frozen=True)
class Hole:
    """A full cylindrical face: ``bore`` (material outside it) or ``boss``."""

    kind: str
    axis: str
    diameter_mm: float
    centre_mm: tuple[float, float, float]  # from the part's minimum corner
    span_mm: tuple[float, float]  # along the axis, same origin


@dataclass(frozen=True)
class Outline:
    """A non-round inner loop on flat faces normal to ``axis``: a wall opening,
    a pocket, a lip step. The same loop seen on several faces (both sides of a
    wall, a chamfered mouth) is one outline with every face ``levels_mm`` lists;
    ``width_mm`` x ``height_mm`` is the smallest, i.e. the clear size, and
    ``largest_mm`` the biggest. CADEX's profiles carry "levels" the same way."""

    axis: str
    width_mm: float
    height_mm: float
    largest_mm: tuple[float, float]
    centre_mm: tuple[float, float]  # the two coordinates across ``axis``
    levels_mm: tuple[float, ...]  # face positions along ``axis``


@dataclass(frozen=True)
class Measured:
    size_mm: tuple[float, float, float]
    holes: tuple[Hole, ...]
    outlines: tuple[Outline, ...]
    partial_cylinders: int  # fillets and arcs: counted, not drawn as holes

    def schedule(self) -> list[str]:
        rows = []
        for i, h in enumerate(self.holes, 1):
            x, y, z = h.centre_mm
            rows.append(
                f"H{i}  dia {h.diameter_mm:.2f} {h.kind}, axis {h.axis}, "
                f"at X {x:.2f} Y {y:.2f} Z {z:.2f}, "
                f"{h.axis} {h.span_mm[0]:.2f} to {h.span_mm[1]:.2f}"
            )
        for i, o in enumerate(self.outlines, 1):
            across = [a for a in "XYZ" if a != o.axis]
            size = f"{o.width_mm:.2f} x {o.height_mm:.2f}"
            if o.largest_mm != (o.width_mm, o.height_mm):
                size += f" (up to {o.largest_mm[0]:.2f} x {o.largest_mm[1]:.2f})"
            levels = ", ".join(f"{v:.2f}" for v in o.levels_mm)
            rows.append(
                f"P{i}  {size} outline normal to {o.axis}, centre "
                f"{across[0]} {o.centre_mm[0]:.2f} {across[1]} "
                f"{o.centre_mm[1]:.2f}, faces at {o.axis} {levels}"
            )
        if self.partial_cylinders:
            rows.append(
                f"{self.partial_cylinders} partial cylindrical face(s) "
                "(fillets, rounded corners): not dimensioned"
            )
        return rows


@dataclass(frozen=True)
class Sheet:
    part: str
    path: Path
    scale: float
    measured: Measured


@dataclass(frozen=True)
class DrawingReceipt:
    sheets: tuple[Sheet, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


# -- measuring --------------------------------------------------------------------


def _axis_name(vector: tuple[float, float, float]) -> str | None:
    for i, name in enumerate("XYZ"):
        if abs(abs(vector[i]) - 1.0) < _AXIS_TOL:
            return name
    return None


def measure(shape: Any) -> Measured:
    """The size, round holes and flat-face outlines ``shape`` really has, in mm
    from its minimum corner."""
    b = require_kernel()
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    box = shape.bounding_box()
    origin = (box.min.X, box.min.Y, box.min.Z)
    size = (box.size.X, box.size.Y, box.size.Z)

    def rel(p) -> tuple[float, float, float]:
        return (p[0] - origin[0], p[1] - origin[1], p[2] - origin[2])

    holes: dict[tuple, Hole] = {}
    loops: dict[tuple, list[tuple[float, float, float]]] = {}
    partial = 0
    for face in shape.faces():
        kind = face.geom_type
        if kind == b.GeomType.CYLINDER:
            surface = BRepAdaptor_Surface(face.wrapped)
            cylinder = surface.Cylinder()
            d = cylinder.Axis().Direction()
            axis = _axis_name((d.X(), d.Y(), d.Z()))
            sweep = surface.LastUParameter() - surface.FirstUParameter()
            if axis is None or abs(sweep - 2 * math.pi) > 1e-6:
                partial += 1
                continue
            k = "XYZ".index(axis)
            loc = cylinder.Location()
            centre = [loc.X(), loc.Y(), loc.Z()]
            fb = face.bounding_box()
            lo, hi = (
                (fb.min.X, fb.min.Y, fb.min.Z)[k],
                (fb.max.X, fb.max.Y, fb.max.Z)[k],
            )
            centre[k] = (lo + hi) / 2
            # Inside or outside: the face normal points away from the axis on a
            # boss and towards it in a bore.
            u = (surface.FirstUParameter() + surface.LastUParameter()) / 2
            v = (surface.FirstVParameter() + surface.LastVParameter()) / 2
            p = surface.Value(u, v)
            point = (p.X(), p.Y(), p.Z())
            radial = [point[i] - centre[i] if i != k else 0.0 for i in range(3)]
            normal = face.normal_at(b.Vector(*point))
            inward = sum(r * n for r, n in zip(radial, tuple(normal), strict=True)) < 0
            c = rel(centre)
            hole = Hole(
                "bore" if inward else "boss",
                axis,
                2 * cylinder.Radius(),
                tuple(round(x, 4) for x in c),
                (round(lo - origin[k], 4), round(hi - origin[k], 4)),
            )
            holes.setdefault(
                (
                    hole.kind,
                    axis,
                    hole.centre_mm,
                    round(hole.diameter_mm, 4),
                    hole.span_mm,
                ),
                hole,
            )
        elif kind == b.GeomType.PLANE:
            n = tuple(face.normal_at(face.center()))
            axis = _axis_name(n)
            if axis is None:
                continue
            k = "XYZ".index(axis)
            for wire in face.inner_wires():
                if all(e.geom_type == b.GeomType.CIRCLE for e in wire.edges()):
                    continue  # a round hole: its cylinder is already measured
                wb = wire.bounding_box()
                lo = (wb.min.X, wb.min.Y, wb.min.Z)
                hi = (wb.max.X, wb.max.Y, wb.max.Z)
                across = [i for i in range(3) if i != k]
                centre = tuple(
                    round((lo[i] + hi[i]) / 2 - origin[i], 2) for i in across
                )
                loops.setdefault((axis, *centre), []).append(
                    (
                        round(lo[k] - origin[k], 4),
                        round(hi[across[0]] - lo[across[0]], 4),
                        round(hi[across[1]] - lo[across[1]], 4),
                    )
                )
    outlines = []
    for (axis, c0, c1), seen in loops.items():
        smallest = min(seen, key=lambda t: t[1] * t[2])
        largest = max(seen, key=lambda t: t[1] * t[2])
        outlines.append(
            Outline(
                axis,
                smallest[1],
                smallest[2],
                (largest[1], largest[2]),
                (c0, c1),
                tuple(sorted({t[0] for t in seen})),
            )
        )
    return Measured(
        tuple(round(s, 4) for s in size),
        tuple(sorted(holes.values(), key=lambda h: (h.axis, h.centre_mm))),
        tuple(sorted(outlines, key=lambda o: (o.axis, o.centre_mm))),
        partial,
    )


# -- the sheet --------------------------------------------------------------------


def _views(b, part, s: float, page) -> tuple[list, list, dict]:
    """Plan, front, side and isometric, placed on the page; the recipe's
    ``project_to_2d`` with the part centred on the origin first."""
    centred = b.Pos(*(-part.bounding_box().center())) * part
    if s != 1.0:
        centred = b.scale(centred, s)
    width, height = page.X, page.Y
    places = {
        "plan": ((0, 0, 100), (0, 1, 0), (-0.25 * width, 0.18 * height)),
        "front": ((0, -100, 0), (0, 0, 1), (-0.25 * width, -0.14 * height)),
        "side": ((100, 0, 0), (0, 0, 1), (0.05 * width, -0.14 * height)),
        "iso": ((100, -100, 100), (0, 0, 1), (0.05 * width, 0.18 * height)),
    }
    visible, hidden, per_view = [], [], {}
    for name, (eye, up, at) in places.items():
        vis, hid = centred.project_to_viewport(eye, up, look_at=(0, 0, 0))
        vis = [b.Pos(*at) * e for e in vis]
        hid = [b.Pos(*at) * e for e in hid]
        visible.extend(vis)
        if name != "iso":
            hidden.extend(hid)
        per_view[name] = b.ShapeList(vis)
    return visible, hidden, per_view


#: Chord length for flattening curves on paper, mm. Half a millimetre is below
#: what a printed A4 sheet shows at 1:1.
_CHORD_MM = 0.5


def _flatten(b, shapes) -> list:
    """Every edge of ``shapes`` as straight segments.

    build123d's ``ExportSVG`` converts each curved edge to Bezier arcs by
    iterating ``Geom_BezierCurve.Poles()`` through the OCP bindings, about
    16 ms a curve here; a case sheet has thousands (projected fillets, circles,
    font glyphs), and one lid took 68 s, all of it in that loop
    (``exporters.py`` ``_bspline_segments``, measured with cProfile
    2026-09-27). Straight lines export directly, so curves are flattened
    first; nothing on the sheet is a measurement, those are in the schedule.
    """
    out = []
    for shape in shapes:
        for edge in shape.edges():
            if edge.geom_type == b.GeomType.LINE:
                out.append(edge)
                continue
            n = max(2, min(96, math.ceil(edge.length / _CHORD_MM) + 1))
            points = [edge.position_at(i / (n - 1)) for i in range(n)]
            out.extend(
                b.Edge.make_line(a, c)
                for a, c in zip(points, points[1:], strict=False)
                if (c - a).length > 1e-9
            )
    return out


def _fits(size_mm: tuple[float, float, float], s: float, page) -> bool:
    """Each orthographic view gets a quarter-page cell, less room for dimensions."""
    x, y, z = (v * s for v in size_mm)
    cell_w, cell_h = 0.38 * page.X, 0.26 * page.Y
    return max(x, y) <= cell_w and max(y, z) <= cell_h


def _ratio(scale: float) -> float | int:
    """The N of "1:N": 1 not 1.0, 2.5 stays 2.5."""
    n = round(1 / scale, 2)
    return int(n) if n == int(n) else n


def _border(b, *, part: str, title: str, when: date | None, scale: float):
    return b.TechnicalDrawing(
        designed_by="Ada",
        design_date=when or date.today(),
        page_size=b.PageSize.A4,
        title=title,
        # Short on purpose: TechnicalDrawing does not wrap
        # its title box, and "base, units mm, measured from
        # the solid" ran 82 mm off an A4 page.
        sub_title=f"{part} (mm)",
        drawing_number=f"ADA-{part.upper()}",
        sheet_number=1,
        drawing_scale=_ratio(scale),
    )


def compose(
    shape: Any, *, part: str, title: str, when: date | None = None
) -> tuple[Measured, float, dict[str, list]]:
    """The sheet as shape groups -- ``visible``, ``hidden``, ``border``,
    ``dims``, ``labels`` -- in page millimetres, plus what was measured and
    the scale chosen. :func:`draw_part` writes them; tests read them."""
    b = require_kernel()
    measured = measure(shape)
    page = _border(b, part=part, title=title, when=when, scale=1.0).bounding_box().size
    s = next((c for c in _SCALES if _fits(measured.size_mm, c, page)), _SCALES[-1])
    border = _border(b, part=part, title=title, when=when, scale=s)
    visible, hidden, views = _views(b, shape, s, page)

    # Dimensions print the model length, not the paper length: build123d's
    # ExtensionLine measures what it is given, so a scaled view is labelled with
    # its measured value divided back by the scale via a custom label.
    draft = b.Draft(font_size=3.0, decimal_precision=2, display_units=False)
    dims = []
    x_mm, y_mm, z_mm = measured.size_mm

    def extension(edge, offset, value):
        return b.ExtensionLine(
            border=edge, offset=offset, draft=draft, label=f"{value:.2f}"
        )

    plan = b.Curve(views["plan"]).bounding_box()
    rect = b.Pos(*plan.center()) * b.Rectangle(plan.size.X, plan.size.Y)
    dims.append(extension(rect.edges().sort_by(b.Axis.Y)[0], 6, x_mm))
    dims.append(extension(rect.edges().sort_by(b.Axis.X)[0], 6, y_mm))
    front = b.Curve(views["front"]).bounding_box()
    rect = b.Pos(*front.center()) * b.Rectangle(front.size.X, front.size.Y)
    dims.append(extension(rect.edges().sort_by(b.Axis.X)[0], 6, z_mm))

    labels = []
    for name, text in (
        ("plan", "Plan"),
        ("front", "Front"),
        ("side", "Side"),
        ("iso", "Isometric"),
    ):
        vb = b.Curve(views[name]).bounding_box()
        labels.append(b.Pos(vb.center().X, vb.max.Y + 5) * b.Text(text, 4))
    # The schedule sits in the bottom-left band, inside the frame and left of
    # the title block: TechnicalDrawing's frame is ``margin`` in from the page
    # edge plus a 10 mm zone strip (drafting.py ``TechnicalDrawing``).
    schedule = measured.schedule()
    rows = [
        "Measured from the solid, mm from the part's minimum corner:",
        *schedule[:_SCHEDULE_ROWS],
    ]
    if len(schedule) > _SCHEDULE_ROWS:
        rows.append(f"... and {len(schedule) - _SCHEDULE_ROWS} more (see the receipt)")
    left = -page.X / 2 + border.margin + 12
    bottom = -page.Y / 2 + border.margin + 12
    for i, row in enumerate(rows):
        y = bottom + (len(rows) - 1 - i) * 3.0
        labels.append(
            b.Pos(left, y) * b.Text(row, 2.0, align=(b.Align.MIN, b.Align.MIN))
        )

    return (
        measured,
        s,
        {
            "visible": visible,
            "hidden": hidden,
            "border": [border],
            "dims": dims,
            "labels": labels,
        },
    )


def draw_part(
    shape: Any, path: Path, *, part: str, title: str, when: date | None = None
) -> Sheet:
    """Write one part's sheet to ``path`` and return what it measured."""
    b = require_kernel()
    measured, s, groups = compose(shape, part=part, title=title, when=when)
    visible, hidden = _flatten(b, groups["visible"]), _flatten(b, groups["hidden"])
    decorations = _flatten(b, [*groups["border"], *groups["dims"], *groups["labels"]])
    exporter = b.ExportSVG(unit=b.Unit.MM)
    exporter.add_layer("Visible")
    exporter.add_layer("Hidden", line_color=(99, 99, 99), line_type=b.LineType.ISO_DOT)
    exporter.add_shape(visible, layer="Visible")
    exporter.add_shape(hidden, layer="Hidden")
    exporter.add_shape(decorations, layer="Visible")
    path.parent.mkdir(parents=True, exist_ok=True)
    exporter.write(str(path))
    return Sheet(part, path, s, measured)


def draw_model(
    model: EnclosureModel,
    directory: str | Path,
    stem: str = "enclosure",
    *,
    title: str = "Ada case",
    when: date | None = None,
) -> DrawingReceipt:
    """``<stem>-base.svg`` and, when there is a lid, ``<stem>-lid.svg``, each in
    the part's printed orientation (what the printer and the person holding
    the part see)."""
    directory = Path(directory)
    sheets = [
        draw_part(
            model.base,
            directory / f"{stem}-base.svg",
            part="base",
            title=title,
            when=when,
        )
    ]
    if model.lid is not None:
        sheets.append(
            draw_part(
                model.lid,
                directory / f"{stem}-lid.svg",
                part="lid",
                title=title,
                when=when,
            )
        )
    warnings = tuple(
        f"{s.part} drawn at 1:{_ratio(s.scale)} to fit A4"
        for s in sheets
        if s.scale != 1.0
    )
    return DrawingReceipt(tuple(sheets), warnings)
