"""The arm kernel, and the arm assembled with its electronics.

The mechanism package had no tests of its own until 2026-09-16. These build a
real three-joint arm with build123d (gated like the enclosure kernel), import
STEP files for the board and for an arm someone already has, and check every
system clause both ways: a board beside the base passes with a positive
margin, a board dropped into the arm fails with a negative one. The expected
geometry is computed here from the numbers the test itself chose, never read
back from the module under test.
"""

from __future__ import annotations

import pytest
from silkscreen.enclosure.cad import kernel_available
from silkscreen.enclosure.rules import STANDOFF_HEIGHT_NM
from silkscreen.mechanism.errors import MechanismBuildError
from silkscreen.mechanism.ir import parse_mechanism_spec

needs_build123d = pytest.mark.skipif(
    not kernel_available(), reason="build123d not installed (pip install -e '.[cad]')"
)

ARM = {
    "name": "desk arm",
    "base": {"type": "bolt_down"},
    "joints": [
        {"id": "pan", "axis": "yaw", "range_deg": [-90, 90],
         "actuator": "STS3215", "bearing": "none", "link_mm": 70},
        {"id": "shoulder", "axis": "pitch", "range_deg": [-60, 60],
         "actuator": "STS3215", "bearing": "608", "link_mm": 110},
        {"id": "elbow", "axis": "pitch", "range_deg": [-90, 90],
         "actuator": "STS3215", "bearing": "none", "link_mm": 100},
    ],
    "payload_g": 50,
    "reach_mm": 180,
    "tool": {"length_mm": 30, "mass_g": 20},
    "material": "PLA",
}

#: A stand-in board: 40 x 30 mm, 1.6 mm thick, what kicad-cli would export
#: for a bare two-layer outline. Its origin is deliberately far from the arm,
#: so a passing clause proves the assembly moved it.
BOARD_MM = (40.0, 30.0, 1.6)
BOARD_ORIGIN_MM = (500.0, -300.0, 20.0)


@pytest.fixture(scope="module")
def arm():
    from silkscreen.mechanism.cad import build_mechanism

    return build_mechanism(parse_mechanism_spec(ARM))


@pytest.fixture(scope="module")
def board_step(tmp_path_factory):
    import build123d as b

    path = tmp_path_factory.mktemp("board") / "board.step"
    x, y, z = BOARD_MM
    box = b.Pos(*BOARD_ORIGIN_MM) * b.Box(x, y, z, align=(b.Align.MIN,) * 3)
    b.export_step(box, path)
    return path


@needs_build123d
def test_the_sample_arm_builds_and_every_clause_passes(arm):
    from silkscreen.mechanism.kernel import verify_mechanism

    assert [p.name for p in arm.parts] == [
        "base", "link1_pan", "link2_shoulder", "link3_elbow",
    ]
    report = verify_mechanism(arm)
    assert report.passed, report.text()
    # 70 + 110 + 100 + 30 mm of chain; the kernel may not claim more reach
    # than the links are long.
    assert 180_000_000 <= report.reach_nm <= 310_000_000


@needs_build123d
def test_a_link_too_short_for_its_housings_is_named():
    from silkscreen.mechanism.cad import build_mechanism

    short = {**ARM, "joints": [dict(j) for j in ARM["joints"]]}
    short["joints"][1]["link_mm"] = 5
    with pytest.raises(MechanismBuildError) as caught:
        build_mechanism(parse_mechanism_spec(short))
    assert caught.value.failure_class == "LINK_TOO_SHORT"


@needs_build123d
def test_the_board_sits_beside_the_base_on_standoffs_and_clears_every_pose(
    arm, board_step
):
    from silkscreen.mechanism.system import BOARD_GAP_NM, assemble_system, verify_system

    assembly = assemble_system(arm=arm, board_step=board_step)
    bb = assembly.board_shape.bounding_box()
    arm_min_x = min(s.bounding_box().min.X for _, s in assembly.arm_parts())
    assert pytest.approx(arm_min_x - BOARD_GAP_NM / 1e6, abs=1e-3) == bb.max.X
    assert pytest.approx(STANDOFF_HEIGHT_NM / 1e6, abs=1e-3) == bb.min.Z
    assert pytest.approx(BOARD_MM[0], abs=1e-3) == bb.size.X

    report = verify_system(assembly)
    assert report.passed, report.as_dict()
    assert report.clause("board_imported").margin == 1_000_000
    home = report.clause("board_clear_home")
    sampled = report.clause("board_clear_sampled")
    assert home.margin > 0 and sampled.margin > 0
    # Sampling visits the home pose's neighbours too, so it can only be
    # tighter than or equal to home.
    assert sampled.margin <= home.margin


@needs_build123d
def test_a_board_inside_the_arm_fails_with_a_negative_margin(arm, board_step):
    from silkscreen.mechanism.system import assemble_system, verify_system

    # A negative gap pushes the board 60 mm into the base.
    assembly = assemble_system(arm=arm, board_step=board_step, gap_nm=-60_000_000)
    report = verify_system(assembly)
    home = report.clause("board_clear_home")
    assert not home.passed
    assert home.margin < 0
    assert "interfere" in home.detail
    assert "board_clear_home" in report.failed


