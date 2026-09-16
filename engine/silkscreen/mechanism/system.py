"""The whole robot: the arm and its electronics as one checked assembly.

The system agent's ``assemble`` tool (docs/agent-harness.md, item 6; TODO.txt
"build AI CAD for a robot arm, then import and assemble the entire thing with
the electronics"). Two inputs, each either designed here or imported:

- **the arm** -- a :class:`~.cad.MechanismModel` the mechanism kernel built
  and judged, *or* any STEP file a person already has
  (:func:`import_step_part`), such as a vendor arm or last year's design;
- **the board** -- the routed ``.kicad_pcb`` exported through
  ``kicad-cli pcb export step``
  (:func:`silkscreen.enclosure.assembly.export_board_step`), or a board STEP
  someone hands over.

The board is laid flat on the bench beside the base, on the side the base's
cable exit faces (-X, :func:`~.cad._build_base`), on standoffs of the
enclosure's own height (:data:`silkscreen.enclosure.rules.STANDOFF_HEIGHT_NM`),
so the servo leads have the shortest run. Then the kernel measures, with the
same :class:`~.kernel.Clause` shape and signed margins as the arm itself:

- ``arm_imported`` / ``board_imported`` -- the file held at least one solid;
- ``board_clear_home`` -- the board and the arm do not touch with every joint
  at 0;
- ``board_clear_sampled`` -- nor at any pose
  :func:`~.kernel.sampled_poses` visits (a designed arm only).

The honest boundary: **an imported arm carries no joints.** STEP describes
shapes, not kinematics, so an imported arm is checked where it stands and
nothing is claimed about its motion -- the report says so in words, and the
sampled clause is absent rather than green. The prior art for keeping joints
in source rather than in the exchange file is text-to-cad's cadgen
assemblies (earthtojake/text-to-cad ``skills/cad``), which the TODO cites.

Importing this module never imports build123d; assembling does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..enclosure import kernel as enclosure_kernel
from ..enclosure.rules import STANDOFF_HEIGHT_NM
from ..units import mm
from .cad import MechanismModel, posed_parts, require_kernel
from .errors import MechanismBuildError
from .kernel import Clause, _cube, _nm, sampled_poses

__all__ = [
    "BOARD_GAP_NM",
    "ImportedPart",
    "SystemAssembly",
    "SystemReport",
    "assemble_system",
    "export_system",
    "import_step_part",
    "verify_system",
]

#: Clear distance between the base's -X face and the board's nearest edge.
#: A designer's allowance for a hand and a connector plug between the two,
#: not a sourced tolerance; the clauses below measure what it leaves.
BOARD_GAP_NM: int = mm(10)

_VOLUME_TOL_MM3 = 1e-6


@dataclass(frozen=True)
class ImportedPart:
    """A STEP file read into one build123d shape, with where it came from."""

    name: str
    shape: Any
    source: Path
    solids: int


@dataclass(frozen=True)
class SystemAssembly:
    """Every solid of the robot in the bench frame (mm, Z up, base at origin)."""

    arm_model: MechanismModel | None
    arm_import: ImportedPart | None
    board: ImportedPart | None
    board_shape: Any | None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def arm_parts(self, q_rad: list[float] | None = None) -> list[tuple[str, Any]]:
        if self.arm_model is not None:
            return posed_parts(self.arm_model, q_rad)
        assert self.arm_import is not None
        return [(self.arm_import.name, self.arm_import.shape)]


@dataclass(frozen=True)
class SystemReport:
    clauses: tuple[Clause, ...]
    warnings: tuple[str, ...] = ()

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
        }


def import_step_part(path: str | Path, *, name: str | None = None) -> ImportedPart:
    """Read ``path`` as STEP. Raises :class:`MechanismBuildError`
    (``IMPORT_FAILED``) when OCCT cannot read it or it holds no solid -- a file
    that parses to nothing is the quiet zero this package refuses."""
    b = require_kernel()
    path = Path(path)
    if not path.is_file():
        raise MechanismBuildError("IMPORT_FAILED", f"{path}: no such file")
    try:
        shape = b.import_step(path)
    except Exception as exc:  # noqa: BLE001 - OCCT raises its own zoo
        raise MechanismBuildError(
            "IMPORT_FAILED", f"{path}: not readable as STEP: {exc}"
        ) from exc
    solids = list(shape.solids()) if shape is not None else []
    if not solids:
        raise MechanismBuildError(
            "IMPORT_FAILED", f"{path}: parsed as STEP but holds no solid"
        )
    return ImportedPart(
        name=name or path.stem, shape=shape, source=path, solids=len(solids)
    )


def _base_face_x(assembly_arm: list[tuple[str, Any]]) -> float:
    """The arm's -X extent at home: where the bench space beside it starts."""
    return min(shape.bounding_box().min.X for _, shape in assembly_arm)


