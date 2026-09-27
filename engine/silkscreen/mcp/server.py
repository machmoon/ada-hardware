"""An MCP server over stdio.

The previous project shipped a directory named ``mcp/`` and called that an MCP
integration; there was no protocol anywhere in it. This is the actual thing:
JSON-RPC 2.0 over stdin/stdout, speaking ``initialize``, ``tools/list`` and
``tools/call`` per the Model Context Protocol, exposing the engine's useful
operations to any MCP client -- validation, placement, board and footprint
generation, and SPICE simulation. ``TOOLS`` is the list; do not restate its
length here, since it has already drifted once.

The transport is deliberately separable from the dispatch: :func:`handle` maps
one message to one response (or to none) and never touches a stream, which is
why the protocol is testable without spawning a process.

The revision is MCP **2025-11-25** (``docs/specification/2025-11-25/`` in
modelcontextprotocol/modelcontextprotocol at the release tag, ``38c84e9``).
``initialize`` echoes any of ``SUPPORTED_PROTOCOL_VERSIONS`` and answers
``2025-11-25`` to anything else (``basic/lifecycle.mdx:172-174``). The list
and the rule are python-sdk's exactly: ``src/mcp/shared/version.py:3`` and
``src/mcp/server/session.py:183-187`` at v1.30.0 (``8c2fa6e``),
``src/mcp-types/mcp_types/version.py:33-38`` and ``src/mcp/server/runner.py:425``
at v2.2.0 (``9972c21``). ``2026-07-28`` is not among them: it removes
``initialize``, and a client that probes for it with ``server/discover`` gets
``-32601`` here and falls back to the handshake. python-sdk refuses an
``initialize`` with no ``protocolVersion``; this answers it the latest version
instead, since the spec puts that MUST on the client, not the server.

What :func:`handle` enforces, each rule with its source in that revision:

* every message is classified before anything runs -- request, notification,
  response, or ``-32600`` (``basic/index.mdx:29-49``). A request id is a
  string or an integer, never ``null`` (``:48-49``), and an error whose id
  could not be read *omits* it (``:91``; ``schema.ts:124,161-163``), where
  python-sdk v1 sends ``"server-error"`` (``streamable_http.py:354``);
* a notification, or a response the client sent, gets no reply and reaches
  no handler (``basic/index.mdx:98``), so a ``tools/call`` sent without an id
  cannot start a paid ``generate_board``;
* tool arguments are checked against ``inputSchema`` before the tool runs,
  and a structured result against ``outputSchema`` after it
  (``server/tools.mdx:329,501-505``) -- python-sdk's ``call_tool`` with its
  "Input validation error" and "Output validation error" texts
  (``src/mcp/server/lowlevel/server.py:533-575`` at v1.30.0). The validator is
  stdlib and implements exactly the JSON Schema 2020-12 keywords these schemas
  use (``SCHEMA_KEYWORDS``; 2020-12 is the default dialect, ``basic/index.mdx:182``);
  any other keyword raises rather than being ignored. Unlike python-sdk, which
  reports the first failure, it reports every one -- the ``netlist.py`` rule,
  so a model can repair in one pass;
* ``tools/call`` is rate limited (:class:`RateLimiter`,
  ``MCP_TOOL_CALLS_PER_MINUTE``) and ``generate_board`` runs one at a time,
  because the spec makes rate limiting a MUST (``server/tools.mdx:504``) and
  python-sdk has no limiter to copy;
* output is sanitised: a non-finite float becomes ``null`` and nothing is
  serialised with ``allow_nan``, since ``NaN`` is not JSON.

stdio specifics (``basic/transports.mdx:24-37``): each line is decoded as
strict UTF-8 (python-sdk wraps stdin with ``errors="replace"``,
``src/mcp/server/stdio.py:47-49``; a specific ``-32700`` is the better
answer), a JSON array is refused because stdio messages are individual, and
tool code runs with ``sys.stdout`` pointed at stderr so a stray ``print`` cannot
corrupt the channel. Writes to file descriptor 1 from native code are not
caught; python-sdk has no guard at all. A ``tools/call`` runs on a thread of
its own (:data:`SPAWNED_METHODS`), so a ping sent during a long call is still
answered at once (``basic/utilities/ping.mdx:31``); everything else is answered
inline, in the order it arrived.

Run it with::

    python -m silkscreen.mcp
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..board import build_board, emit_kicad_pcb, package_errors
from ..footprints import CHIP_SIZES, UnsupportedPackage, chip_passive
from ..netlist import ValidationError, parse_circuit_spec
from ..packing import PackStatus, Part, pack
from ..spice import MEASUREMENT_KINDS, SpiceError, build_deck, check_all, simulate_deck
from ..spice.simulators import available_simulators
from ..spice.spec import REQUEST_SCHEMA as SPICE_REQUEST_SCHEMA
from ..spice.spec import assertions_from_dict, testbench_from_dict
from ..units import to_mm

__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TOOLS",
    "SPAWNED_METHODS",
    "ENGINE",
    "RateLimiter",
    "Server",
    "Toolset",
    "classify",
    "decode",
    "dumps",
    "handle",
    "main",
    "negotiate",
    "schema_errors",
]

#: The revision ``initialize`` answers when it cannot echo the client's.
LATEST_PROTOCOL_VERSION = "2025-11-25"
#: Every revision this server can honour, oldest first -- python-sdk v1.30.0's
#: ``SUPPORTED_PROTOCOL_VERSIONS`` and v2.2.0's ``HANDSHAKE_PROTOCOL_VERSIONS``.
#: Nothing served differs by version except HTTP batching (``http.py``); the
#: newer tool fields are additive and older clients ignore them.
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
#: Kept under its old name: callers and tests import it.
PROTOCOL_VERSION = LATEST_PROTOCOL_VERSION
#: ``title`` is optional in ``Implementation`` since 2025-06-18 (``schema.ts:548``);
#: ``name`` stays the engine's, which clients already key on.
SERVER_INFO = {"name": "silkscreen", "title": "Ada", "version": "0.1.0"}

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _circuit_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "devices": {"type": "object"},
            "passives": {"type": "object"},
            "nets": {"type": "object"},
        },
        "required": ["nets"],
    }


#: Hints for a tool that computes in this process and changes nothing outside
#: it (``schema.ts:1178-1223``). ``destructiveHint`` and ``idempotentHint``
#: mean nothing when ``readOnlyHint`` is true, so they are left out.
_LOCAL_READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


def _tool(
    name: str,
    title: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    output_schema: dict[str, Any] | None = None,
    hints: dict[str, bool] = _LOCAL_READ_ONLY,
) -> dict[str, Any]:
    """One ``Tool``. The title is given twice on purpose: top-level ``title``
    is 2025-06-18, ``annotations.title`` is 2025-03-26, and a client shows the
    first it knows (``schema.ts:1289``)."""
    tool: dict[str, Any] = {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": input_schema,
    }
    if output_schema is not None:
        tool["outputSchema"] = output_schema
    tool["annotations"] = {"title": title, **hints}
    return tool


# Output schemas. The root is always ``type: object`` (``schema.ts:1275``), no
# ``$schema`` is named so 2020-12 applies, and only keywords the validator
# below implements appear.
_STRINGS = {"type": "array", "items": {"type": "string"}}
_MM_PAIR = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}
_PACK_STATUS = {"type": "string", "enum": [status.value for status in PackStatus]}
#: ``agents/review.py`` ``Severity``, spelled out so this module need not
#: import the agents layer; test_mcp.py pins the two together.
_SEVERITIES = ["blocker", "marginal", "note"]


def _object(
    properties: dict[str, Any],
    *,
    required: list[str] | None = None,
    closed: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
    }
    if closed:
        schema["additionalProperties"] = False
    return schema


_COUNT = {"type": "integer", "minimum": 0}

TOOLS: list[dict[str, Any]] = [
    _tool(
        "validate_circuit",
        "Validate a circuit",
        "Validate a circuit against the Silkscreen IR. Returns every error "
        "at once, not the first, so a caller can repair in one pass.",
        _circuit_schema(),
        # A verdict, not a failure: ``valid: false`` is a successful call.
        output_schema=_object(
            {
                "valid": {"type": "boolean"},
                "errors": _STRINGS,
                "devices": _COUNT,
                "passives": _COUNT,
                "nets": _COUNT,
            },
            required=["valid"],
            closed=True,
        ),
    ),
    _tool(
        "build_board",
        "Place a circuit on a board",
        "Turn a validated circuit into generated footprints, nets and a "
        "CP-SAT placement. Returns board size, wirelength and solver status.",
        {
            "type": "object",
            "properties": {
                "circuit": _circuit_schema(),
                "time_limit_s": {"type": "number", "default": 20.0},
            },
            "required": ["circuit"],
        },
        output_schema=_object(
            {
                "status": _PACK_STATUS,
                "board_mm": _MM_PAIR,
                "wirelength_mm": {"type": ["number", "null"]},
                "parts": {
                    "type": "array",
                    "items": _object(
                        {"ref": {"type": "string"}, "footprint": {"type": "string"}}
                    ),
                },
                "warnings": _STRINGS,
            }
        ),
    ),
    # No outputSchema: a structured copy of a whole .kicad_pcb beside the
    # SHOULD-text copy would put the file in the response twice. The file
    # belongs in a resource; until then this stays text only.
    _tool(
        "emit_kicad_pcb",
        "Write a KiCad board file",
        "Emit a complete .kicad_pcb file for a circuit. No KiCad install "
        "and no footprint library required.",
        {
            "type": "object",
            "properties": {
                "circuit": _circuit_schema(),
                "time_limit_s": {"type": "number", "default": 20.0},
            },
            "required": ["circuit"],
        },
    ),
    _tool(
        "place_parts",
        "Pack rectangles",
        "Place bare rectangles with the CP-SAT packer. Sizes in millimetres.",
        {
            "type": "object",
            "properties": {
                "parts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string"},
                            "width_mm": {"type": "number"},
                            "height_mm": {"type": "number"},
                        },
                        "required": ["ref", "width_mm", "height_mm"],
                    },
                },
                "clearance_mm": {"type": "number", "default": 0.25},
                "time_limit_s": {"type": "number", "default": 10.0},
            },
            "required": ["parts"],
        },
        output_schema=_object(
            {
                "status": _PACK_STATUS,
                "board_mm": _MM_PAIR,
                "placements": {
                    "type": "array",
                    "items": _object(
                        {
                            "ref": {"type": "string"},
                            "x_mm": {"type": "number"},
                            "y_mm": {"type": "number"},
                            "rotated": {"type": "boolean"},
                        }
                    ),
                },
                "warnings": _STRINGS,
            }
        ),
    ),
    _tool(
        "generate_footprint",
        "Generate a chip footprint",
        "Generate an IPC-7351 land pattern for a two-terminal chip package. "
        "Returns pads and courtyard in millimetres.",
        {
            "type": "object",
            "properties": {
                "package": {"type": "string", "enum": sorted(CHIP_SIZES)},
            },
            "required": ["package"],
        },
        output_schema=_object(
            {
                "name": {"type": "string"},
                "pads": {
                    "type": "array",
                    "items": _object(
                        {
                            "number": {"type": "string"},
                            "x_mm": {"type": "number"},
                            "y_mm": {"type": "number"},
                            "width_mm": {"type": "number"},
                            "height_mm": {"type": "number"},
                        }
                    ),
                },
                "courtyard_mm": _MM_PAIR,
            }
        ),
    ),
    _tool(
        "simulate_circuit",
        "Simulate and check a circuit",
        "Simulate a circuit with SPICE and check it against a "
        "specification. This is the behavioural verifier: DRC says whether "
        "a board can be made, this says whether the circuit works. Give it "
        "a circuit, a testbench (sources plus one analysis) and a list of "
        "assertions; it returns a pass/fail verdict with the measured "
        "number and a signed margin beside every clause. Omit assertions "
        "to just read the waveforms back. Fails loudly: a missing device "
        "model, a probe on a net that does not exist, or a solver that "
        "will not converge is an error, never an empty result.",
        SPICE_REQUEST_SCHEMA,
        # Loose on purpose: the waveform summary under ``result`` is the
        # simulator's shape. What a client branches on -- ``passed`` and each
        # clause's measured number and margin -- is pinned. ``ok`` is always
        # true here, because every ``ok: false`` is an ``isError`` result.
        output_schema=_object(
            {
                "ok": {"const": True},
                "passed": {"type": "boolean"},
                "summary": {"type": "string"},
                "warnings": _STRINGS,
                "assertions": {
                    "type": "array",
                    "items": _object(
                        {
                            "name": {"type": "string"},
                            "passed": {"type": "boolean"},
                            "measured": {"type": ["number", "null"]},
                            "expected": {"type": "number"},
                            "op": {"type": "string"},
                            "unit": {"type": "string"},
                            "margin": {"type": ["number", "null"]},
                            "description": {"type": "string"},
                            "error": {"type": ["string", "null"]},
                        },
                        required=["name", "passed", "measured", "margin"],
                    ),
                },
                "result": {"type": ["object", "null"]},
            },
            required=["ok"],
        ),
    ),
    _tool(
        "spice_capabilities",
        "List SPICE capabilities",
        "Which SPICE simulators this machine can run, and every "
        "measurement kind an assertion may use. Call this before building "
        "a simulation request.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        output_schema=_object(
            {
                "simulators": _STRINGS,
                "measurement_kinds": _STRINGS,
                "analysis_kinds": {
                    "type": "array",
                    "items": {"enum": ["op", "tran", "ac", "dc"]},
                },
                "operators": _STRINGS,
            }
        ),
    ),
    _tool(
        "generate_board",
        "Design a board from a description",
        "Design a whole KiCad project from a plain-language request: "
        "read any datasheets given, propose and validate a circuit, "
        "place it with CP-SAT, draw the schematic, route the copper and "
        "run the design critic. Writes .kicad_pro, .kicad_sch and "
        ".kicad_pcb and returns their paths, the nets it could not "
        "route (left as ratsnest, never hidden) and every review finding "
        "with its severity. Unlike the other tools this one calls a "
        "Gemini model, so it needs GOOGLE_API_KEY (or a .env in the "
        "Ada checkout) and takes one to two minutes; one runs at a time.",
        {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": (
                        "What to build, e.g. 'a 3.3V LDO board from 5V USB'."
                    ),
                },
                "output": {
                    "type": "string",
                    "description": (
                        "Path of the .kicad_pcb to write; the schematic and "
                        "project land beside it. Default: a new folder under "
                        "~/Hardy/boards."
                    ),
                },
                "datasheets": {
                    "type": "object",
                    "description": "{part_number: pdf_url} to read before designing.",
                    "additionalProperties": {"type": "string"},
                },
                "effort": {
                    "type": "string",
                    "enum": ["fast", "balanced", "thorough"],
                    "description": "Solver budget and repair rounds; default fast.",
                },
                "route": {
                    "type": "boolean",
                    "description": "Lay copper (default true).",
                },
                "review": {
                    "type": "boolean",
                    "description": "Run the critic (default true).",
                },
            },
            "required": ["intent"],
            "additionalProperties": False,
        },
        output_schema=_object(
            {
                "summary": {"type": "string"},
                "files": _object(
                    {
                        key: {"type": ["string", "null"]}
                        for key in ("board", "schematic", "project", "placed_board")
                    }
                ),
                "size_mm": _MM_PAIR,
                "solver_status": {"type": "string"},
                "unrouted": {
                    "type": ["object", "null"],
                    "additionalProperties": {"type": "string"},
                },
                "review": _object(
                    {
                        "ran": {"type": "boolean"},
                        "note": {"type": ["string", "null"]},
                        "blockers": _COUNT,
                    }
                ),
                "findings": {
                    "type": "array",
                    "items": _object(
                        {
                            "severity": {"type": "string", "enum": _SEVERITIES},
                            "title": {"type": "string"},
                            "detail": {"type": "string"},
                            "parts": _STRINGS,
                            "suggested_fix": {"type": "string"},
                        }
                    ),
                },
            }
        ),
        # An explicit ``output`` overwrites an existing board; every run makes
        # new model calls and a new folder; it reaches Gemini and fetches
        # datasheet URLs.
        hints={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    ),
]

_TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}
#: Tools whose answer is a judgement of their input. Their arguments are still
#: checked against ``inputSchema`` (``server/tools.mdx:502``), but by the tool,
#: and a failure is the verdict -- ``valid: false``, ``isError`` false -- rather
#: than the "Input validation error" text every other tool answers. Otherwise
#: the one tool a caller asks "is this circuit right?" would answer the
#: malformed circuits with plain text that is not JSON.
_VERDICT_TOOLS = frozenset({"validate_circuit"})


def _finite(value: Any) -> Any:
    """``value`` with every non-finite float replaced by ``None``.

    ``NaN`` and ``Infinity`` are not JSON, and a simulator can produce either
    (``spice/result.py``); ``null`` is the honest spelling of "no number".
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def _json_text(payload: Any) -> str:
    return json.dumps(_finite(payload), indent=2, allow_nan=False)


