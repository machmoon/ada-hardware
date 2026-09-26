"""The voice session's state machine, and the only module that runs the pipeline.

A voice agent cannot wait. Alexa+ answers a person within seconds, CloudFront
times an origin out at thirty, and the pipeline behind one board takes
minutes: the 2026-09-24 rehearsal measured planning at 30 s and proposing at
42 s. So no tool here waits on a model or on the solver (reviewer finding
B1). A tool call does three things at most -- validate, read or write one
SQLite row, start a thread -- and the work runs on that thread, through the
same ``service/steps.py`` stage bodies the desktop's step routes use. The
agent learns how it is going by calling ``board_status``, which reads the
row and nothing else.

The start/poll shape is deliberate, not a stopgap. MCP 2025-11-25 carries
tasks in core, but python-sdk marks them **deferred** -- "Tasks ship in
2026-07-28 as SEP-2663 ... wire-incompatible with the 2025-11-25 in-core
design" (``examples/stories/tasks/README.md``, read at main ``f1b6589``) -- so
no tool declares ``execution`` and nothing here depends on a client that
speaks them. The poll is KayLerch/alexa-skill-mcp-bridge's (``docs/
decisions.md`` D8, D9 at ``ca2c2ef``): the agent answers "pending" inside its
deadline and a later call returns the stored result.

States, and the ``steps`` call each worker makes (on the worker, never the
request thread)::

    reading    start_once(plan_first, prefetch off)   -> questions | proposing
    questions  (parked; answer_design_questions)       -> proposing
    proposing  advance(propose, {answers})             -> drafted
    drafted    (parked; continue_design)               -> placing
    placing    advance(place)                          -> routing
    routing    advance(route)                          -> reviewing
    reviewing  advance(review)                         -> done
    failed     any exception before the board existed, or a restart

**The idempotency key is the account plus a ``request_id`` the agent mints**
(reviewer finding M1). python-sdk numbers JSON-RPC requests per connection
from zero, so every session sends ids 1, 2, 3 and a key built from them
collides across people; the handlers here never see the JSON-RPC id at all.
The key is a ``UNIQUE (account, request_id)`` row that survives a restart,
and it reaches ``steps.start_once`` as ``alexa:<account>:<request_id>`` so it
cannot collide with the desktop's keys either. A retried start with the same
key returns the same board, whatever became of it: a background failure does
not free the key, because the call itself succeeded -- it made a board -- and
the failure is that board's history. A retry that sends a different request
under a used key is refused, the IETF idempotency-key draft's key-reuse rule
(``draft-ietf-httpapi-idempotency-key-header.md`` at ``dab060c``).

**Honesty.** A failure before the board exists is state ``failed`` with the
stage, the cause by exception class, and the exception text in
``failure_reason`` -- never a quiet zero. That text stays in the row: a tool
sends the class and the cause in words (``speech.cause_text``), because it
can quote a provider's error or a URL with a key in it. A review that raises
after routing is ``done`` with the review ``failed``, not ``failed``: the
routed board is the product (``steps.py``'s rule for the case and the BOM),
and "failed" would hide it. A worker that hangs cannot be killed, so a status past
:data:`STALL_AFTER_S` in one working state says it may be stuck.

``service.steps``, ``service.app`` and ``service.cache`` are imported in
:meth:`Runner.warm`, at startup, so no tool call pays for the import.
"""

from __future__ import annotations

import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import speech
from .store import TERMINAL_STATES, Board, BoardStore

__all__ = [
    "MAX_ANSWER_CHARS",
    "MAX_INTENT_CHARS",
    "REQUEST_ID",
    "STALL_AFTER_S",
    "Refusal",
    "Reply",
    "Runner",
    "empty_summary",
]

#: How long one working state may last before a status says it may be stuck.
STALL_AFTER_S = 300.0
#: The ``AccountId`` alphabet (``billing/accounts.py``), 8 to 64 long: a UUID
#: fits, and so does anything an agent is likely to mint.
REQUEST_ID = re.compile(r"\A[A-Za-z0-9_.:-]{8,64}\Z")
MAX_INTENT_CHARS = 500
#: ``agents/plan.py`` ``LINE_MAX_CHARS``: longer answers are cut there anyway.
MAX_ANSWER_CHARS = 200
DEFAULT_MAX_ACTIVE = 4


