"""Design the case, not just size it: a model-written style pass, kernel-gated.

:func:`~silkscreen.agents.enclosure.propose_enclosure` produces a case whose
mechanics are measured and right and whose shape is always the same box. This
module is the design half, built the way the open-source AI-CAD projects with
real users build it: the model writes CAD code and revises it against what it
made (earthtojake/text-to-cad ``skills/cad/references/repair-loop.md``;
Adam-CAD/CADAM ``shared/chatAi.ts``, where ``build_parametric_model`` is called
again after "inspect the returned multi-view preview sheet"). Here the model
sees a four-view render of the verified case, writes a build123d
``style(base, lid, facts)`` function, the script runs sandboxed
(:mod:`silkscreen.enclosure.restyle`), and the kernel re-measures every clause.

Two rules keep design from ever costing fit:

* **No new failures.** A restyle is accepted only when every clause the
  plain case passed still passes. Anything else goes back to the model with
  the failing clause lines, the same batched-repair shape as every other loop
  in :mod:`silkscreen.agents`.
* **The plain case is the floor.** When the budget runs out the verified plain
  case ships, with a warning saying the restyle was tried and why it was not
  used -- never a half-styled case and never a silent fallback.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, NamedTuple

from ..enclosure.board_shape import BoardEnvelope
from ..enclosure.cad import build_enclosure
from ..enclosure.errors import EnclosureError
from ..enclosure.ir import MIN_WALL_NM, EnclosureSpec
from ..enclosure.kernel import KernelReport, verify_model
from ..enclosure.restyle import (
    extract_script,
    restyle_violations,
    run_style_script,
    style_facts,
    unsandboxed_refusal,
)
from ..enclosure.snapshot import render_grid
from ..units import mm
from .enclosure import EnclosureProposal, _facts_block
from .model import Document, Model

__all__ = ["STYLE_MARKER", "STYLE_PROMPT", "StyleOutcome", "restyle_enclosure"]

#: Keys ``ScriptedModel.by_marker``; appears verbatim in every style prompt.
STYLE_MARKER = "ENCLOSURE-STYLE v1"

#: Repair rounds after the first attempt. Two, like rigorous sourcing: a fillet
#: radius that OCCT refuses is fixed in one round far more often than not.
DEFAULT_STYLE_REPAIRS = 2

#: The modeling guidance is adapted from text-to-cad's
#: ``skills/cad/references/build123d-modeling.md`` (MIT, Thompson Labs LLC):
#: operation order, overshooting boolean tools, selecting by position rather
#: than list index, fillets last.
STYLE_PROMPT = f"""\
You are an industrial designer finishing a 3D-printed enclosure for a PCB
({STYLE_MARKER}). The attached image shows the case as it is now: isometric,
opposite isometric, top view and a section. Its mechanics are already correct
and measured -- standoffs, connector openings, the lid lip and the fit around
the board. Your job is to make it look and feel like a designed product.

Write ONE Python code block defining:

    def style(base, lid, facts):
        ...
        return base, lid

Frames and units (millimetres):
- base: assembled frame, floor on z=0, open top at z = facts["base_height_mm"].
- lid: printed orientation, its outer face on the bed at z=0 (it is flipped
  onto the base when assembled). lid is None when facts["has_lid"] is false;
  return None for it then.
- facts: outer_mm [x, y, z], wall_mm, lid_thickness_mm, lip_depth_mm,
  cavity_mm, params_mm (every number the builder used).