def _text_result(payload: Any) -> dict[str, Any]:
    """MCP tool results are content blocks, not bare JSON."""
    return {
        "content": [{"type": "text", "text": _json_text(payload)}],
        "isError": False,
    }


def _structured_result(payload: dict[str, Any]) -> dict[str, Any]:
    """The result of a tool that declares an ``outputSchema``.

    The object rides as ``structuredContent`` and again as JSON text, which
    the spec asks for so pre-2025-06-18 clients still see it
    (``server/tools.mdx:322``); python-sdk builds the same pair with
    ``json.dumps(results, indent=2)`` (``lowlevel/server.py:557`` at v1.30.0).
    """
    clean = _finite(payload)
    return {
        "content": [
            {"type": "text", "text": json.dumps(clean, indent=2, allow_nan=False)}
        ],
        "structuredContent": clean,
        "isError": False,
    }


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _error_payload(payload: Any) -> dict[str, Any]:
    """A tool failure whose details are JSON: the same text, ``isError`` set.

    It carries no ``structuredContent``: that must conform to the
    ``outputSchema``, which describes success.
    """
    return {"content": [{"type": "text", "text": _json_text(payload)}], "isError": True}


def _tool_validate_circuit(args: dict[str, Any]) -> dict[str, Any]:
    # What the schema refuses is what build_board would refuse before it ran
    # (it is the same schema), so it belongs in the verdict too; it comes
    # first, then everything the IR finds, all at once.
    shape = schema_errors(
        args, _TOOL_BY_NAME["validate_circuit"]["inputSchema"], "circuit"
    )
    try:
        spec = parse_circuit_spec(args)
    except ValidationError as exc:
        return _structured_result({"valid": False, "errors": shape + list(exc.errors)})
    if shape:
        return _structured_result({"valid": False, "errors": shape})
    # The IR can be well formed and still not drawable: a pin numbered for a
    # pad its land pattern lacks. ``build_board`` refuses that, so a
    # validator that said "valid" here would disagree with the next tool.
    drawable = package_errors(spec)
    if drawable:
        return _structured_result({"valid": False, "errors": drawable})
    return _structured_result(
        {
            "valid": True,
            "devices": len(spec.devices),
            "passives": len(spec.passives),
            "nets": len(spec.connections),
        }
    )


