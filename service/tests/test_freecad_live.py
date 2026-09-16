"""The FreeCAD live client (``desktop/freecad_live.py``), against a loopback
XML-RPC server of the test's own that answers the macro's two methods. What
FreeCAD does with ``replace_shape`` is the macro's business and unverified
here; what the client *sends* and how it fails are pinned."""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path
from xmlrpc.server import SimpleXMLRPCServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))

import freecad_live  # noqa: E402


class _Server:
    def __init__(self):
        self.calls: list[tuple] = []
        self.refuse = False
        self.server = SimpleXMLRPCServer(
            ("127.0.0.1", 0), allow_none=True, logRequests=False
        )
        self.port = self.server.server_address[1]
        self.server.register_function(self.ping, "ping")
        self.server.register_function(self.replace_shape, "replace_shape")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def ping(self):
        return True

    def replace_shape(self, path, label):
        self.calls.append((path, label))
        if self.refuse:
            raise RuntimeError("no such file: " + path)
        return True

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server():
    s = _Server()
    try:
        yield s
    finally:
        s.close()


def test_replace_shape_sends_the_path_and_label_to_the_macro(server):
    live = freecad_live.FreeCADLive(server.port)
    live.wait_ready(timeout_s=5.0, poll_s=0.05)
    live.replace_shape(Path("/tmp/case-01-board.step"), "board")
    live.replace_shape(Path("/tmp/case-02-base.step"), "base")
    assert server.calls == [
        ("/tmp/case-01-board.step", "board"),
        ("/tmp/case-02-base.step", "base"),
    ]


def test_a_refusal_from_freecad_is_an_error_in_its_own_words(server):
    server.refuse = True
    live = freecad_live.FreeCADLive(server.port)
    with pytest.raises(freecad_live.FreeCADLiveError) as caught:
        live.replace_shape(Path("/nowhere.step"), "lid")
    assert "FreeCAD refused lid" in str(caught.value)
    assert "no such file" in str(caught.value)


def test_no_freecad_on_the_port_is_said_not_hung():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    live = freecad_live.FreeCADLive(free_port)
    assert live.ready() is False
    with pytest.raises(freecad_live.FreeCADLiveError) as caught:
        live.wait_ready(timeout_s=0.3, poll_s=0.05)
    assert "did not answer" in str(caught.value)
    with pytest.raises(freecad_live.FreeCADLiveError) as caught:
        live.replace_shape(Path("/x.step"), "base")
    assert "unreachable" in str(caught.value)


def test_launch_command_goes_through_open_on_macos_and_the_binary_elsewhere():
    macro = Path("/m/HardyLive.FCMacro")
    if sys.platform == "darwin":
        assert freecad_live.launch_command("/Applications/FreeCAD.app", macro) == [
            "/usr/bin/open", "-n", "-a", "/Applications/FreeCAD.app",
            "--args", str(macro),
        ]
    assert freecad_live.launch_command("/usr/bin/freecad", macro) == [
        "/usr/bin/freecad", str(macro)
    ]


def test_the_macro_ships_and_compiles():
    macro = freecad_live.MACRO
    assert macro.is_file()
    compile(macro.read_text(encoding="utf-8"), str(macro), "exec")
    assert "replace_shape" in macro.read_text(encoding="utf-8")
