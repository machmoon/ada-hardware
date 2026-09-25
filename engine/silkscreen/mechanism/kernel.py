"""Acceptance gate for a built :class:`~.cad.MechanismModel`.

The enclosure kernel's contract (``enclosure/kernel.py``) applied to a
mechanism: every clause is measured -- on the B-rep by OCCT, or on the chain
by :mod:`.kinematics` with masses the B-rep supplied -- and reported with a
**signed margin**; negative means violated, by that much. A clause that
cannot be evaluated fails with a reason; a clause with genuinely nothing to
measure passes with margin 0 and a detail beginning ``nothing to check``.

Clause names (stable; they are the vocabulary of the repair prompt):

``valid_solids``           every printed part is one valid solid of positive volume
``print_bed``              every part, in its printed orientation, fits the
                           220 x 220 x 250 bed
``servo_pocket``           each servo envelope (built here from the rules table)
                           sits in its housing without interference, with at
                           least a press-fit gap
``bearing_seat``           each seat bore, found on the B-rep, presses its
                           bearing with an interference inside the band, is deep
                           enough, and is on the axis
``min_wall``               ray-sampled outer walls >= the printable minimum
                           (enclosure sampler)
``overhang``               no down-facing face steeper than 45 deg in printed
                           orientation (enclosure measurement)
``self_collision_home``    no two parts touch or interfere at the home pose
``self_collision_sampled`` the same at the outstretched pose and at every
                           joint's range limits
``reach``                  max horizontal tool reach inside the ranges >= the
                           spec's target
``joint_torque``           worst joint: stall x derating - gravity moment, outstretched
``base_stability``         freestanding: the base resists the outstretched
                           overturning moment
``total_mass``             printed parts + vitamins + tool within the spec's mass budget

Units: each :class:`Clause` carries ``margin`` as an integer in millionths
of ``unit`` (``mm``, ``N-mm``, ``deg``, ``g``, ``defects``), so ``text()``
prints ``margin / 1e6`` followed by the unit. Volumes are reported as the side
of the cube with that volume, negative, as in the enclosure kernel.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from ..enclosure import kernel as enclosure_kernel
from . import rules
from .cad import MechanismModel, posed_parts, require_kernel
from .kinematics import (
    JointTorque,
    PointMass,
    apply,
    link_frames,
    max_reach_nm,
    outstretched_pose,
    static_torques,
    tool_point,
)
from .layout import ServoPlacement

__all__ = [
    "CLAUSES",
    "Clause",
    "MechanismReport",
    "UNEVALUATED",
    "part_mass_mg",
    "sampled_poses",
    "verify_mechanism",
]

CLAUSES: tuple[str, ...] = (
    "valid_solids",
    "print_bed",
    "servo_pocket",
    "bearing_seat",
    "min_wall",
    "overhang",
    "self_collision_home",
    "self_collision_sampled",
    "reach",
    "joint_torque",
    "base_stability",
    "total_mass",
)

UNEVALUATED: int = -1_000_000
_NOTHING = "nothing to check"
_VOLUME_TOL_MM3 = 1e-6
_NEAR_MM = 5.0   # exact distance only for pairs whose boxes are this close


@dataclass(frozen=True)
class Clause:
    name: str
    passed: bool
    margin: int      # signed, millionths of ``unit``
    unit: str
    detail: str


@dataclass(frozen=True)
class MechanismReport:
    clauses: tuple[Clause, ...]
    warnings: tuple[str, ...] = ()
    torques: tuple[JointTorque, ...] = ()
    masses_mg: dict[str, int] = field(default_factory=dict)
    reach_nm: int = 0

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.clauses)

    @property
    def failed(self) -> list[str]:
        return [c.name for c in self.clauses if not c.passed]

    def clause(self, name: str) -> Clause:
        for c in self.clauses:
            if c.name == name:
                return c
        raise KeyError(name)

    def text(self) -> str:
        out = [
            f"{'PASS' if c.passed else 'FAIL'} {c.name} margin={c.margin / 1e6:+.3f} "
            f"{c.unit}: {c.detail}"
            for c in self.clauses
        ]
        out.extend(f"WARN {w}" for w in self.warnings)
        return "\n".join(out)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "clauses": [
                {"name": c.name, "passed": c.passed, "margin": c.margin / 1e6,
                 "unit": c.unit, "detail": c.detail}
                for c in self.clauses
            ],
            "warnings": list(self.warnings),
            "torques": [
                {"joint": t.joint_id, "actuator": t.actuator,
                 "required_nmm": t.required_unmm / 1e6,
                 "available_nmm": t.available_unmm / 1e6,
                 "margin_nmm": t.margin_unmm / 1e6}
                for t in self.torques
            ],
            "masses_g": {k: v / 1000 for k, v in self.masses_mg.items()},
            "reach_mm": self.reach_nm / 1e6,
        }


def _nm(value_mm: float) -> int:
    return int(round(value_mm * 1e6))


def _cube(volume_mm3: float) -> float:
    return abs(volume_mm3) ** (1 / 3)


# --- mass ----------------------------------------------------------------------

def part_mass_mg(volume_mm3: float, area_mm2: float, material: str) -> int:
    """Printed mass: a solid ``PRINT_SHELL`` skin over the surface, the rest at
    ``PRINT_INFILL``; never more than the solid part."""
    shell_mm3 = min(volume_mm3, area_mm2 * rules.PRINT_SHELL_NM / 1e6)
    core_mm3 = max(0.0, volume_mm3 - shell_mm3)
    effective = shell_mm3 + core_mm3 * rules.PRINT_INFILL_PPM / 1e6
    return round(effective * rules.DENSITY_UG_PER_MM3[material] / 1000)


def _point_masses(
    model: MechanismModel,
) -> tuple[list[PointMass], dict[str, int], int, int]:
    """``(masses on links >= 1, per-item grams table, base mass, base servo mass)``."""
    b = require_kernel()
    spec, lay = model.spec, model.layout
    masses: list[PointMass] = []
    table: dict[str, int] = {}
    base_mg = 0
    for p in model.parts:
        m = part_mass_mg(p.local.volume, p.local.area, spec.material)
        table[p.name] = m
        if p.link == 0:
            base_mg = m
            continue
        c = p.local.center(b.CenterOf.MASS)
        masses.append(PointMass(p.name, p.link, m, (c.X * 1e6, c.Y * 1e6, c.Z * 1e6)))
    placements: list[tuple[int, ServoPlacement]] = [(0, lay.base.servo)]
    placements += [(ll.index, ll.servo) for ll in lay.links if ll.servo is not None]
    base_servo_mg = 0
    for link, sp in placements:
        act = rules.ACTUATORS[spec.joints[sp.joint_index].actuator]
        name = f"servo_{spec.joints[sp.joint_index].id}"
        table[name] = act.mass_mg
        if link == 0:
            base_servo_mg = act.mass_mg
            continue
        centre = sp.to_link(
            -act.body_h_nm / 2, act.body_l_nm / 2 - act.shaft_from_end_nm, 0
        )
        masses.append(PointMass(name, link, act.mass_mg, centre))
    for i, j in enumerate(spec.joints):
        if j.bearing != "none":
            bm = rules.BEARINGS[j.bearing].mass_mg
            table[f"bearing_{j.id}"] = bm
            masses.append(PointMass(f"bearing_{j.id}", i + 1, bm, (0.0, 0.0, 0.0)))
    n = len(spec.joints)
    L_n = spec.joints[-1].link_nm
    if spec.tool_mass_mg:
        table["tool"] = spec.tool_mass_mg
        masses.append(PointMass("tool", n, spec.tool_mass_mg,
                                (0.0, 0.0, L_n + spec.tool_length_nm / 2)))
    if spec.payload_mg:
        table["payload"] = spec.payload_mg
        masses.append(PointMass("payload", n, spec.payload_mg,
                                (0.0, 0.0, L_n + spec.tool_length_nm)))
    return masses, table, base_mg, base_servo_mg


# --- geometry clauses ------------------------------------------------------------

def _valid_solids(model: MechanismModel) -> Clause:
    from OCP.BRepCheck import BRepCheck_Analyzer

    defects, notes = 0, []
    for p in model.parts:
        solids = list(p.local.solids())
        if len(solids) != 1:
            defects += 1
            notes.append(f"{p.name} has {len(solids)} solids")
        if not BRepCheck_Analyzer(p.local.wrapped).IsValid():
            defects += 1
            notes.append(f"{p.name} fails BRepCheck")
        for s in solids:
            if s.volume <= 0:
                defects += 1
                notes.append(f"{p.name} has a solid of volume {s.volume:.3f} mm^3")
    detail = (
        f"{len(model.parts)} parts, each one BRepCheck-valid solid of positive volume"
        if not defects else "; ".join(notes)
    )
    return Clause("valid_solids", defects == 0, -defects * 1_000_000, "defects", detail)


def _print_bed(model: MechanismModel) -> Clause:
    bed = [v / 1e6 for v in rules.PRINT_BED_NM]
    worst, where = math.inf, ""
    for p in model.parts:
        size = p.printed.bounding_box().size
        for axis, have, limit in zip("XYZ", (size.X, size.Y, size.Z), bed, strict=True):
            if limit - have < worst:
                worst = limit - have
                where = f"{p.name} {axis} {have:.1f} mm vs {limit:.0f} mm"
    return Clause("print_bed", worst >= 0, _nm(worst), "mm",
                  f"tightest: {where} (printed orientation)")


def _servo_envelope(sp: ServoPlacement, act: rules.Actuator) -> Any:
    """The servo as the rules table states it, built here independently of
    ``cad.py``: body, rear boss, rotor to the coupling face, ears."""
    b = require_kernel()

    def box(s0, s1, u0, u1, v0, v1):
        pts = [
            sp.to_link(s, u, v) for s in (s0, s1) for u in (u0, u1) for v in (v0, v1)
        ]
        xs, ys, zs = (sorted(c / 1e6 for c in axis) for axis in zip(*pts, strict=True))
        return b.Pos(xs[0], ys[0], zs[0]) * b.Box(
            xs[-1] - xs[0], ys[-1] - ys[0], zs[-1] - zs[0], align=(b.Align.MIN,) * 3
        )

    e, L_, h, w = act.shaft_from_end_nm, act.body_l_nm, act.body_h_nm, act.body_w_nm
    shape = box(-h, 0, -e, L_ - e, -w / 2, w / 2)
    if act.back_boss_nm:
        shape = shape + box(-h - act.back_boss_nm, -h, -e, L_ - e, -w / 2, w / 2)
    if act.ear_len_nm:
        s0 = -h + act.ear_s_nm
        shape = shape + box(s0, s0 + act.ear_t_nm, -e - act.ear_len_nm,
                            L_ - e + act.ear_len_nm, -w / 2, w / 2)
    # Rotor: a cylinder along s from the body top to the coupling face.
    o = sp.to_link(0, 0, 0)
    top = sp.to_link(act.coupling_face_nm, 0, 0)
    axis = [abs(c) for c in sp.s].index(1)
    r = act.rotor_d_nm / 2e6
    length = act.coupling_face_nm / 1e6
    rotor = b.Cylinder(r, length, align=(b.Align.CENTER, b.Align.CENTER, b.Align.MIN))
    lo = [c / 1e6 for c in (o if o[axis] <= top[axis] else top)]
    if axis == 2:
        rotor = b.Pos(*lo) * rotor
    elif axis == 1:
        rotor = b.Pos(*lo) * b.Rot(-90, 0, 0) * rotor
    else:
        rotor = b.Pos(*lo) * b.Rot(0, 90, 0) * rotor
    return shape + rotor


def _servo_pocket(model: MechanismModel) -> Clause:
    spec, lay = model.spec, model.layout
    placements = [(0, lay.base.servo)] + [
        (ll.index, ll.servo) for ll in lay.links if ll.servo is not None
    ]
    need = rules.FIT_PRESS_NM / 1e6
    worst, where = math.inf, ""
    for link, sp in placements:
        joint = spec.joints[sp.joint_index]
        act = rules.ACTUATORS[joint.actuator]
        env = _servo_envelope(sp, act)
        housing = next(p.local for p in model.parts if p.link == link)
        clash = enclosure_kernel._intersection_volume(env, housing)
        if clash > _VOLUME_TOL_MM3:
            margin = -_cube(clash)
            text = (f"{act.name} for {joint.id!r} interferes with its housing "
                    f"by {clash:.3f} mm^3")
        else:
            gap = env.distance(housing)
            margin = gap - need
            text = (f"{act.name} for {joint.id!r}: {gap:.3f} mm gap to its housing vs "
                    f"{need:.3f} mm press-fit minimum")
        if margin < worst:
            worst, where = margin, text
    return Clause("servo_pocket", worst >= -1e-6, _nm(worst), "mm",
                  f"worst of {len(placements)} servos: {where}")


def _bearing_seat(model: MechanismModel) -> Clause:
    from build123d import GeomType

    spec = model.spec
    rows = [(i, j) for i, j in enumerate(spec.joints) if j.bearing != "none"]
    if not rows:
        return Clause("bearing_seat", True, 0, "mm",
                      f"{_NOTHING}: no joint names a bearing")
    worst, where = math.inf, ""
    for i, joint in rows:
        br = rules.BEARINGS[joint.bearing]
        part = next(p.local for p in model.parts if p.link == i + 1)
        best = None
        for face in part.faces():
            if face.geom_type != GeomType.CYLINDER:
                continue
            ax = face.axis_of_rotation
            if ax is None or abs(abs(ax.direction.Y) - 1) > 1e-6:
                continue
            if math.hypot(ax.position.X, ax.position.Z) > 0.05:
                continue
            r = float(face.radius)
            if not br.bore_nm / 2e6 < r <= br.od_nm / 2e6 + 1.0:
                continue
            if best is None or abs(r - br.od_nm / 2e6) < abs(best[0] - br.od_nm / 2e6):
                best = (r, face)
        if best is None:
            margin = UNEVALUATED / 1e6
            text = (f"{joint.id!r}: no cylindrical seat for a {br.name} found "
                    f"on the Y axis")
        else:
            r, face = best
            interference = (br.od_nm + rules.HOLE_COMPENSATION_NM) / 1e6 - 2 * r
            lo = rules.SEAT_INTERFERENCE_MIN_NM / 1e6
            hi = rules.SEAT_INTERFERENCE_MAX_NM / 1e6
            depth = face.bounding_box().size.Y
            fit = min(interference - lo, hi - interference)
            deep = depth - br.width_nm / 1e6
            margin = min(fit, deep)
            comp = rules.HOLE_COMPENSATION_NM / 1e6
            text = (f"{joint.id!r} {br.name}: seat d{2 * r:.3f} mm gives "
                    f"{interference:.3f} mm diametral interference (band "
                    f"{lo:.2f}..{hi:.2f}, with {comp:.1f} mm FDM compensation), "
                    f"depth {depth:.3f} mm vs B {br.width_nm / 1e6:.1f} mm")
        if margin < worst:
            worst, where = margin, text
    return Clause("bearing_seat", worst >= -1e-6, _nm(worst), "mm",
                  f"worst of {len(rows)}: {where}")


def _enclosure_ctx(warnings: list[str]) -> Any:
    """What ``enclosure.kernel``'s wall and overhang samplers read from their
    context, with the mechanism's printable minimum as the wall."""
    return SimpleNamespace(
        spec=SimpleNamespace(wall_nm=rules.MIN_WALL_NM),
        boss_radius_mm=lambda: rules.HOUSING_WALL_NM / 1e6,
        model=SimpleNamespace(standoffs=()),
        warnings=warnings,
    )


