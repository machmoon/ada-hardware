"""Outside signals as verdicts (``verify/ingest.py``).

Fixtures are the formats the real tools print: GNU diagnostics as gcc and
clang write them, a SARIF 2.1.0 log shaped like clang's, reviewdog rdjsonl,
``service/logs.py`` JSON lines and ``aws logs filter-log-events`` output. The
compiler tests at the bottom run the machine's C compiler and skip without
one, the ``needs_kicad`` convention.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from silkscreen.agents.harness import (
    Agent,
    Runner,
    ScriptedToolModel,
    Turn,
    command_verifier,
    context_verifier,
)
from silkscreen.verify import ingest
from silkscreen.verify.ingest import Diagnostic

GCC_OUT = """\
main.c: In function 'main':
main.c:3:5: error: 'x' undeclared (first use in this function)
    3 |     x = 1;
      |     ^
main.c:4:12: warning: unused variable 'y' [-Wunused-variable]
main.c:3:5: note: each undeclared identifier is reported only once
ld: symbol(s) not found for architecture arm64
clang: error: linker command failed with exit code 1 (use -v to see invocation)
"""

SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {
                "driver": {
                    "name": "clang",
                    "rules": [
                        {"id": "unused", "defaultConfiguration": {"level": "warning"}}
                    ],
                }
            },
            "results": [
                {
                    "ruleId": "undeclared",
                    "level": "error",
                    "message": {"text": "use of undeclared identifier 'x'"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "file:///tmp/main.c"},
                                "region": {"startLine": 3, "startColumn": 5},
                            }
                        }
                    ],
                },
                {"ruleId": "unused", "message": {"text": "unused variable 'y'"}},
                {
                    "ruleId": "undeclared",
                    "level": "error",
                    "message": {"text": "silenced"},
                    "suppressions": [{"kind": "inSource"}],
                },
            ],
        }
    ],
}


def test_severity_follows_reviewdog():
    assert ingest.severity("E") == "error"
    assert ingest.severity("Warning") == "warning"
    assert ingest.severity("note") == "info"
    assert ingest.severity("fatal") == "unknown"


def test_gnu_text_keeps_location_and_kind():
    diags = ingest.from_gnu(GCC_OUT, source="gcc")
    kinds = [(d.severity, d.path, d.line, d.column) for d in diags]
    assert ("error", "main.c", 3, 5) in kinds
    assert ("warning", "main.c", 4, 12) in kinds
    assert ("info", "main.c", 3, 5) in kinds
    # the located-nowhere linker failure is still an error
    bare = [d for d in diags if not d.path]
    assert bare and all(d.severity == "error" for d in bare)
    assert any("linker command failed" in d.message for d in bare)


def test_sarif_uses_rule_default_and_skips_suppressed():
    diags = ingest.from_sarif(SARIF)
    assert [(d.severity, d.code) for d in diags] == [
        ("error", "undeclared"),
        ("warning", "unused"),
    ]
    assert diags[0].line == 3 and diags[0].source == "clang"
    assert all(d.message != "silenced" for d in diags)


def test_rdjsonl_round_trips_reviewdog_lines():
    line = json.dumps(
        {
            "message": "shellcheck SC2086",
            "severity": "WARNING",
            "location": {
                "path": "run.sh",
                "range": {"start": {"line": 7, "column": 1}},
            },
            "source": {"name": "shellcheck"},
            "code": {"value": "SC2086"},
        }
    )
    (d,) = ingest.from_rdjsonl(line + "\n")
    assert (d.severity, d.path, d.line, d.source, d.code) == (
        "warning",
        "run.sh",
        7,
        "shellcheck",
        "SC2086",
    )


def test_log_lines_keep_errors_and_count_what_they_skipped():
    lines = [
        json.dumps({"level": "INFO", "message": "request done", "service": "ada"}),
        json.dumps(
            {
                "level": "ERROR",
                "message": "generate failed",
                "error_id": "e-1",
                "service": "ada",
            }
        ),
        json.dumps({"level": "WARNING", "message": "slow model", "service": "ada"}),
        "Traceback (most recent call last):",
    ]
    diags, skipped = ingest.from_log_lines(lines)
    assert [d.severity for d in diags] == ["error", "warning"]
    assert "error_id=e-1" in diags[0].message
    assert skipped == 1
    verdict = ingest.diagnostics_verdict(
        "service_logs", diags, evidence={"skipped_lines": skipped}
    )
    assert verdict.status == "blocked"
    assert verdict.evidence["skipped_lines"] == 1


def test_cloudwatch_events_wrap_the_same_lines():
    doc = {
        "events": [
            {
                "message": json.dumps(
                    {"level": "ERROR", "message": "boom", "service": "ada"}
                )
            },
            {"message": "START RequestId: 123"},
        ]
    }
    diags, skipped = ingest.from_cloudwatch(json.dumps(doc))
    assert len(diags) == 1 and diags[0].source == "ada"
    assert skipped == 1


def test_verdict_errors_block_warnings_ride_and_info_is_counted():
    diags = [
        Diagnostic("w", "warning", "a.c", 1),
        Diagnostic("e", "error", "a.c", 2),
        Diagnostic("n", "info", "a.c", 3),
        Diagnostic("?", "unknown"),
    ]
    v = ingest.diagnostics_verdict("cc", diags)
    assert v.status == "blocked"
    assert v.failures[0].detail == "a.c:2: e"  # errors first
    assert [c.severity for c in v.clauses].count("warning") == 2
    assert v.evidence["counts"] == {"error": 1, "warning": 1, "info": 1, "unknown": 1}


def test_warnings_alone_are_ok_and_a_clean_run_says_so():
    assert ingest.diagnostics_verdict("cc", [Diagnostic("w", "warning")]).ok
    clean = ingest.diagnostics_verdict("cc", [])
    assert clean.ok and clean.clauses[0].detail == "no errors or warnings reported"


def test_nonzero_exit_without_a_parsed_error_is_red():
    v = ingest.diagnostics_verdict("cc", [], exit_status=2)
    assert v.status == "blocked"
    assert v.clauses[0].name == "cc.exit_status"


def test_the_clause_cap_never_hides_an_error():
    many = [Diagnostic(f"w{i}", "warning") for i in range(ingest.MAX_CLAUSES + 5)]
    many.append(Diagnostic("the one error", "error"))
    v = ingest.diagnostics_verdict("cc", many)
    assert len(v.clauses) == ingest.MAX_CLAUSES
    assert v.clauses[0].detail == "the one error"
    assert v.evidence["not_listed"] == 6


def test_a_missing_tool_is_unverified():
    v = ingest.run_check("build", ["definitely-not-a-compiler-xyz", "a.c"])
    assert v.status == "unverified"
    assert "not installed" in v.unverified_reason


def test_simulation_only_ran_is_a_verdict():
    assert ingest.from_simulation(None).status == "unverified"
    skipped = SimpleNamespace(status="unavailable", detail="install ngspice")
    assert ingest.from_simulation(skipped).unverified_reason == "install ngspice"
    ran = SimpleNamespace(
        status="ran",
        warnings=[],
        clauses=[
            SimpleNamespace(
                name="rise",
                passed=False,
                critical=True,
                margin=-0.2,
                line=lambda: "FAIL rise",
            ),
            SimpleNamespace(
                name="ripple",
                passed=False,
                critical=False,
                margin=-1.0,
                line=lambda: "FAIL ripple",
            ),
        ],
    )
    v = ingest.from_simulation(ran)
    assert v.status == "blocked"
    assert [c.severity for c in v.clauses] == ["blocker", "warning"]
    assert v.clauses[0].margin == -0.2


def test_kernel_and_mechanism_margins_become_millimetres():
    kernel = SimpleNamespace(
        clauses=[
            SimpleNamespace(
                name="headroom", passed=False, margin_nm=-250_000, detail="lid hits C1"
            )
        ],
        warnings=(),
    )
    v = ingest.from_kernel(kernel)
    assert v.status == "blocked" and v.clauses[0].margin == pytest.approx(-0.25)
    mech = SimpleNamespace(
        clauses=[
            SimpleNamespace(
                name="reach", passed=True, margin=3_000_000, unit="mm", detail="ok"
            )
        ],
        warnings=(),
    )
    assert ingest.from_mechanism(mech).ok
    assert ingest.from_kernel(None).status == "unverified"


def test_context_verifier_gates_on_a_stage_result():
    tool = context_verifier("case", "enclosure kernel", "kernel", ingest.from_kernel)
    assert tool({}).status == "unverified"
    ok = SimpleNamespace(
        clauses=[
            SimpleNamespace(
                name="wall", passed=True, margin_nm=1_000, detail="2 mm wall"
            )
        ],
        warnings=(),
    )
    verdict = tool({"kernel": ok})
    assert verdict.ok and verdict.verifier == "case"


def test_cloud_logs_check_reads_the_cli_output(tmp_path: Path):
    events = {
        "events": [
            {
                "message": json.dumps(
                    {"level": "ERROR", "message": "5xx", "service": "ada"}
                )
            }
        ]
    }
    fake = tmp_path / "aws"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(events)}\nEOF\n")
    fake.chmod(0o755)
    v = ingest.cloud_logs_check("/ecs/ada", aws=str(fake), now_ms=10_000_000)
    assert v.status == "blocked"
    assert "5xx" in v.failures[0].detail
    assert "--log-group-name" in v.evidence["command"]


def test_cloud_logs_check_without_the_cli_is_unverified():
    v = ingest.cloud_logs_check("/ecs/ada", aws="no-such-aws-cli-xyz")
    assert v.status == "unverified"


# ---- the real compiler -----------------------------------------------------

CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
needs_cc = pytest.mark.skipif(CC is None, reason="no C compiler on PATH")


@needs_cc
def test_a_real_compile_error_blocks_with_its_line(tmp_path: Path):
    src = tmp_path / "bad.c"
    src.write_text("int main(void) {\n    return x;\n}\n")
    v = ingest.run_check("cc", [CC, "-c", str(src), "-o", str(tmp_path / "bad.o")])
    assert v.status == "blocked"
    err = v.failures[0]
    assert err.severity == "blocker"
    assert f"{src}:2:" in err.detail


@needs_cc
def test_a_real_clean_compile_is_ok(tmp_path: Path):
    src = tmp_path / "ok.c"
    src.write_text("int main(void) { return 0; }\n")
    v = ingest.run_check("cc", [CC, "-c", str(src), "-o", str(tmp_path / "ok.o")])
    assert v.ok, v.as_dict()
    assert v.evidence["exit_status"] == 0


def _supports_sarif() -> bool:
    if CC is None:
        return False
    probe = subprocess.run(
        [CC, "-fdiagnostics-format=sarif", "-x", "c", "-fsyntax-only", "-"],
        input="int main(void){return 0;}\n",
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


@needs_cc
def test_a_real_sarif_compile_error_blocks(tmp_path: Path):
    if not _supports_sarif():
        pytest.skip("this compiler has no -fdiagnostics-format=sarif")
    src = tmp_path / "bad.c"
    src.write_text("int main(void) {\n    return x;\n}\n")
    v = ingest.run_check(
        "cc",
        [CC, "-fdiagnostics-format=sarif", "-fsyntax-only", str(src)],
        "sarif",
        stream="stderr",
    )
    assert v.status == "blocked"
    assert any(":2" in c.detail for c in v.failures)


@needs_cc
def test_the_loop_refuses_code_that_does_not_compile(tmp_path: Path):
    """A compiler as ``required_verifiers``: red output is a repair round."""

    def argv(context):
        src = tmp_path / "agent.c"
        src.write_text(context["artifact"])
        return [CC, "-c", str(src), "-o", str(tmp_path / "agent.o")]

    agent = Agent(
        "coder",
        "MARK",
        tools=(command_verifier("cc", "compile the offered C file", argv),),
        required_verifiers=("cc",),
    )
    bad = "int main(void) { return x; }\n"
    good = "int main(void) { return 0; }\n"
    model = ScriptedToolModel({"MARK": [Turn.final(bad), Turn.final(good)]})
    events = []
    result = Runner().run(agent, "write main", model=model, on_event=events.append)
    assert result.status == "ok"
    assert result.final_output == good
    rounds = [e for e in events if e["event"] == "propose.round"]
    assert len(rounds) == 1 and "cc:" in rounds[0]["first_error"]
