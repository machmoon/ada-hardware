"""``python -m alexabot``: Ada's voice tools as one MCP endpoint.

This reuses :mod:`silkscreen.mcp.http` -- the engine's Streamable HTTP
transport, with its Origin check, its framing rules and its bearer gate --
and hands it two things through the seams it grew for this (reviewer finding
m5: the transport must not import service code): ``dispatch``, which is
:func:`silkscreen.mcp.server.handle` bound to the voice :class:`~silkscreen.
mcp.server.Toolset`, and ``verify_token``, which maps a credential to an
account (:mod:`alexabot.auth`). The same process holds the
``service/steps.py`` sessions the workers run in, which is the other half of
m5: steps sessions live in memory, so the tools must run where they are.

Startup order: read ``.env`` (setdefault, ``ADA_REPO_ROOT`` then the cwd --
the ``silkscreen.mcp.server`` rule); read and check the configuration; read
the rate limit (``MCP_TOOL_CALLS_PER_MINUTE``, process-wide; a
``poll_after_s`` of at least three keeps one session near twenty calls a
minute); in live mode, refuse to start without a model provider, naming
``--scripted``; open the board store and fail every board an earlier process
left mid-run; warm the pipeline imports; serve.

There is no stdio mode: the Alexa+ path is Streamable HTTP.

**Unverified live.** No Alexa+ agent has called this server; the only
independent clients it has met are python-sdk's.
"""

from __future__ import annotations

import functools
import os
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from silkscreen.mcp import http as mcp_http
from silkscreen.mcp import server as mcp_server

from . import auth, scripted, tools
from .config import DEFAULT_STEPS_DIR, Config, ConfigError, load_config
from .runner import Runner
from .store import BoardStore

__all__ = ["StartupError", "build_runner", "main", "make_server", "startup"]


def build_runner(config: Config, *, steps: Any = None) -> Runner:
    """A warmed :class:`Runner` over the configured store and model."""
    store = BoardStore(config.db)
    if config.scripted:
        from service.cache import MemoryFactStore

        factory = scripted.model_factory(config.scripted_delay_s)
        fact_store: Any = MemoryFactStore()
    else:
        from service.app import build_model, build_store

        # The /steps failover ladder, built per board on the worker; the fact
        # store once, here (known issue 5: a Firestore client per request).
        factory = build_model
        fact_store = build_store()
    runner = Runner(
        store,
        factory,
        fact_store,
        steps=steps,
        max_active=config.max_active,
        scripted=config.scripted,
    )
    return runner.warm()


def make_server(
    config: Config, *, runner: Runner | None = None, key_store: Any = None
) -> ThreadingHTTPServer:
    """Bind the endpoint; ``runner`` and ``key_store`` are the tests' seams."""
    runner = runner if runner is not None else build_runner(config)
    toolset = tools.toolset(runner)
    server = mcp_http.make_server(
        config.host,
        config.port,
        origins=config.origins,
        dispatch=functools.partial(mcp_server.handle, toolset=toolset),
        verify_token=auth.make_verifier(config, key_store=key_store),
    )
    server.runner = runner  # type: ignore[attr-defined]
    return server


def _load_dotenv() -> None:
    from silkscreen.cli import _load_dotenv as load

    for root in (os.environ.get("ADA_REPO_ROOT"), os.getcwd()):
        if root:
            load(Path(root) / ".env")


def _provider_problem() -> str | None:
    """Why live mode cannot run, or ``None``."""
    from silkscreen.agents.providers import NoProviderConfigured, provider_order

    try:
        provider_order()
    except NoProviderConfigured as exc:
        return str(exc)
    return None


class StartupError(RuntimeError):
    """The endpoint cannot start as configured; the message says why."""


def startup(config: Config, *, banner: str | None = None) -> Runner:
    """Everything :func:`main` does before it binds, shared with the sim.

    Rate limit, the steps-dir default, the scripted environment pair (or the
    provider check), the runner, and the sweep of boards an earlier process
    left mid-run. ``.env`` is read by each ``main``, not here. ``banner``
    goes to stderr where ``main`` always printed the scripted one.
    """
    try:
        mcp_server.configure_rate_limit()
    except ValueError as exc:
        raise StartupError(str(exc)) from None
    # Files the history names should outlive a reboot, so not the temp dir.
    os.environ.setdefault(
        "SILKSCREEN_STEPS_DIR", str(Path(DEFAULT_STEPS_DIR).expanduser())
    )
    if config.scripted:
        # The root conftest.py pair: a canned answer cannot repair itself, and
        # the session must go the same way with or without kicad-cli here.
        os.environ.setdefault("SILKSCREEN_ERC_IN_LOOP", "0")
        os.environ.setdefault("SILKSCREEN_KICAD_LIBRARY", "0")
        if banner:
            print(banner, file=sys.stderr)
    else:
        problem = _provider_problem()
        if problem:
            raise StartupError(f"{problem} Or run with --scripted.")

    runner = build_runner(config)
    swept = runner.store.fail_unfinished(time.time())
    if swept:
        print(
            f"alexabot: {swept} board(s) were mid-run when the service last "
            "stopped; they are marked failed with the reason",
            file=sys.stderr,
        )
    return runner


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    try:
        config = load_config(argv)
    except ConfigError as exc:
        print(f"alexabot: {exc}", file=sys.stderr)
        return 2
    try:
        runner = startup(config, banner=scripted.BANNER)
    except StartupError as exc:
        print(f"alexabot: {exc}", file=sys.stderr)
        return 2
    server = make_server(config, runner=runner)
    host, port = server.server_address[:2]
    auth_words = (
        "ada_ keys" if config.keys_db else
        "bearer token" if config.token else "no auth, loopback only"
    )
    print(
        f"Ada voice tools (MCP) at http://{host}:{port}{mcp_http.ENDPOINT} "
        f"({auth_words}; history in {config.db})",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runner.store.close()
    return 0