def _build(args: dict[str, Any]):
    spec = parse_circuit_spec(args.get("circuit") or {})
    return build_board(spec, time_limit_s=float(args.get("time_limit_s", 20.0)))


def _tool_build_board(args: dict[str, Any]) -> dict[str, Any]:
    result = _build(args)
    return _structured_result(
        {
            "status": str(result.solver_status),
            "board_mm": [
                round(to_mm(result.width_nm), 3),
                round(to_mm(result.height_nm), 3),
            ],
            "wirelength_mm": (
                round(to_mm(result.wirelength_nm), 2)
                if result.wirelength_nm is not None
                else None
            ),
            "parts": [
                {"ref": p.ref, "footprint": p.footprint.name} for p in result.parts
            ],
            "warnings": list(result.warnings),
        }
    )


def _tool_emit_kicad_pcb(args: dict[str, Any]) -> dict[str, Any]:
    result = _build(args)
    text = emit_kicad_pcb(result)
    return _text_result(
        {
            "kicad_pcb": text,
            "bytes": len(text.encode()),
            "footprints": len(result.parts),
        }
    )


def _tool_place_parts(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("parts") or []
    if not raw:
        return _error_result("place_parts needs at least one part")
    parts = [
        Part(
            width_nm=int(round(float(p["width_mm"]) * 1e6)),
            height_nm=int(round(float(p["height_mm"]) * 1e6)),
            ref=str(p["ref"]),
        )
        for p in raw
    ]
    result = pack(
        parts,
        clearance_nm=int(round(float(args.get("clearance_mm", 0.25)) * 1e6)),
        time_limit_s=float(args.get("time_limit_s", 10.0)),
    )
    return _structured_result(
        {
            "status": str(result.status),
            "board_mm": [
                round(to_mm(result.board_width_nm), 3),
                round(to_mm(result.board_height_nm), 3),
            ],
            "placements": [
                {
                    "ref": p.ref,
                    "x_mm": round(to_mm(p.x_nm), 3),
                    "y_mm": round(to_mm(p.y_nm), 3),
                    "rotated": p.rotated,
                }
                for p in result.placements
            ],
            "warnings": list(result.warnings),
        }
    )


def _tool_generate_footprint(args: dict[str, Any]) -> dict[str, Any]:
    package = str(args.get("package", ""))
    try:
        fp = chip_passive(package)
    except UnsupportedPackage as exc:
        return _error_result(str(exc))
    return _structured_result(
        {
            "name": fp.name,
            "pads": [
                {
                    "number": pad.number,
                    "x_mm": round(to_mm(pad.x_nm), 4),
                    "y_mm": round(to_mm(pad.y_nm), 4),
                    "width_mm": round(to_mm(pad.w_nm), 4),
                    "height_mm": round(to_mm(pad.h_nm), 4),
                }
                for pad in fp.pads
            ],
            "courtyard_mm": [
                round(to_mm(fp.courtyard_w_nm * 2), 4),
                round(to_mm(fp.courtyard_h_nm * 2), 4),
            ],
        }
    )


_SIMULATION_FIELDS = frozenset(
    {"circuit", "testbench", "assertions", "simulator", "timeout_s", "max_points"}
)
_MAX_SIMULATION_TIMEOUT_S = 120.0
_MAX_WAVEFORM_POINTS = 2000


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _simulation_controls(
    args: Any,
) -> tuple[list[str], str | None, float, int]:
    """Validate resource controls before any external simulator is started."""
    if not isinstance(args, dict):
        return ["simulation request must be an object"], None, 60.0, 0

    errors = [
        f"simulation request: unknown field {key!r}"
        for key in sorted(set(args) - _SIMULATION_FIELDS)
    ]

    simulator = args.get("simulator")
    if simulator is not None and simulator not in ("ngspice", "ltspice"):
        errors.append("'simulator' must be 'ngspice' or 'ltspice'")

    timeout = args.get("timeout_s", 60.0)
    if (
        not _is_finite_number(timeout)
        or not 0 < timeout <= _MAX_SIMULATION_TIMEOUT_S
    ):
        errors.append(
            f"'timeout_s' must be a finite number greater than 0 and no more "
            f"than {_MAX_SIMULATION_TIMEOUT_S:g}"
        )
        timeout = 60.0

    max_points = args.get("max_points", 0)
    if (
        isinstance(max_points, bool)
        or not isinstance(max_points, int)
        or not 0 <= max_points <= _MAX_WAVEFORM_POINTS
    ):
        errors.append(
            f"'max_points' must be an integer between 0 and {_MAX_WAVEFORM_POINTS}"
        )
        max_points = 0

    return errors, simulator, float(timeout), max_points


def _tool_simulate_circuit(args: dict[str, Any]) -> dict[str, Any]:
    """Run one simulation and check it against a specification.

    Every failure comes back as an ``isError`` result carrying the simulator's
    own words, as JSON text naming the stage that refused. A tool that
    answered "no results" here would be read by a model as "the circuit does
    nothing", which is the one outcome this must never produce. Most request
    problems are caught earlier, by the input schema; ``_simulation_controls``
    still guards what a schema cannot say (a ``NaN`` timeout passes every
    numeric bound in JSON Schema).
    """
    request_errors, simulator, timeout_s, max_points = _simulation_controls(args)
    if request_errors:
        return _error_payload(
            {"ok": False, "stage": "request", "errors": request_errors}
        )

    try:
        spec = parse_circuit_spec(args.get("circuit") or {})
    except ValidationError as exc:
        return _error_payload(
            {"ok": False, "stage": "circuit", "errors": list(exc.errors)}
        )

    try:
        bench = testbench_from_dict(args.get("testbench") or {})
        assertions = assertions_from_dict(args.get("assertions"))
        deck = build_deck(spec, bench)
    except SpiceError as exc:
        return _error_payload(
            {
                "ok": False,
                "stage": "testbench",
                "errors": list(getattr(exc, "errors", [])) or [str(exc)],
            }
        )

    try:
        result = simulate_deck(
            deck,
            simulator=simulator,
            timeout_s=timeout_s,
        )
    except SpiceError as exc:
        return _error_payload(
            {
                "ok": False,
                "stage": "simulation",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "deck": deck.text,
            }
        )

    if not assertions:
        return _structured_result(
            {"ok": True, "result": result.to_dict(max_points=max_points)}
        )

    report = check_all(result, assertions)
    payload = report.to_dict(max_points=max_points)
    payload["ok"] = True
    payload["summary"] = report.summary()
    return _structured_result(payload)


def _tool_spice_capabilities(args: dict[str, Any]) -> dict[str, Any]:
    return _structured_result(
        {
            "simulators": [sim.name for sim in available_simulators()],
            "measurement_kinds": sorted(MEASUREMENT_KINDS),
            "analysis_kinds": ["op", "tran", "ac", "dc"],
            "operators": ["<", "<=", ">", ">=", "==", "!=", "within"],
        }
    )


#: Where ``generate_board`` writes when the caller names no path. Under the
#: home directory rather than the cwd because an MCP client (Claude Desktop)
#: launches the server with no meaningful working directory.
DEFAULT_BOARDS_DIR = Path("~/Hardy/boards")


def _load_env_if_needed() -> None:
    """Read ``.env`` the way the CLI does, without making it a requirement.

    ``ADA_REPO_ROOT`` (the desktop app's own variable) names the checkout;
    the cwd is tried after it. Nothing is overwritten -- the same setdefault
    rule as :func:`silkscreen.cli._load_dotenv`.
    """
    from ..cli import _load_dotenv

    for root in (os.environ.get("ADA_REPO_ROOT"), os.getcwd()):
        if root:
            _load_dotenv(Path(root) / ".env")


def build_model() -> Any:
    """The model ``generate_board`` designs with; a module-level seam so the
    tests can substitute a :class:`ScriptedModel` and stay offline."""
    _load_env_if_needed()
    from ..agents.model import GeminiModel

    return GeminiModel()


def _default_output(intent: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", intent.lower()).strip("-")[:40] or "board"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return DEFAULT_BOARDS_DIR.expanduser() / f"{stamp}-{slug}" / "board.kicad_pcb"


#: ``generate_board`` runs one at a time, process-wide: a run takes one to two
#: minutes and spends the Gemini key, so a second concurrent one is refused in
#: words rather than queued behind the first where a client would time out.
_BOARD_RUN = threading.Lock()
_board_run_started = 0.0


def _tool_generate_board(args: dict[str, Any]) -> dict[str, Any]:
    global _board_run_started
    if not _BOARD_RUN.acquire(blocking=False):
        running_s = time.monotonic() - _board_run_started
        return _error_result(
            "busy: generate_board is already designing a board (started "
            f"{running_s:.0f} s ago; a run takes one to two minutes). This "
            "server runs one at a time; call again when it has finished."
        )
    _board_run_started = time.monotonic()
    try:
        return _generate_board(args)
    finally:
        _BOARD_RUN.release()


def _generate_board(args: dict[str, Any]) -> dict[str, Any]:
    intent = args.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return _error_result("intent must be a non-empty string")
    output = (
        Path(args["output"]).expanduser() if args.get("output")
        else _default_output(intent)
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    from ..agents import generate_pcb

    kwargs: dict[str, Any] = {}
    if args.get("effort"):
        kwargs["effort"] = args["effort"]
    result = generate_pcb(
        build_model(),
        intent.strip(),
        datasheets=args.get("datasheets") or None,
        output=output,
        route=args.get("route", True),
        review=args.get("review", True),
        **kwargs,
    )

    def _path(p: Path | None) -> str | None:
        return None if p is None else str(p)

    findings = [
        {
            "severity": str(f.severity),
            "title": f.title,
            "detail": f.detail,
            "parts": list(f.parts),
            "suggested_fix": f.suggested_fix,
        }
        for f in result.findings
    ]
    return _structured_result(
        {
            "summary": result.summary(),
            "files": {
                "board": _path(result.board_path),
                "schematic": _path(result.schematic_path),
                "project": _path(result.project_path),
                "placed_board": _path(result.placed_board_path),
            },
            "size_mm": list(result.board.size_mm),
            "solver_status": result.board.solver_status,
            "unrouted": (
                dict(result.route.unrouted) if result.route is not None else None
            ),
            "review": {
                "ran": result.review.ok,
                "note": None if result.review.ok else result.review.note(),
                "blockers": len(result.blockers),
            },
            "findings": findings,
        }
    )


DISPATCH: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "validate_circuit": _tool_validate_circuit,
    "build_board": _tool_build_board,
    "emit_kicad_pcb": _tool_emit_kicad_pcb,
    "place_parts": _tool_place_parts,
    "generate_footprint": _tool_generate_footprint,
    "simulate_circuit": _tool_simulate_circuit,
    "spice_capabilities": _tool_spice_capabilities,
    "generate_board": _tool_generate_board,
}


@dataclass(frozen=True)
class Toolset:
    """What one MCP endpoint exposes: its tools, their handlers, and what
    ``initialize`` says about the server.

    :func:`handle` serves the engine's own tools unless it is handed another
    set, which is how a front end (``alexabot/``) puts a different tool list
    behind the same protocol code -- the classification, the input and output
    validation, the rate limit -- without importing anything of its own into
    this module. ``tools`` and ``dispatch`` are read at call time, so a test
    that patches an entry sees it.

    ``instructions`` is ``InitializeResult.instructions`` (``schema.ts:290`` at
    ``38c84e9``): how to use this server, which a client may put in its
    system prompt. It is sent only when set, so the engine's ``initialize``
    answer is unchanged. ``verdict_tools`` is :data:`_VERDICT_TOOLS` for the
    set it belongs to.
    """

    tools: Sequence[dict[str, Any]]
    dispatch: Mapping[str, Callable[[dict[str, Any]], dict[str, Any]]]
    server_info: Mapping[str, str] = field(default_factory=lambda: dict(SERVER_INFO))
    instructions: str | None = None
    verdict_tools: frozenset[str] = frozenset()


#: The engine's tools. The *same* list and dict objects as :data:`TOOLS` and
#: :data:`DISPATCH`, so ``monkeypatch.setitem(DISPATCH, ...)`` still reaches a
#: call made through :func:`handle` with no toolset.
ENGINE = Toolset(TOOLS, DISPATCH, SERVER_INFO, None, _VERDICT_TOOLS)


# --------------------------------------------------------------------------
# JSON Schema: the subset the schemas above use
# --------------------------------------------------------------------------

#: The JSON Schema 2020-12 keywords :func:`schema_errors` implements -- the
#: ones ``TOOLS`` uses, walked and pinned by test_mcp.py -- plus the two
#: annotations, which constrain nothing. Meeting any other keyword raises
#: :class:`UnsupportedKeyword`: an ignored keyword is a constraint that
#: silently stopped holding.
SCHEMA_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "anyOf",
        "const",
        "enum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "properties",
        "required",
        "type",
        "default",
        "description",
    }
)


