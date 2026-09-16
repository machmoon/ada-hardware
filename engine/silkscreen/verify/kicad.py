"""KiCad's own checks as verifiers: ERC, DRC and schematic parity.

``kicad-cli`` is the authority every Python check in this repo was calibrated
against (CLAUDE.md, "Verifying against real KiCad"), and until 2026-09-15 no
generated run ever called it -- it was a gate a person ran afterwards. This
module runs it the way atopile does (``src/faebryk/libs/kicad/drc.py::run_drc``:
a JSON report into a temporary directory, ``--severity-all``) and turns the
report into a :class:`~silkscreen.verify.verdict.Verdict`:

* each violation becomes one failed :class:`Clause`, blocking when KiCad's own
  ``severity`` is ``error`` and a warning otherwise -- the verdict does not
  re-rank KiCad;
* the types the emitter produces on purpose (``lib_symbol_issues``,
  ``footprint_link_issues``, ``lib_footprint_issues``: every symbol and
  footprint is embedded in the file rather than looked up) are counted in
  the evidence and never become clauses. That list is pinned by
  ``engine/tests/test_schematic.py``'s ERC test and must stay short;
* an ERC type is followed by what it means *in the IR*, because KiCad's
  wording ("add a PWR_FLAG") names a fix the model cannot express;
* no ``kicad-cli`` is :attr:`Verdict.unverified` naming the install, never
  ``ok``.

:func:`erc_from_spec` emits the schematic for a validated spec into a
temporary directory first, so the proposer's repair loop can ask KiCad about
a circuit before anything is placed.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..netlist import CircuitSpec
from .verdict import Clause, Verdict

__all__ = [
    "erc",
    "drc",
    "parity",
    "erc_from_spec",
    "kicad_cli_path",
    "fill_zones",
    "BENIGN_TYPES",
]

#: Report types the emitter produces by design: every symbol and footprint is
#: embedded (``lib_symbols`` in the sheet, ``footprint`` blocks in the board),
#: so KiCad notes that the library they claim is not installed. Anything else
#: is reported.
BENIGN_TYPES = frozenset(
    {
        "lib_symbol_issues",
        "footprint_link_issues",
        "lib_footprint_issues",
        "lib_footprint_mismatch",
    }
)

#: What an ERC type means for the circuit IR, appended to KiCad's own text.
_IR_MEANING = {
    "pin_not_connected": (
        "the pin is on no net and not under no_connect: wire it, or declare it"
    ),
    "power_pin_not_driven": (
        "a power_in pin sits on a net nothing sources: connect the pin to the "
        "rail that feeds it, or the rail to its source (a regulator output, a "
        "connector's supply pin)"
    ),
    "pin_to_pin": "two pins that must not share a net do (an output on an output, "
    "a power output on a power output)",
    "no_connect_connected": "a pin declared no_connect is also on a net; remove one",
    "no_connect_dangling": "a no_connect flag sits on nothing",
    "unconnected_wire_endpoint": "a wire ends on nothing",
    "label_dangling": "a net label is attached to nothing",
    "net_not_bus_member": "a net name does not belong to the bus it joins",
    "different_unit_net": "two units of one part disagree about a net",
}

_REF_RE = re.compile(r"\b([A-Z]{1,3}\d+)\b")
_TIMEOUT_S = 120


def kicad_cli_path() -> str | None:
    """The ``kicad-cli`` to run, or None (``SILKSCREEN_KICAD_CLI``, PATH, installs)."""
    from ..enclosure.assembly import kicad_cli

    return kicad_cli()


def _report(cli: str, args: list[str], report: Path) -> dict[str, Any] | str:
    """Run one ``kicad-cli`` check; the parsed JSON, or the reason it failed."""
    try:
        proc = subprocess.run(
            [cli, *args, "--format", "json", "-o", str(report)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"kicad-cli did not run: {exc}"
    if not report.exists():
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return f"kicad-cli wrote no report (exit {proc.returncode}): {' | '.join(tail)}"
    try:
        return json.loads(report.read_text(encoding="utf-8"))
    except ValueError as exc:
        return f"kicad-cli report is not JSON: {exc}"


def _clauses(check: str, violations: list[dict[str, Any]], evidence: dict[str, Any]):
    clauses: list[Clause] = []
    benign: dict[str, int] = evidence.setdefault("benign", {})
    for v in violations:
        vtype = str(v.get("type", "unknown"))
        if vtype in BENIGN_TYPES:
            benign[vtype] = benign.get(vtype, 0) + 1
            continue
        items = [str(i.get("description", "")) for i in v.get("items", [])]
        refs = tuple(sorted({m for i in items for m in _REF_RE.findall(i)}))
        meaning = _IR_MEANING.get(vtype)
        detail = f"{v.get('description', vtype)}: {'; '.join(items) or 'no item'}"
        if meaning:
            detail += f" -- {meaning}"
        clauses.append(
            Clause(
                f"{check}.{vtype}",
                False,
                detail,
                severity="blocker" if v.get("severity") == "error" else "warning",
                refs=refs,
            )
        )
    return clauses


def erc(sch_path: str | Path) -> Verdict:
    """KiCad ERC on a ``.kicad_sch``."""
    cli = kicad_cli_path()
    if cli is None:
        return Verdict.unverified(
            "erc", "kicad-cli not found: install KiCad or set SILKSCREEN_KICAD_CLI"
        )
    sch = Path(sch_path)
    with tempfile.TemporaryDirectory(prefix="erc_") as tmp:
        report = _report(
            cli, ["sch", "erc", "--severity-all", str(sch)], Path(tmp) / "erc.json"
        )
    if isinstance(report, str):
        return Verdict.unverified("erc", report)
    evidence: dict[str, Any] = {"cli": cli, "file": str(sch)}
    violations = [v for s in report.get("sheets", []) for v in s.get("violations", [])]
    clauses = _clauses("erc", violations, evidence)
    if not clauses:
        notes = sum(evidence.get("benign", {}).values())
        clauses = [
            Clause("erc", True, f"ERC clean beyond {notes} embedded-library note(s)")
        ]
    return Verdict("erc", tuple(clauses), evidence)


def _without_stray_prl(pcb_path: str | Path):
    """KiCad drops a ``.kicad_prl`` (local UI state) beside any board it
    touches with ``--refill-zones``; a caller who asked for a check gets no
    new file. Returns a callable that removes the one KiCad created."""
    prl = Path(pcb_path).with_suffix(".kicad_prl")
    had = prl.exists()

    def cleanup() -> None:
        if not had and prl.exists():
            prl.unlink()

    return cleanup


def _drc_report(pcb_path: str | Path, *, with_parity: bool):
    cli = kicad_cli_path()
    if cli is None:
        return None, "kicad-cli not found: install KiCad or set SILKSCREEN_KICAD_CLI"
    # ``--refill-zones`` computes the copper pours before checking, without
    # saving, so a ground carried by a zone counts as connected here exactly
    # as it does in KiCad.
    args = ["pcb", "drc", "--severity-all", "--refill-zones"]
    if with_parity:
        args.append("--schematic-parity")
    args.append(str(pcb_path))
    cleanup = _without_stray_prl(pcb_path)
    with tempfile.TemporaryDirectory(prefix="drc_") as tmp:
        report = _report(cli, args, Path(tmp) / "drc.json")
    cleanup()
    if isinstance(report, str):
        return None, report
    return report, cli


def drc(pcb_path: str | Path) -> Verdict:
    """KiCad DRC on a ``.kicad_pcb``: design-rule violations and unconnected items."""
    report, cli = _drc_report(pcb_path, with_parity=False)
    if report is None:
        return Verdict.unverified("drc", cli)
    evidence: dict[str, Any] = {"cli": cli, "file": str(pcb_path)}
    clauses = _clauses("drc", report.get("violations", []), evidence)
    unconnected = report.get("unconnected_items", [])
    if unconnected:
        items = [
            str(i.get("description", ""))
            for u in unconnected
            for i in u.get("items", [])
        ]
        clauses.append(
            Clause(
                "drc.unconnected_items",
                False,
                f"{len(unconnected)} unconnected item(s): {'; '.join(items)[:400]}",
                refs=tuple(sorted({m for i in items for m in _REF_RE.findall(i)})),
            )
        )
    if not clauses:
        clauses = [Clause("drc", True, "DRC clean, nothing unconnected")]
    return Verdict("drc", tuple(clauses), evidence)


def parity(pcb_path: str | Path) -> Verdict:
    """Schematic-to-board parity: the two files describe one circuit."""
    report, cli = _drc_report(pcb_path, with_parity=True)
    if report is None:
        return Verdict.unverified("parity", cli)
    evidence: dict[str, Any] = {"cli": cli, "file": str(pcb_path)}
    clauses = _clauses("parity", report.get("schematic_parity", []), evidence)
    if not clauses:
        clauses = [Clause("parity", True, "schematic and board agree")]
    return Verdict("parity", tuple(clauses), evidence)


def erc_from_spec(spec: CircuitSpec, *, project_name: str = "verify") -> Verdict:
    """Draw the schematic for ``spec`` in a temporary directory and run ERC on it.

    Refuses in words without ``kicad-cli`` before drawing anything. A spec the
    emitter cannot draw is an ``unverified`` verdict naming the exception --
    it is not KiCad's opinion of the circuit.
    """
    if kicad_cli_path() is None:
        return Verdict.unverified(
            "erc", "kicad-cli not found: install KiCad or set SILKSCREEN_KICAD_CLI"
        )
    from ..schematic import build_schematic, write_project, write_schematic

    with tempfile.TemporaryDirectory(prefix="erc_spec_") as tmp:
        sch = Path(tmp) / f"{project_name}.kicad_sch"
        try:
            write_schematic(build_schematic(spec), sch, project_name=project_name)
            write_project(sch.with_suffix(".kicad_pro"), spec=spec)
        except Exception as exc:  # the emitter's failure, not KiCad's verdict
            return Verdict.unverified("erc", f"could not draw the schematic: {exc}")
        return erc(sch)


def fill_zones(pcb_path: str | Path) -> str | None:
    """Ask KiCad to fill every zone in ``pcb_path`` and save it in place.

    ``kicad-cli pcb drc --refill-zones --save-board`` is the one CLI path
    that computes fill polygons (KiCad 8+); the DRC report it also writes is
    discarded here. **Not used by ``write_board``**: KiCad saves the file in
    its own format version, which ``kiutils`` and the bridge cannot read
    (2026-09-15). Kept for a caller who wants a filled file for a viewer.
    Returns ``None`` on success, otherwise the reason in words. Never raises.
    """
    cli = kicad_cli_path()
    if cli is None:
        return "kicad-cli not found"
    cleanup = _without_stray_prl(pcb_path)
    with tempfile.TemporaryDirectory(prefix="fill_") as tmp:
        report = Path(tmp) / "drc.json"
        try:
            proc = subprocess.run(
                [
                    cli,
                    "pcb",
                    "drc",
                    "--refill-zones",
                    "--save-board",
                    "--format",
                    "json",
                    "-o",
                    str(report),
                    str(pcb_path),
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"kicad-cli did not run: {exc}"
    cleanup()
    if proc.returncode not in (
        0,
        5,
    ):  # 5 is "violations found" with --exit-code-violations
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-2:]
        return f"kicad-cli exited {proc.returncode}: {' | '.join(tail)}"
    return None
