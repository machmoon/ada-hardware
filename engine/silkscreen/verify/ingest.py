"""Outside signals as verdicts: compilers, logs, cloud, SPICE, CAD kernels.

The harness trusts a :class:`~silkscreen.verify.Verdict` and nothing else, so
every tool that can say "this is wrong" gets one adapter here and then works
as an ``Agent.required_verifiers`` gate with no loop changes.

The shape is reviewdog's (MIT, github.com/reviewdog/reviewdog):

* :class:`Diagnostic` is ``proto/rdf/reviewdog.proto``'s ``Diagnostic``
  reduced to what a repair prompt uses (message, location, severity, source,
  code).
* :func:`severity` is ``parser/parser.go::severity`` verbatim: note counts as
  info, anything unrecognised is ``unknown``.
* :func:`from_sarif` follows ``parser/sarif.go``: the level comes from the
  result, else the rule's ``defaultConfiguration``; a result with a
  suppression whose status is missing or ``accepted`` is skipped.
* :func:`from_rdjsonl` reads reviewdog's own line format, so any of the
  hundreds of linters reviewdog already wraps can feed the loop through
  ``reviewdog -f=<tool> -reporter=... `` or its ``-diff`` output.

Deviation from reviewdog, stated: an ``unknown`` severity becomes a
*warning* clause, not a dropped line, because a quiet drop is how an agent
concludes a failing build is fine. For the same reason a command that exits
non-zero with no parsed error still fails, on an ``exit_status`` clause, and
a tool that cannot run is ``unverified``, never ``ok``.

Compiler text uses the GNU error format every gcc and clang release prints
(``file:line:col: error: message``), the pattern Vim's default
``errorformat`` and reviewdog's ``%f:%l:%c: %t%*[^:]: %m`` read. Prefer SARIF
where the compiler offers it (``gcc -fdiagnostics-format=sarif-stderr``,
``clang -fdiagnostics-format=sarif``).

Logs are the JSON lines ``service/logs.py`` writes (Powertools key names), and
CloudWatch's ``aws logs filter-log-events`` JSON wraps the same lines, so one
reader covers local runs and the deployed service.

The SPICE and kernel adapters are duck-typed: this package imports nothing
from ``agents`` (the engine stays model-free), the ``specreview.evidence_block``
convention.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .verdict import Clause, Verdict

__all__ = [
    "Diagnostic",
    "severity",
    "from_sarif",
    "from_gnu",
    "from_rdjsonl",
    "from_log_lines",
    "from_cloudwatch",
    "diagnostics_verdict",
    "run_check",
    "cloud_logs_check",
    "from_simulation",
    "from_kernel",
    "from_mechanism",
]

Level = Literal["error", "warning", "info", "unknown"]

#: A cap on clauses per verdict: a broken header can emit thousands of
#: errors, and a repair prompt with thousands of items is not read. The
#: count past the cap is kept in ``evidence``.
MAX_CLAUSES = 50
_TIMEOUT_S = 600


@dataclass(frozen=True)
class Diagnostic:
    """One finding from an outside tool (reviewdog's ``rdf.Diagnostic``)."""

    message: str
    severity: Level = "unknown"
    path: str = ""
    line: int = 0
    column: int = 0
    source: str = ""
    code: str = ""

    def where(self) -> str:
        if not self.path:
            return ""
        pos = f":{self.line}" if self.line else ""
        pos += f":{self.column}" if self.line and self.column else ""
        return f"{self.path}{pos}"


def severity(s: str) -> Level:
    """reviewdog's mapping (``parser/parser.go``); note is info."""
    if s in ("error", "ERROR", "Error", "e", "E"):
        return "error"
    if s in ("warning", "WARNING", "Warning", "w", "W"):
        return "warning"
    if s in ("info", "INFO", "Info", "i", "I", "note", "NOTE", "Note", "n", "N"):
        return "info"
    return "unknown"


# ---------------------------------------------------------------- parsers


def _sarif_text(msg: Mapping[str, Any] | None) -> str:
    if not msg:
        return ""
    # reviewdog prefers markdown when both are present.
    return str(msg.get("markdown") or msg.get("text") or "")


def _suppressed(result: Mapping[str, Any]) -> bool:
    for s in result.get("suppressions") or ():
        status = s.get("status")
        if status is None or status == "accepted":
            return True
    return False


def from_sarif(doc: Mapping[str, Any] | str) -> list[Diagnostic]:
    """Every unsuppressed result of a SARIF 2.1.0 log, one per location."""
    if isinstance(doc, str):
        doc = json.loads(doc)
    out: list[Diagnostic] = []
    for run in doc.get("runs") or ():
        driver = (run.get("tool") or {}).get("driver") or {}
        name = str(driver.get("name", ""))
        rules = {r.get("id"): r for r in driver.get("rules") or ()}
        for result in run.get("results") or ():
            if _suppressed(result):
                continue
            rule_id = str(result.get("ruleId") or "")
            level = result.get("level")
            if level is None:
                level = (
                    (rules.get(rule_id) or {}).get("defaultConfiguration") or {}
                ).get("level")
            sev = severity(str(level)) if level else "unknown"
            msg = _sarif_text(result.get("message"))
            locations = result.get("locations") or [{}]
            for loc in locations:
                phys = loc.get("physicalLocation") or {}
                art = phys.get("artifactLocation") or {}
                region = phys.get("region") or {}
                out.append(
                    Diagnostic(
                        message=msg,
                        severity=sev,
                        path=str(art.get("uri", "")),
                        line=int(region.get("startLine", 0) or 0),
                        column=int(region.get("startColumn", 0) or 0),
                        source=name,
                        code=rule_id,
                    )
                )
    return out


# file:line[:col]: [fatal ]error|warning|note: message  (gcc, clang, collect2)
_GNU_RE = re.compile(
    r"^(?P<path>[^:\s][^:]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?:fatal\s+)?(?P<kind>error|warning|note)\s*:\s*(?P<msg>.*)$"
)
# Errors with no location: "ld: symbol(s) not found", "clang: error: ..."
_GNU_BARE_RE = re.compile(
    r"^(?P<src>[\w.+-]+):\s*(?:fatal\s+)?(?P<kind>error)\s*:\s*(?P<msg>.*)$"
)


def from_gnu(text: str, *, source: str = "cc") -> list[Diagnostic]:
    """gcc/clang/ld diagnostics in the GNU ``file:line:col: kind: msg`` form."""
    out: list[Diagnostic] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _GNU_RE.match(line)
        if m:
            out.append(
                Diagnostic(
                    message=m["msg"],
                    severity=severity(m["kind"]),
                    path=m["path"],
                    line=int(m["line"]),
                    column=int(m["col"] or 0),
                    source=source,
                )
            )
            continue
        b = _GNU_BARE_RE.match(line)
        if b:
            out.append(Diagnostic(message=b["msg"], severity="error", source=b["src"]))
    return out


def from_rdjsonl(text: str) -> list[Diagnostic]:
    """reviewdog's rdjsonl: one ``rdf.Diagnostic`` JSON object per line."""
    out: list[Diagnostic] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        d = json.loads(raw)
        loc = d.get("location") or {}
        start = ((loc.get("range") or {}).get("start")) or {}
        out.append(
            Diagnostic(
                message=str(d.get("message", "")),
                severity=severity(str(d.get("severity", ""))),
                path=str(loc.get("path", "")),
                line=int(start.get("line", 0) or 0),
                column=int(start.get("column", 0) or 0),
                source=str((d.get("source") or {}).get("name", "")),
                code=str((d.get("code") or {}).get("value", "")),
            )
        )
    return out


_LOG_LEVELS: dict[str, Level] = {
    "CRITICAL": "error",
    "FATAL": "error",
    "ERROR": "error",
    "WARNING": "warning",
    "WARN": "warning",
}


def _log_diag(record: Mapping[str, Any], source: str) -> Diagnostic | None:
    level = _LOG_LEVELS.get(str(record.get("level", "")).upper())
    if level is None:
        return None
    msg = str(record.get("message", ""))
    for key in ("error_id", "request_id", "run_id"):
        if record.get(key):
            msg += f" ({key}={record[key]})"
    return Diagnostic(
        message=msg,
        severity=level,
        source=str(record.get("service") or source),
        code=str(record.get("error_type") or record.get("event") or ""),
    )


def from_log_lines(
    lines: str | Iterable[str], *, source: str = "logs"
) -> tuple[list[Diagnostic], int]:
    """ERROR and WARNING records from JSON-lines logs.

    Returns the diagnostics and the number of lines that were not JSON, so a
    verdict can say it skipped them instead of reading them as clean.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    out: list[Diagnostic] = []
    skipped = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue
        d = _log_diag(record, source)
        if d is not None:
            out.append(d)
    return out, skipped


def from_cloudwatch(
    doc: Mapping[str, Any] | str, *, source: str = "cloudwatch"
) -> tuple[list[Diagnostic], int]:
    """``aws logs filter-log-events`` output; each event's message is a log line."""
    if isinstance(doc, str):
        doc = json.loads(doc)
    return from_log_lines(
        (str(e.get("message", "")) for e in doc.get("events") or ()), source=source
    )


# ---------------------------------------------------------------- verdicts


def _clause(d: Diagnostic) -> Clause:
    where = d.where()
    name = ".".join(p for p in (d.source or "diag", d.code or d.severity) if p)
    detail = f"{where}: {d.message}" if where else d.message
    return Clause(
        name,
        False,
        detail,
        severity="blocker" if d.severity == "error" else "warning",
    )


def diagnostics_verdict(
    verifier: str,
    diagnostics: Sequence[Diagnostic],
    *,
    exit_status: int | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> Verdict:
    """Errors fail as blockers, warnings and unknowns ride as warnings.

    ``info`` is counted, not listed. A non-zero ``exit_status`` with no parsed
    error is itself a blocker: the tool failed in a way the parser did not
    understand, and that must not read as green.
    """
    ev: dict[str, Any] = dict(evidence or {})
    counts = {"error": 0, "warning": 0, "info": 0, "unknown": 0}
    listed: list[Diagnostic] = []
    for d in diagnostics:
        counts[d.severity] += 1
        if d.severity != "info":
            listed.append(d)
    # errors first, so the cap never hides one behind a warning
    listed.sort(key=lambda d: d.severity != "error")
    ev["counts"] = counts
    if len(listed) > MAX_CLAUSES:
        ev["not_listed"] = len(listed) - MAX_CLAUSES
        listed = listed[:MAX_CLAUSES]
    clauses = [_clause(d) for d in listed]
    if exit_status is not None:
        ev["exit_status"] = exit_status
        if exit_status != 0 and counts["error"] == 0:
            clauses.insert(
                0,
                Clause(
                    f"{verifier}.exit_status",
                    False,
                    f"exited {exit_status} without a parseable error; "
                    "see evidence.output_tail",
                ),
            )
    if not clauses:
        clauses.append(
            Clause(f"{verifier}.clean", True, "no errors or warnings reported")
        )
    return Verdict(verifier, tuple(clauses), ev)


Parser = Callable[[str], list[Diagnostic]]


def run_check(
    verifier: str,
    argv: Sequence[str],
    parse: Parser | str = "gnu",
    *,
    cwd: str | Path | None = None,
    timeout_s: float = _TIMEOUT_S,
    stream: Literal["both", "stdout", "stderr"] = "both",
) -> Verdict:
    """Run a build or check command and turn what it printed into a verdict.

    ``parse`` is a callable or one of ``gnu``, ``sarif``, ``rdjsonl``.
    A missing executable or a timeout is ``unverified`` and says which.
    """
    parsers: dict[str, Parser] = {
        "gnu": lambda t: from_gnu(t, source=Path(argv[0]).name),
        "sarif": _sarif_stream,
        "rdjsonl": from_rdjsonl,
    }
    fn = parsers[parse] if isinstance(parse, str) else parse
    exe = argv[0]
    if shutil.which(exe) is None and not Path(exe).exists():
        return Verdict(
            verifier, unverified_reason=f"{exe} is not installed or not on PATH"
        )
    try:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Verdict(
            verifier, unverified_reason=f"{exe} did not finish in {timeout_s:.0f} s"
        )
    except OSError as exc:
        return Verdict(verifier, unverified_reason=f"{exe} did not run: {exc}")
    text = {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "both": f"{proc.stdout}\n{proc.stderr}",
    }[stream]
    try:
        diags = fn(text)
    except ValueError as exc:
        diags = [
            Diagnostic(f"output did not parse: {exc}", "error", source=Path(exe).name)
        ]
    tail = [ln for ln in text.strip().splitlines() if ln.strip()][-5:]
    return diagnostics_verdict(
        verifier,
        diags,
        exit_status=proc.returncode,
        evidence={"command": list(argv), "output_tail": tail},
    )


def cloud_logs_check(
    log_group: str,
    *,
    since_minutes: int = 60,
    now_ms: int | None = None,
    aws: str = "aws",
    verifier: str = "cloud_logs",
) -> Verdict:
    """ERROR and WARNING lines a CloudWatch log group logged recently.

    Runs ``aws logs filter-log-events`` (the console's Logs Insights view as a
    command) and reads each event as a ``service/logs.py`` line. No AWS CLI or
    no credentials is ``unverified`` or a red ``exit_status``, never clean.
    """
    import time

    now = int(time.time() * 1000) if now_ms is None else now_ms
    argv = [
        aws,
        "logs",
        "filter-log-events",
        "--log-group-name",
        log_group,
        "--start-time",
        str(now - since_minutes * 60_000),
        "--filter-pattern",
        "?ERROR ?WARNING ?CRITICAL",
        "--output",
        "json",
    ]

    def parse(text: str) -> list[Diagnostic]:
        return from_cloudwatch(text)[0]

    return run_check(verifier, argv, parse, stream="stdout", timeout_s=60)


def _sarif_stream(text: str) -> list[Diagnostic]:
    """SARIF from a stream that may carry other lines around the JSON."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no SARIF object in output")
    return from_sarif(text[start : end + 1])


# ---------------------------------------------------------------- engine stages


def from_simulation(result: Any, *, verifier: str = "spice") -> Verdict:
    """``agents.simulate.SimulationResult`` as a verdict.

    Only ``ran`` is a verdict; every other status is ``unverified`` with the
    stage's own sentence, because no simulation is not a passing one.
    """
    status = getattr(result, "status", None)
    if result is None or status != "ran":
        reason = getattr(result, "detail", "") or f"simulation status {status!r}"
        return Verdict(verifier, unverified_reason=reason)
    clauses = []
    for c in result.clauses:
        detail = (
            c.line() if hasattr(c, "line") else str(getattr(c, "description", c.name))
        )
        clauses.append(
            Clause(
                f"{verifier}.{c.name}",
                bool(c.passed),
                detail,
                severity="blocker" if getattr(c, "critical", False) else "warning",
                margin=getattr(c, "margin", None),
            )
        )
    return Verdict(
        verifier, tuple(clauses), {"warnings": list(getattr(result, "warnings", []))}
    )


def from_kernel(report: Any, *, verifier: str = "enclosure") -> Verdict:
    """``enclosure.kernel.KernelReport``: every clause a blocker, margin in mm."""
    if report is None:
        return Verdict(verifier, unverified_reason="the enclosure kernel did not run")
    clauses = tuple(
        Clause(
            f"{verifier}.{c.name}",
            bool(c.passed),
            f"{c.detail} (margin {c.margin_nm / 1e6:+.3f} mm)",
            margin=c.margin_nm / 1e6,
        )
        for c in report.clauses
    )
    return Verdict(
        verifier, clauses, {"warnings": list(getattr(report, "warnings", ()))}
    )


def from_mechanism(report: Any, *, verifier: str = "mechanism") -> Verdict:
    """``mechanism.kernel.MechanismReport`` (margins in millionths of ``unit``)."""
    if report is None:
        return Verdict(verifier, unverified_reason="the mechanism kernel did not run")
    clauses = tuple(
        Clause(
            f"{verifier}.{c.name}",
            bool(c.passed),
            f"{c.detail} (margin {c.margin / 1e6:+g} {c.unit})",
            margin=c.margin / 1e6,
        )
        for c in report.clauses
    )
    return Verdict(
        verifier, clauses, {"warnings": list(getattr(report, "warnings", ()))}
    )
