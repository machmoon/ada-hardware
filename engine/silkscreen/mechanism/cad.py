"""build123d parts for a MECHANISM-SPEC chain: base, links, joints.

Every dimension comes from :mod:`.layout` (itself arithmetic on
:mod:`.rules`); nothing the model said reaches this module except through the
validated spec. Integer nanometres until :func:`_mm`, the one crossing into
the kernel's millimetre floats (the ``enclosure/cad.py`` convention).

What each part is, in its own link frame (see :mod:`.layout` for frames):

* **base** -- a square plinth with an open-top pocket for the joint-1 servo,
  ear ledges and pilot holes where the actuator has ears, a horizontal cable
  exit, and four M4 desk-bolt holes (``bolt_down``). Printed as it stands.
* **link k** -- one printed part made of three features:

  - its *proximal coupling* to joint ``k``: a **clevis** for a hinge (two
    cheeks straddling the housing in link ``k-1``; the horn-side cheek carries
    the coupling hub and the spline/horn-screw bore, the idler cheek a pressed
    bearing seat with a shoulder no wider than the bearing's ``Da``, the
    bd_warehouse ``default_countersink_profile`` shape) or a **coupling
    plate** for a twist;
  - a **beam** with an open cable channel on its +X face;
  - its *distal feature*: the **housing** for joint ``k+1``'s servo (a frame
    open through X so the servo slides in, a slot in the +S wall for the
    rotor, ear slots and pilots, a pressed dowel-axle hole in the back wall
    when that joint has a bearing) or, on the last link, a **tool flange**
    with two M3 heat-set insert bores.

  Printed lying on its -X face: a link is one profile extruded along X plus
  round holes, so every face is vertical, faces up, or is a horizontal round
  hole -- the SO-ARM100 print rule "no supports in the screw holes with
  horizontal axes".

The servo, bearing, dowel and screws are vitamins: they are not in the
exported solids, and the kernel builds its own servo envelopes from the rules
table to check the pockets.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..enclosure.cad import kernel_available, require_kernel
from . import rules
from .errors import MechanismBuildError
from .ir import MechanismSpec
from .kinematics import Mat, link_frames
from .layout import (
    Layout,
    ServoPlacement,
    dowel_hole_nm,
    housing_dims,
    layout_for,
    seat_bore_nm,
)

__all__ = [
    "MechanismPart",
    "MechanismModel",
    "MechanismExports",
    "kernel_available",
    "build_mechanism",
    "posed_parts",
    "matrix_location",
    "export_mechanism",
]

#: Overshoot so a cutter never shares a face with what it cuts.
_EPS = 0.01


def _mm(value_nm: float) -> float:
    return value_nm / 1e6


@dataclass(frozen=True)
class MechanismPart:
    name: str
    link: int                 # 0 = base
    local: Any                # build123d Part in the link frame
    printed: Any              # the same Part in its printed orientation


@dataclass(frozen=True)
class MechanismModel:
    spec: MechanismSpec
    layout: Layout
    parts: tuple[MechanismPart, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def part(self, name: str) -> MechanismPart:
        for p in self.parts:
            if p.name == name:
                return p
        raise KeyError(name)


@dataclass(frozen=True)
class MechanismExports:
    step: Path
    stls: tuple[Path, ...]


class _K:
    """Thin box/cylinder helpers over build123d, in mm."""

    def __init__(self) -> None:
        self.b = require_kernel()

    def box(self, x0: float, x1: float, y0: float, y1: float, z0: float, z1: float):
        b = self.b
        lo = (min(x0, x1), min(y0, y1), min(z0, z1))
        size = (abs(x1 - x0), abs(y1 - y0), abs(z1 - z0))
        if min(size) <= 0:
            raise MechanismBuildError("INVALID_SHAPE", f"empty box {lo} {size}")
        return b.Pos(*lo) * b.Box(*size, align=(b.Align.MIN,) * 3)

    def cyl(self, axis: str, centre: Sequence[float], a0: float, a1: float, d: float):
        """Cylinder of diameter ``d`` along world ``axis`` from ``a0`` to ``a1``;
        ``centre`` gives the other two coordinates (the axis entry is ignored)."""
        b = self.b
        lo, hi = min(a0, a1), max(a0, a1)
        c = b.Cylinder(d / 2, hi - lo, align=(b.Align.CENTER, b.Align.CENTER, b.Align.MIN))
        if axis == "z":
            return b.Pos(centre[0], centre[1], lo) * c
        if axis == "y":
            # Cylinder +Z -> +Y is a -90 deg turn about X.
            return b.Pos(centre[0], lo, centre[2]) * b.Rot(-90, 0, 0) * c
        return b.Pos(lo, centre[1], centre[2]) * b.Rot(0, 90, 0) * c

    @staticmethod
    def fuse(what: str, a, parts):
        try:
            for p in parts:
                a = a + p
        except Exception as exc:  # noqa: BLE001 - OCCT raises many types
            raise MechanismBuildError("BOOLEAN_FAILED", f"{what}: {exc}") from exc
        return a

    @staticmethod
    def cut(what: str, a, parts):
        try:
            for p in parts:
                a = a - p
        except Exception as exc:  # noqa: BLE001
            raise MechanismBuildError("BOOLEAN_FAILED", f"{what}: {exc}") from exc
        return a


# --- servo-frame helpers -------------------------------------------------------

def _sbox(k: _K, sp: ServoPlacement, s: tuple[float, float], u: tuple[float, float],
          v: tuple[float, float]):
    """A box given in servo-frame nm ranges, placed in the link frame."""
    corners = [sp.to_link(si, ui, vi) for si in s for ui in u for vi in v]
    xs, ys, zs = zip(*corners, strict=True)
    return k.box(_mm(min(xs)), _mm(max(xs)), _mm(min(ys)), _mm(max(ys)),
                 _mm(min(zs)), _mm(max(zs)))


def _scyl(k: _K, sp: ServoPlacement, along: str, at: tuple[float, float],
          span: tuple[float, float], d_nm: float):
    """A cylinder along servo axis ``along`` ('s' or 'u'), at the other two
    servo coordinates ``at`` (for 's': (u, v)), over ``span`` nm."""
    if along == "s":
        p0 = sp.to_link(span[0], at[0], at[1])
        p1 = sp.to_link(span[1], at[0], at[1])
        vec = sp.s
    else:
        raise ValueError(along)
    axis = "xyz"[[abs(c) for c in vec].index(1)]
    i = "xyz".index(axis)
    centre = tuple(_mm(c) for c in p0)
    return k.cyl(axis, centre, _mm(p0[i]), _mm(p1[i]), _mm(d_nm))


def _housing(k: _K, sp: ServoPlacement, act: rules.Actuator, width_nm: int,
             bearing: str, *, top_wall: bool, outer: bool = True):
    """``(outer solid or None, [cutters])`` for a servo housing."""
    d = housing_dims(act, top_wall=top_wall)
    c = rules.POCKET_CLEARANCE_NM
    e, L_, h, back = act.shaft_from_end_nm, act.body_l_nm, act.body_h_nm, act.back_boss_nm
    half_w = width_nm / 2
    big = half_w + 1_000_000
    solid = None
    if outer:
        solid = _sbox(k, sp, (d.s_min_nm, d.s_max_nm), (d.u_min_nm, d.u_max_nm),
                      (-half_w, half_w))
    s_top = c if top_wall else d.s_max_nm + 1_000_000
    cutters = []
    if top_wall:
        cutters.append(_sbox(k, sp, (-h - back - c, s_top), (-e - c, L_ - e + c),
                             (-big, big)))
    else:
        cutters.append(_sbox(k, sp, (-h - back - c, s_top),
                             (-e - c, L_ - e + c),
                             (-act.body_w_nm / 2 - c, act.body_w_nm / 2 + c)))
    if act.ear_len_nm:
        s0 = -h + act.ear_s_nm - c
        s1 = (-h + act.ear_s_nm + act.ear_t_nm + c) if top_wall else s_top
        vr = (-big, big) if top_wall else (-act.body_w_nm / 2 - c, act.body_w_nm / 2 + c)
        cutters.append(_sbox(k, sp, (s0, s1),
                             (-e - act.ear_len_nm - c, L_ - e + act.ear_len_nm + c), vr))
        pilot = act.ear_hole_d_nm - rules.SELF_TAP_UNDERSIZE_NM
        for u in (-e - act.ear_hole_from_end_nm, L_ - e + act.ear_hole_from_end_nm):
            for v in act.ear_hole_v_nm:
                cutters.append(_scyl(k, sp, "s", (u, v),
                                     (s0 - rules.EAR_SCREW_DEPTH_NM, s0 + 10_000), pilot))
    if top_wall:
        span = (c - 10_000, d.s_max_nm + 10_000)
        cutters.append(_scyl(k, sp, "s", (0, 0), span, d.opening_d_nm))
        cutters.append(_sbox(k, sp, span, (-d.opening_d_nm / 2, d.opening_d_nm / 2),
                             (0, big)))
    if bearing != "none":
        hole = dowel_hole_nm(rules.BEARINGS[bearing])
        cutters.append(_scyl(k, sp, "s", (0, 0),
                             (d.s_min_nm - 10_000, -h - back - c + 10_000), hole))
    return solid, cutters


# --- parts ---------------------------------------------------------------------

def _build_base(k: _K, spec: MechanismSpec, lay: Layout):
    base = lay.base
    act = rules.ACTUATORS[spec.joints[0].actuator]
    half = _mm(base.half_side_nm)
    top = _mm(base.plinth_top_nm)
    solid = k.box(-half, half, -half, half, 0.0, top)
    _, cutters = _housing(k, base.servo, act, base.half_side_nm * 2, "none",
                          top_wall=False, outer=False)
    # Cable exit: a horizontal round hole from the pocket out through -X.
    floor = _mm(rules.HOUSING_WALL_NM)
    r = _mm(rules.CABLE_EXIT_D_NM) / 2
    u_mid = _mm(act.body_l_nm / 2 - act.shaft_from_end_nm)
    cutters.append(k.cyl("x", (0.0, u_mid, floor + r), -half - 1.0, 0.0,
                         _mm(rules.CABLE_EXIT_D_NM)))
    if spec.base_type == "bolt_down":
        b = _mm(base.bolt_xy_nm)
        for sx in (-1, 1):
            for sy in (-1, 1):
                cutters.append(k.cyl("z", (sx * b, sy * b, 0.0), -1.0, top + 1.0,
                                     _mm(rules.BASE_BOLT_CLEARANCE_NM
                                         + rules.HOLE_COMPENSATION_NM)))
    return k.cut("base", solid, cutters)


def _build_link(k: _K, spec: MechanismSpec, lay: Layout, idx: int):
    ll = lay.links[idx - 1]
    joint = spec.joints[idx - 1]
    act = rules.ACTUATORS[joint.actuator]
    hw = _mm(ll.width_nm) / 2
    L = _mm(ll.length_nm)
    solids, cutters = [], []
    mw = rules.MIN_WALL_NM

    # proximal coupling
    if ll.hinge:
        yc, tc = _mm(ll.yc_nm), _mm(ll.cheek_t_nm)
        zr, zb = -_mm(ll.cheek_r_nm), _mm(ll.bridge_z_nm)
        ztop = zb + _mm(rules.BRIDGE_T_NM)
        solids.append(k.box(-hw, hw, yc, yc + tc, zr, ztop))
        solids.append(k.box(-hw, hw, -yc - tc, -yc, zr, ztop))
        solids.append(k.box(-hw, hw, -yc - tc, yc + tc, zb, ztop))
        face = yc - _mm(ll.hub_len_nm)
        if ll.hub_len_nm > 0:
            solids.append(k.cyl("y", (0.0, 0.0, 0.0), face, yc + _EPS, _mm(ll.hub_d_nm)))
        cutters.append(k.cyl("y", (0.0, 0.0, 0.0), face - _EPS,
                             face + _mm(act.spline_h_nm),
                             _mm(act.spline_d_nm + 2 * rules.FIT_SLIDE_NM)))
        if joint.bearing != "none":
            br = rules.BEARINGS[joint.bearing]
            seat = _mm(seat_bore_nm(br))
            # Recessed one slide fit so the outer ring never rubs the housing.
            depth = _mm(br.width_nm + rules.FIT_SLIDE_NM)
            cutters.append(k.cyl("y", (0.0, 0.0, 0.0), -yc + _EPS, -yc - depth, seat))
            shoulder = _mm((br.bore_nm + br.shoulder_max_nm) / 2)
            cutters.append(k.cyl("y", (0.0, 0.0, 0.0), -yc - depth + _EPS,
                                 -yc - tc - 1.0, shoulder))
    else:
        pt = _mm(ll.plate_t_nm)
        y0 = min(-hw, _mm(ll.beam_y_nm[0]))
        y1 = max(hw, _mm(ll.beam_y_nm[1]))
        solids.append(k.box(-hw, hw, y0, y1, 0.0, pt))
        face = -_mm(ll.hub_len_nm)
        if ll.hub_len_nm > 0:
            solids.append(k.cyl("z", (0.0, 0.0, 0.0), face, _EPS, _mm(ll.hub_d_nm)))
        cutters.append(k.cyl("z", (0.0, 0.0, 0.0), face - _EPS,
                             face + _mm(act.spline_h_nm),
                             _mm(act.spline_d_nm + 2 * rules.FIT_SLIDE_NM)))

    # beam
    za, zb_ = _mm(ll.z_a_nm), _mm(ll.z_b_nm)
    by0, by1 = _mm(ll.beam_y_nm[0]), _mm(ll.beam_y_nm[1])
    if zb_ - za > _EPS:
        solids.append(k.box(-hw, hw, by0, by1, za - _EPS, zb_ + _EPS))
        width = min(_mm(rules.CABLE_CHANNEL_W_NM), (by1 - by0) - 2 * _mm(mw))
        depth = min(_mm(rules.CABLE_CHANNEL_D_NM), hw - _mm(mw))
        if width > 1.0 and depth > 0.5 and zb_ - za > 2.0:
            ym = (by0 + by1) / 2
            cutters.append(k.box(hw - depth, hw + 1.0, ym - width / 2, ym + width / 2,
                                 za, zb_))

    # distal feature
    if ll.distal in ("hinge", "twist"):
        nxt = spec.joints[idx]
        nact = rules.ACTUATORS[nxt.actuator]
        solid, cut = _housing(k, ll.servo, nact, ll.width_nm, nxt.bearing, top_wall=True)
        solids.append(solid)
        cutters.extend(cut)
    else:
        solids.append(k.box(-hw, hw, -hw, hw, zb_, L))
        ins = rules.FLANGE_INSERT
        r = _mm(ins.bore_nm) / 2
        yb = hw - _mm(rules.BOSS_MIN_WALL_NM) - r
        for sy in (-1, 1):
            cutters.append(k.cyl("z", (0.0, sy * yb, 0.0),
                                 L - _mm(ins.length_nm + rules.INSERT_BORE_EXTRA_NM),
                                 L + 1.0, 2 * r))

    first, rest = solids[0], solids[1:]
    part = k.fuse(f"link {idx}", first, rest)
    return k.cut(f"link {idx}", part, cutters)


def _check(part: Any, name: str) -> None:
    solids = list(part.solids())
    if not solids:
        raise MechanismBuildError("INVALID_SHAPE", f"{name}: no solid")


def _printed(k: _K, part: Any, link: int):
    """Links lie on their -X face (X -> print Z); the base prints as built."""
    b = k.b
    shape = part if link == 0 else b.Rot(0, -90, 0) * part
    bb = shape.bounding_box()
    return b.Pos(-bb.min.X, -bb.min.Y, -bb.min.Z) * shape


def build_mechanism(spec: MechanismSpec) -> MechanismModel:
    """Build every printed part. Raises :class:`MechanismBuildError` for a link
    too short for its housings, a failed boolean, or an empty shape; and
    :class:`~silkscreen.enclosure.errors.KernelUnavailable` without build123d."""
    k = _K()
    lay = layout_for(spec)
    parts: list[MechanismPart] = []
    base = _build_base(k, spec, lay)
    _check(base, "base")
    parts.append(MechanismPart("base", 0, base, _printed(k, base, 0)))
    for idx in range(1, len(spec.joints) + 1):
        link = _build_link(k, spec, lay, idx)
        name = f"link{idx}_{spec.joints[idx - 1].id}"
        _check(link, name)
        parts.append(MechanismPart(name, idx, link, _printed(k, link, idx)))
    return MechanismModel(spec=spec, layout=lay, parts=tuple(parts))


def matrix_location(m: Mat):
    """A build123d ``Location`` from a 4x4 mm matrix."""
    b = require_kernel()
    from OCP.gp import gp_Trsf

    t = gp_Trsf()
    t.SetValues(m[0][0], m[0][1], m[0][2], m[0][3],
                m[1][0], m[1][1], m[1][2], m[1][3],
                m[2][0], m[2][1], m[2][2], m[2][3])
    return b.Location(t)


def posed_parts(model: MechanismModel, q_rad: Sequence[float] | None = None) -> list[tuple[str, Any]]:
    """Every part moved to the world frame at pose ``q_rad`` (default home)."""
    q = [0.0] * len(model.spec.joints) if q_rad is None else list(q_rad)
    frames = link_frames(model.spec, model.layout.offsets_nm, q)
    return [(p.name, p.local.moved(matrix_location(frames[p.link]))) for p in model.parts]


def export_mechanism(
    model: MechanismModel, directory: str | Path, stem: str = "mechanism",
    q_rad: Sequence[float] | None = None,
) -> MechanismExports:
    """``<stem>.step`` (the assembly at ``q_rad``, one labelled child per part)
    and ``<stem>-<part>.stl`` per part in printed orientation."""
    b = require_kernel()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    children = [b.Part(shape.wrapped, label=name) for name, shape in posed_parts(model, q_rad)]
    step = directory / f"{stem}.step"
    try:
        b.export_step(b.Compound(label=stem, children=children), step)
        stls = []
        for p in model.parts:
            path = directory / f"{stem}-{p.name}.stl"
            b.export_stl(p.printed, path)
            stls.append(path)
    except Exception as exc:  # noqa: BLE001
        raise MechanismBuildError("INVALID_SHAPE", f"export: {exc}") from exc
    return MechanismExports(step=step, stls=tuple(stls))
