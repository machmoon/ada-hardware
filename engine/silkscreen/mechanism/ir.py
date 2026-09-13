"""MECHANISM-SPEC v1: the serial chain a model may propose.

The contract between a model and the mechanism kernel, shaped like
:mod:`silkscreen.netlist` and :mod:`silkscreen.enclosure.ir`: the model's raw
answer goes through :func:`parse_mechanism_spec`, which collects **every**
problem into one :class:`~.errors.MechanismValidationError` so the whole
batch can go back as a single repair prompt.

What the model says is design intent -- which joints, in which order, about
which axis, over what range, driven by which catalogue actuator, with which
catalogue bearing, and how long each link is -- plus the requirement
(payload, reach). What it never says is a bracket dimension: those come from
:mod:`.rules`.

The chain is URDF-shaped rather than Denavit-Hartenberg-shaped (the choice
ikpy's ``URDFLink`` makes, ``src/ikpy/link.py``): each joint rotates about an
axis *named in the incoming link's frame*, then the link extends along its
own +Z by ``link_mm``. ``pitch`` is about the link frame's Y (a hinge), and
``yaw``/``roll`` are about its Z (a twist; the two words are the same
rotation, kept apart because an engineer calls the one at the base "yaw" and
the one at the wrist "roll"). The home pose is every joint at 0: the arm
straight up.

JSON shape::

    {
      "name": "desk arm",
      "base": {"type": "bolt_down" | "freestanding", "footprint_mm": <opt>},
      "joints": [
        {"id": "shoulder_pan", "axis": "yaw" | "pitch" | "roll",
         "range_deg": [-110, 110], "actuator": "STS3215",
         "bearing": "608" | "6800" | "623" | "none", "link_mm": 60}
      ],
      "payload_g": 200,
      "reach_mm": 300,
      "tool": {"length_mm": 60, "mass_g": 40},
      "material": "PLA" | "PETG" | "ABS",
      "mass_budget_g": <optional>
    }

Integer units: nanometres, milligrams, millidegrees.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..units import mm
from . import rules
from .errors import MechanismValidationError

__all__ = [
    "MECHANISM_SPEC_VERSION",
    "AXES",
    "HINGE_AXES",
    "TWIST_AXES",
    "BASE_TYPES",
    "MATERIALS",
    "Joint",
    "MechanismSpec",
    "parse_mechanism_spec",
]

MECHANISM_SPEC_VERSION = "MECHANISM-SPEC v1"
AXES = ("yaw", "pitch", "roll")
HINGE_AXES = ("pitch",)
TWIST_AXES = ("yaw", "roll")
BASE_TYPES = ("bolt_down", "freestanding")
MATERIALS = tuple(rules.DENSITY_UG_PER_MM3)

_TOP_KEYS = {
    "name", "base", "joints", "payload_g", "reach_mm", "tool", "material",
    "mass_budget_g",
}
_JOINT_KEYS = {"id", "axis", "range_deg", "actuator", "bearing", "link_mm"}
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


@dataclass(frozen=True)
class Joint:
    id: str
    axis: str
    range_mdeg: tuple[int, int]
    actuator: str
    bearing: str          # a BEARINGS key or "none"
    link_nm: int          # from this joint to the next one (or the flange)

    @property
    def is_hinge(self) -> bool:
        return self.axis in HINGE_AXES


@dataclass(frozen=True)
class MechanismSpec:
    name: str
    base_type: str
    base_footprint_nm: int | None
    joints: tuple[Joint, ...]
    payload_mg: int
    reach_nm: int
    tool_length_nm: int
    tool_mass_mg: int
    material: str
    mass_budget_mg: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """The spec back in its JSON shape (mm, g, deg)."""
        out: dict[str, Any] = {
            "name": self.name,
            "base": {"type": self.base_type},
            "joints": [
                {
                    "id": j.id,
                    "axis": j.axis,
                    "range_deg": [j.range_mdeg[0] / 1000, j.range_mdeg[1] / 1000],
                    "actuator": j.actuator,
                    "bearing": j.bearing,
                    "link_mm": j.link_nm / 1e6,
                }
                for j in self.joints
            ],
            "payload_g": self.payload_mg / 1000,
            "reach_mm": self.reach_nm / 1e6,
            "tool": {
                "length_mm": self.tool_length_nm / 1e6,
                "mass_g": self.tool_mass_mg / 1000,
            },
            "material": self.material,
        }
        if self.base_footprint_nm is not None:
            out["base"]["footprint_mm"] = self.base_footprint_nm / 1e6
        if self.mass_budget_mg is not None:
            out["mass_budget_g"] = self.mass_budget_mg / 1000
        return out


def _strip_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines)
    return s


def _number(
    errors: list[str], where: str, value: Any, lo: float, hi: float,
    *, lo_open: bool = False,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{where} must be a number, got {value!r}")
        return None
    bad_lo = value <= lo if lo_open else value < lo
    if bad_lo or value > hi:
        bracket = "(" if lo_open else "["
        errors.append(f"{where} must be in {bracket}{lo:g}, {hi:g}], got {value:g}")
        return None
    return float(value)


def parse_mechanism_spec(raw: str | dict[str, Any]) -> MechanismSpec:
    """Validate model output into a :class:`MechanismSpec`.

    Accepts a dict or JSON text (a Markdown fence is tolerated). Every
    problem is collected; nothing is raised until all were found.
    """
    errors: list[str] = []
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_fence(raw))
        except json.JSONDecodeError as exc:
            raise MechanismValidationError([f"not valid JSON: {exc}"]) from exc
    else:
        data = raw
    if not isinstance(data, dict):
        raise MechanismValidationError(["the spec must be one JSON object"])

    for key in sorted(set(data) - _TOP_KEYS):
        errors.append(f"unknown key {key!r} (allowed: {', '.join(sorted(_TOP_KEYS))})")

    name = data.get("name", "mechanism")
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        errors.append("name must be a non-empty string of at most 60 characters")
        name = "mechanism"

    base = data.get("base", {"type": "bolt_down"})
    base_type, footprint_nm = "bolt_down", None
    if not isinstance(base, dict):
        errors.append("base must be an object like {\"type\": \"bolt_down\"}")
    else:
        for key in sorted(set(base) - {"type", "footprint_mm"}):
            errors.append(f"base: unknown key {key!r}")
        base_type = base.get("type", "bolt_down")
        if base_type not in BASE_TYPES:
            errors.append(f"base.type must be one of {BASE_TYPES}, got {base_type!r}")
            base_type = "bolt_down"
        if "footprint_mm" in base and base["footprint_mm"] is not None:
            fp = _number(errors, "base.footprint_mm", base["footprint_mm"], 0, 400,
                         lo_open=True)
            footprint_nm = None if fp is None else mm(fp)

    joints: list[Joint] = []
    raw_joints = data.get("joints")
    if not isinstance(raw_joints, list) or not raw_joints:
        errors.append("joints must be a non-empty list")
        raw_joints = []
    elif len(raw_joints) > rules.MAX_JOINTS:
        errors.append(f"at most {rules.MAX_JOINTS} joints, got {len(raw_joints)}")
    seen: set[str] = set()
    for i, rj in enumerate(raw_joints):
        where = f"joints[{i}]"
        if not isinstance(rj, dict):
            errors.append(f"{where} must be an object")
            continue
        for key in sorted(set(rj) - _JOINT_KEYS):
            errors.append(f"{where}: unknown key {key!r}")
        jid = rj.get("id")
        if not isinstance(jid, str) or not _ID.match(jid):
            errors.append(f"{where}.id must be an identifier like 'shoulder_lift', got {jid!r}")
            jid = f"j{i + 1}"
        elif jid in seen:
            errors.append(f"{where}.id {jid!r} is used twice")
        seen.add(jid)
        where = f"joint {jid!r}"
        axis = rj.get("axis")
        if axis not in AXES:
            errors.append(f"{where}: axis must be one of {AXES}, got {axis!r}")
            axis = "pitch"
        if i == 0 and axis != "yaw":
            errors.append(
                f"{where}: the first joint must be 'yaw' (a turntable base, as on "
                f"SO-101 and every surveyed desktop arm), got {axis!r}"
            )
        rng = rj.get("range_deg")
        lo_mdeg, hi_mdeg = -90_000, 90_000
        if (
            not isinstance(rng, list) or len(rng) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in rng)
        ):
            errors.append(f"{where}: range_deg must be [min, max] in degrees, got {rng!r}")
        else:
            lo, hi = float(rng[0]), float(rng[1])
            if not (-180 <= lo < hi <= 180):
                errors.append(
                    f"{where}: range_deg must satisfy -180 <= min < max <= 180, got {rng}"
                )
            elif not lo <= 0 <= hi:
                errors.append(f"{where}: range_deg must include 0 (the home pose), got {rng}")
            else:
                lo_mdeg, hi_mdeg = round(lo * 1000), round(hi * 1000)
        actuator = rj.get("actuator")
        if actuator not in rules.ACTUATORS:
            errors.append(
                f"{where}: actuator must be one of {sorted(rules.ACTUATORS)}, got {actuator!r}"
            )
            actuator = "SG90"
        bearing = rj.get("bearing", "none")
        if bearing != "none" and bearing not in rules.BEARINGS:
            errors.append(
                f"{where}: bearing must be 'none' or one of {sorted(rules.BEARINGS)}, "
                f"got {bearing!r}"
            )
            bearing = "none"
        elif bearing != "none" and axis not in HINGE_AXES:
            errors.append(
                f"{where}: a bearing is only supported on a 'pitch' joint (it seats in "
                f"the idler cheek of the clevis), got axis {axis!r}"
            )
            bearing = "none"
        link = _number(errors, f"{where}: link_mm", rj.get("link_mm"), 0,
                       rules.MAX_LINK_NM / 1e6, lo_open=True)
        joints.append(
            Joint(
                id=jid, axis=axis, range_mdeg=(lo_mdeg, hi_mdeg), actuator=actuator,
                bearing=bearing, link_nm=mm(link) if link is not None else mm(50),
            )
        )

    payload = _number(errors, "payload_g", data.get("payload_g"), 0, 5000)
    reach = _number(errors, "reach_mm", data.get("reach_mm"), 0, 2000, lo_open=True)
    tool = data.get("tool", {})
    tool_len, tool_mass = 0.0, 0.0
    if not isinstance(tool, dict):
        errors.append("tool must be an object like {\"length_mm\": 60, \"mass_g\": 40}")
    else:
        for key in sorted(set(tool) - {"length_mm", "mass_g"}):
            errors.append(f"tool: unknown key {key!r}")
        tool_len = _number(errors, "tool.length_mm", tool.get("length_mm", 0), 0, 300) or 0.0
        tool_mass = _number(errors, "tool.mass_g", tool.get("mass_g", 0), 0, 2000) or 0.0
    material = data.get("material", "PLA")
    if material not in MATERIALS:
        errors.append(f"material must be one of {MATERIALS}, got {material!r}")
        material = "PLA"
    budget = data.get("mass_budget_g")
    budget_mg = None
    if budget is not None:
        b = _number(errors, "mass_budget_g", budget, 0, 20000, lo_open=True)
        budget_mg = None if b is None else round(b * 1000)

    if errors:
        raise MechanismValidationError(errors)
    return MechanismSpec(
        name=name.strip(),
        base_type=base_type,
        base_footprint_nm=footprint_nm,
        joints=tuple(joints),
        payload_mg=round((payload or 0.0) * 1000),
        reach_nm=mm(reach or 0.0),
        tool_length_nm=mm(tool_len),
        tool_mass_mg=round(tool_mass * 1000),
        material=material,
        mass_budget_mg=budget_mg,
    )