class UnsupportedKeyword(Exception):
    """A schema here uses a keyword the validator does not implement."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "number": _is_number,
    # 2020-12 types the value, not its spelling: 1.0 is an integer.
    "integer": lambda v: _is_number(v) and (isinstance(v, int) or v.is_integer()),
}


def _json_equal(a: Any, b: Any) -> bool:
    """Equality as JSON means it: ``1 == 1.0``, but ``true`` is not ``1``."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(map(_json_equal, a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    if _is_number(a) and _is_number(b):
        return a == b
    return type(a) is type(b) and a == b


def _show(value: Any) -> str:
    text = json.dumps(value, default=repr)
    return text if len(text) <= 60 else text[:57] + "..."


def schema_errors(instance: Any, schema: Any, where: str = "arguments") -> list[str]:
    """Every way ``instance`` fails ``schema``, each naming where; ``[]`` if none.

    Bounds are written the way jsonschema writes them -- "fails when
    ``x < minimum``" -- so ``NaN``, which compares false both ways, passes
    them here exactly as it does there.
    """
    if schema is True:
        return []
    if schema is False:
        return [f"{where}: no value is allowed here"]
    unknown = sorted(set(schema) - SCHEMA_KEYWORDS)
    if unknown:
        raise UnsupportedKeyword(f"{where}: the validator does not implement {unknown}")

    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_TYPE_CHECKS[name](instance) for name in names):
            # Nothing else about a value of the wrong type is worth saying.
            return [f"{where}: {_show(instance)} is not of type {' or '.join(names)}"]

    errors: list[str] = []
    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append(f"{where}: must be {_show(schema['const'])}")
    if "enum" in schema and not any(_json_equal(instance, v) for v in schema["enum"]):
        allowed = _show(schema["enum"])
        errors.append(f"{where}: {_show(instance)} is not one of {allowed}")

    if _is_number(instance):
        low = schema.get("minimum")
        if low is not None and instance < low:
            errors.append(f"{where}: {instance!r} is below the minimum {low}")
        above = schema.get("exclusiveMinimum")
        if above is not None and instance <= above:
            errors.append(f"{where}: {instance!r} must be greater than {above}")
        high = schema.get("maximum")
        if high is not None and instance > high:
            errors.append(f"{where}: {instance!r} is above the maximum {high}")

    if isinstance(instance, str) and len(instance) < schema.get("minLength", 0):
        errors.append(f"{where}: shorter than {schema['minLength']} character(s)")

    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{where}: fewer than {schema['minItems']} item(s)")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{where}: more than {schema['maxItems']} item(s)")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors += schema_errors(item, schema["items"], f"{where}[{index}]")

    if isinstance(instance, dict):
        for name in schema.get("required", ()):
            if name not in instance:
                errors.append(f"{where}: missing required property {name!r}")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for name, value in instance.items():
            if name in properties:
                errors += schema_errors(value, properties[name], f"{where}.{name}")
            elif extra is False:
                errors.append(f"{where}: unexpected property {name!r}")
            else:
                errors += schema_errors(value, extra, f"{where}.{name}")

    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            errors += _branch_errors(instance, schema[keyword], keyword, where)
    return errors