- `bd` is build123d, already imported. Do not import anything else.
- Use `safe_fillet(shape, edges, radius)` and `safe_chamfer(shape, edges,
  length)` instead of bd.fillet / bd.chamfer. They cap the size at what the
  kernel can build on those edges (OCCT's max_fillet) and step down until it
  succeeds, returning the shape unchanged only if nothing fits.

Hard rules -- a CAD kernel re-checks all of them and rejects the script:
- Return exactly one valid solid for base and one for lid.
- Stay inside the original outer bounding box: remove material, round and
  chamfer; never add material outside it.
- Keep every wall at least wall_mm thick; never cut into the cavity, the lid
  lip, the standoffs or the existing connector openings.
- Faces that print flat on the bed must stay flat, and keep overhangs at or
  under 45 degrees.

How to model (build123d):
- Order: major edge treatment first (vertical corner rounds), then recesses
  and patterns, fillets and chamfers on small edges last.
- Select edges and faces by axis and position (bounding box, filter_by,
  sort_by), never by list index.
- Overshoot cutting tools about 1 mm past the faces they cut; cut repeated
  features in one combined operation.
- Prefer a modest size (1-2 mm on a small case); safe_fillet shrinks it if the
  geometry cannot take it, but a design that relies on the shrink looks
  unintended.

Good design moves: rounded vertical corners, a softened top edge on the lid,
a shallow recessed panel or grip pattern on the lid, a small chamfer on the
bottom edge so the first layer does not flare, a debossed line that reads as
a parting seam. Honour the style the user asked for when there is one.
"""


class StyleOutcome(NamedTuple):
    """The proposal that ships, the script that styled it, and what to tell."""

    proposal: EnclosureProposal
    script: str | None
    rounds: int
    warnings: tuple[str, ...]


#: Extra wall a restyle may shape, in nm. The plain case's walls are exactly
#: the spec's, and ``min_wall`` requires the spec wall less 50 um -- so on the
#: first live run (gemini-3.5-flash, 2026-09-14) every chamfer or fillet on an
#: outer edge thinned a wall below the requirement, and the design space was
#: nothing but vertical corner rounds. A designer leaves material to shape:
#: the case to be styled is rebuilt with walls this much thicker and still
#: verified against the *original* spec, so every guarantee is unchanged and
#: the model has a millimetre of skin to work in. The case grows by twice this
#: in X and Y.
STYLE_SKIN_NM = mm(1.0)


def _with_skin(
    proposal: EnclosureProposal, envelope: BoardEnvelope
) -> tuple[Any, KernelReport]:
    """The case to style: walls thickened by the skin, verified against the
    original spec; the accepted plain case when the thicker one does not
    build or does not pass everything the plain one passed."""
    spec = proposal.spec
    try:
        thick = build_enclosure(
            replace(spec, wall_nm=spec.wall_nm + STYLE_SKIN_NM), envelope
        )
        report = verify_model(thick, spec, envelope)
    except EnclosureError:
        return proposal.model, proposal.kernel
    if set(report.failed) - set(proposal.kernel.failed):
        return proposal.model, proposal.kernel
    return thick, report


def _gist(text: str) -> str:
    """One line worth showing: the headline plus a traceback's final line."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    gist = lines[0] if len(lines) == 1 else f"{lines[0]} {lines[-1]}"
    return gist[:200]


def _new_failures(plain: KernelReport, styled: KernelReport) -> list[str]:
    """Clause lines the restyle broke that the plain case passed."""
    already = set(plain.failed)
    return [
        line
        for line in styled.text().splitlines()
        if line.startswith("FAIL") and line.split()[1] not in already
    ]


def restyle_enclosure(
    model: Model,
    proposal: EnclosureProposal,
    envelope: BoardEnvelope,
    *,
    style_hint: str = "",
    max_repairs: int = DEFAULT_STYLE_REPAIRS,
    timeout_s: float | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> StyleOutcome:
    """Restyle an accepted case; return it styled, or the plain case and why.

    A :class:`~silkscreen.agents.model.ModelError` is not wrapped (the
    ``propose_circuit`` convention): an outage is a different condition from a
    script that did not work, and the caller decides what an outage means for
    the case. Every script failure, by contrast, is handled here.
    """
    spec: EnclosureSpec = proposal.spec
    if proposal.model is None or proposal.kernel is None:
        return StyleOutcome(proposal, None, 0, ("restyle skipped: no built case",))
    # Before the model is asked: a script that may not run is not worth a call.
    refusal = unsandboxed_refusal()
    if refusal is not None:
        return StyleOutcome(proposal, None, 0, (refusal,))
    plain, plain_report = _with_skin(proposal, envelope)

    render = Document(data=render_grid(plain), mime_type="image/png")

    base_prompt = (
        f"{STYLE_PROMPT}\n"
        + (f"\nStyle the user asked for:\n{style_hint}\n" if style_hint else "")
        + f"\nBoard facts:\n{_facts_block(envelope)}\n"
        + f"\nfacts passed to style():\n{json.dumps(style_facts(plain), indent=1)}\n"
    )
    prompt = base_prompt
    last = ""
    kwargs = {} if timeout_s is None else {"timeout_s": timeout_s}
    for round_no in range(max_repairs + 1):
        # 32k, not the 8k default: on Gemini 3 reasoning tokens come out of the
        # same budget, and 8k was measured cutting a style script off at
        # 1,279 characters (gemini-3.5-flash, 2026-09-14).
        raw = model.generate(
            prompt, documents=[render], temperature=0.2, max_output_tokens=32768
        )
        script = extract_script(raw)
        result = run_style_script(plain, script, **kwargs)
        if result.model is not None:
            styled_report = verify_model(result.model, spec, envelope)
            # The kernel samples; restyle_violations is exact. Both must hold.
            required = max(spec.wall_nm, MIN_WALL_NM) / 1e6 - 0.05
            broke = _new_failures(plain_report, styled_report) + restyle_violations(
                plain, result.model, required
            )
            if not broke:
                if on_event is not None:
                    event = {"event": "enclosure.style", "round": round_no + 1}
                    on_event({**event, "accepted": True})
                styled = proposal._replace(model=result.model, kernel=styled_report)
                return StyleOutcome(styled, script, round_no, ())
            problems = "\n".join(broke)
            last = f"the kernel rejected the restyle:\n{problems}"
        else:
            last = f"the script failed:\n{result.error}"
        if on_event is not None:
            on_event(
                {
                    "event": "enclosure.style",
                    "round": round_no + 1,
                    "accepted": False,
                    # The informative line of a traceback is its last one.
                    "reason": _gist(last),
                }
            )
        prompt = (
            f"{base_prompt}\nYour previous script was rejected -- {last}\n\n"
            f"Previous script:\n```python\n{script}\n```\n"
            "Return the corrected script."
        )
    note = _gist(last) if last else "no script"
    return StyleOutcome(
        proposal,
        None,
        max_repairs + 1,
        (
            f"restyle not used after {max_repairs + 1} attempt(s) ({note}); "
            "the verified plain case ships",
        ),
    )
