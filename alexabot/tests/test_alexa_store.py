"""The board history: per-account, durable, and honest about restarts."""

import os
import sqlite3
import stat

import pytest

from alexabot.store import SCHEMA_VERSION, BoardStore, StoreError


def _claim(store, account="alice", request_id="req-0001", intent="a board", now=1.0):
    return store.claim(
        account, request_id, intent, session_id=f"brd_{account}_{request_id}_{now}",
        now=now,
    )


def test_claim_is_unique_per_account_and_request_id(store):
    first, created = _claim(store)
    again, created_again = _claim(store, now=2.0)
    assert created and not created_again
    assert again.session_id == first.session_id and again.state == "reading"


def test_two_accounts_same_request_id_get_two_rows(store):
    alice, _ = _claim(store, "alice")
    bob, created = _claim(store, "bob")
    assert created and alice.session_id != bob.session_id
    assert store.get("alice", bob.session_id) is None
    assert store.get("bob", alice.session_id) is None


def test_recall_is_newest_first_and_account_scoped(store):
    for n, intent in enumerate(["usb-c charger", "led blinker", "usb hub"]):
        _claim(store, "alice", f"req-000{n}", intent, now=float(n))
    _claim(store, "bob", "req-0009", "usb thing for bob", now=9.0)
    boards, total = store.recall("alice", None, 2)
    assert total == 3 and [b.intent for b in boards] == ["usb hub", "led blinker"]
    boards, total = store.recall("alice", "USB", 5)
    assert total == 2 and [b.intent for b in boards] == ["usb hub", "usb-c charger"]
    assert store.count("alice") == 3 and store.count("bob") == 1


def test_query_escapes_percent_and_underscore(store):
    _claim(store, intent="a 50% duty cycle", request_id="req-0001")
    _claim(store, intent="a plain board", request_id="req-0002", now=2.0)
    _claim(store, intent="net USB_D", request_id="req-0003", now=3.0)
    assert store.recall("alice", "%", 5)[1] == 1
    assert store.recall("alice", "_", 5)[1] == 1
    assert store.recall("alice", "B_D", 5)[1] == 1


def test_reopen_keeps_rows(tmp_path):
    path = tmp_path / "boards.sqlite3"
    first = BoardStore(path)
    board, _ = _claim(first)
    first.set_state("alice", board.session_id, "done", now=5.0,
                    summary={"parts": 3})
    first.close()
    second = BoardStore(path)
    kept = second.get("alice", board.session_id)
    assert kept.state == "done" and kept.summary == {"parts": 3}
    second.close()


def test_fail_unfinished_names_the_restart_and_leaves_done_rows(store):
    running, _ = _claim(store, request_id="req-0001")
    store.set_state("alice", running.session_id, "routing", now=2.0,
                    summary={"files": {"schematic": "/b/x.kicad_sch"}})
    finished, _ = _claim(store, request_id="req-0002")
    store.set_state("alice", finished.session_id, "done", now=2.0)
    assert store.fail_unfinished(10.0) == 1
    failed = store.get("alice", running.session_id)
    assert (failed.state, failed.failure_stage, failed.failure_cause) == (
        "failed", "restart", "restart")
    assert "at routing" in failed.failure_reason
    assert "/b/x.kicad_sch" in failed.failure_reason
    assert store.get("alice", finished.session_id).state == "done"


def test_state_since_moves_only_on_a_state_change(store):
    board, _ = _claim(store)
    store.set_state("alice", board.session_id, "proposing", now=5.0)
    same = store.set_state("alice", board.session_id, now=9.0, summary={"x": 1})
    assert same.state_since == 5.0 and same.updated_at == 9.0
    moved = store.set_state("alice", board.session_id, "drafted", now=12.0)
    assert moved.state_since == 12.0


def test_an_unknown_state_or_field_is_refused(store):
    board, _ = _claim(store)
    with pytest.raises(ValueError):
        store.set_state("alice", board.session_id, "shipped", now=2.0)
    with pytest.raises(ValueError):
        store.set_state("alice", board.session_id, now=2.0, intent="other")
    with pytest.raises(KeyError):
        store.set_state("bob", board.session_id, "done", now=2.0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_file_is_0600_wal_and_synchronous_full(tmp_path):
    path = tmp_path / "boards.sqlite3"
    board_store = BoardStore(path)
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        db = board_store._db
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        board_store.close()


def test_newer_user_version_is_refused(tmp_path):
    path = tmp_path / "boards.sqlite3"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.commit()
    db.close()
    with pytest.raises(StoreError, match="newer"):
        BoardStore(path)


def test_the_restart_write_is_a_compare_and_set(store):
    store.claim("local", "req-00000001", "a board", session_id="brd_a", now=1.0)
    store.set_state("local", "brd_a", "done", now=2.0)
    kept = store.set_state("local", "brd_a", "failed", now=3.0, expect="reviewing",
                           failure_stage="restart", failure_cause="restart",
                           failure_reason="the service restarted")
    assert kept.state == "done" and kept.failure_stage is None
    assert kept.updated_at == 2.0
    moved = store.set_state("local", "brd_a", "failed", now=4.0, expect="done",
                            failure_stage="restart")
    assert moved.state == "failed"
    with pytest.raises(KeyError):
        store.set_state("local", "brd_missing", "failed", now=5.0, expect="done")