def _min_wall(model: MechanismModel, warnings: list[str]) -> Clause:
    ctx = _enclosure_ctx(warnings)
    samples = []
    for p in model.parts:
        samples += enclosure_kernel._wall_samples(
            ctx, f"part {p.name}", p.printed, z_only=False
        )
    if not samples:
        return Clause("min_wall", False, UNEVALUATED, "mm",
                      "no outer-face sample produced a thickness")
    w = min(samples, key=lambda s: s.margin)
    return Clause(
        "min_wall", w.margin >= -1e-6, _nm(w.margin), "mm",
        f"thinnest of {len(samples)} ray samples: {w.thickness:.3f} mm on {w.where} vs "
        f"{w.required:.3f} mm required",
    )


def _overhang(model: MechanismModel, warnings: list[str]) -> Clause:
    ctx = _enclosure_ctx(warnings)
    limit = float(rules.OVERHANG_LIMIT_DEG)
    worst, where = None, ""
    for p in model.parts:
        angle, loc, bridges = enclosure_kernel._overhang_part(ctx, p.name, p.printed)
        warnings.extend(f"overhang: {x}" for x in bridges)
        if angle is not None and (worst is None or angle > worst):
            worst, where = angle, loc
    if worst is None:
        return Clause("overhang", True, _nm(limit), "deg",
                      "no down-facing face off the bed in any printed part")
    return Clause("overhang", limit - worst >= -0.5, _nm(limit - worst), "deg",
                  f"worst {worst:.1f} deg from vertical on {where} vs {limit:.0f} deg")


