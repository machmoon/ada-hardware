"""Drive a running FreeCAD from outside: the client half of HardyLive.

``desktop/freecad/HardyLive.FCMacro`` runs *inside* FreeCAD and serves one
XML-RPC method; this module is the plain ``xmlrpc.client`` caller the service
uses (``service/steps.py::_FreeCADLive``). It knows FreeCAD and the files the
engine wrote and never imports the engine, the same standing as
``kicad_live.py``. Structure per neka-nat/freecad-mcp's client
(``src/freecad_mcp/server.py``), which talks to its addon the same way.

Nothing here is verified against a running FreeCAD by the test suite: the
tests stand up a ``SimpleXMLRPCServer`` of their own on the loopback and
check what this client sends. What FreeCAD does with ``replace_shape`` is
the macro's business and is checked by eye on this Mac.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import xmlrpc.client
from pathlib import Path

DEFAULT_PORT = 9877
PORT_ENV = "HARDY_FREECAD_LIVE_PORT"
MACRO = Path(__file__).resolve().parent / "freecad" / "HardyLive.FCMacro"


class FreeCADLiveError(RuntimeError):
    """FreeCAD could not be reached or refused the call, in words."""


def port_from_env(environ: dict[str, str] | None = None) -> int:
    raw = (environ if environ is not None else os.environ).get(PORT_ENV, "")
    try:
        return int(raw) if raw.strip() else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def launch_command(app: str, macro: Path = MACRO) -> list[str]:
    """The argv that starts FreeCAD running ``macro``. On macOS the bundle
    goes through ``open -n -a … --args`` (the measured way a command-line
    file reaches ``App::Application::processFiles``, see
    ``service/steps.py::open_in_freecad``); elsewhere the binary is run
    directly with the macro as its argument."""
    if sys.platform == "darwin" and app.endswith(".app"):
        return ["/usr/bin/open", "-n", "-a", app, "--args", str(macro)]
    return [app, str(macro)]


class FreeCADLive:
    """One live FreeCAD window, addressed by port."""

    def __init__(self, port: int | None = None, *, host: str = "127.0.0.1") -> None:
        self.port = port if port is not None else port_from_env()
        self.host = host
        self._proxy = xmlrpc.client.ServerProxy(
            f"http://{host}:{self.port}/", allow_none=True
        )

    def launch(self, app: str, *, macro: Path = MACRO) -> None:
        """Start FreeCAD with the macro; never waits for it (see ``wait_ready``)."""
        if not macro.is_file():
            raise FreeCADLiveError(f"the HardyLive macro is missing: {macro}")
        argv = launch_command(app, macro)
        try:
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, PORT_ENV: str(self.port)},
            )
        except OSError as exc:
            raise FreeCADLiveError(f"FreeCAD failed to start: {exc}") from exc

    def ready(self) -> bool:
        try:
            return bool(self._proxy.ping())
        except (TimeoutError, OSError, xmlrpc.client.Error):
            return False

    def wait_ready(self, timeout_s: float = 60.0, *, poll_s: float = 0.5) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.ready():
                return
            if time.monotonic() >= deadline:
                raise FreeCADLiveError(
                    f"FreeCAD did not answer on 127.0.0.1:{self.port} within "
                    f"{timeout_s:.0f} s: is the HardyLive macro running?"
                )
            time.sleep(poll_s)

    def replace_shape(self, path: Path, label: str) -> None:
        """Swap the STEP at ``path`` in as the object labelled ``label``."""
        try:
            ok = self._proxy.replace_shape(str(path), label)
        except xmlrpc.client.Fault as exc:
            raise FreeCADLiveError(
                f"FreeCAD refused {label}: {exc.faultString}"
            ) from exc
        except (TimeoutError, OSError, xmlrpc.client.Error) as exc:
            raise FreeCADLiveError(f"FreeCAD unreachable for {label}: {exc}") from exc
        if not ok:
            raise FreeCADLiveError(f"FreeCAD did not place {label}")
