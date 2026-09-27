"""The board history: every board a voice session started, per account.

``recall_my_boards`` answers "what did I build last week?" from here, and
``board_status`` and ``explain_finding`` answer from here too, so a board a
person started in one conversation can be talked about in the next -- and
after a restart, since ``service/steps.py`` keeps its sessions in memory only.
The row is the **only** source of truth for anything spoken: a worker thread
writes it, a tool call reads it, and nothing is said that the row does not
hold.

The SQLite settings are the ones ``service/auth.py::SqliteKeyStore`` and
``billing/sqlite_ledger.py`` already use: WAL, ``synchronous=FULL``, one
connection with ``check_same_thread=False`` behind one lock, and the file set
to ``0600``. ``UNIQUE (account, request_id)`` is the idempotency key the
voice agent mints (see :mod:`alexabot.runner`), enforced by the database so it
survives a restart. Rows are mutable board records rather than money, so the
ledger's append-only rule does not apply; no transition log is kept.

Every query carries ``account = ?``. There is no way to read another
account's board through this class, which is the whole of the isolation
between two people's histories.

Imports nothing but the standard library.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "STATES",
    "TERMINAL_STATES",
    "Board",
    "BoardStore",
    "StoreError",
]

#: ``PRAGMA user_version`` of the schema below. A file written by a newer
#: version is refused rather than read with the wrong idea of its columns.
SCHEMA_VERSION = 1

#: Every state a board can be in, in the order a run passes through them.
STATES = (
    "reading",
    "questions",
    "proposing",
    "drafted",
    "placing",
    "routing",
    "reviewing",
    "done",
    "failed",
)
TERMINAL_STATES = frozenset({"done", "failed"})
_STATE_LIST = ", ".join(f"'{s}'" for s in STATES)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS boards (
    session_id     TEXT PRIMARY KEY,
    account        TEXT NOT NULL,
    request_id     TEXT NOT NULL,
    intent         TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ({_STATE_LIST})),
    failure_stage  TEXT,
    failure_cause  TEXT,
    failure_reason TEXT,
    steps_session  TEXT,
    questions_json TEXT NOT NULL DEFAULT '[]',
    answers_json   TEXT NOT NULL DEFAULT '{{}}',
    summary_json   TEXT,
    continue_asked INTEGER NOT NULL DEFAULT 0,
    scripted       INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    state_since    REAL NOT NULL,
    UNIQUE (account, request_id)
);
CREATE INDEX IF NOT EXISTS boards_by_account ON boards (account, created_at DESC);
"""

_COLUMNS = (
    "session_id, account, request_id, intent, state, failure_stage, "
    "failure_cause, failure_reason, steps_session, questions_json, answers_json, "
    "summary_json, continue_asked, scripted, created_at, updated_at, state_since"
)

#: The fields :meth:`BoardStore.set_state` may write, and how each is stored.
_JSON_FIELDS = {"questions": "questions_json", "answers": "answers_json",
                "summary": "summary_json"}
_PLAIN_FIELDS = {"failure_stage", "failure_cause", "failure_reason",
                 "steps_session", "continue_asked"}


class StoreError(RuntimeError):
    """The database cannot be used as this version understands it."""


@dataclass(frozen=True)
class Board:
    """One row, decoded. ``summary`` is ``None`` until something is known."""

    session_id: str
    account: str
    request_id: str
    intent: str
    state: str
    failure_stage: str | None
    failure_cause: str | None
    failure_reason: str | None
    steps_session: str | None
    questions: list[dict[str, str]]
    answers: dict[str, str]
    summary: dict[str, Any] | None
    continue_asked: bool
    scripted: bool
    created_at: float
    updated_at: float
    state_since: float

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_STATES


def _decode(row: tuple) -> Board:
    (session_id, account, request_id, intent, state, failure_stage, failure_cause,
     failure_reason, steps_session, questions_json, answers_json, summary_json,
     continue_asked, scripted, created_at, updated_at, state_since) = row
    return Board(
        session_id=session_id,
        account=account,
        request_id=request_id,
        intent=intent,
        state=state,
        failure_stage=failure_stage,
        failure_cause=failure_cause,
        failure_reason=failure_reason,
        steps_session=steps_session,
        questions=json.loads(questions_json),
        answers=json.loads(answers_json),
        summary=None if summary_json is None else json.loads(summary_json),
        continue_asked=bool(continue_asked),
        scripted=bool(scripted),
        created_at=created_at,
        updated_at=updated_at,
        state_since=state_since,
    )


