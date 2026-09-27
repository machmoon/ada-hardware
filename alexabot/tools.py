"""The six MCP tools a voice agent calls, and how each result is shaped.

Every success carries ``structuredContent`` -- the data, checked against the
tool's ``outputSchema`` by :func:`silkscreen.mcp.server.handle` after the
handler returns -- and two text blocks: first the ``speech`` string alone,
then the same object as indented JSON. The first is KayLerch's sample-server
shape, where the text is the spoken line and ``structuredContent`` the data
(``examples/national-parks-mcp-server/src/tools.ts`` in
KayLerch/alexa-skill-mcp-bridge at ``ca2c2ef``); the second is the spec's
SHOULD for clients older than 2025-06-18 (``server/tools.mdx:322``). The
bridge joins every text block (``packages/agent/src/mcp/result.ts``), so an
agent that reads text still hears the sentence first.

A tool execution error (``isError``, ``tools.mdx:458-464``) is one text
block: a sentence to speak when the person caused it, a correction when the
agent sent bad arguments, and never exception text -- the bridge's
``prompts/tool-result.md`` tells its agent "Do not repeat the error text",
and the safest error text is the one never sent.

Descriptions lead with a sentence that stands alone, because the bridge
builds its tool list from each description's first sentence
(``packages/agent/src/agent/prompt.ts`` ``formatToolList``). Schemas use only
the keywords the engine's validator implements (``SCHEMA_KEYWORDS``); there is
no ``maxLength`` or ``pattern``, so the handlers check lengths and alphabets
themselves and every description states the bound.

The principal comes from :func:`silkscreen.mcp.http.get_access_token`, which
the HTTP transport sets from ``verify_token`` (:mod:`alexabot.auth`); a call
with none -- a direct ``handle`` in a test -- is the ``local`` account. The
JSON-RPC id never reaches a handler: ``handle`` passes the arguments alone.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import sys
from collections.abc import Callable
from typing import Any

from silkscreen.mcp.http import get_access_token
from silkscreen.mcp.server import Toolset

from . import speech
from .runner import MAX_ANSWER_CHARS, MAX_INTENT_CHARS, REQUEST_ID, Refusal, Runner
from .store import STATES, Board

__all__ = [
    "INSTRUCTIONS",
    "SERVER_INFO",
    "TOOLS",
    "status_view",
    "toolset",
]

SERVER_INFO = {"name": "ada-voice", "title": "Ada", "version": "0.1.0"}

#: ``InitializeResult.instructions``; the bridge puts it in the agent's
#: system prompt as ``serverInstructions`` (``prompt.ts``).
INSTRUCTIONS = (
    "Ada designs printed circuit boards by voice and checks them. Work runs in "
    "the background: start_board_design, answer_design_questions and "
    "continue_design return at once, and board_status says where a board "
    "stands, including the one question Ada needs answered. Speak the speech "
    "field; it already follows voice rules. Mint a new UUID as request_id for "
    "each new board and reuse it only to retry that same call. Ada drafts and "
    "checks boards; it never orders or buys anything."
)

#: Seconds to wait before the next ``board_status`` in each working state.
POLL_AFTER_S = {"reading": 5, "proposing": 10, "placing": 3, "routing": 3,
                "reviewing": 5}
_WORKING = frozenset(POLL_AFTER_S)

# -- schemas ----------------------------------------------------------------------


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [{"type": "null"}, schema]}


def _closed(properties: dict[str, Any], required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties) if required is None else required,
        "properties": properties,
    }


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_COUNT_OR_NULL = {"type": ["integer", "null"], "minimum": 0}
_REVIEW_STATUS = {"type": "string", "enum": ["not_run", "ok", "failed"]}

SUMMARY = _closed(
    {
        "parts": _COUNT_OR_NULL,
        "nets": _COUNT_OR_NULL,
        "board_mm": _nullable(
            {"type": "array", "items": {"type": "number"}, "minItems": 2,
             "maxItems": 2}
        ),
        "placement_status": {"type": ["string", "null"]},
        "routed_fraction": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "unrouted": _nullable(
            {"type": "array", "items": _closed({"net": _STRING, "reason": _STRING})}
        ),
        "review": _closed(
            {
                "status": _REVIEW_STATUS,
                "detail": {"type": ["string", "null"]},
                "findings": _COUNT_OR_NULL,
                "blockers": _COUNT_OR_NULL,
            }
        ),
        "findings": {
            "type": "array",
            "items": _closed(
                {
                    "number": {"type": "integer", "minimum": 1},
                    "severity": _STRING,
                    "title": _STRING,
                    "refs": _STRINGS,
                }
            ),
        },
        "datasheets_read": {"type": "integer", "minimum": 0},
        "files": _closed(
            {
                "schematic": {"type": ["string", "null"]},
                "board": {"type": ["string", "null"]},
                "project": {"type": ["string", "null"]},
            }
        ),
        "notes": _STRINGS,
    }
)

STATUS = _closed(
    {
        "session_id": _STRING,
        "state": {"type": "string", "enum": list(STATES)},
        "speech": {
            "type": "string",
            "description": "Say this to the person, as it is.",
        },
        "intent": _STRING,
        "replayed": {"type": "boolean"},
        "scripted": {"type": "boolean"},
        "elapsed_s": {
            "type": "number",
            "minimum": 0,
            "description": "Seconds since the board was started; for a finished "
            "board, how long it took.",
        },
        "stalled": {"type": "boolean"},
        "poll_after_s": {"type": ["integer", "null"], "minimum": 1},
        "question": _nullable(
            _closed(
                {
                    "index": {"type": "integer", "minimum": 0},
                    "ask": _STRING,
                    "default": _STRING,
                    "remaining": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Open questions after this one.",
                    },
                }
            )
        ),
        "answers": {
            "type": "array",
            "items": _closed(
                {
                    "index": {"type": "integer", "minimum": 0},
                    "ask": _STRING,
                    "answer": _STRING,
                    "source": {"type": "string", "enum": ["user", "default"]},
                }
            ),
        },
        "failure": _nullable(
            _closed(
                {
                    "stage": {
                        "type": "string",
                        "enum": ["reading", "proposing", "placing", "routing",
                                 "reviewing", "restart"],
                    },
                    "reason": _STRING,
                }
            )
        ),
        "summary": _nullable(SUMMARY),
    }
)

EXPLAIN = _closed(
    {
        "session_id": _STRING,
        "which": {"type": "integer", "minimum": 1},
        "count": _COUNT_OR_NULL,
        "review_status": _REVIEW_STATUS,
        "finding": _nullable(
            _closed(
                {
                    "number": {"type": "integer", "minimum": 1},
                    "severity": _STRING,
                    "title": _STRING,
                    "detail": _STRING,
                    "parts": _STRINGS,
                    "refs": _STRINGS,
                    "suggested_fix": {"type": ["string", "null"]},
                    "citation": {"type": ["string", "null"]},
                }
            )
        ),
        "speech": _STRING,
    }
)

RECALL = _closed(
    {
        "query": {"type": ["string", "null"]},
        "total": {"type": "integer", "minimum": 0},
        "speech": _STRING,
        "boards": {
            "type": "array",
            "items": _closed(
                {
                    "session_id": _STRING,
                    "intent": _STRING,
                    "state": _STRING,
                    "created_at": {"type": "string", "description": "ISO 8601, UTC"},
                    "updated_at": {"type": "string", "description": "ISO 8601, UTC"},
                    "headline": _STRING,
                    "routed_fraction": {"type": ["number", "null"]},
                    "unrouted": _COUNT_OR_NULL,
                    "blockers": _COUNT_OR_NULL,
                    "review_status": _REVIEW_STATUS,
                }
            ),
        },
    }
)

_SESSION = {"type": "string", "minLength": 1,
            "description": "The session_id a previous call returned."}

_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
          "openWorldHint": True}
_READ = {"readOnlyHint": True, "openWorldHint": False}


def _tool(name, title, description, input_schema, output_schema, hints):
    """One ``Tool``, titled twice (2025-06-18 ``title`` and 2025-03-26
    ``annotations.title``), the engine's ``_tool`` shape."""
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": input_schema,
        "outputSchema": output_schema,
        "annotations": {"title": title, **hints},
    }


