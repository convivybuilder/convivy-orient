"""Schema + datastore tests for the control plane (WS0.5, ARCH-348).

Proves the alembic-managed schema actually holds the invariants the datastore
depends on: WAL mode, and the vault tables are actually gone."""
from __future__ import annotations

import os
import sqlite3
import time

import pytest
from sqlalchemy import text

import control_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh control.db on a temp path, schema brought to head by alembic —
    the same init_db() the app runs at startup."""
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    yield


def test_schema_created_at_head(db):
    with control_db.session() as s:
        tables = set(s.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'")).scalars())
    # Orient's own concerns only (ADR-0022) — everything else was a second
    # copy of what the Library owns alone, dropped with
    # issue_01M08YKNV185DFJ6832GV8ZY22.
    # Field Notes joined that list on 2026-08-22: Orient is the store of truth
    # for the blog and every public surface is a renderer
    # (issue_01M0N650ZJHPQYPF54HD22RJEH), so the corpus is Orient's own concern
    # in the same sense the Reading Room is.
    assert {"reading_rooms", "room_entries", "room_requests",
            "push_subscriptions", "blog_posts", "blog_post_revisions"} <= tables
    vault_tables = {"issue_items", "issue_comments", "issue_events",
                    "issue_leases", "lease_refusals", "signals", "signal_acks",
                    "courses", "course_events", "decision_items",
                    "decision_comments", "decision_events"}
    assert not (vault_tables & tables)


def test_no_vec0_index_survives_a_fresh_init(db):
    """The vec0 virtual tables are gone too — nothing embeds anything locally
    any more, so there is nothing for them to serve."""
    with control_db.session() as s:
        tables = set(s.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'")).scalars())
    assert not ({"library_vectors", "signal_vectors"} & tables)


def test_wal_mode_is_on(db):
    with control_db.session() as s:
        mode = s.execute(text("PRAGMA journal_mode")).scalar()
    assert mode.lower() == "wal"


def test_init_db_persists_wal_without_a_write(tmp_path, monkeypatch):
    """init_db alone must leave the DB in WAL, not depend on the first app write to
    flip it. Read journal_mode over a RAW sqlite3 connection that never runs the
    engine's connect-pragma (unlike session() above, which flips WAL as a side
    effect and would mask the gap). Fails on pre-fix init_db, which left `delete`."""
    import sqlite3
    dbfile = tmp_path / "control.db"
    monkeypatch.setattr(control_db, "CONTROL_DB", dbfile)
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    conn = sqlite3.connect(str(dbfile))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


# --- owner-only modes (issue_01M2MFGVAW1C8ZQ7G9CT0E1YXZ) ---------------------
# A permissive umask is forced here and restored after: the point is that the
# mode comes from the code, not from whatever umask the caller happened to
# have. Without the fix, `mkdir`/`open` under 0o022 leave the dir 0o755 and
# the db file 0o644.


def test_init_db_creates_the_dir_0700_and_the_db_0600(tmp_path, monkeypatch):
    dbfile = tmp_path / "sub" / "control.db"
    monkeypatch.setattr(control_db, "CONTROL_DB", dbfile)
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    old_umask = os.umask(0o022)
    try:
        control_db.init_db()
    finally:
        os.umask(old_umask)
    assert oct(dbfile.parent.stat().st_mode & 0o777) == oct(0o700)
    assert oct(dbfile.stat().st_mode & 0o777) == oct(0o600)


def test_get_engine_creates_the_dir_0700_and_the_db_0600(tmp_path, monkeypatch):
    """get_engine() is the other creation path (a caller that opens a session
    before anything has called init_db) and must hold the same guarantee."""
    dbfile = tmp_path / "sub" / "control.db"
    monkeypatch.setattr(control_db, "CONTROL_DB", dbfile)
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    old_umask = os.umask(0o022)
    try:
        control_db.get_engine()
    finally:
        os.umask(old_umask)
    assert oct(dbfile.parent.stat().st_mode & 0o777) == oct(0o700)
    assert oct(dbfile.stat().st_mode & 0o777) == oct(0o600)


def test_an_already_existing_looser_dir_is_tightened(tmp_path, monkeypatch):
    """`mkdir(..., exist_ok=True)` is a no-op on a directory that already
    exists, so a directory some earlier writer left at 0755 must be
    tightened explicitly rather than left alone — Orient owns this directory."""
    ctrl_dir = tmp_path / "ctrl"
    ctrl_dir.mkdir(mode=0o755)
    dbfile = ctrl_dir / "control.db"
    monkeypatch.setattr(control_db, "CONTROL_DB", dbfile)
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    assert oct(ctrl_dir.stat().st_mode & 0o777) == oct(0o700)


def test_wal_and_shm_siblings_are_0600_after_a_write(tmp_path, monkeypatch):
    """SQLite gives -wal/-shm the mode of the main db file at the time they're
    created, so the fix has to land before the first write, not after."""
    dbfile = tmp_path / "control.db"
    monkeypatch.setattr(control_db, "CONTROL_DB", dbfile)
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    old_umask = os.umask(0o022)
    try:
        control_db.init_db()
        conn = sqlite3.connect(str(dbfile))
        try:
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        finally:
            conn.close()
    finally:
        os.umask(old_umask)
    wal = dbfile.with_name(dbfile.name + "-wal")
    shm = dbfile.with_name(dbfile.name + "-shm")
    assert wal.exists(), "the write above should have created the WAL file"
    assert oct(wal.stat().st_mode & 0o777) == oct(0o600)
    if shm.exists():  # -shm only appears once a second connection opens the db
        assert oct(shm.stat().st_mode & 0o777) == oct(0o600)


def test_new_ulid_unique_and_time_ordered():
    ids = [control_db.new_ulid() for _ in range(500)]
    assert len(set(ids)) == 500  # no collisions
    assert all(len(u) == 26 for u in ids)
    # sortability holds across the millisecond timestamp, not within one ms (the
    # random tail isn't monotonic) — a later ULID sorts after an earlier one.
    a = control_db.new_ulid()
    time.sleep(0.005)
    b = control_db.new_ulid()
    assert a < b


# The expected value is written out per case, not derived from ISSUE_TERMINAL,
# so emptying or shrinking that set fails here. Before 2026-09-12 the only test
# reaching the "item is terminal" branch compared parked_sweep's copy of this
# rule against this one, and it went with the sweep.
@pytest.mark.parametrize("entry, met", [
    ({"text": "x", "met": True}, True),
    ({"text": "x", "met": False}, False),
    ({"text": "x"}, False),
    ({"item": "i", "state": "fixed"}, True),
    ({"item": "i", "state": "wontfix"}, True),
    ({"item": "i", "state": "duplicate"}, True),
    ({"item": "i", "state": "redirected"}, True),
    ({"item": "i", "state": "open"}, False),
    ({"item": "i", "state": "claimed"}, False),
    ({"item": "i", "state": "pr-open"}, False),
    # An item the reader may not see is never met, even beside a terminal
    # state: "I cannot see whether this cleared" is not "this cleared".
    ({"item": "i", "missing": True}, False),
    ({"item": "i", "missing": True, "state": "fixed"}, False),
    ({"item": "i"}, False),
])
def test_prerequisite_met_reads_each_shape(entry, met):
    assert control_db.prerequisite_met(entry) is met
