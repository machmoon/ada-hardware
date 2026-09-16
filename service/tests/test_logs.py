"""Structured logs and request ids (service/logs.py and the handler hooks)."""

from __future__ import annotations

import io
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from service import app as app_module
from service import logs


def test_json_lines_carry_the_powertools_keys_and_the_bound_fields():
    out = io.StringIO()
    logs.bind(request_id="r-1")
    try:
        logs.emit("info", "hello", stream=out, environ={logs.LOG_FORMAT_ENV: "json"},
                  now=0.25, status=200, skipped=None)
    finally:
        logs.bind(request_id=None)
    record = json.loads(out.getvalue())
    assert record == {
        "level": "INFO", "message": "hello", "timestamp": "1970-01-01T00:00:00.250Z",
        "service": "silkscreen", "request_id": "r-1", "status": 200,
    }


def test_text_mode_is_the_line_a_developer_always_read():
    out = io.StringIO()
    logs.emit("info", "listening on :8081", stream=out, environ={}, status=1)
    assert out.getvalue() == "listening on :8081\n"
    out = io.StringIO()
    logs.emit("error", "error abc: boom", stream=out, environ={}, trace="Traceback\n")
    assert out.getvalue() == "error abc: boom\nTraceback\n\n"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Request-Id": "abc-123"}, "abc-123"),
        ({"X-Request-Id": "has space", "X-Amzn-Trace-Id": "Root=1-abc-def;Parent=x"},
         "1-abc-def"),
        ({"X-Request-Id": "x" * 200}, None),
        ({}, None),
    ],
)
def test_request_id_is_kept_only_when_sane(headers, expected):
    got = logs.request_id_from(headers)
    if expected is None:
        assert len(got) == 32 and all(c in "0123456789abcdef" for c in got)
    else:
        assert got == expected


def test_a_log_line_that_cannot_be_written_does_not_raise():
    class Broken(io.StringIO):
        def write(self, _):
            raise OSError("disk gone")

    logs.emit("info", "x", stream=Broken(), environ={logs.LOG_FORMAT_ENV: "json"})


def test_the_path_is_logged_without_its_query_or_token_segments():
    loggable = app_module._loggable_path
    assert loggable("/steps/abc123/plan?key=secret") == "/steps/abc123/plan"
    assert loggable("/mcp/0123456789abcdefABCDEF0123456789") == "/mcp/***"


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv(logs.LOG_FORMAT_ENV, "json")
    monkeypatch.delenv(app_module.ACCESS_TOKEN_ENV, raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def test_every_response_names_its_request_and_one_json_line_says_how_it_went(
    server, capfd
):
    request = urllib.request.Request(f"{server}/healthz?probe=1",
                                     headers={"X-Request-Id": "probe-7"})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.headers["X-Request-Id"] == "probe-7"
        assert response.headers.get_all("X-Request-Id") == ["probe-7"]
    minted = urllib.request.urlopen(f"{server}/healthz", timeout=5)
    assert len(minted.headers["X-Request-Id"]) == 32
    lines = [json.loads(line) for line in capfd.readouterr().err.splitlines()
             if line.startswith("{")]
    access = [r for r in lines if r.get("request_id") == "probe-7"]
    assert len(access) == 1
    assert access[0]["path"] == "/healthz"
    assert access[0]["status"] == 200
    assert access[0]["method"] == "GET"
    assert access[0]["duration_ms"] >= 0
