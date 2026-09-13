"""Mechanical design rules for a printed serial-chain mechanism (a robot arm).

This is the mechanism counterpart of :mod:`silkscreen.enclosure.rules`: every
number a joint, a bracket or a link needs lives here, each with the file it
was read from, and the model never chooses one. The model picks an actuator
*by name*, a bearing *by name*, link lengths and joint ranges (design intent,
stated in its answer and checked by the kernel), and the builder looks every
millimetre up in these tables.

Sources, read in source form (shallow clones, 2026-09-13):

* **STS3215** envelope -- measured from the SO-ARM100 servo mesh
  ``Simulation/SO101/assets/sts3215_03a_no_horn_v1.stl`` (TheRobotStudio/
  SO-ARM100, metres): body box 45.4 x 24.8 x 31.8 mm (z -15.9..15.9), output
  shaft 12.5 mm off the body centre along the long side, a 20 mm horn disc
  from 0.3 mm to 2.8 mm above the body top, the horn screw head to 4.3 mm, and
  a rear boss to 3.5 mm below the body. Stall torque from SO-ARM100
  ``README.md`` line 73: "The 7.4V has a stall torque of 16.5kg.cm at 6V ...
  The 12V version has a stall torque of 30kg.cm".
* **SG90** envelope -- openscad/MCAD ``servos.scad`` ``towerprosg90``: body
  22.5 x 11.8 x 22.7, shaft 5.9 mm from one end, output boss d11.8 x 4, spline
  d4.6 x 3.2, ears 2.5 mm thick at z 15.9 extending 4.7 mm past each end,
  d2 holes 2.3 mm past each end.
* **MG996R** envelope -- openscad/MCAD ``servos.scad`` ``futabas3003`` (the
  standard-size servo class the MG996R shares): body 20.1 x 39.9 x 36.1, shaft
  30 mm from one end (9.9 from the other), plate d12 x 0.4, spline d5 x 4.9,
  ears 2.5 thick at z 26.6 extending 7.6 mm, two d4 holes 10 mm apart.
* **Bearings** -- gumyr/bd_warehouse
  ``src/bd_warehouse/data/single_row_deep_groove_ball_bearing_parameters.csv``
  rows ``M8-22-7`` (608), ``M10-19-5`` (61800, sold as "6800") and ``M3-10-4``
  (623): d, D, B, the housing-shoulder maximum ``Da`` and the mass. The seat
  shape (a ``D/2 - interference`` counterbore) is bd_warehouse ``bearing.py``
  ``default_countersink_profile``; NopSCADlib ``vitamins/ball_bearings.scad``
  agrees on 608 = 8/22/7.
* **Clearance holes** -- bd_warehouse ``data/clearance_hole_sizes.csv``
  (``M3,3.2,3.4,3.6``; ``M4,4.3,4.5,4.8``).
* **FDM fits, hole compensation, minimum wall, overhang limit, heat-set insert
  bores** -- reused from :mod:`silkscreen.enclosure.rules` and
  :mod:`silkscreen.enclosure.ir` (YAPP_Box, NopSCADlib ``inserts.scad``) so
  one printer has one set of numbers.
* **Print settings for the mass estimate** -- SO-ARM100 ``README.md`` "Step
  1": "0.4mm nozzle diameter at 0.2mm layer height ... Infill Density: 15%".

Stated, not sourced from code (see the report's unverified list): the servo
masses (TowerPro/Feetech listings), the MG996R and SG90 stall torques
(TowerPro datasheets, 4.8 V column), the two-perimeter shell of the mass
estimate, the 0.5 stall derating, and the filament densities.

Integer units throughout, the ``units.py`` rule: nanometres for length,
milligrams for mass, micro-newton-millimetres (``unmm``) for torque, and
micrograms per cubic millimetre for density.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..enclosure import rules as enclosure_rules
from ..enclosure.ir import MIN_WALL_NM
from ..units import mm

__all__ = [
    "Actuator",
    "ACTUATORS",
    "Bearing",
    "BEARINGS",
    "KGCM_TO_UNMM",
    "GRAVITY_MM_S2",
    "STALL_DERATING_PPM",
    "POCKET_CLEARANCE_NM",
    "JOINT_GAP_NM",
    "HOUSING_WALL_NM",
    "MIN_WALL_NM",
    "BRIDGE_T_NM",
    "HOLE_COMPENSATION_NM",
    "FIT_PRESS_NM",
    "FIT_SLIDE_NM",
    "FIT_LOOSE_NM",
    "SEAT_INTERFERENCE_MIN_NM",
    "SEAT_INTERFERENCE_MAX_NM",
    "OVERHANG_LIMIT_DEG",
    "CABLE_CHANNEL_W_NM",
    "CABLE_CHANNEL_D_NM",
    "CABLE_EXIT_D_NM",
    "BASE_BOLT_CLEARANCE_NM",
    "BOSS_MIN_WALL_NM",
    "FLANGE_INSERT",
    "INSERT_BORE_EXTRA_NM",
    "SELF_TAP_UNDERSIZE_NM",
    "EAR_SCREW_DEPTH_NM",
    "DENSITY_UG_PER_MM3",
    "PRINT_SHELL_NM",
    "PRINT_INFILL_PPM",
    "PRINT_BED_NM",
    "MAX_JOINTS",
    "MAX_LINK_NM",
]

#: 1 kg-cm = 9.80665 N x 10 mm = 98.0665 N-mm = 98_066_500 uN-mm.
KGCM_TO_UNMM: int = 98_066_500
#: Standard gravity in mm/s^2 (ISO 80000-3).
GRAVITY_MM_S2: int = 9_806_650

#: Use at most this fraction of stall torque, in parts per million. A servo
#: held at stall overheats and its position loop has no authority left to
#: correct a disturbance; the hobby-servo rule of thumb is half. The ask's
#: "stall torque x safety factor" is this number.
STALL_DERATING_PPM: int = 500_000


@dataclass(frozen=True)
class Actuator:
    """One servo, in its own frame.

    The *servo frame*: origin on the output shaft axis at the body top face;
    ``s`` along the shaft (out of the body), ``u`` along the long side of the
    body (the body spans ``u`` in ``[-shaft_from_end, body_l - shaft_from_end]``),
    ``v`` across it (``+-body_w/2``). ``rotor_d`` is whatever turns between
    the body top and ``coupling_face_nm`` (a horn disc or an output boss); the
    coupling plate bears on ``coupling_face_nm`` and takes the ``spline``
    (spline, or horn screw head) in a bore.
    """

    name: str
    source: str
    body_l_nm: int
    body_w_nm: int
    body_h_nm: int
    shaft_from_end_nm: int
    rotor_d_nm: int
    coupling_face_nm: int
    spline_d_nm: int
    spline_h_nm: int
    back_boss_nm: int
    #: Ears: length past each body end, thickness, bottom height above the
    #: body bottom, screw diameter, screw centre past the body end, and the
    #: screw offsets across the ear (``v``). ``ear_len_nm == 0`` means none.
    ear_len_nm: int
    ear_t_nm: int
    ear_s_nm: int
    ear_hole_d_nm: int
    ear_hole_from_end_nm: int
    ear_hole_v_nm: tuple[int, ...]
    stall_unmm: int
    stall_note: str
    mass_mg: int


ACTUATORS: dict[str, Actuator] = {
    "SG90": Actuator(
        "SG90", "openscad/MCAD servos.scad towerprosg90",
        body_l_nm=mm(22.5), body_w_nm=mm(11.8), body_h_nm=mm(22.7),
        shaft_from_end_nm=mm(5.9), rotor_d_nm=mm(11.8), coupling_face_nm=mm(4.0),
        spline_d_nm=mm(4.6), spline_h_nm=mm(3.2), back_boss_nm=0,
        ear_len_nm=mm(4.7), ear_t_nm=mm(2.5), ear_s_nm=mm(15.9),
        ear_hole_d_nm=mm(2.0), ear_hole_from_end_nm=mm(2.3), ear_hole_v_nm=(0,),
        stall_unmm=int(1.8 * KGCM_TO_UNMM), stall_note="1.8 kg-cm at 4.8 V",
        mass_mg=9_000,
    ),
    "MG996R": Actuator(
        "MG996R", "openscad/MCAD servos.scad futabas3003 (standard-size class)",
        body_l_nm=mm(39.9), body_w_nm=mm(20.1), body_h_nm=mm(36.1),
        shaft_from_end_nm=mm(9.9), rotor_d_nm=mm(12.0), coupling_face_nm=mm(0.4),
        spline_d_nm=mm(5.0), spline_h_nm=mm(4.9), back_boss_nm=0,
        ear_len_nm=mm(7.6), ear_t_nm=mm(2.5), ear_s_nm=mm(26.6),
        ear_hole_d_nm=mm(4.0), ear_hole_from_end_nm=mm(4.0),
        ear_hole_v_nm=(mm(-5.0), mm(5.0)),
        stall_unmm=int(9.4 * KGCM_TO_UNMM), stall_note="9.4 kg-cm at 4.8 V",
        mass_mg=55_000,
    ),
    "STS3215": Actuator(
        "STS3215", "SO-ARM100 sts3215_03a_no_horn_v1.stl; README.md:73",
        body_l_nm=mm(45.4), body_w_nm=mm(24.8), body_h_nm=mm(31.8),
        shaft_from_end_nm=mm(10.2), rotor_d_nm=mm(20.0), coupling_face_nm=mm(2.8),
        spline_d_nm=mm(9.0), spline_h_nm=mm(1.5), back_boss_nm=mm(3.5),
        ear_len_nm=0, ear_t_nm=0, ear_s_nm=0, ear_hole_d_nm=0,
        ear_hole_from_end_nm=0, ear_hole_v_nm=(),
        stall_unmm=int(16.5 * KGCM_TO_UNMM), stall_note="16.5 kg-cm at 6 V (7.4 V C001)",
        mass_mg=55_000,
    ),
    "STS3215_12V": Actuator(
        "STS3215_12V", "SO-ARM100 sts3215_03a_no_horn_v1.stl; README.md:73",
        body_l_nm=mm(45.4), body_w_nm=mm(24.8), body_h_nm=mm(31.8),
        shaft_from_end_nm=mm(10.2), rotor_d_nm=mm(20.0), coupling_face_nm=mm(2.8),
        spline_d_nm=mm(9.0), spline_h_nm=mm(1.5), back_boss_nm=mm(3.5),
        ear_len_nm=0, ear_t_nm=0, ear_s_nm=0, ear_hole_d_nm=0,
        ear_hole_from_end_nm=0, ear_hole_v_nm=(),
        stall_unmm=int(30.0 * KGCM_TO_UNMM), stall_note="30 kg-cm at 12 V",
        mass_mg=55_000,
    ),
}


@dataclass(frozen=True)
class Bearing:
    """One deep-groove ball bearing (bd_warehouse csv row)."""

    name: str
    row: str
    bore_nm: int      # d
    od_nm: int        # D
    width_nm: int     # B
    shoulder_max_nm: int  # Da: the housing shoulder may be no wider than this
    mass_mg: int


BEARINGS: dict[str, Bearing] = {
    "608": Bearing("608", "M8-22-7", mm(8), mm(22), mm(7), mm(20.0), 12_000),
    "6800": Bearing("6800", "M10-19-5 (61800)", mm(10), mm(19), mm(5), mm(17.0), 5_300),
    "623": Bearing("623", "M3-10-4", mm(3), mm(10), mm(4), mm(8.8), 1_500),
}

# --- FDM fits, reused so one printer has one set of numbers ------------------
HOLE_COMPENSATION_NM: int = enclosure_rules.HOLE_COMPENSATION_NM
FIT_PRESS_NM: int = enclosure_rules.FIT_PRESS_NM
FIT_SLIDE_NM: int = enclosure_rules.FIT_SLIDE_NM
FIT_LOOSE_NM: int = enclosure_rules.FIT_LOOSE_NM
OVERHANG_LIMIT_DEG: int = enclosure_rules.OVERHANG_LIMIT_DEG
BOSS_MIN_WALL_NM: int = enclosure_rules.BOSS_MIN_WALL_NM
SELF_TAP_UNDERSIZE_NM: int = enclosure_rules.SELF_TAP_UNDERSIZE_NM
FLANGE_INSERT = enclosure_rules.INSERTS_SHORT["M3"]
INSERT_BORE_EXTRA_NM: int = mm(1.0)  # enclosure/cad.py INSERT_BORE_EXTRA_NM

#: A servo in its pocket: the sliding fit (it must go in and come out).
POCKET_CLEARANCE_NM: int = FIT_SLIDE_NM
#: Between a moving part and the part it moves against: the loose fit.
JOINT_GAP_NM: int = FIT_LOOSE_NM
#: Structural wall of a housing: twice the printable minimum.
HOUSING_WALL_NM: int = 2 * MIN_WALL_NM
#: The clevis bridge that carries both cheeks: four printable minimums.
BRIDGE_T_NM: int = 4 * MIN_WALL_NM
#: A pressed bearing seat, modelled as ``D + HOLE_COMPENSATION - interference``
#: (the bd_warehouse counterbore with the enclosure's FDM compensation). The
#: diametral interference must land in this band: below it the outer ring
#: spins in PLA, above it the cheek splits.
SEAT_INTERFERENCE_MIN_NM: int = FIT_PRESS_NM
SEAT_INTERFERENCE_MAX_NM: int = 3 * FIT_PRESS_NM
#: Screw depth into the ear-support material.
EAR_SCREW_DEPTH_NM: int = mm(6.0)

#: Cable channel: a 3-way 2.54 mm servo lead connector (3 x 2.54 = 7.62 mm)
#: plus a slide fit either side.
CABLE_CHANNEL_W_NM: int = mm(7.62) + 2 * FIT_SLIDE_NM
CABLE_CHANNEL_D_NM: int = mm(3.0)
CABLE_EXIT_D_NM: int = mm(8.0)
#: Desk bolts through the base: M4, bd_warehouse clearance "Normal" 4.5 mm.
BASE_BOLT_CLEARANCE_NM: int = mm(4.5)

#: Filament density, micrograms per mm^3 (1.24 g/cm^3 = 1240 ug/mm^3).
DENSITY_UG_PER_MM3: dict[str, int] = {"PLA": 1_240, "PETG": 1_270, "ABS": 1_040}
#: Mass estimate: a solid shell of two 0.4 mm perimeters, 15 % infill inside.
PRINT_SHELL_NM: int = mm(0.8)
PRINT_INFILL_PPM: int = 150_000
#: The smallest bed SO-ARM100 targets ("printer bed sizes of 220mmx220mm").
PRINT_BED_NM: tuple[int, int, int] = (mm(220), mm(220), mm(250))

MAX_JOINTS: int = 7
MAX_LINK_NM: int = mm(400)
