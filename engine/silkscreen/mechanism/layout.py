"""Where every feature of the chain sits, as arithmetic -- no kernel.

:mod:`.cad` draws what this module places, and :mod:`.kinematics` moves the
frames it defines, so the two can never disagree about where joint 3 is.
Every number is derived from :mod:`.rules` and the spec; nothing here is a
new mechanical constant.

Frames
------
Link ``k`` (``k = 1..n``) has its frame at joint ``k``: origin on the joint
axis, +Z along the link at the home pose. A **hinge** (``pitch``) turns about
the frame's Y; a **twist** (``yaw``/``roll``) about its Z, and its origin is
the coupling plane -- the face the next part bears on. Link 0 is the base,
world frame, Z up, origin at the bottom face; joint 1 sits at
``(0, 0, base_height_nm)``. Link ``k``'s next joint is at ``(0, 0, L_k)``.

Servo placement (the servo frame of :class:`~.rules.Actuator`, mapped into
the link frame that houses it):

* base, joint 1 (twist): ``s -> +Z``, ``u -> +Y``, ``v -> +X``;
* hinge housing at the far end of link ``k``: ``s -> +Y``, ``u -> -Z``,
  ``v -> +X``, origin ``(0, y_top, L_k)`` -- the body lies back inside link
  ``k``, and the pair (servo, coupling) is centred on ``y = 0`` so both
  cheeks of the next clevis stand the same gap off the housing;
* twist housing at the far end of link ``k``: ``s -> +Z``, ``u -> +Y``,
  ``v -> +X``, origin ``(0, 0, L_k - a)``.

A housing is a frame around the servo, open through X: the servo slides in
along X (its rotor through a slot in the +S wall), as a printed part made by
extruding one profile along X -- which is also why every link prints lying on
its -X face with nothing but vertical walls, upward floors and horizontal
round holes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import rules
from .errors import MechanismBuildError
from .ir import MechanismSpec

__all__ = [
    "ServoPlacement",
    "HousingDims",
    "LinkLayout",
    "BaseLayout",
    "Layout",
    "housing_dims",
    "layout_for",
]

_T = rules.HOUSING_WALL_NM
_C = rules.POCKET_CLEARANCE_NM
_GAP = rules.JOINT_GAP_NM
_MW = rules.MIN_WALL_NM


@dataclass(frozen=True)
class ServoPlacement:
    """A servo frame inside a link frame: origin and the s/u/v unit axes."""

    joint_index: int              # 0-based index of the joint it drives
    origin_nm: tuple[int, int, int]
    s: tuple[int, int, int]
    u: tuple[int, int, int]
    v: tuple[int, int, int]

    def to_link(self, s: float, u: float, v: float) -> tuple[float, float, float]:
        """A servo-frame point (nm) in the link frame (nm, float)."""
        o = self.origin_nm
        return tuple(
            o[i] + s * self.s[i] + u * self.u[i] + v * self.v[i] for i in range(3)
        )  # type: ignore[return-value]


@dataclass(frozen=True)
class HousingDims:
    """The housing around one actuator, in its servo frame (nm)."""

    a_nm: int            # body top -> coupling plane
    hub_len_nm: int      # coupling hub below the cheek/plate, >= 0
    hub_d_nm: int
    opening_d_nm: int    # the rotor slot in the +S wall
    s_min_nm: int
    s_max_nm: int
    u_min_nm: int
    u_max_nm: int
    width_need_nm: int


def housing_dims(act: rules.Actuator, *, top_wall: bool = True) -> HousingDims:
    """``top_wall=False`` is the base pocket (open at the top)."""
    if top_wall:
        a = max(act.coupling_face_nm, _C + _T + _GAP)
    else:
        a = max(act.coupling_face_nm, _GAP)
    hub_len = a - act.coupling_face_nm
    opening = act.rotor_d_nm + 2 * rules.FIT_LOOSE_NM
    return HousingDims(
        a_nm=a,
        hub_len_nm=hub_len,
        hub_d_nm=act.rotor_d_nm,
        opening_d_nm=opening,
        s_min_nm=-(act.body_h_nm + act.back_boss_nm + _C + _T),
        s_max_nm=_C + _T,
        u_min_nm=-(act.shaft_from_end_nm + _C + _T + act.ear_len_nm),
        u_max_nm=act.body_l_nm - act.shaft_from_end_nm + _C + _T + act.ear_len_nm,
        width_need_nm=max(act.body_w_nm + 2 * _C, opening + 2 * _T),
    )


def seat_bore_nm(bearing: rules.Bearing) -> int:
    """The modelled bearing-seat diameter: ``D + compensation - interference``
    with the interference at the middle of the allowed band."""
    interference = (
        rules.SEAT_INTERFERENCE_MIN_NM + rules.SEAT_INTERFERENCE_MAX_NM
    ) // 2
    return bearing.od_nm + rules.HOLE_COMPENSATION_NM - interference


def dowel_hole_nm(bearing: rules.Bearing) -> int:
    """The press hole for the steel dowel axle in the housing back wall."""
    return bearing.bore_nm + rules.HOLE_COMPENSATION_NM - 2 * rules.FIT_PRESS_NM


@dataclass(frozen=True)
class LinkLayout:
    index: int                      # 1..n
    length_nm: int
    width_nm: int                   # X extent of the whole link
    # proximal coupling (joint index-1)
    hinge: bool
    yc_nm: int                      # hinge: cheek inner face |y|
    cheek_t_nm: int
    cheek_r_nm: int                 # cheek extent below the axis
    bridge_z_nm: int                # hinge: bridge underside
    plate_t_nm: int                 # twist: coupling plate thickness
    hub_len_nm: int
    hub_d_nm: int
    z_a_nm: int                     # top of the proximal feature
    # distal feature
    distal: str                     # "hinge", "twist", "flange"
    z_b_nm: int                     # bottom of the distal feature
    beam_y_nm: tuple[int, int]
    servo: ServoPlacement | None    # the servo housed at the distal end
    y_top_nm: int                   # hinge distal: servo s origin offset

    @property
    def min_length_nm(self) -> int:
        return self.length_nm - (self.z_b_nm - self.z_a_nm)


@dataclass(frozen=True)
class BaseLayout:
    half_side_nm: int
    plinth_top_nm: int
    height_nm: int                  # joint 1 coupling plane
    servo: ServoPlacement
    bolt_xy_nm: int


@dataclass(frozen=True)
class Layout:
    base: BaseLayout
    links: tuple[LinkLayout, ...]
    #: Kinematic offsets: [base height, L_1, ..., L_n].
    offsets_nm: tuple[int, ...]


def _hinge_pair(act: rules.Actuator) -> tuple[int, int]:
    """``(y_top, Yc)`` for a hinge housing: the servo and its coupling centred."""
    d = housing_dims(act)
    a = d.a_nm
    b = act.body_h_nm + act.back_boss_nm + _C + _T + _GAP
    return (b - a) // 2, (a + b) // 2


def layout_for(spec: MechanismSpec) -> Layout:
    """Place every feature; raises :class:`MechanismBuildError` naming each
    link that is too short to hold the housings at its two ends."""
    joints = spec.joints
    n = len(joints)
    acts = [rules.ACTUATORS[j.actuator] for j in joints]

    # --- base ---------------------------------------------------------------
    a1 = acts[0]
    d1 = housing_dims(a1, top_wall=False)
    body_bottom = _T + _C + a1.back_boss_nm
    body_top = body_bottom + a1.body_h_nm
    bolt_margin = rules.BASE_BOLT_CLEARANCE_NM + 2 * rules.BOSS_MIN_WALL_NM
    half = max(-d1.u_min_nm, d1.u_max_nm, a1.body_w_nm // 2 + _C + _T) + bolt_margin
    if spec.base_footprint_nm is not None:
        half = max(half, spec.base_footprint_nm // 2)
    base = BaseLayout(
        half_side_nm=half,
        plinth_top_nm=body_top,
        height_nm=body_top + d1.a_nm,
        servo=ServoPlacement(0, (0, 0, body_top), (0, 0, 1), (0, 1, 0), (1, 0, 0)),
        bolt_xy_nm=half - bolt_margin // 2,
    )

    # --- widths ---------------------------------------------------------------
    def proximal_need(k: int) -> int:
        j, act = joints[k - 1], acts[k - 1]
        d = housing_dims(act)
        need = max(d.hub_d_nm, act.spline_d_nm + 2 * _MW) + 2 * _T
        if j.bearing != "none":
            need = max(need, seat_bore_nm(rules.BEARINGS[j.bearing]) + 2 * _T)
        return need

    def distal_need(k: int) -> int:
        if k < n:
            return housing_dims(acts[k]).width_need_nm
        flange = rules.FLANGE_INSERT
        return 2 * (flange.bore_nm + 2 * rules.BOSS_MIN_WALL_NM) + 2 * _MW

    widths = {k: max(proximal_need(k), distal_need(k)) for k in range(1, n + 1)}
    widths[0] = 2 * half

    links: list[LinkLayout] = []
    short: list[str] = []
    for k in range(1, n + 1):
        j, act = joints[k - 1], acts[k - 1]
        L = j.link_nm
        W = widths[k]
        d = housing_dims(act, top_wall=True)
        yc = cheek_t = cheek_r = bridge_z = plate_t = 0
        if j.is_hinge:
            _, yc = _hinge_pair(act)
            spline_need = act.spline_h_nm + _MW - d.hub_len_nm
            cheek_t = max(_T, spline_need)
            if j.bearing != "none":
                cheek_t = max(cheek_t, rules.BEARINGS[j.bearing].width_nm
                              + rules.FIT_SLIDE_NM + _MW)
            seat = seat_bore_nm(rules.BEARINGS[j.bearing]) if j.bearing != "none" else 0
            cheek_r = max(d.hub_d_nm, seat, act.spline_d_nm) // 2 + _T
            # The bridge must clear the distal corner of the housing it
            # straddles (that housing lives in link k-1, width W_{k-1}).
            reach = -d.u_min_nm
            corner = math.isqrt(reach * reach + (widths[k - 1] // 2) ** 2)
            bridge_z = max(corner + _GAP, cheek_r)
            z_a = bridge_z + rules.BRIDGE_T_NM
            hub_len = d.hub_len_nm
        else:
            plate_t = max(rules.BRIDGE_T_NM, act.spline_h_nm + _MW - d.hub_len_nm)
            z_a = plate_t
            hub_len = d.hub_len_nm
        servo: ServoPlacement | None = None
        y_top = 0
        if k < n:
            nxt, nact = joints[k], acts[k]
            nd = housing_dims(nact)
            if nxt.is_hinge:
                y_top, nyc = _hinge_pair(nact)
                distal = "hinge"
                z_b = L - nd.u_max_nm
                beam_y = (-nyc + _GAP, nyc - _GAP)
                servo = ServoPlacement(
                    k, (0, y_top, L), (0, 1, 0), (0, 0, -1), (1, 0, 0)
                )
            else:
                distal = "twist"
                z_b = L - nd.a_nm + nd.s_min_nm
                beam_y = (nd.u_min_nm, nd.u_max_nm)
                servo = ServoPlacement(
                    k, (0, 0, L - nd.a_nm), (0, 0, 1), (0, 1, 0), (1, 0, 0)
                )
        else:
            distal = "flange"
            flange = rules.FLANGE_INSERT
            z_b = L - (flange.length_nm + rules.INSERT_BORE_EXTRA_NM + _MW)
            beam_y = (-W // 2, W // 2)
        lay = LinkLayout(
            index=k, length_nm=L, width_nm=W, hinge=j.is_hinge, yc_nm=yc,
            cheek_t_nm=cheek_t, cheek_r_nm=cheek_r, bridge_z_nm=bridge_z,
            plate_t_nm=plate_t, hub_len_nm=hub_len, hub_d_nm=d.hub_d_nm,
            z_a_nm=z_a, distal=distal, z_b_nm=z_b, beam_y_nm=beam_y,
            servo=servo, y_top_nm=y_top,
        )
        if lay.z_b_nm < lay.z_a_nm:
            short.append(
                f"link after joint {j.id!r} is {L / 1e6:.1f} mm; the "
                f"{'clevis' if j.is_hinge else 'coupling plate'} at its start and the "
                f"{distal} {'housing' if distal != 'flange' else ''} at its end need "
                f"{lay.min_length_nm / 1e6:.1f} mm"
            )
        links.append(lay)
    if short:
        raise MechanismBuildError("LINK_TOO_SHORT", "; ".join(short))
    return Layout(
        base=base,
        links=tuple(links),
        offsets_nm=(base.height_nm,) + tuple(j.link_nm for j in joints),
    )


def min_link_table() -> dict[tuple[str, str, str, str], int]:
    """Minimum link length for every (joint kind, actuator, next kind, next
    actuator) pair, for the prompt. Computed through :func:`layout_for` on a
    two-joint probe so the prompt can never disagree with the builder."""
    from .ir import Joint, MechanismSpec

    out: dict[tuple[str, str, str, str], int] = {}
    for a in rules.ACTUATORS:
        for kind in ("pitch", "yaw"):
            for b in rules.ACTUATORS:
                for nkind in ("pitch", "roll"):
                    probe = MechanismSpec(
                        name="probe", base_type="bolt_down", base_footprint_nm=None,
                        joints=(
                            Joint("j0", "yaw", (0, 0), a, "none", rules.MAX_LINK_NM),
                            Joint("j1", kind, (0, 0), a, "none", rules.MAX_LINK_NM),
                            Joint("j2", nkind, (0, 0), b, "none", rules.MAX_LINK_NM),
                        ),
                        payload_mg=0, reach_nm=1, tool_length_nm=0, tool_mass_mg=0,
                        material="PLA",
                    )
                    out[(kind, a, nkind, b)] = layout_for(probe).links[1].min_length_nm
    return out