class Refusal(Exception):
    """A tool call that cannot be done, in words.

    ``speakable`` refusals are for the person and the agent reads them aloud;
    the others correct the agent's arguments. Neither ever carries exception
    text.
    """

    def __init__(self, text: str, *, speakable: bool = True) -> None:
        super().__init__(text)
        self.text = text
        self.speakable = speakable


@dataclass(frozen=True)
class Reply:
    """A board plus how the call that produced it should be spoken."""

    board: Board
    replayed: bool = False
    created: bool = False
    acknowledged: bool = False
    you_chose: bool = False


@dataclass
class VoiceRun:
    """In-memory handles for one board's worker; the row holds the facts."""

    session_id: str
    account: str
    request_id: str
    intent: str
    steps_session: str | None = None
    model: Any = None
    #: Worker threads alive for this board. A count, not a flag: the plan
    #: thread's exit and the propose thread's start can interleave.
    workers: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def busy(self) -> bool:
        return self.workers > 0


def empty_summary(notes: Sequence[str] = ()) -> dict[str, Any]:
    """The summary before anything is measured: every unknown is ``None``.

    ``datasheets_read`` is a real ``0``, not a placeholder -- a URL cannot be
    spoken, so a voice run reads none, and the speech must not imply it did.
    """
    return {
        "parts": None,
        "nets": None,
        "board_mm": None,
        "placement_status": None,
        "routed_fraction": None,
        "unrouted": None,
        "review": {"status": "not_run", "detail": None, "findings": None,
                   "blockers": None},
        "findings": [],
        "datasheets_read": 0,
        "files": {"schematic": None, "board": None, "project": None},
        "notes": list(notes),
    }


