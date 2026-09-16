"""Structured logs: one line per event, JSON in a container, text on a laptop.

``docs/production-plan.md`` (operational excellence): "Structured JSON logs
with ``run_id``, ``account_id``, ``request_id`` on every line, shipped to
CloudWatch". CloudWatch Logs Insights parses a JSON line into fields with no
configuration, so a stuck run or a 500 is one query away instead of a grep
through prose.

The field names follow AWS Powertools' logger, the de facto shape for Python
services on AWS: ``level``, ``message``, ``timestamp`` and ``service`` are its
default keys (aws-powertools/powertools-lambda-python
``aws_lambda_powertools/logging/formatter.py`` at eceb809, MIT-0), and
anything else rides beside them as a top-level key. Powertools itself is not
a dependency: it is built around the Lambda context, this is a long-running
stdlib server, and the whole format is ``json.dumps`` of one dict.

``SILKSCREEN_LOG_FORMAT=json`` turns it on (the Dockerfile sets it). Unset,
a line is the text a developer has always read, so nothing changes locally.

Request ids: an inbound ``X-Request-Id`` is kept when it is a sane token, an
ALB's ``X-Amzn-Trace-Id`` root is used next, and otherwise one is minted.
The id is echoed on the response, so a customer quoting it finds the line.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any, TextIO

__all__ = [
    "LOG_FORMAT_ENV",
    "REQUEST_ID_HEADER",
    "SERVICE",
    "bind",
    "bound",
    "emit",
    "json_enabled",
    "request_id_from",
]

LOG_FORMAT_ENV = "SILKSCREEN_LOG_FORMAT"
REQUEST_ID_HEADER = "X-Request-Id"
SERVICE = "silkscreen"

#: What an inbound request id may look like before it is trusted into a log
#: line and a response header: no spaces, no control characters, bounded.
_SANE_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_TRACE_ROOT = re.compile(r"Root=([A-Za-z0-9\-]{1,64})")


_local = threading.local()


def bind(**fields: Any) -> None:
    """Set the fields every later line on this thread carries (``None``
    removes one). The server handles each request on its own thread, so this
    is how a helper deep in a request logs with that request's id."""
    current = dict(getattr(_local, "fields", {}))
    for key, value in fields.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    _local.fields = current


def bound() -> dict[str, Any]:
    return dict(getattr(_local, "fields", {}))


def json_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(LOG_FORMAT_ENV) or "").strip().lower() == "json"


def request_id_from(headers: Mapping[str, str] | Any) -> str:
    """The id this request is known by, from its headers or freshly minted."""
    given = (headers.get(REQUEST_ID_HEADER) or "").strip()
    if _SANE_ID.match(given):
        return given
    trace = _TRACE_ROOT.search(headers.get("X-Amzn-Trace-Id") or "")
    if trace:
        return trace.group(1)
    return uuid.uuid4().hex


def _timestamp(now: float) -> str:
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{whole}.{int(now % 1 * 1000):03d}Z"


def emit(
    level: str,
    message: str,
    *,
    stream: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    now: float | None = None,
    **fields: Any,
) -> None:
    """Write one log line. Fields whose value is ``None`` are left out.

    Never raises: a log line that cannot be written must not turn a served
    request into a failed one. A value JSON cannot encode is written with
    ``str``.
    """
    out = sys.stderr if stream is None else stream
    kept = {**bound(), **{k: v for k, v in fields.items() if v is not None}}
    try:
        if json_enabled(environ):
            record = {
                "level": level.upper(),
                "message": message,
                "timestamp": _timestamp(time.time() if now is None else now),
                "service": SERVICE,
                **kept,
            }
            out.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
        else:
            # The line a developer has always read; fields are for machines.
            trace = kept.get("trace")
            out.write(f"{message}\n{trace}\n" if trace else f"{message}\n")
    except (OSError, ValueError):
        pass
