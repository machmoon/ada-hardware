"""Fixtures shared by both suites.

At the repository root rather than under ``engine/tests`` because
``service/tests`` needs the same thing: ``testpaths`` covers both, and pytest
only reads a conftest at or above the test it is collecting.

The only thing here is the datasheet downloader. ``read_datasheet`` fetches a
``pdf_url`` itself now -- Gemini does not fetch arbitrary URLs, so the bytes
have to travel in the request -- which means any test that hands the pipeline a
datasheet URL would otherwise reach for DNS. The suite is offline by contract,
so those tests take ``offline_pdf_fetch`` and get a real, tiny PDF back.

It is deliberately not autouse. ``test_grounding.py`` exercises the genuine
``fetch_pdf`` against a local HTTP server, and a fixture that silently replaced
the network everywhere would gut exactly the tests that are supposed to use it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from silkscreen.agents import grounding

# Off before any fixture of any scope runs: module-scoped fixtures build
# schematics before a function-scoped autouse fixture ever applies, and on a
# machine with KiCad installed they drew library connectors (see
# _kicad_library_off_by_default below). setdefault, so a run can opt in.
os.environ.setdefault("SILKSCREEN_KICAD_LIBRARY", "0")
# Same reason for KiCad ERC inside the proposer's repair loop
# (``agents.propose.ERC_IN_LOOP_ENV``): a scripted circuit that leaves a signal
# pin open must be refused identically on a laptop with kicad-cli and on a
# runner without one. ``test_verify.py`` opts back in, gated on the binary.
os.environ.setdefault("SILKSCREEN_ERC_IN_LOOP", "0")

FIXTURE_PDF = (
    Path(__file__).resolve().parent
    / "engine" / "tests" / "fixtures" / "tiny_datasheet.pdf"
)


@pytest.fixture
def offline_pdf_fetch(monkeypatch):
    """Serve ``fixtures/tiny_datasheet.pdf`` in place of any download.

    Yields the list of URLs asked for, so a test can assert what the pipeline
    tried to fetch as well as that it never left the machine.
    """
    pdf = FIXTURE_PDF.read_bytes()
    asked: list[str] = []

    def fake_fetch(url: str, **kwargs: object) -> bytes:
        asked.append(url)
        return pdf

    monkeypatch.setattr(grounding, "fetch_pdf", fake_fetch)
    return asked


@pytest.fixture(autouse=True)
def _kicad_library_off_by_default(monkeypatch):
    """Every test sees the engine's own land patterns unless it opts in.

    ``silkscreen.kicadlib`` switches proposals and boards over to KiCad's
    installed libraries when they exist. CI has no KiCad and developer machines
    do, so without this the same test would build a different board on a laptop
    than on a runner. The library tests turn it back on with
    ``monkeypatch.setenv("SILKSCREEN_KICAD_LIBRARY", "1")``.
    """
    monkeypatch.setenv("SILKSCREEN_KICAD_LIBRARY", "0")
    monkeypatch.setenv("SILKSCREEN_ERC_IN_LOOP", "0")


@pytest.fixture(autouse=True)
def _environment_restored():
    """Whatever a test writes into ``os.environ`` is gone after it.

    The CLIs load the developer's ``.env`` with ``setdefault`` (``cli.py``,
    ``audit/cli.py``), which ``monkeypatch`` never sees. Measured 2026-09-16:
    ``test_audit.py``'s CLI test left a real ``ANTHROPIC_API_KEY`` behind, and
    four model-ladder tests in ``service/`` then failed only in a full run on
    a machine with a ``.env`` -- green in isolation and on CI.
    """
    saved = dict(os.environ)
    yield
    for key in set(os.environ) - set(saved):
        del os.environ[key]
    for key, value in saved.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
