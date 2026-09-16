"""Run a model-written build123d *style* script on a verified case, sandboxed.

Why a script at all: every open-source AI-CAD project with real users has the
model write CAD code and look at what it made -- earthtojake/text-to-cad
(``skills/cad/SKILL.md``, build123d scripts with an inspect-and-repair loop,
``references/repair-loop.md``) and Adam-CAD/CADAM (``shared/chatAi.ts``,
``build_parametric_model`` writes OpenSCAD and revises from a multi-view
preview). A JSON spec filled into one template can only ever produce that
template, which is why every case this engine made was the same plain box.

Why a *style* pass over the deterministic case rather than a free-form part:
:func:`~silkscreen.enclosure.cad.build_enclosure` already makes the mechanics
right -- standoffs on the board's holes, openings sized to the mating plug, a
lid that seats -- and :mod:`~silkscreen.enclosure.kernel` measures them. The
script receives that verified base and lid and returns restyled solids; the
kernel then re-measures **every** clause against keep-outs computed from the
board, never from the script. A style that breaks a clause is rejected, so
design freedom can never cost fit.

The contract the script is held to::

    def style(base, lid, facts):
        ...
        return base, lid

``base`` is in the assembled frame (X as KiCad, Y flipped, Z up, floor at
z=0); ``lid`` is in its printed orientation (outer face on the bed at z=0);
``facts`` is a plain dict of millimetre numbers. Both returns must be single
valid solids inside the original outer bounding box -- rounding, chamfering,
recessing and cutting patterns, not adding bulk outside it.

Isolation: the script runs in a child interpreter with a timeout. On macOS it
runs under ``sandbox-exec`` with network denied and writes denied everywhere
but its own scratch directory, so model-written code can read nothing out and
change nothing outside the job. Where ``sandbox-exec`` does not exist the
subprocess and timeout still apply and :data:`SANDBOXED` says so -- the
difference is stated, never hidden.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cad import EnclosureModel, require_kernel

__all__ = [
    "SANDBOXED",
    "STYLE_TIMEOUT_S",
    "StyleResult",
    "extract_script",
    "restyle_violations",
    "run_style_script",
]

#: Seconds one style script may run. Fillets on a small case take well under
#: a second (measured 0.2 s on the assembly fixture); a script that needs a
#: minute is looping or fighting the kernel, and the repair loop is cheaper.
STYLE_TIMEOUT_S = 60.0

#: Most characters of script accepted and of error text sent back.
MAX_SCRIPT_CHARS = 20_000
MAX_ERROR_CHARS = 1_500

SANDBOXED = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

_RUNNER = r'''
import json, sys, traceback
import build123d as bd

def _one_solid(shape, what):
    solids = shape.solids() if hasattr(shape, "solids") else []
    if len(solids) != 1:
        raise ValueError(f"{what} must be exactly one solid, got {len(solids)}")
    if not solids[0].is_valid:
        raise ValueError(f"{what} is not a valid solid")
    return solids[0]

def safe_fillet(shape, edges, radius):
    """Fillet at up to ``radius``: capped at OCCT's max_fillet for these edges,
    then stepped down; the shape back unchanged when nothing fits. The OCCT
    error itself says "try a smaller value or use max_fillet()", and
    text-to-cad's repair-loop guidance is the same -- so the script is given
    that repair instead of dying on its first radius."""
    edges = list(edges)
    if not edges:
        return shape
    try:
        radius = min(radius, 0.95 * shape.max_fillet(edges, tolerance=0.05))
    except Exception:
        pass
    size = radius
    while size >= 0.1:
        try:
            return bd.fillet(edges, size)
        except Exception:
            size *= 0.7
    return shape

def safe_chamfer(shape, edges, length):
    """Chamfer at up to ``length``, stepping down; unchanged when nothing fits."""
    edges = list(edges)
    size = length
    while edges and size >= 0.1:
        try:
            return bd.chamfer(edges, size)
        except Exception:
            size *= 0.7
    return shape

try:
    facts = json.load(open("facts.json"))
    base = _one_solid(bd.import_step("base_in.step"), "input base")
    lid = None
    if facts["has_lid"]:
        lid = _one_solid(bd.import_step("lid_in.step"), "input lid")
    scope = {
        "bd": bd,
        "safe_fillet": safe_fillet,
        "safe_chamfer": safe_chamfer,
        "__name__": "style_script",
    }
    exec(compile(open("style.py").read(), "style.py", "exec"), scope)
    if "style" not in scope:
        raise NameError("the script must define style(base, lid, facts)")
    out = scope["style"](base, lid, facts)
    if not (isinstance(out, tuple) and len(out) == 2):
        raise TypeError("style() must return (base, lid)")
    new_base = _one_solid(out[0], "returned base")
    bd.export_step(new_base, "base_out.step")
    if lid is not None:
        new_lid = _one_solid(out[1], "returned lid")
        bd.export_step(new_lid, "lid_out.step")
except BaseException:
    sys.stderr.write(traceback.format_exc())
    sys.exit(3)
'''


@dataclass(frozen=True)
class StyleResult:
    """The restyled model, or the reason there is none."""

    model: EnclosureModel | None
    error: str | None


def extract_script(text: str) -> str:
    """The Python in a model answer: the first fenced block, else the text."""
    match = _FENCE.search(text or "")
    return (match.group(1) if match else (text or "")).strip()


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_ERROR_CHARS:
        return text
    # The tail of a traceback names the failing line; keep that half.
    return "…" + text[-(MAX_ERROR_CHARS - 1) :]


def _sandbox_argv(workdir: Path, argv: list[str]) -> list[str]:
    if not SANDBOXED:
        return argv
    real = os.path.realpath(workdir)
    profile = (
        "(version 1)(allow default)(deny network*)(deny file-write*)"
        f'(allow file-write* (subpath "{real}") (subpath "/private/var/folders")'
        ' (literal "/dev/null") (literal "/dev/tty"))'
    )
    return ["sandbox-exec", "-p", profile, *argv]


def style_facts(model: EnclosureModel) -> dict[str, Any]:
    """The numbers a style script may design around, in millimetres."""
    ox, oy, oz = model.outer_nm
    p = model.params_mm
    return {
        "outer_mm": [ox / 1e6, oy / 1e6, oz / 1e6],
        "base_height_mm": p.get("base_z"),
        "wall_mm": p.get("wall"),
        "lid_thickness_mm": p.get("lid_z"),
        "lip_depth_mm": p.get("lip_depth"),
        "cavity_mm": [p.get("cavity_x"), p.get("cavity_y"), p.get("cavity_z")],
        "has_lid": model.lid is not None,
        "params_mm": dict(p),
    }


def run_style_script(
    model: EnclosureModel, script: str, *, timeout_s: float = STYLE_TIMEOUT_S
) -> StyleResult:
    """Apply ``script`` to ``model``'s base and lid; never raises for the script.

    Every way the script can fail -- a syntax error, a kernel exception, a
    timeout, returning two solids where one was promised -- comes back as
    ``StyleResult(None, error)`` with the child's own traceback tail, which is
    exactly what the repair prompt needs. The returned model keeps the board,
    plugs, standoffs and outer size of the input, so the kernel checks the
    restyle against the same keep-outs the deterministic case passed.
    """
    b = require_kernel()
    if not script.strip():
        return StyleResult(None, "the answer contained no Python script")
    if len(script) > MAX_SCRIPT_CHARS:
        return StyleResult(
            None,
            f"the script is {len(script)} characters; the limit is {MAX_SCRIPT_CHARS}",
        )
    with tempfile.TemporaryDirectory(prefix="ada-style-") as tmp:
        work = Path(tmp)
        b.export_step(model.base, work / "base_in.step")
        if model.lid is not None:
            b.export_step(model.lid, work / "lid_in.step")
        (work / "facts.json").write_text(json.dumps(style_facts(model)))
        (work / "style.py").write_text(script)
        (work / "runner.py").write_text(_RUNNER)
        argv = _sandbox_argv(work, [sys.executable, "-I", "runner.py"])
        try:
            proc = subprocess.run(
                argv,
                cwd=work,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env={"PATH": os.environ.get("PATH", ""), "HOME": str(work)},
            )
        except subprocess.TimeoutExpired:
            return StyleResult(None, f"the script ran past its {timeout_s:.0f} s limit")
        if proc.returncode != 0:
            detail = _clip(proc.stderr) or f"exit code {proc.returncode}"
            return StyleResult(None, detail)
        try:
            base = b.import_step(work / "base_out.step").solids()[0]
            lid = (
                b.import_step(work / "lid_out.step").solids()[0]
                if model.lid is not None
                else None
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return StyleResult(None, f"could not read the restyled solids back: {exc}")
    from dataclasses import replace

    return StyleResult(replace(model, base=base, lid=lid), None)


#: Volume, in mm³, below which a boolean difference is kernel noise rather
#: than material a script added or removed.
NOISE_MM3 = 0.5


def restyle_violations(
    plain: EnclosureModel, styled: EnclosureModel, required_wall_mm: float
) -> list[str]:
    """What a restyle broke that the sampled kernel clauses cannot see.

    Measured 2026-09-14: a script that cut a 4 x 4 mm hole straight through the
    case floor passed all thirteen kernel clauses, because ``min_wall`` samples
    76 rays over the outer faces and a small cut falls between them. The
    deterministic builder never makes such a cut; a model-written script can.
    So the restyle is judged exactly, as booleans against the verified case:

    * **nothing added** -- ``styled - plain`` is empty, so the case cannot grow
      into anything the keep-outs were measured against;
    * **nothing removed near the inside** -- no material within the required
      wall of the base cavity, or of the lid's lip and underside, may go. That
      is the ``min_wall`` rule stated as geometry: a point on the new surface
      closer than the wall to the inside is a wall thinner than allowed.
    """
    b = require_kernel()
    problems: list[str] = []
    p = plain.params_mm

    def vol(shape) -> float:
        try:
            return float(shape.volume) if shape is not None else 0.0
        except Exception:  # noqa: BLE001 - an empty result has no volume
            return 0.0

    def common(a, c):
        """``a & c``, or None when ``a`` is empty -- build123d refuses to
        intersect an empty compound, which is exactly what an untouched part's
        difference is."""
        if vol(a) <= NOISE_MM3:
            return None
        return a & c

    pairs = [("base", plain.base, styled.base)]
    if plain.lid is not None and styled.lid is not None:
        pairs.append(("lid", plain.lid, styled.lid))
    for name, before, after in pairs:
        added = vol(after - before)
        if added > NOISE_MM3:
            problems.append(
                f"{name}: {added:.1f} mm³ of material added outside the verified "
                "case; a restyle may only round, chamfer and cut"
            )

    wall = p["wall"]
    floor = p["base_z"] - p["cavity_z"]
    cavity = b.Box(
        p["cavity_x"], p["cavity_y"], p["cavity_z"] + 1.0,
        align=(b.Align.MIN, b.Align.MIN, b.Align.MIN),
    ).moved(b.Location((wall, wall, floor)))
    guard = b.offset(cavity, required_wall_mm, kind=b.Kind.ARC)
    cut = vol(common(plain.base - styled.base, guard))
    if cut > NOISE_MM3:
        problems.append(
            f"base: {cut:.1f} mm³ removed within {required_wall_mm:.2f} mm of the "
            "cavity -- a wall or the floor would be thinner than required (or "
            "cut through)"
        )

    if plain.lid is not None and styled.lid is not None:
        bb = plain.lid.bounding_box()
        above_plate = b.Box(
            bb.size.X + 2, bb.size.Y + 2, bb.max.Z - p["lid_z"] + 1,
            align=(b.Align.MIN, b.Align.MIN, b.Align.MIN),
        ).moved(b.Location((bb.min.X - 1, bb.min.Y - 1, p["lid_z"])))
        inside = plain.lid & above_plate
        if vol(inside) > NOISE_MM3:
            guard = b.offset(inside, required_wall_mm, kind=b.Kind.ARC)
            cut = vol(common(plain.lid - styled.lid, guard))
            if cut > NOISE_MM3:
                problems.append(
                    f"lid: {cut:.1f} mm³ removed within {required_wall_mm:.2f} mm "
                    "of the lip or underside -- the lid would be thinner than "
                    "required where it seats"
                )
    return problems
