"""Fixtures for the alexabot suite. Offline: no key, no network, no KiCad."""

from __future__ import annotations

import pytest
import silkscreen.mcp.server as mcp_server
from silkscreen.mcp.server import RateLimiter

from alexabot import tools
from alexabot.runner import Runner
from alexabot.store import BoardStore
from alexabot.tests.fakes import FakeSteps


@pytest.fixture(autouse=True)
def _unlimited_tool_calls(monkeypatch):
    """A session polls faster than the default limit allows; the limiter has
    its own tests in engine/tests/test_mcp.py."""
    monkeypatch.setattr(mcp_server, "LIMITER", RateLimiter(1_000_000))


@pytest.fixture
def steps_dir(tmp_path, monkeypatch):
    """Where the real ``service.steps`` writes, and a clean registry."""
    from service import steps

    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    steps.reset_sessions()
    yield tmp_path / "steps"
    steps.reset_sessions()


@pytest.fixture
def store(tmp_path):
    board_store = BoardStore(tmp_path / "boards.sqlite3")
    yield board_store
    board_store.close()


@pytest.fixture
def fake_steps():
    return FakeSteps()


@pytest.fixture
def runner(store, fake_steps):
    built = Runner(store, lambda: object(), object(), steps=fake_steps).warm()
    yield built
    # Open every gate a test left shut, then let the workers finish before
    # the store they write to is closed.
    for gate in fake_steps.gates.values():
        gate.set()
    assert built.join(10), "a worker is still running"


@pytest.fixture
def toolset(runner):
    return tools.toolset(runner)