def _misses_discriminator(instance: Any, branch: Any) -> bool:
    """Whether a property the instance has is pinned by the branch
    (``const``/``enum``) to something else."""
    if not isinstance(instance, dict) or not isinstance(branch, dict):
        return False
    for name, sub in branch.get("properties", {}).items():
        if name not in instance or not isinstance(sub, dict):
            continue
        value = instance[name]
        if "const" in sub and not _json_equal(value, sub["const"]):
            return True
        if "enum" in sub and not any(_json_equal(value, v) for v in sub["enum"]):
            return True
    return False


def _branch_errors(
    instance: Any, branches: list[Any], keyword: str, where: str
) -> list[str]:
    results = [schema_errors(instance, branch, where) for branch in branches]
    matched = [i + 1 for i, errs in enumerate(results) if not errs]
    if matched and (keyword == "anyOf" or len(matched) == 1):
        return []
    if matched:
        return [
            f"{where}: matches allowed shapes {matched}, and exactly one is allowed"
        ]
    # Report the shape the caller most likely meant: first one whose
    # discriminator it matches (``kind: "tran"`` means the tran shape, even
    # though ``{"kind": "tran"}`` misses the op shape by one error and the tran
    # shape by two), then the one with the fewest failures.
    nearest = min(
        range(len(branches)),
        key=lambda i: (_misses_discriminator(instance, branches[i]), len(results[i])),
    )
    nearest = results[nearest]
    return [
        f"{where}: matches none of the {len(branches)} allowed shapes; "
        f"nearest: {'; '.join(nearest)}"
    ]


