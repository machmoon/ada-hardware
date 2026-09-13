"""Forward kinematics, reach and static joint torque for a MECHANISM-SPEC chain.

No kernel, no numpy: 4x4 homogeneous matrices as nested tuples of floats in
millimetres, so the verifier can run anywhere the spec parses.

Two references, read in source:

* **Forward kinematics** is the product of elementary transforms that ikpy's
  ``URDFLink.get_link_frame_matrix`` computes (``src/ikpy/link.py``, the numpy
  branch: translate by the link origin, then rotate about the joint axis) and
  ``Chain.forward_kinematics`` multiplies left to right
  (``src/ikpy/chain.py``). :func:`dh_matrix` is the standard
  Denavit-Hartenberg link transform exactly as robotics-toolbox-python writes
  it (``src/roboticstoolbox/robot/DHLink.py``, ``DHLink.A``, the ``mdh == 0``
  branch), kept so a chain can be checked against a textbook DH table.
* **Static torque** is what ``DHRobot.gravload`` computes (recursive
  Newton-Euler with zero velocity and acceleration): the moment of every mass
  distal to a joint about that joint's axis, ``tau_j = z_j . sum((p_c - p_j)
  x m g)``. Here it is summed directly over point masses, which is the same
  quantity for a static pose.

The pose that loads every hinge worst is the arm stretched horizontal: the
first ``pitch`` joint at 90 degrees and every other joint at 0 puts all
distal links on one horizontal line (a twist keeps its links collinear). It
is used whatever the joint ranges say, because a range limit is not a
guarantee the arm is never pushed there.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from . import rules
from .ir import MechanismSpec

__all__ = [
    "Mat",
    "IDENTITY",
    "mat_mul",
    "translate",
    "rot_y",
    "rot_z",
    "dh_matrix",
    "apply",
    "link_frames",
    "tool_point",
    "outstretched_pose",
    "max_reach_nm",
    "PointMass",
    "JointTorque",
    "static_torques",
]

Mat = tuple[tuple[float, float, float, float], ...]
IDENTITY: Mat = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0),
                 (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def mat_mul(a: Mat, b: Mat) -> Mat:
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4))
        for i in range(4)
    )  # type: ignore[return-value]


def translate(x: float, y: float, z: float) -> Mat:
    return ((1.0, 0.0, 0.0, x), (0.0, 1.0, 0.0, y), (0.0, 0.0, 1.0, z),
            (0.0, 0.0, 0.0, 1.0))


def rot_y(theta: float) -> Mat:
    c, s = math.cos(theta), math.sin(theta)
    return ((c, 0.0, s, 0.0), (0.0, 1.0, 0.0, 0.0), (-s, 0.0, c, 0.0),
            (0.0, 0.0, 0.0, 1.0))


def rot_z(theta: float) -> Mat:
    c, s = math.cos(theta), math.sin(theta)
    return ((c, -s, 0.0, 0.0), (s, c, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0))


def dh_matrix(theta: float, d: float, a: float, alpha: float) -> Mat:
    """Standard DH link transform (robotics-toolbox ``DHLink.A``)."""
    ct, st = math.cos(theta), math.sin(theta)
    ca, sa = math.cos(alpha), math.sin(alpha)
    return ((ct, -st * ca, st * sa, a * ct), (st, ct * ca, -ct * sa, a * st),
            (0.0, sa, ca, d), (0.0, 0.0, 0.0, 1.0))


def apply(m: Mat, p: Sequence[float]) -> tuple[float, float, float]:
    return tuple(
        m[i][0] * p[0] + m[i][1] * p[1] + m[i][2] * p[2] + m[i][3] for i in range(3)
    )  # type: ignore[return-value]


def link_frames(
    spec: MechanismSpec, offsets_nm: Sequence[int], q_rad: Sequence[float]
) -> list[Mat]:
    """World frames of link 0 (base, identity) through link n, in mm.

    ``offsets_nm`` is ``[base height, L_1, ..., L_n]`` (``layout.offsets_nm``).
    ``T_k = T_{k-1} . Trans(0, 0, offset_{k-1}) . Rot(axis_k, q_k)``.
    """
    if len(q_rad) != len(spec.joints):
        raise ValueError(f"pose has {len(q_rad)} angles for {len(spec.joints)} joints")
    frames = [IDENTITY]
    t = IDENTITY
    for k, (joint, q) in enumerate(zip(spec.joints, q_rad, strict=True)):
        t = mat_mul(t, translate(0.0, 0.0, offsets_nm[k] / 1e6))
        t = mat_mul(t, rot_y(q) if joint.is_hinge else rot_z(q))
        frames.append(t)
    return frames


def tool_point(spec: MechanismSpec, offsets_nm: Sequence[int], q_rad: Sequence[float]):
    """World position (mm) of the payload: flange face plus tool length."""
    last = link_frames(spec, offsets_nm, q_rad)[-1]
    return apply(last, (0.0, 0.0, (offsets_nm[-1] + spec.tool_length_nm) / 1e6))


def outstretched_pose(spec: MechanismSpec) -> list[float]:
    q = [0.0] * len(spec.joints)
    for i, j in enumerate(spec.joints):
        if j.is_hinge:
            q[i] = math.pi / 2
            break
    return q


def max_reach_nm(spec: MechanismSpec, offsets_nm: Sequence[int], steps: int = 12) -> tuple[int, list[float]]:
    """Largest horizontal distance of the tool point from the base axis over a
    grid of hinge angles inside their ranges (twists do not change it).

    Each hinge is sampled at ``steps + 1`` evenly spaced angles plus 0 and
    +-90 degrees where the range includes them. Returns ``(reach, pose)``.
    """
    hinge_idx = [i for i, j in enumerate(spec.joints) if j.is_hinge]
    grids: list[list[float]] = []
    for i in hinge_idx:
        lo, hi = (v / 1000 for v in spec.joints[i].range_mdeg)
        vals = {lo + (hi - lo) * s / steps for s in range(steps + 1)}
        vals |= {v for v in (0.0, 90.0, -90.0) if lo <= v <= hi}
        grids.append(sorted(math.radians(v) for v in vals))
    best, best_q = -1.0, [0.0] * len(spec.joints)

    def walk(level: int, q: list[float]) -> None:
        nonlocal best, best_q
        if level == len(hinge_idx):
            x, y, _ = tool_point(spec, offsets_nm, q)
            r = math.hypot(x, y)
            if r > best:
                best, best_q = r, list(q)
            return
        for v in grids[level]:
            q[hinge_idx[level]] = v
            walk(level + 1, q)
        q[hinge_idx[level]] = 0.0

    walk(0, [0.0] * len(spec.joints))
    return round(best * 1e6), best_q


@dataclass(frozen=True)
class PointMass:
    """A mass rigidly attached to link ``link`` at ``local_nm`` in its frame."""

    name: str
    link: int
    mass_mg: int
    local_nm: tuple[float, float, float]


@dataclass(frozen=True)
class JointTorque:
    joint_id: str
    actuator: str
    required_unmm: int       # |gravity moment| about the axis, uN-mm
    available_unmm: int      # stall x derating
    lever_nm: int            # the distance from the axis to the payload

    @property
    def margin_unmm(self) -> int:
        return self.available_unmm - self.required_unmm


def static_torques(
    spec: MechanismSpec,
    offsets_nm: Sequence[int],
    masses: Sequence[PointMass],
    q_rad: Sequence[float] | None = None,
) -> list[JointTorque]:
    """Gravity moment about every joint axis at ``q_rad`` (default: the
    outstretched pose). ``masses`` should include the printed links, the
    servos housed in them, the tool and the payload; a mass on link ``k``
    loads every joint ``j <= k`` (joint ``j`` drives link ``j``)."""
    q = list(outstretched_pose(spec) if q_rad is None else q_rad)
    frames = link_frames(spec, offsets_nm, q)
    g = rules.GRAVITY_MM_S2 / 1e6  # m/s^2
    tool = tool_point(spec, offsets_nm, q)
    out: list[JointTorque] = []
    for j_idx, joint in enumerate(spec.joints):
        k = j_idx + 1
        f = frames[k]
        p_j = (f[0][3], f[1][3], f[2][3])
        col = 1 if joint.is_hinge else 2
        axis = (f[0][col], f[1][col], f[2][col])
        total = 0.0
        for pm in masses:
            if pm.link < k:
                continue
            p = apply(frames[pm.link], tuple(c / 1e6 for c in pm.local_nm))
            r = (p[0] - p_j[0], p[1] - p_j[1], p[2] - p_j[2])
            force = (0.0, 0.0, -pm.mass_mg * g)
            moment = (
                r[1] * force[2] - r[2] * force[1],
                r[2] * force[0] - r[0] * force[2],
                r[0] * force[1] - r[1] * force[0],
            )
            total += sum(moment[i] * axis[i] for i in range(3))
        # mass [mg] x g [m/s^2] x r [mm] = 1e-6 kg m/s^2 mm = 1 uN-mm.
        act = rules.ACTUATORS[joint.actuator]
        lever = math.dist(p_j, tool)
        out.append(
            JointTorque(
                joint_id=joint.id,
                actuator=joint.actuator,
                required_unmm=round(abs(total)),
                available_unmm=act.stall_unmm * rules.STALL_DERATING_PPM // 1_000_000,
                lever_nm=round(lever * 1e6),
            )
        )
    return out