# --- motion clauses --------------------------------------------------------------

def sampled_poses(model: MechanismModel) -> list[tuple[str, list[float]]]:
    """Outstretched, then every joint alone at each end of its range."""
    spec = model.spec
    poses = [("outstretched", outstretched_pose(spec))]
    for i, j in enumerate(spec.joints):
        for end, mdeg in (("min", j.range_mdeg[0]), ("max", j.range_mdeg[1])):
            if mdeg == 0:
                continue
            q = [0.0] * len(spec.joints)
            q[i] = math.radians(mdeg / 1000)
            poses.append((f"{j.id} at {end} {mdeg / 1000:g} deg", q))
    return poses


def _pose_clearance(model: MechanismModel, q: Sequence[float]) -> tuple[float, str]:
    """Smallest signed clearance between any two parts at ``q``: a distance,
    or minus the cube side of an interference volume."""
    posed = posed_parts(model, q)
    boxes = [shape.bounding_box() for _, shape in posed]
    pairs = []
    for i in range(len(posed)):
        for j in range(i + 1, len(posed)):
            a, bb = boxes[i], boxes[j]
            gap = max(
                a.min.X - bb.max.X, bb.min.X - a.max.X,
                a.min.Y - bb.max.Y, bb.min.Y - a.max.Y,
                a.min.Z - bb.max.Z, bb.min.Z - a.max.Z,
            )
            pairs.append((gap, i, j))
    pairs.sort()
    worst, where = math.inf, ""
    for gap, i, j in pairs:
        if gap > min(worst, _NEAR_MM):
            if worst == math.inf:
                worst, where = gap, f"{posed[i][0]} / {posed[j][0]} (bounding-box gap)"
            break
        a, bb = posed[i][1], posed[j][1]
        d = a.distance(bb)
        if d <= 1e-6:
            vol = enclosure_kernel._intersection_volume(a, bb)
            margin = -_cube(vol) if vol > _VOLUME_TOL_MM3 else 0.0
            pair = f"{posed[i][0]} / {posed[j][0]}"
            text = (f"{pair} interfere by {vol:.3f} mm^3"
                    if vol > _VOLUME_TOL_MM3 else f"{pair} touch")
        else:
            margin, text = d, f"{posed[i][0]} / {posed[j][0]} {d:.3f} mm apart"
        if margin < worst:
            worst, where = margin, text
    return worst, where