# --------------------------------------------------------------------------
# Rate limiting (server/tools.mdx:504: servers MUST rate limit tool calls)
# --------------------------------------------------------------------------

RATE_LIMIT_ENV = "MCP_TOOL_CALLS_PER_MINUTE"
DEFAULT_TOOL_CALLS_PER_MINUTE = 60


class RateLimiter:
    """A token bucket over ``tools/call``, shared by every transport in the process.

    ``per_minute`` calls may arrive in a burst; after that they refill evenly.
    A refusal says how long to wait, since the caller is usually a model that
    can read a number and try again. The clock is injectable for the tests.
    """

    def __init__(
        self, per_minute: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if (
            isinstance(per_minute, bool)
            or not isinstance(per_minute, int)
            or per_minute < 1
        ):
            raise ValueError(
                f"{RATE_LIMIT_ENV} must be a positive whole number, got {per_minute!r}"
            )
        self.per_minute = per_minute
        self._clock = clock
        self._tokens = float(per_minute)
        self._stamp = clock()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Any = None) -> RateLimiter:
        raw = (os.environ if env is None else env).get(RATE_LIMIT_ENV, "").strip()
        if not raw:
            return cls(DEFAULT_TOOL_CALLS_PER_MINUTE)
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(
                f"{RATE_LIMIT_ENV} must be a positive whole number, got {raw!r}"
            ) from None
        return cls(value)

    def acquire(self) -> float | None:
        """Take one call: ``None`` when allowed, else the seconds until one is."""
        rate = self.per_minute / 60.0
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._stamp)
            self._tokens = min(float(self.per_minute), self._tokens + elapsed * rate)
            self._stamp = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return None
            return (1.0 - self._tokens) / rate


#: The process-wide limiter. The entry points replace it from the environment
#: (:func:`configure_rate_limit`); a library caller of :func:`handle` gets the
#: default.
LIMITER = RateLimiter(DEFAULT_TOOL_CALLS_PER_MINUTE)