TOOLS: list[dict[str, Any]] = [
    _tool(
        "start_board_design",
        "Start a board design",
        "Start designing a printed circuit board from what the user asked for; "
        "it returns at once while Ada plans the board in the background. Call "
        "board_status next to hear progress and the question Ada needs "
        "answered. request_id is a UUID you mint once for this board (8 to 64 "
        "characters of letters, digits, dash, underscore, dot or colon); "
        "resend the same request_id only to retry this same call.",
        _closed(
            {
                "intent": {
                    "type": "string",
                    "minLength": 3,
                    "description": "What the board should do, in the user's "
                    f"words; at most {MAX_INTENT_CHARS} characters.",
                },
                "request_id": {
                    "type": "string",
                    "minLength": 8,
                    "description": "A UUID minted once per new board; resend "
                    "unchanged only to retry.",
                },
            }
        ),
        STATUS,
        _WRITE,
    ),
    _tool(
        "answer_design_questions",
        "Answer Ada's design question",
        "Give Ada the user's answer to the question board_status asked, or let "
        "Ada choose. Send answers as {index, answer}, usually one per call; set "
        "you_choose to true when the user leaves the remaining questions to "
        "Ada, and each takes the default Ada stated. When every question has an "
        "answer or a default, drafting the schematic starts in the background "
        "and this returns at once.",
        _closed(
            {
                "session_id": _SESSION,
                "answers": {
                    "type": "array",
                    "maxItems": 4,
                    "default": [],
                    "items": _closed(
                        {
                            "index": {"type": "integer", "minimum": 0, "maximum": 3},
                            "answer": {
                                "type": "string",
                                "minLength": 1,
                                "description": f"At most {MAX_ANSWER_CHARS} "
                                "characters.",
                            },
                        }
                    ),
                },
                "you_choose": {"type": "boolean", "default": False},
            },
            required=["session_id"],
        ),
        STATUS,
        _WRITE,
    ),
    _tool(
        "continue_design",
        "Place, route and review",
        "Place, route and review a drafted board in the background; it returns "
        "at once. Call it when the user agrees after board_status says the "
        "schematic is drafted; called while Ada is still planning or drafting, "
        "it is queued and runs as soon as the schematic is ready.",
        _closed({"session_id": _SESSION}),
        STATUS,
        _WRITE,
    ),
    _tool(
        "board_status",
        "Board status",
        "Where a board stands: its state, one sentence to speak, the next "
        "question if Ada is waiting on the user, and, once finished, the parts, "
        "how much is routed with every unrouted net named, and what the design "
        "review found. Leave session_id out for the user's most recent board. "
        "While work is running, call again after poll_after_s seconds.",
        _closed({"session_id": _SESSION}, required=[]),
        STATUS,
        _READ,
    ),
    _tool(
        "explain_finding",
        "Explain a review finding",
        "Explain one design-review finding of a finished board in plain speech, "
        "naming the parts it involves. which is the finding's number as "
        "board_status lists them, starting at 1.",
        _closed(
            {
                "session_id": _SESSION,
                "which": {"type": "integer", "minimum": 1, "default": 1},
            },
            required=["session_id"],
        ),
        EXPLAIN,
        _READ,
    ),
    _tool(
        "recall_my_boards",
        "Recall my boards",
        "List this user's earlier boards, newest first, with how each one "
        "ended, including boards from earlier conversations. query, if given, "
        "matches words in the original request, such as 'USB-C' or "
        "'regulator'.",
        _closed(
            {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10,
                          "default": 3},
            },
            required=[],
        ),
        RECALL,
        _READ,
    ),
]