def _collision_clause(name: str, model: MechanismModel,
                      poses: list[tuple[str, list[float]]]) -> Clause:
    worst, where = math.inf, ""
    for label, q in poses:
        margin, text = _pose_clearance(model, q)
        if margin < worst:
            worst, where = margin, f"{text} at {label}"
    # Touching (0) is a failure: a moving joint that rubs is not clear.
    return Clause(name, worst > 1e-6, _nm(worst), "mm",
                  f"closest pair of {len(model.parts)} parts over {len(poses)} "
                  f"pose(s): {where}")


def verify_mechanism(
    model: MechanismModel, *, sample_poses: bool = True
) -> MechanismReport:
    """Run every clause in :data:`CLAUSES` order. A clause that raises fails
    with :data:`UNEVALUATED` and the exception in its detail."""
    spec, lay = model.spec, model.layout
    warnings: list[str] = list(model.warnings)
    torques: list[JointTorque] = []
    table: dict[str, int] = {}
    reach_holder = [0]

    def reach() -> Clause:
        r, q = max_reach_nm(spec, lay.offsets_nm)
        reach_holder[0] = r
        pose = ", ".join(f"{math.degrees(v):.0f}" for v in q)
        return Clause("reach", r >= spec.reach_nm, r - spec.reach_nm, "mm",
                      f"max horizontal tool reach {r / 1e6:.1f} mm "
                      f"(pose deg [{pose}]) vs {spec.reach_nm / 1e6:.1f} mm target")

    def torque() -> Clause:
        masses, t, _, _ = _point_masses(model)
        table.update(t)
        torques.extend(static_torques(spec, lay.offsets_nm, masses))
        w = min(torques, key=lambda x: x.margin_unmm)
        rows = "; ".join(
            f"{x.joint_id} {x.actuator} needs {x.required_unmm / 1e6:.0f} of "
            f"{x.available_unmm / 1e6:.0f} N-mm" for x in torques
        )
        return Clause("joint_torque", w.margin_unmm >= 0, w.margin_unmm, "N-mm",
                      f"worst {w.joint_id!r} ({w.actuator}, stall x "
                      f"{rules.STALL_DERATING_PPM / 1e6:.2f}) outstretched: {rows}")

    def stability() -> Clause:
        masses, _, base_mg, base_servo_mg = _point_masses(model)
        q = outstretched_pose(spec)
        frames = link_frames(spec, lay.offsets_nm, q)
        edge = lay.base.half_side_nm / 1e6
        g = rules.GRAVITY_MM_S2 / 1e6
        moment = (base_mg + base_servo_mg) * g * edge
        for pm in masses:
            x = apply(frames[pm.link], tuple(c / 1e6 for c in pm.local_nm))[0]
            moment += pm.mass_mg * g * (edge - x)
        if spec.base_type != "freestanding":
            return Clause("base_stability", True, 0, "N-mm",
                          f"{_NOTHING}: base is bolted down (freestanding it would "
                          f"have {moment / 1e6:+.0f} N-mm righting minus overturning "
                          f"moment about its edge, outstretched)")
        return Clause("base_stability", moment >= 0, round(moment), "N-mm",
                      f"righting minus overturning moment about the {2 * edge:.0f} mm "
                      f"base edge, outstretched (tool at x = "
                      f"{tool_point(spec, lay.offsets_nm, q)[0]:.0f} mm)")

    def total_mass() -> Clause:
        if not table:
            _, t, _, _ = _point_masses(model)
            table.update(t)
        total = sum(table.values())
        if spec.mass_budget_mg is None:
            infill = rules.PRINT_INFILL_PPM / 1e4
            return Clause("total_mass", True, 0, "g",
                          f"{_NOTHING}: no mass budget; total {total / 1000:.0f} g "
                          f"(printed at {infill:.0f} % infill + servos, "
                          f"bearings, tool, payload)")
        m = spec.mass_budget_mg - total
        return Clause("total_mass", m >= 0, m * 1000, "g",
                      f"total {total / 1000:.0f} g vs budget "
                      f"{spec.mass_budget_mg / 1000:.0f} g")

    checks = {
        "valid_solids": lambda: _valid_solids(model),
        "print_bed": lambda: _print_bed(model),
        "servo_pocket": lambda: _servo_pocket(model),
        "bearing_seat": lambda: _bearing_seat(model),
        "min_wall": lambda: _min_wall(model, warnings),
        "overhang": lambda: _overhang(model, warnings),
        "self_collision_home": lambda: _collision_clause(
            "self_collision_home", model, [("home", [0.0] * len(spec.joints))]),
        "self_collision_sampled": lambda: (
            _collision_clause("self_collision_sampled", model, sampled_poses(model))
            if sample_poses
            else Clause("self_collision_sampled", False, UNEVALUATED, "mm",
                        "sampling switched off by the caller")
        ),
        "reach": reach,
        "joint_torque": torque,
        "base_stability": stability,
        "total_mass": total_mass,
    }
    clauses = []
    for name in CLAUSES:
        try:
            clauses.append(checks[name]())
        except Exception as exc:  # noqa: BLE001 - a clause that raises fails in words
            clauses.append(Clause(name, False, UNEVALUATED, "mm",
                                  f"could not evaluate: {type(exc).__name__}: {exc}"))
    return MechanismReport(
        clauses=tuple(clauses), warnings=tuple(warnings), torques=tuple(torques),
        masses_mg=dict(table), reach_nm=reach_holder[0],
    )