def assemble_system(
    *,
    arm: MechanismModel | None = None,
    arm_step: str | Path | None = None,
    board_step: str | Path | None = None,
    gap_nm: int = BOARD_GAP_NM,
) -> SystemAssembly:
    """Put the arm and the board on one bench.

    Exactly one of ``arm`` and ``arm_step``. ``board_step`` is optional: an
    arm with no electronics yet still assembles, and the report says the
    board clauses had nothing to check.
    """
    if (arm is None) == (arm_step is None):
        raise ValueError("assemble_system needs exactly one of arm= and arm_step=")
    b = require_kernel()
    warnings: list[str] = []
    arm_import = None
    if arm_step is not None:
        arm_import = import_step_part(arm_step, name="arm")
    if arm_import is not None:
        warnings.append(
            f"the arm was imported from {arm_import.source.name}: a STEP file carries "
            "no joints, so only its pose as drawn is checked"
        )
    model = SystemAssembly(arm, arm_import, None, None)
    home = model.arm_parts()
    board = None
    placed = None
    if board_step is not None:
        board = import_step_part(board_step, name="board")
        bb = board.shape.bounding_box()
        # Flat on standoffs, its +X edge gap_nm short of the arm's -X face,
        # centred on the arm in Y.
        arm_y = [shape.bounding_box() for _, shape in home]
        mid_y = (min(a.min.Y for a in arm_y) + max(a.max.Y for a in arm_y)) / 2
        target_max_x = _base_face_x(home) - gap_nm / 1e6
        dx = target_max_x - bb.max.X
        dy = mid_y - (bb.min.Y + bb.max.Y) / 2
        dz = STANDOFF_HEIGHT_NM / 1e6 - bb.min.Z
        placed = board.shape.moved(b.Location((dx, dy, dz)))
    return SystemAssembly(arm, arm_import, board, placed, tuple(warnings))


def _clearance(a: Any, b: Any) -> tuple[float, str]:
    """Signed clearance: a distance, or minus the cube side of an overlap."""
    d = a.distance(b)
    if d > 1e-6:
        return d, f"{d:.3f} mm apart"
    # The enclosure kernel's boolean, which the arm's own collision clause
    # uses too: one definition of "how much do these overlap". Solid by
    # solid: OCCT's common against an imported STEP *compound* returns an
    # empty result, which read a board sunk 60 mm into the base as "touch".
    vol = sum(enclosure_kernel._intersection_volume(a, s) for s in b.solids())
    if vol > _VOLUME_TOL_MM3:
        return -_cube(vol), f"interfere by {vol:.3f} mm^3"
    return 0.0, "touch"


def _board_clearance(
    assembly: SystemAssembly, q: list[float] | None
) -> tuple[float, str]:
    worst, where = math.inf, ""
    for name, shape in assembly.arm_parts(q):
        margin, text = _clearance(shape, assembly.board_shape)
        if margin < worst:
            worst, where = margin, f"board / {name} {text}"
    return worst, where


def verify_system(
    assembly: SystemAssembly, *, sample_poses: bool = True
) -> SystemReport:
    """Measure the assembly. A clause that raises fails in words."""
    clauses: list[Clause] = []
    warnings = list(assembly.warnings)

    def guarded(name: str, unit: str, fn) -> None:
        try:
            clauses.append(fn())
        except Exception as exc:  # noqa: BLE001
            clauses.append(Clause(name, False, 0, unit,
                                  f"could not evaluate: {type(exc).__name__}: {exc}"))

    if assembly.arm_import is not None:
        imp = assembly.arm_import
        clauses.append(Clause("arm_imported", True, imp.solids * 1_000_000, "solids",
                              f"{imp.source.name}: {imp.solids} solid(s)"))
    if assembly.board is None:
        clauses.append(Clause("board_imported", True, 0, "solids",
                              "nothing to check: no board STEP was given"))
        warnings.append("no board was assembled; the electronics are not checked")
        return SystemReport(tuple(clauses), tuple(warnings))
    brd = assembly.board
    clauses.append(Clause("board_imported", True, brd.solids * 1_000_000, "solids",
                          f"{brd.source.name}: {brd.solids} solid(s)"))

    def home() -> Clause:
        n = len(assembly.arm_model.spec.joints) if assembly.arm_model else 0
        margin, where = _board_clearance(assembly, [0.0] * n if n else None)
        return Clause("board_clear_home", margin > 1e-6, _nm(margin), "mm",
                      f"closest: {where} with every joint at 0")

    guarded("board_clear_home", "mm", home)

    if assembly.arm_model is not None and sample_poses:
        def sampled() -> Clause:
            worst, where = math.inf, ""
            poses = sampled_poses(assembly.arm_model)
            for label, q in poses:
                margin, text = _board_clearance(assembly, q)
                if margin < worst:
                    worst, where = margin, f"{text} at {label}"
            return Clause("board_clear_sampled", worst > 1e-6, _nm(worst), "mm",
                          f"closest over {len(poses)} pose(s): {where}")

        guarded("board_clear_sampled", "mm", sampled)
    elif assembly.arm_model is None:
        warnings.append(
            "board_clear_sampled not measured: an imported arm has no joints"
        )
    return SystemReport(tuple(clauses), tuple(warnings))


def export_system(
    assembly: SystemAssembly, directory: str | Path, stem: str = "robot",
) -> Path:
    """``<stem>.step``: every arm part and the board, one labelled child each."""
    b = require_kernel()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    children = [
        b.Part(shape.wrapped, label=name) for name, shape in assembly.arm_parts()
    ]
    if assembly.board_shape is not None:
        children.extend(
            b.Part(solid.wrapped, label=f"board_{i}")
            for i, solid in enumerate(assembly.board_shape.solids())
        )
    path = directory / f"{stem}.step"
    try:
        b.export_step(b.Compound(label=stem, children=children), path)
    except Exception as exc:  # noqa: BLE001
        raise MechanismBuildError("INVALID_SHAPE", f"export: {exc}") from exc
    return path