# -- views ------------------------------------------------------------------------


def _iso(ts: float) -> str:
    stamp = datetime.datetime.fromtimestamp(ts, tz=datetime.UTC)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


_REVIEW_KEYS = ("status", "detail", "findings", "blockers")


def _public_summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """The stored summary as ``SUMMARY`` shows it: each finding by its number,
    severity, title and refs (``explain_finding`` has the rest), and the
    review without ``error``, the exception text a raising review left in the
    row (:func:`speech.cause_text` says why it stays there)."""
    if summary is None:
        return None
    view = dict(summary)
    view["findings"] = [
        {k: f[k] for k in ("number", "severity", "title", "refs")}
        for f in summary.get("findings") or []
    ]
    review = summary.get("review")
    if isinstance(review, dict):
        view["review"] = {k: review.get(k) for k in _REVIEW_KEYS}
    return view


#: ``failure_reason`` as :meth:`Runner._fail` writes it: ``"<Class>: <text>"``.
_CLASS_PREFIX = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*): ")


def _public_reason(board: Board) -> str:
    """``failure.reason`` as it is sent: the class and the cause in words.

    A restart's reason is this package's own sentence (it names no
    exception), so it is sent as written; anything else loses its message
    (:func:`speech.cause_text`).
    """
    if board.failure_cause == "restart":
        return board.failure_reason or speech.CAUSE_WORDS["restart"]
    if not board.failure_reason and not board.failure_cause:
        return "no reason was recorded"
    match = _CLASS_PREFIX.match(board.failure_reason or "")
    return speech.cause_text(match.group(1) if match else "", board.failure_cause)