def _same_intent(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


_SEVERITY_RANK = {"blocker": 0, "marginal": 1, "note": 2}


def _numbered(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Blocker, marginal, note -- stably, an unknown severity as a note
    (``frontend/src/lib/severity.js`` ``SEVERITY_ORDER``) -- numbered from 1;
    that number is what ``explain_finding`` takes."""
    ranked = sorted(
        findings,
        key=lambda f: _SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 2),
    )
    return [
        {
            "number": index,
            "severity": str(f.get("severity", "")),
            "title": str(f.get("title", "")),
            "refs": [str(r) for r in f.get("refs") or []],
            "detail": str(f.get("detail") or ""),
            "parts": [str(p) for p in f.get("parts") or []],
            "suggested_fix": f.get("suggested_fix") or None,
            "citation": f.get("citation") or None,
        }
        for index, f in enumerate(ranked, start=1)
    ]


class Runner:
    """Starts, advances and reports voice boards.

    ``steps`` is ``service.steps`` or a stand-in with the same
    ``start_once``/``advance`` signatures (the tests' seam);
    ``model_factory`` builds one model per board, on the worker thread, so
    even constructing the provider ladder never happens on a request.
    """

    def __init__(
        self,
        store: BoardStore,
        model_factory: Callable[[], Any],
        fact_store: Any = None,
        *,
        steps: Any = None,
        max_active: int = DEFAULT_MAX_ACTIVE,
        scripted: bool = False,
        clock: Callable[[], float] = time.time,
        stall_after_s: float = STALL_AFTER_S,
    ) -> None:
        self.store = store
        self.scripted = scripted
        self.max_active = max_active
        self.stall_after_s = stall_after_s
        self._model_factory = model_factory
        self._fact_store = fact_store
        self._steps = steps
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, VoiceRun] = {}
        self._threads: set[threading.Thread] = set()

    def warm(self) -> Runner:
        """Import the pipeline now, at startup, so no tool call pays for it."""
        if self._steps is None:
            from service import steps

            self._steps = steps
        # steps imports these inside its stage bodies; importing them here
        # moves that cost off the first worker, too.
        import service.app  # noqa: F401

        if self._fact_store is None:
            from service.cache import MemoryFactStore

            self._fact_store = MemoryFactStore()
        return self

    def now(self) -> float:
        return self._clock()

    # -- request thread: every method below returns without waiting ----------

    def start(self, account: str, request_id: str, intent: str) -> Reply:
        with self._lock:
            existing = self.store.by_request(account, request_id)
            if existing is not None:
                if not _same_intent(existing.intent, intent):
                    raise Refusal(
                        f"request_id {request_id!r} was already used for a "
                        "different board; mint a new request_id for a new board",
                        speakable=False,
                    )
                # A replay is answered before the capacity check, so a retry
                # is never refused for being a retry.
                return Reply(self._fresh_locked(existing), replayed=True)
            live = [run for run in self._runs.values() if run.busy]
            if any(run.account == account for run in live):
                raise Refusal(speech.REFUSALS["busy"])
            if len(live) >= self.max_active:
                raise Refusal(speech.REFUSALS["capacity"])
            session_id = "brd_" + uuid.uuid4().hex[:12]
            board, created = self.store.claim(
                account, request_id, intent,
                session_id=session_id, now=self.now(), scripted=self.scripted,
            )
            run = VoiceRun(session_id, account, request_id, intent)
            self._runs[session_id] = run
            self._spawn(run, "plan", self._plan)
        return Reply(board, created=created)

    def answer(
        self,
        account: str,
        session_id: str,
        answers: Sequence[Mapping[str, Any]],
        you_choose: bool,
    ) -> Reply:
        if not answers and not you_choose:
            raise Refusal("Send an answer, or set you_choose to true.", speakable=False)
        board = self.board(account, session_id)
        run = self._runs.get(session_id)
        if run is None:
            return Reply(board)
        with run.lock:
            board = self._require(account, session_id)
            if board.state == "reading":
                # Before the index check: while planning, the questions are
                # not known yet, and "this board has no questions" would be
                # untrue.
                raise Refusal(speech.REFUSALS["still_planning"])
            count = len(board.questions)
            for item in answers:
                if item["index"] >= count:
                    raise Refusal(
                        "This board has no questions to answer."
                        if count == 0
                        else f"There are only {count} questions; index them 0 to "
                        f"{count - 1}.",
                        speakable=False,
                    )
            given = {str(a["index"]): str(a["answer"]).strip() for a in answers}
            if board.state != "questions":
                if any(board.answers.get(k) != v for k, v in given.items()):
                    raise Refusal(speech.REFUSALS["already_drafting"])
                return Reply(board)
            merged = {**board.answers, **given}
            still_open = [i for i in range(count) if str(i) not in merged]
            if still_open and not you_choose:
                board = self.store.set_state(
                    account, session_id, now=self.now(), answers=merged
                )
                return Reply(board, acknowledged=bool(given))
            board = self.store.set_state(
                account, session_id, "proposing", now=self.now(), answers=merged
            )
            with self._lock:
                self._spawn(run, "propose", self._propose)
        return Reply(board, you_chose=you_choose and bool(still_open))

    def continue_design(self, account: str, session_id: str) -> Reply:
        board = self.board(account, session_id)
        run = self._runs.get(session_id)
        if run is None:
            return Reply(board)
        with run.lock:
            board = self._require(account, session_id)
            if board.state == "questions":
                raise Refusal(speech.REFUSALS["need_answer"])
            if board.state == "drafted":
                board = self.store.set_state(
                    account, session_id, "placing", now=self.now()
                )
                with self._lock:
                    self._spawn(run, "build", self._build)
            elif board.state in ("reading", "proposing") and not board.continue_asked:
                # Queued, not refused: it saves the person a spoken turn, and
                # the worker picks it up under this same lock when the
                # schematic lands.
                board = self.store.set_state(
                    account, session_id, now=self.now(), continue_asked=True
                )
        return Reply(board)

    def board(self, account: str, session_id: str | None = None) -> Board:
        """The board as the store holds it, for this account only.

        An unknown id and another account's id get the same refusal, so the
        answer does not say whether the board exists.
        """
        if session_id is None:
            board = self.store.latest(account)
            if board is None:
                raise Refusal(speech.REFUSALS["no_boards"])
        else:
            board = self._require(account, session_id)
        return self._fresh(board)

    def recall(
        self, account: str, query: str | None, limit: int
    ) -> tuple[list[Board], int, int]:
        boards, total = self.store.recall(account, query, limit)
        return [self._fresh(b) for b in boards], total, self.store.count(account)

    # -- helpers -------------------------------------------------------------------

    def _require(self, account: str, session_id: str) -> Board:
        board = self.store.get(account, session_id)
        if board is None:
            raise Refusal(speech.REFUSALS["not_found"])
        return board

    def _fresh(self, board: Board) -> Board:
        with self._lock:
            return self._fresh_locked(board)

    def _fresh_locked(self, board: Board) -> Board:
        """``board``, unless it is mid-run with no worker in this process.

        That is a board an earlier process was running: its ``steps`` session
        lived in that process's memory, so nothing here can finish it, and a
        row left at ``routing`` would be spoken as progress forever.
        ``app.main`` sweeps these at startup (:meth:`BoardStore.fail_unfinished`);
        this catches one a sweep did not see.

        ``board`` was read before this lock was taken, so "no run" can also
        mean a worker finished in between: :meth:`_guard` writes the final
        state first and drops the run afterwards, under this lock. So the row
        is read again here, where no worker can drop a run, and a board that
        has reached ``done`` or ``failed`` since is returned as it is. The
        restart write is also conditional on the state just read
        (``expect``), so it can never overwrite a finish, even from a second
        handle on the file.
        """
        if board.state in TERMINAL_STATES or board.session_id in self._runs:
            return board
        current = self.store.get(board.account, board.session_id)
        if current is None or current.state in TERMINAL_STATES:
            return current or board
        return self.store.set_state(
            current.account, current.session_id, "failed", now=self.now(),
            expect=current.state,
            failure_stage="restart", failure_cause="restart",
            failure_reason=f"the service restarted while this board was at "
            f"{current.state}",
        )

    def _spawn(
        self, run: VoiceRun, name: str, body: Callable[[VoiceRun], None]
    ) -> None:
        """Start ``body`` on a daemon thread. Caller holds ``self._lock``."""
        run.workers += 1
        thread = threading.Thread(
            target=self._guard,
            args=(run, body),
            name=f"alexabot-{run.session_id}-{name}",
            daemon=True,
        )
        self._threads.add(thread)
        thread.start()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for every worker to finish; whether they all did in time.

        For a clean shutdown and for the tests. A worker cannot be killed,
        so a ``False`` here is a worker still inside a model call.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(left)
        return not any(thread.is_alive() for thread in threads)

    def _guard(self, run: VoiceRun, body: Callable[[VoiceRun], None]) -> None:
        try:
            body(run)
        except BaseException as exc:  # noqa: BLE001 -- the row must say it failed
            self._fail(run, exc)
        finally:
            with self._lock:
                run.workers -= 1
                self._threads.discard(threading.current_thread())
                board = self.store.get(run.account, run.session_id)
                if (
                    not run.busy
                    and board is not None
                    and board.state in TERMINAL_STATES
                ):
                    # A finished board needs no handles; the row is the record.
                    self._runs.pop(run.session_id, None)

    def _fail(self, run: VoiceRun, exc: BaseException) -> None:
        board = self.store.get(run.account, run.session_id)
        stage = board.state if board is not None else "reading"
        stage = {"questions": "reading", "drafted": "proposing"}.get(stage, stage)
        if stage in TERMINAL_STATES:
            stage = "reviewing"
        # The whole text is kept in the row for whoever runs the server; the
        # tools send only the class and the cause in words (tools._public_reason).
        reason = f"{type(exc).__name__}: {str(exc)[:160]}"
        # One stderr line, names only: the reason may quote a model.
        print(
            f"[alexabot] {run.session_id} failed while {stage}: "
            f"{type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        self.store.set_state(
            run.account, run.session_id, "failed", now=self.now(),
            failure_stage=stage, failure_cause=speech.cause_key(exc),
            failure_reason=reason,
        )

    def _summary(self, run: VoiceRun) -> dict[str, Any]:
        board = self.store.get(run.account, run.session_id)
        summary = empty_summary()
        if board is not None and board.summary:
            summary.update(board.summary)
        return summary

    @staticmethod
    def _files(summary: dict[str, Any], envelope: Mapping[str, Any]) -> None:
        files = envelope.get("files") or {}
        for role in ("schematic", "board", "project"):
            if files.get(role):
                summary["files"][role] = str(files[role])

    # -- workers ------------------------------------------------------------------

    def _plan(self, run: VoiceRun) -> None:
        run.model = self._model_factory()
        body = self._steps.start_once(
            {"intent": run.intent, "plan_first": True, "prefetch": False},
            model=run.model,
            store=self._fact_store,
            idempotency_key=f"alexa:{run.account}:{run.request_id}",
        )
        run.steps_session = str(body["session"])
        plan = body.get("plan") or {}
        brief = plan.get("plan") or {}
        questions = [
            {"ask": str(q.get("ask", "")), "default": str(q.get("default", ""))}
            for q in brief.get("questions") or []
        ]
        fields: dict[str, Any] = {"steps_session": run.steps_session,
                                  "questions": questions}
        if not plan.get("ok"):
            notes = [str(w) for w in plan.get("warnings") or []] or [
                "no plan was made, so the schematic is drafted from the request alone"
            ]
            fields["summary"] = empty_summary(notes)
        with run.lock:
            state = "questions" if questions else "proposing"
            self.store.set_state(
                run.account, run.session_id, state, now=self.now(), **fields
            )
            if questions:
                return
        self._propose(run)

    def _propose(self, run: VoiceRun) -> None:
        board = self.store.get(run.account, run.session_id)
        answers = dict(board.answers) if board is not None else {}
        envelope = self._steps.advance(
            run.steps_session, "propose", {"answers": answers}, model=run.model
        )
        summary = self._summary(run)
        summary["parts"] = envelope.get("parts")
        summary["nets"] = envelope.get("nets")
        self._files(summary, envelope)
        with run.lock:
            board = self.store.set_state(
                run.account, run.session_id, "drafted", now=self.now(),
                summary=summary,
            )
            if not board.continue_asked:
                return
            self.store.set_state(run.account, run.session_id, "placing", now=self.now())
        self._build(run)

    def _build(self, run: VoiceRun) -> None:
        sid, account = run.session_id, run.account
        placed = self._steps.advance(run.steps_session, "place", {}, model=run.model)
        summary = self._summary(run)
        summary["board_mm"] = placed.get("board_mm")
        summary["placement_status"] = placed.get("status")
        self._files(summary, placed)
        self.store.set_state(account, sid, "routing", now=self.now(), summary=summary)

        routed = self._steps.advance(run.steps_session, "route", {}, model=run.model)
        routing = routed.get("routing") or {}
        summary["routed_fraction"] = routing.get("completion")
        summary["unrouted"] = [
            {"net": str(net), "reason": str(reason)}
            for net, reason in (routing.get("unrouted") or {}).items()
        ]
        self._files(summary, routed)
        self.store.set_state(account, sid, "reviewing", now=self.now(), summary=summary)

        try:
            reviewed = self._steps.advance(
                run.steps_session, "review", {}, model=run.model
            )
        except Exception as exc:  # noqa: BLE001 -- the routed board is the product
            print(
                f"[alexabot] {sid} review raised {type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            summary["review"] = {
                "status": "failed",
                # The class and the cause in words; the text itself stays in
                # the row as ``error``, which no tool sends (speech.cause_text).
                "detail": speech.cause_text(type(exc).__name__, speech.cause_key(exc)),
                "findings": None,
                "blockers": None,
                "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            }
        else:
            block = reviewed.get("review") or {}
            status = block.get("status")
            status = "not_run" if status == "skipped" else status
            if status not in ("ok", "failed", "not_run"):
                status = "failed"
            findings = (
                _numbered(reviewed.get("findings") or []) if status == "ok" else []
            )
            summary["findings"] = findings
            summary["review"] = {
                "status": status,
                "detail": block.get("detail") or None,
                "findings": len(findings) if status == "ok" else None,
                "blockers": (
                    sum(1 for f in findings if f["severity"].lower() == "blocker")
                    if status == "ok" else None
                ),
            }
            for warning in reviewed.get("warnings") or []:
                summary["notes"].append(str(warning))
        self.store.set_state(account, sid, "done", now=self.now(), summary=summary)