@needs_build123d
def test_an_imported_arm_is_checked_where_it_stands_and_says_motion_is_unchecked(
    arm, board_step, tmp_path
):
    from silkscreen.mechanism.cad import export_mechanism
    from silkscreen.mechanism.system import assemble_system, verify_system

    arm_step = export_mechanism(arm, tmp_path, stem="vendor_arm").step
    assembly = assemble_system(arm_step=arm_step, board_step=board_step)
    report = verify_system(assembly)
    names = [c.name for c in report.clauses]
    assert names == ["arm_imported", "board_imported", "board_clear_home"]
    assert report.clause("arm_imported").margin == 4_000_000  # base + three links
    assert report.passed, report.as_dict()
    assert any("no joints" in w for w in report.warnings)
    assert "board_clear_sampled" not in names  # absent, never green


@needs_build123d
def test_no_board_is_reported_rather_than_passed_silently(arm):
    from silkscreen.mechanism.system import assemble_system, verify_system

    report = verify_system(assemble_system(arm=arm))
    assert report.clause("board_imported").detail.startswith("nothing to check")
    assert any("electronics are not checked" in w for w in report.warnings)


@needs_build123d
def test_an_empty_or_missing_step_is_refused(tmp_path):
    from silkscreen.mechanism.system import import_step_part

    with pytest.raises(MechanismBuildError, match="no such file"):
        import_step_part(tmp_path / "absent.step")
    junk = tmp_path / "junk.step"
    junk.write_text("not a step file\n")
    with pytest.raises(MechanismBuildError) as caught:
        import_step_part(junk)
    assert caught.value.failure_class == "IMPORT_FAILED"


def test_assemble_needs_exactly_one_arm():
    from silkscreen.mechanism.system import assemble_system

    with pytest.raises(ValueError, match="exactly one"):
        assemble_system()


@needs_build123d
def test_the_system_step_carries_every_part_and_reads_back(arm, board_step, tmp_path):
    from silkscreen.mechanism.system import (
        assemble_system,
        export_system,
        import_step_part,
    )

    path = export_system(assemble_system(arm=arm, board_step=board_step), tmp_path)
    back = import_step_part(path)
    assert back.solids == 5  # base, three links, the board


@needs_build123d
def test_the_robot_command_designs_assembles_and_exits_by_the_clauses(
    board_step, tmp_path, capsys
):
    import json

    from silkscreen.cli import main

    spec = tmp_path / "arm.json"
    spec.write_text(json.dumps(ARM))
    out = tmp_path / "robot.step"
    code = main(["robot", "--arm-spec", str(spec), "--board-step", str(board_step),
                 "-o", str(out), "--json"])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed["passed"] is True
    assert printed["arm"]["passed"] is True
    assert [c["name"] for c in printed["system"]["clauses"]] == [
        "board_imported", "board_clear_home", "board_clear_sampled",
    ]
    assert out.is_file()
    assert sorted(p.name for p in tmp_path.glob("robot-arm*")) == [
        "robot-arm-base.stl", "robot-arm-link1_pan.stl",
        "robot-arm-link2_shoulder.stl", "robot-arm-link3_elbow.stl", "robot-arm.step",
    ]


@needs_build123d
def test_the_robot_command_imports_an_arm_and_refuses_a_bad_file(
    board_step, tmp_path, capsys
):
    from silkscreen.cli import main

    junk = tmp_path / "arm.step"
    junk.write_text("nope")
    assert main(["robot", "--arm-step", str(junk), "--board-step", str(board_step),
                 "-o", str(tmp_path / "r.step")]) == 1
    assert "IMPORT_FAILED" in capsys.readouterr().err


def _kicad():
    from silkscreen.verify.kicad import kicad_cli_path

    return kicad_cli_path()


@needs_build123d
@pytest.mark.skipif(_kicad() is None, reason="kicad-cli not installed")
def test_an_arm_run_assembles_the_routed_board_it_wrote(tmp_path, capsys):
    """The ``--arm`` tail: a real routed board through kicad-cli, beside the arm."""
    import json
    from types import SimpleNamespace

    from silkscreen.board import build_board, route_board, write_board
    from silkscreen.cli import _assemble_generated_robot
    from silkscreen.netlist import parse_circuit_spec
    from test_ground_fill import REGULATOR

    board = build_board(parse_circuit_spec(json.dumps(REGULATOR)), time_limit_s=5.0)
    route_board(board)
    pcb = write_board(board, tmp_path / "reg.kicad_pcb")
    found = SimpleNamespace(spec=parse_mechanism_spec(ARM))
    _assemble_generated_robot(found, pcb)
    out = capsys.readouterr().out
    assert "PASS board_imported" in out
    assert "PASS board_clear_sampled" in out
    assert (tmp_path / "reg-robot.step").is_file()
    assert (tmp_path / "reg-board.step").is_file()


def test_an_arm_run_with_nothing_built_assembles_nothing(tmp_path, capsys):
    from types import SimpleNamespace

    from silkscreen.cli import _assemble_generated_robot

    _assemble_generated_robot(None, tmp_path / "b.kicad_pcb")
    _assemble_generated_robot(SimpleNamespace(spec=None), tmp_path / "b.kicad_pcb")
    assert capsys.readouterr().out == ""
    assert list(tmp_path.iterdir()) == []