def _question(board: Board) -> dict[str, Any] | None:
    if board.state != "questions":
        return None
    open_ = [i for i in range(len(board.questions)) if str(i) not in board.answers]
    if not open_:
        return None
    index = open_[0]
    q = board.questions[index]
    return {"index": index, "ask": q["ask"], "default": q["default"],
            "remaining": len(open_) - 1}


def _answers(board: Board) -> list[dict[str, Any]]:
    decided = board.state not in ("reading", "questions")
    out = []
    for index, q in enumerate(board.questions):
        answer = board.answers.get(str(index))
        if answer:
            out.append({"index": index, "ask": q["ask"], "answer": answer,
                        "source": "user"})
        elif decided:
            out.append({"index": index, "ask": q["ask"], "answer": q["default"],
                        "source": "default"})
    return out


def status_view(
    board: Board,
    now: float,
    *,
    stall_after_s: float,
    replayed: bool = False,
    created: bool = False,
    acknowledged: bool = False,
    you_chose: bool = False,
) -> dict[str, Any]:
    """The ``STATUS`` object for ``board``: the row, and the words for it."""
    working = board.state in _WORKING
    stalled = working and now - board.state_since > stall_after_s
    question = _question(board)
    failure = (
        {"stage": board.failure_stage or "reading",
         "reason": _public_reason(board)}
        if board.state == "failed" else None
    )
    words = speech.status_speech(
        board.state,
        question=question,
        total_questions=len(board.questions),
        answered=sum(1 for i in range(len(board.questions))
                     if str(i) in board.answers),
        acknowledged=acknowledged,
        summary=board.summary,
        failure=None if failure is None else {**failure,
                                              "cause": board.failure_cause},
        stalled=stalled,
        queued=board.continue_asked,
        you_chose=you_chose,
    )
    if created:
        words = speech.start_speech(scripted=board.scripted)
    elif replayed:
        words = speech.replay_speech(words)
    parked = working or board.state in ("questions", "drafted")
    end = now if parked else board.updated_at
    return {
        "session_id": board.session_id,
        "state": board.state,
        "speech": words,
        "intent": board.intent,
        "replayed": replayed,
        "scripted": board.scripted,
        "elapsed_s": round(max(0.0, end - board.created_at), 1),
        "stalled": stalled,
        "poll_after_s": POLL_AFTER_S.get(board.state),
        "question": question,
        "answers": _answers(board),
        "failure": failure,
        "summary": _public_summary(board.summary),
    }


def _review_status(board: Board) -> str:
    return str(((board.summary or {}).get("review") or {}).get("status") or "not_run")


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def _result(structured: dict[str, Any]) -> dict[str, Any]:
    clean = _finite(structured)
    return {
        "content": [
            {"type": "text", "text": clean["speech"]},
            {"type": "text", "text": json.dumps(clean, indent=2, allow_nan=False)},
        ],
        "structuredContent": clean,
        "isError": False,
    }


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _account() -> str:
    token = get_access_token()
    return token.subject if token is not None else "local"