def configure_rate_limit(env: Any = None) -> None:
    """Read ``MCP_TOOL_CALLS_PER_MINUTE``; a bad value raises ``ValueError``."""
    global LIMITER
    LIMITER = RateLimiter.from_env(env)


# --------------------------------------------------------------------------
# JSON-RPC framing
# --------------------------------------------------------------------------

REQUEST = "request"
NOTIFICATION = "notification"
RESPONSE = "response"
INVALID = "invalid"

#: Marks an error whose request id could not be read. The field is then left
#: out, because ``null`` is not a ``RequestId`` in MCP (``schema.ts:124,163``).
_NO_ID = object()


class ParseError(ValueError):
    """Bytes that are not one UTF-8 JSON value: ``-32700``, with no id."""


def _refuse_constant(name: str) -> Any:
    # ``json.loads`` accepts NaN and Infinity, which are not JSON.
    raise ValueError(f"{name} is not a JSON value")


def decode(raw: bytes | str) -> Any:
    """One message's JSON value, from strict UTF-8 (``transports.mdx:9``)."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParseError(f"message is not UTF-8: {exc}") from None
    try:
        return json.loads(raw, parse_constant=_refuse_constant)
    except RecursionError:
        raise ParseError("invalid JSON: nested too deeply") from None
    except ValueError as exc:
        raise ParseError(f"invalid JSON: {exc}") from None


def dumps(message: Any) -> str:
    """One message as a line of JSON: ASCII, no ``NaN``, no embedded newline."""
    return json.dumps(_finite(message), allow_nan=False)


def _is_request_id(value: Any) -> bool:
    """``RequestId`` is a string or a number (``schema.ts:124``) and, per
    ``basic/index.mdx:48``, an integer when it is a number."""
    return isinstance(value, str) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def _classify(message: Any) -> tuple[str, str]:
    """``(kind, why)``: what the message is, and why when it is ``INVALID``."""
    if isinstance(message, list):
        return INVALID, "a JSON array (batch) is not a message; send one at a time"
    if not isinstance(message, dict):
        return INVALID, "a message must be a JSON object"
    if message.get("jsonrpc") != "2.0":
        return INVALID, "jsonrpc must be '2.0'"
    has_id = "id" in message
    if has_id and not _is_request_id(message["id"]):
        return INVALID, f"id must be a string or an integer, not {_show(message['id'])}"
    if "method" in message:
        if not isinstance(message["method"], str):
            return INVALID, "method must be a string"
        return (REQUEST if has_id else NOTIFICATION), ""
    if ("result" in message) == ("error" in message):
        return INVALID, "a message needs a method, or exactly one of result and error"
    if "result" in message and not has_id:
        return INVALID, "a result needs the id of the request it answers"
    return RESPONSE, ""


def classify(message: Any) -> str:
    """``REQUEST``, ``NOTIFICATION``, ``RESPONSE`` or ``INVALID``.

    The HTTP transport's status code depends on it (``transports.mdx:97-105``),
    so it is public; :func:`handle` applies the same rule.
    """
    return _classify(message)[0]


def _readable_id(message: Any) -> Any:
    if isinstance(message, dict) and _is_request_id(message.get("id")):
        return message["id"]
    return _NO_ID


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(
    req_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0"}
    if req_id is not _NO_ID:
        body["id"] = req_id
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    body["error"] = error
    return body


def error_without_id(code: int, message: str, data: Any = None) -> dict[str, Any]:
    """An error response for input whose id could not be read, which the spec
    lets a transport send with a refusal (``transports.mdx:81,100-102``)."""
    return _err(_NO_ID, code, message, data)


# --------------------------------------------------------------------------
# Methods
# --------------------------------------------------------------------------


def negotiate(requested: Any) -> str:
    """The version ``initialize`` answers (``basic/lifecycle.mdx:172-174``).

    python-sdk v2.2.0's expression, ``src/mcp/server/runner.py:425``.
    """
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def _initialize(
    req_id: Any, params: dict[str, Any], toolset: Toolset = ENGINE
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocolVersion": negotiate(params.get("protocolVersion")),
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": dict(toolset.server_info),
    }
    if toolset.instructions:
        result["instructions"] = toolset.instructions
    return _ok(req_id, result)


def _ping(
    req_id: Any, params: dict[str, Any], toolset: Toolset = ENGINE
) -> dict[str, Any]:
    return _ok(req_id, {})


def _tools_list(
    req_id: Any, params: dict[str, Any], toolset: Toolset = ENGINE
) -> dict[str, Any]:
    if params.get("cursor") is not None:
        # Every tool fits on one page and no nextCursor is ever issued, so
        # any cursor is one this server did not give out
        # (server/utilities/pagination.mdx:99).
        return _err(
            req_id, INVALID_PARAMS, "invalid cursor: this server never issues one"
        )
    return _ok(req_id, {"tools": list(toolset.tools)})


def _tools_call(
    req_id: Any, params: dict[str, Any], toolset: Toolset = ENGINE
) -> dict[str, Any]:
    # A malformed call fails the CallToolRequest schema, which is a protocol
    # error; everything after that is a tool execution error the model can
    # read and correct (server/tools.mdx:449-464).
    name = params.get("name")
    if not isinstance(name, str):
        return _err(req_id, INVALID_PARAMS, "tools/call needs 'name', a string")
    tool = next((t for t in toolset.tools if t.get("name") == name), None)
    handler = toolset.dispatch.get(name)
    if tool is None or handler is None:
        return _err(req_id, INVALID_PARAMS, f"unknown tool: {name!r}")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    elif not isinstance(arguments, dict):
        return _err(req_id, INVALID_PARAMS, "tools/call 'arguments' must be an object")
    return _ok(
        req_id,
        _call_tool(tool, handler, arguments, verdict_tools=toolset.verdict_tools),
    )


def _call_tool(
    tool: dict[str, Any],
    handler: Callable[[dict[str, Any]], dict[str, Any]],
    arguments: dict[str, Any],
    *,
    verdict_tools: frozenset[str] = _VERDICT_TOOLS,
) -> dict[str, Any]:
    wait_s = LIMITER.acquire()
    if wait_s is not None:
        return _error_result(
            f"rate limited: this server takes at most {LIMITER.per_minute} tool "
            f"calls a minute ({RATE_LIMIT_ENV}); try again in {wait_s:.1f} s"
        )
    if tool["name"] not in verdict_tools:
        problems = schema_errors(arguments, tool["inputSchema"])
        if problems:
            return _error_result("Input validation error: " + "; ".join(problems))
    try:
        result = handler(arguments)
    except ValidationError as exc:
        # A malformed circuit is the caller's problem to fix, not a crash.
        return _error_result("; ".join(exc.errors))
    except Exception as exc:
        return _error_result(f"{type(exc).__name__}: {exc}")

    schema = tool.get("outputSchema")
    if schema is None or result.get("isError"):
        return result
    # python-sdk's check and its words (lowlevel/server.py:566-575). Both of
    # its clients raise on a tool with an outputSchema whose success result
    # carries no structuredContent, so this is a failure here, not a warning.
    if "structuredContent" not in result:
        return _error_result(
            "Output validation error: outputSchema defined but no structured "
            "output returned"
        )
    problems = schema_errors(result["structuredContent"], schema, "structuredContent")
    if problems:
        return _error_result("Output validation error: " + "; ".join(problems))
    return result


_METHODS: dict[str, Callable[[Any, dict[str, Any], Toolset], dict[str, Any]]] = {
    "initialize": _initialize,
    "ping": _ping,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
}


def handle(
    message: Any, *, batched: bool = False, toolset: Toolset | None = None
) -> dict[str, Any] | None:
    """Map one JSON-RPC message to its response.

    Returns ``None`` when the message gets no reply: a notification
    (``basic/index.mdx:98``) or a response the client sent. Neither reaches a
    method, so a ``tools/call`` without an id is dropped, never run. Known
    notifications (``notifications/initialized``, ``notifications/cancelled``)
    need no action here, because handling is synchronous and nothing is left
    to cancel; an invalid one is ignored, as ``cancellation.mdx:79`` asks.

    ``batched`` is set by the HTTP transport for the elements of a 2025-03-26
    batch, where ``initialize`` is not allowed
    (``2025-03-26/basic/lifecycle.mdx:74-75``).

    ``toolset`` is what this endpoint exposes; ``None`` is :data:`ENGINE`.
    """
    kind, why = _classify(message)
    if kind == INVALID:
        return _err(_readable_id(message), INVALID_REQUEST, why)
    if kind != REQUEST:
        return None

    req_id = message["id"]
    method = message["method"]
    params = message.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return _err(req_id, INVALID_PARAMS, "params must be an object")
    if batched and method == "initialize":
        return _err(req_id, INVALID_REQUEST, "initialize must not be part of a batch")
    route = _METHODS.get(method)
    if route is None:
        return _err(req_id, METHOD_NOT_FOUND, f"unknown method: {method!r}")
    try:
        return route(req_id, params, ENGINE if toolset is None else toolset)
    except Exception as exc:
        # A bug in a method must still answer the request, not take the
        # transport down with it.
        return _err(req_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")


#: Requests the stdio loop hands to a thread of their own. python-sdk runs
#: every request as its own task (v1.30.0 ``src/mcp/server/lowlevel/server.py:679-690``;
#: v2.2.0 ``src/mcp/shared/jsonrpc_dispatcher.py:592-606``) and keeps a set of
#: ``inline_methods`` on the read loop instead (v2 ``:284-286``; ``initialize``
#: alone, ``src/mcp/server/runner.py:496``). The set is inverted here, because
#: ``tools/call`` is the only method that runs tool code: every other one
#: answers from constant data, so it stays inline and in arrival order, and
#: a thread per ping would buy nothing.
SPAWNED_METHODS = frozenset({"tools/call"})


class Server:
    """Line-delimited JSON-RPC over a pair of streams.

    With no streams given it reads bytes from stdin and decodes each line as
    strict UTF-8 itself, and writes UTF-8 to stdout (python-sdk's
    ``TextIOWrapper(sys.stdout.buffer, encoding="utf-8")``, ``stdio.py:49``),
    whatever the locale says.

    A ``tools/call`` runs on its own thread, so the loop keeps reading while
    it works: a ping MUST be answered promptly (``basic/utilities/ping.mdx:31``
    at ``38c84e9``), and one queued behind a two-minute ``generate_board``
    was not. Replies go out in the order they finish, each a whole line under
    one lock; a client matches them by id. That is the concurrency the HTTP
    transport already has (a thread per connection), over the same
    :func:`handle`, its limiter and its one-``generate_board`` guard -- which
    means a stdio client that pipelines two boards now gets one board and one
    ``busy:`` error, where before this loop queued the second run.
    """

    def __init__(self, stdin=None, stdout=None):
        if stdin is None:
            stdin = getattr(sys.stdin, "buffer", sys.stdin)
        if stdout is None:
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is not None:
                stdout = io.TextIOWrapper(buffer, encoding="utf-8")
            else:
                stdout = sys.stdout
        self.stdin = stdin
        self.stdout = stdout
        self._write_lock = threading.Lock()

    def serve_forever(self) -> None:
        workers: list[threading.Thread] = []
        # stdout carries only MCP messages (transports.mdx:35): tool code that
        # prints lands on stderr. The channel is ``self.stdout``, captured
        # before this swap, so it is unaffected. The swap is process-wide, so
        # it spans the whole loop: one per call would be undone by whichever
        # of two concurrent calls finished first.
        with contextlib.redirect_stdout(sys.stderr):
            for line in self.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = decode(line)
                except ParseError as exc:
                    self._write(_err(_NO_ID, PARSE_ERROR, str(exc)))
                    continue
                kind = classify(message)
                if kind == REQUEST and message["method"] in SPAWNED_METHODS:
                    workers = [worker for worker in workers if worker.is_alive()]
                    worker = threading.Thread(
                        target=self._answer,
                        args=(message,),
                        name=f"mcp-tools-call-{message['id']}",
                        daemon=True,
                    )
                    worker.start()
                    workers.append(worker)
                else:
                    self._answer(message)
            # End of input. The calls already read are finished and answered,
            # as the sequential loop did, because ``printf ... | silkscreen-mcp``
            # waits on those answers. python-sdk cancels them instead
            # (v1 ``lowlevel/server.py:691-696``); a thread cannot be
            # cancelled, and a client that wants it gone sooner sends SIGTERM,
            # which ``basic/lifecycle.mdx:236-238`` tells it to.
            for worker in workers:
                worker.join()

    def _answer(self, message: Any) -> None:
        response = handle(message)
        if response is not None:
            self._write(response)

    def _write(self, payload: dict[str, Any]) -> None:
        line = dumps(payload) + "\n"
        with self._write_lock:
            self.stdout.write(line)
            self.stdout.flush()


def main() -> int:
    try:
        configure_rate_limit()
    except ValueError as exc:
        print(f"silkscreen-mcp: {exc}", file=sys.stderr)
        return 2
    Server().serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