def _like_escape(text: str) -> str:
    """``text`` as a literal inside ``LIKE ... ESCAPE '\\'``: a ``%`` or ``_``
    the person said is a character, not a wildcard."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class BoardStore:
    """SQLite board history. ``":memory:"`` is allowed, for unit tests."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        memory = self._path == ":memory:"
        if not memory:
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._path = str(Path(self._path).expanduser())
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError(
                    f"{self._path} was written by a newer alexabot (schema "
                    f"{version}; this one reads {SCHEMA_VERSION}); refusing to "
                    "guess at its columns"
                )
            if not memory:
                self._db.execute("PRAGMA journal_mode=WAL")
            # FULL, not NORMAL: "did this request_id already start a board"
            # is the idempotency guarantee, and NORMAL may lose the last
            # transactions on a power cut (the ledger's reasoning).
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.commit()
        except BaseException:
            self._db.close()
            raise
        if not memory:
            with contextlib.suppress(OSError):
                os.chmod(self._path, 0o600)

    @property
    def path(self) -> str:
        return self._path

    # -- writes ---------------------------------------------------------------

    def claim(
        self,
        account: str,
        request_id: str,
        intent: str,
        *,
        session_id: str,
        now: float,
        scripted: bool = False,
    ) -> tuple[Board, bool]:
        """Insert a new board in ``reading``, or return the one this
        ``(account, request_id)`` already names. ``(board, created)``."""
        with self._lock, self._db:
            try:
                self._db.execute(
                    "INSERT INTO boards (session_id, account, request_id, intent,"
                    " state, scripted, created_at, updated_at, state_since)"
                    " VALUES (?, ?, ?, ?, 'reading', ?, ?, ?, ?)",
                    (session_id, account, request_id, intent, int(scripted),
                     now, now, now),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM boards WHERE account = ? AND request_id = ?",
                (account, request_id),
            ).fetchone()
        return _decode(row), created

    def set_state(
        self,
        account: str,
        session_id: str,
        state: str | None = None,
        *,
        now: float,
        expect: str | None = None,
        **fields: Any,
    ) -> Board:
        """One UPDATE: ``state`` (when given) plus any of the named fields.

        ``state_since`` moves only when the state actually changes, so a
        worker that rewrites the summary does not reset the stall clock.

        ``expect`` makes the write a compare-and-set: it applies only while
        the row is still in that state, and otherwise the row comes back
        unchanged. A write decided from an earlier read -- the restart
        sweep in :meth:`Runner._fresh_locked` -- uses it so it cannot land
        on a board that finished after the read.
        """
        if state is not None and state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        if expect is not None and expect not in STATES:
            raise ValueError(f"unknown state {expect!r}")
        sets = ["updated_at = ?"]
        values: list[Any] = [now]
        if state is not None:
            sets.append("state_since = CASE WHEN state = ? THEN state_since ELSE ? END")
            values += [state, now]
            sets.append("state = ?")
            values.append(state)
        for name, value in fields.items():
            if name in _JSON_FIELDS:
                sets.append(f"{_JSON_FIELDS[name]} = ?")
                values.append(None if value is None else json.dumps(value))
            elif name in _PLAIN_FIELDS:
                sets.append(f"{name} = ?")
                values.append(int(value) if name == "continue_asked" else value)
            else:
                raise ValueError(f"set_state cannot write {name!r}")
        where = "account = ? AND session_id = ?"
        values += [account, session_id]
        if expect is not None:
            where += " AND state = ?"
            values.append(expect)
        with self._lock, self._db:
            done = self._db.execute(
                f"UPDATE boards SET {', '.join(sets)} WHERE {where}", values
            )
            if done.rowcount != 1 and expect is None:
                raise KeyError(session_id)
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM boards WHERE account = ? AND session_id = ?",
                (account, session_id),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _decode(row)

    def fail_unfinished(self, now: float) -> int:
        """Mark every board that was mid-run as failed by a restart.

        The ``service/steps.py`` sessions those boards were running in lived
        in memory and are gone, so nothing will ever finish them; a row left
        at ``routing`` would be spoken as progress forever. Safe only because
        one process owns this database, which ``docs/alexa.md`` states.
        """
        with self._lock, self._db:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM boards WHERE state NOT IN ('done', 'failed')"
            ).fetchall()
            for raw in rows:
                board = _decode(raw)
                schematic = ((board.summary or {}).get("files") or {}).get("schematic")
                reason = (
                    f"the service restarted while this board was at {board.state}"
                    + (f"; the schematic is at {schematic}" if schematic else "")
                )
                self._db.execute(
                    "UPDATE boards SET state = 'failed', failure_stage = 'restart',"
                    " failure_cause = 'restart', failure_reason = ?,"
                    " updated_at = ?, state_since = ?"
                    " WHERE session_id = ?",
                    (reason, now, now, board.session_id),
                )
        return len(rows)

    # -- reads ------------------------------------------------------------------

    def _one(self, where: str, args: tuple) -> Board | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_COLUMNS} FROM boards WHERE {where}", args
            ).fetchone()
        return None if row is None else _decode(row)

    def by_request(self, account: str, request_id: str) -> Board | None:
        return self._one("account = ? AND request_id = ?", (account, request_id))

    def get(self, account: str, session_id: str) -> Board | None:
        return self._one("account = ? AND session_id = ?", (account, session_id))

    def latest(self, account: str) -> Board | None:
        return self._one(
            "account = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (account,)
        )

    def count(self, account: str) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM boards WHERE account = ?", (account,)
            ).fetchone()[0]

    def recall(
        self, account: str, query: str | None = None, limit: int = 3
    ) -> tuple[list[Board], int]:
        """This account's boards, newest first; ``(boards[:limit], total)``.

        ``query`` matches words of the original request, case-insensitively
        for ASCII (SQLite's ``LIKE``), every word required.
        """
        where = ["account = ?"]
        args: list[Any] = [account]
        for word in (query or "").split():
            where.append("intent LIKE ? ESCAPE '\\'")
            args.append(f"%{_like_escape(word)}%")
        clause = " AND ".join(where)
        with self._lock:
            total = self._db.execute(
                f"SELECT COUNT(*) FROM boards WHERE {clause}", args
            ).fetchone()[0]
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM boards WHERE {clause}"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                [*args, int(limit)],
            ).fetchall()
        return [_decode(r) for r in rows], total

    def active(self) -> list[Board]:
        """Every board not yet finished, any account (the capacity check)."""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {_COLUMNS} FROM boards WHERE state NOT IN ('done', 'failed')"
            ).fetchall()
        return [_decode(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