def _guarded(
    name: str, body: Callable[[dict[str, Any]], dict[str, Any]]
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """``body`` with its refusals as tool errors and anything else as one
    fixed sentence -- the exception's class goes to stderr, its text nowhere."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return body(arguments)
        except Refusal as refusal:
            return _error(refusal.text)
        except Exception as exc:  # noqa: BLE001 -- never raw text to a voice
            print(f"[alexabot] {name} raised {type(exc).__name__}", file=sys.stderr)
            return _error(speech.REFUSALS["broken"])

    handler.__name__ = f"tool_{name}"
    return handler


def toolset(runner: Runner) -> Toolset:
    """The six tools, bound to ``runner``, as one endpoint's ``Toolset``."""

    def view(reply, **flags) -> dict[str, Any]:
        return status_view(
            reply.board,
            runner.now(),
            stall_after_s=runner.stall_after_s,
            replayed=reply.replayed,
            created=reply.created,
            acknowledged=reply.acknowledged,
            you_chose=reply.you_chose,
            **flags,
        )

    def start(args: dict[str, Any]) -> dict[str, Any]:
        intent = " ".join(str(args["intent"]).split())
        if len(intent) < 3:
            raise Refusal("intent must say what the board should do.", speakable=False)
        if len(intent) > MAX_INTENT_CHARS:
            raise Refusal(
                f"intent is {len(intent)} characters; shorten it to at most "
                f"{MAX_INTENT_CHARS}.",
                speakable=False,
            )
        request_id = str(args["request_id"])
        if not REQUEST_ID.match(request_id):
            raise Refusal(
                "request_id must be 8 to 64 characters of letters, digits, dash, "
                "underscore, dot or colon; mint a UUID.",
                speakable=False,
            )
        return _result(view(runner.start(_account(), request_id, intent)))

    def answer(args: dict[str, Any]) -> dict[str, Any]:
        answers = list(args.get("answers") or [])
        for item in answers:
            if len(str(item["answer"]).strip()) > MAX_ANSWER_CHARS:
                raise Refusal(
                    f"each answer is at most {MAX_ANSWER_CHARS} characters; "
                    "shorten it.",
                    speakable=False,
                )
            if not str(item["answer"]).strip():
                raise Refusal("an answer cannot be blank.", speakable=False)
        reply = runner.answer(
            _account(), str(args["session_id"]), answers,
            bool(args.get("you_choose", False)),
        )
        return _result(view(reply))

    def continue_(args: dict[str, Any]) -> dict[str, Any]:
        reply = runner.continue_design(_account(), str(args["session_id"]))
        return _result(view(reply))

    def status(args: dict[str, Any]) -> dict[str, Any]:
        sid = args.get("session_id")
        board = runner.board(_account(), None if sid is None else str(sid))
        return _result(
            status_view(board, runner.now(), stall_after_s=runner.stall_after_s)
        )

    def explain(args: dict[str, Any]) -> dict[str, Any]:
        board = runner.board(_account(), str(args["session_id"]))
        which = int(args.get("which", 1))
        review_status = _review_status(board)
        findings = (board.summary or {}).get("findings") or []
        count: int | None = len(findings) if review_status == "ok" else None
        finding = None
        if count:
            if which > count:
                raise Refusal(
                    f"This board has {count} findings; which runs 1 to {count}.",
                    speakable=False,
                )
            stored = findings[which - 1]
            finding = {
                "number": stored["number"],
                "severity": stored["severity"],
                "title": stored["title"],
                "detail": stored.get("detail") or "",
                "parts": list(stored.get("parts") or []),
                "refs": list(stored.get("refs") or []),
                "suggested_fix": stored.get("suggested_fix"),
                "citation": stored.get("citation"),
            }
        return _result(
            {
                "session_id": board.session_id,
                "which": which,
                "count": count,
                "review_status": review_status,
                "finding": finding,
                "speech": speech.explain_speech(
                    finding, number=which, count=count, review_status=review_status
                ),
            }
        )

    def recall(args: dict[str, Any]) -> dict[str, Any]:
        query = " ".join(str(args.get("query") or "").split()) or None
        limit = int(args.get("limit", 3))
        boards, total, total_all = runner.recall(_account(), query, limit)
        now = runner.now()
        listed = []
        for board in boards:
            summary = board.summary or {}
            unrouted = summary.get("unrouted")
            listed.append(
                {
                    "session_id": board.session_id,
                    "intent": board.intent,
                    "state": board.state,
                    "created_at": _iso(board.created_at),
                    "updated_at": _iso(board.updated_at),
                    "headline": speech.headline(
                        board.state, board.summary, board.failure_stage
                    ),
                    "routed_fraction": summary.get("routed_fraction"),
                    "unrouted": None if unrouted is None else len(unrouted),
                    "blockers": (summary.get("review") or {}).get("blockers"),
                    "review_status": _review_status(board),
                }
            )
        spoken = speech.recall_speech(
            [
                {"intent": b.intent, "state": b.state, "summary": b.summary,
                 "failure_stage": b.failure_stage, "created_at": b.created_at}
                for b in boards
            ],
            total=total,
            query=query,
            total_all=total_all,
            now=now,
        )
        return _result({"query": query, "total": total, "speech": spoken,
                        "boards": listed})

    bodies = {
        "start_board_design": start,
        "answer_design_questions": answer,
        "continue_design": continue_,
        "board_status": status,
        "explain_finding": explain,
        "recall_my_boards": recall,
    }
    return Toolset(
        tools=TOOLS,
        dispatch={name: _guarded(name, body) for name, body in bodies.items()},
        server_info=SERVER_INFO,
        instructions=INSTRUCTIONS,
        verdict_tools=frozenset(),
    )
