"""Control-plane datastore (WS0.5, ARCH-348).

One WAL-mode SQLite database on the mini, at ~/.orient-control/control.db —
separate from the read-only KB index. Single writer: the orient process. Agents,
humans, and integrations all write THROUGH the HTTP API, never by opening this
file, so there is one place for validation, authz, and audit (the same model as
the KB index and the ARCH-102/ADR-0012 gateway-metering plane).

Orient's own concerns only (ADR-0022) as of 2026-08-19: the Reading Room and
Web Push subscriptions. The issue queue, signal memory, decisions and the
Course used to live here too — a second copy of what the Library now owns
alone — and are gone with issue_01M08YKNV185DFJ6832GV8ZY22, vec0 index
included (nothing left embeds anything locally).

alembic owns the schema (init_db runs `upgrade head`); models.py is the source
of truth its migrations are generated from.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from models import utcnow  # noqa: F401 — re-exported: every caller of control_db.utcnow stays valid
from owner_only_paths import ensure_owner_only_dir, ensure_owner_only_file

DIR = Path(__file__).resolve().parent

# Overridable for tests; the real DB lives outside the repo, one per host.
CONTROL_DB = Path(os.environ.get(
    "ORIENT_CONTROL_DB", str(Path.home() / ".orient-control" / "control.db")))

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (no I,L,O,U)


def new_ulid() -> str:
    """A ULID: 48-bit millisecond timestamp + 80-bit randomness, Crockford
    base32 (26 chars). Sortable by creation time; uniqueness from the random tail.
    (time.time() is fine here — this is app code, not a resumable workflow.)"""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    val = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[val & 0x1F])
        val >>= 5
    return "".join(reversed(out))


# --- issue-queue vocabulary --------------------------------------------------
# Orient no longer holds an issue queue, but the product+engineering pages
# (issue_build.py) still render state/type/needs read through the Library, and
# need the same vocabulary the Library's rows carry to classify and lane them.
# Kept here rather than duplicated in library_client, per
# issue_01M08YKNV185DFJ6832GV8ZY22's own instruction.
ISSUE_OPEN = "open"
ISSUE_CLAIMED = "claimed"
ISSUE_PR_OPEN = "pr-open"
ISSUE_TERMINAL = frozenset({"fixed", "wontfix", "duplicate", "redirected"})
ISSUE_ACTIVE = frozenset({ISSUE_OPEN, ISSUE_CLAIMED, ISSUE_PR_OPEN})


def prerequisite_met(entry: dict) -> bool:
    """Whether one of an item's prerequisites is satisfied (decisions/0074).

    This is the canonical reading of a prerequisite entry.

    Three shapes:

    - `{"text": ..., "met": bool}` carries its own verdict, written by any
      caller holding `item.resolve`.
    - `{"item": id, "state": ...}` is met once the item it names is terminal.
    - `{"item": id, "missing": True}` is an item the reader may not see. **It is
      NOT met.** "I cannot see whether this cleared" is not "this cleared", and
      the other reading lets a reference outside the reader's scope silently
      unblock an item.
    """
    if "text" in entry:
        return bool(entry.get("met"))
    if entry.get("missing"):
        return False
    return entry.get("state") in ISSUE_TERMINAL

ISSUE_TYPE_BUG = "bug"
ISSUE_TYPE_NIT = "nit"
ISSUE_TYPE_IMPROVEMENT = "improvement"
ISSUE_TYPE_FEATURE = "feature"
ISSUE_TYPE_TASK = "task"
ISSUE_TYPE_EPIC = "epic"
# The types a new filing may carry: the Library's `store.ITEM_TYPES`, which
# dropped `feature`. A filing typed `feature` is refused with 400, so the
# "Log an issue" panel must not offer it (issue_01M3J6D09C38T4AT0SGZ20XHDW).
# `ISSUE_TYPE_FEATURE` stays because older items still carry it.
ISSUE_TYPES = (ISSUE_TYPE_BUG, ISSUE_TYPE_NIT, ISSUE_TYPE_IMPROVEMENT,
               ISSUE_TYPE_TASK, ISSUE_TYPE_EPIC)

ISSUE_NEEDS_AGENT = "agent"
ISSUE_NEEDS_OPERATOR = "operator"
ISSUE_NEEDS_EITHER = "either"
ISSUE_DEFAULT_NEEDS = ISSUE_NEEDS_AGENT
ISSUE_DEFAULT_TYPE = ISSUE_TYPE_BUG


def new_room_id() -> str:
    return f"room_{new_ulid()}"


def new_room_request_id() -> str:
    return f"rreq_{new_ulid()}"


def new_signal_id() -> str:
    """A `sig_<ulid>` id — for the caller-side dedup keys and refs that still
    reference a signal locally (e.g. push delivery), not for constructing a
    row: signals live in the Library now (library_client.create_signal)."""
    return f"sig_{new_ulid()}"


def new_issue_id() -> str:
    """An `issue_<ulid>` id. Pure string generation, kept as a convenience for
    tests seeding `library_fake` — issues live in the Library now, and nothing
    in production calls this any more."""
    return f"issue_{new_ulid()}"


# --- signal privacy classes (S0, ARCH-349) ------------------------------------
# 'internal' is the engineering/System-A default; 'customer' marks System-B
# customer-authored content, gated to internal-only identities by the authz layer.
# Still needed here: push_send filters delivery on it, and library_client's
# writers pass it through.
PRIVACY_INTERNAL = "internal"
PRIVACY_CUSTOMER = "customer"
PRIVACY_CLASSES = frozenset({PRIVACY_INTERNAL, PRIVACY_CUSTOMER})


def new_ack_id() -> str:
    return f"ack_{new_ulid()}"


def new_push_id() -> str:
    return f"push_{new_ulid()}"


def _url_for(db_path: Path) -> str:
    return f"sqlite:///{db_path}"


_engine = None
_engine_url = None


def get_engine():
    """The process-wide engine on CONTROL_DB. Rebuilt if CONTROL_DB changed
    (tests point it at a temp file)."""
    global _engine, _engine_url
    url = _url_for(CONTROL_DB)
    if _engine is None or _engine_url != url:
        ensure_owner_only_dir(CONTROL_DB.parent)
        ensure_owner_only_file(CONTROL_DB)
        eng = create_engine(url, future=True)

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")   # one writer, many readers
            cur.execute("PRAGMA foreign_keys=ON")    # enforce the CASCADE deletes
            cur.execute("PRAGMA busy_timeout=5000")  # wait, don't fail, on a brief lock
            cur.close()

        _engine, _engine_url = eng, url
    return _engine


@contextmanager
def session():
    """A transactional session: commit on success, roll back on error, always
    close. The single place operational state is written."""
    s = Session(get_engine(), future=True)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db(url: str | None = None) -> None:
    """Bring the schema to head via alembic and persist WAL on the DB file.
    Idempotent — safe on every startup.

    alembic runs its upgrade over its own connection, which never triggers the
    engine's WAL connect-pragma (get_engine), so a freshly-created DB would come
    up in `delete` mode and flip to WAL only on the first app write. WAL is stored
    in the DB header and sticks for every later connection, so set it here at
    creation — matching the WS0.5 single-writer design regardless of when the
    first write lands."""
    import logging
    logging.getLogger("alembic").setLevel(logging.WARNING)  # quiet per-restart INFO in serve.log
    url = url or _url_for(CONTROL_DB)
    db_path = Path(url[len("sqlite:///"):]) if url.startswith("sqlite:///") else None
    if db_path is not None:
        ensure_owner_only_dir(db_path.parent)
        ensure_owner_only_file(db_path)
    cfg = Config(str(DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    if db_path is not None:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # persisted in the header
            _drop_vector_tables_once(db_path)
        finally:
            conn.close()


def _drop_vector_tables_once(db_path: Path) -> None:
    """Drop the vec0 virtual tables (`library_vectors`, the pre-Phase-1
    `signal_vectors`), left over from when this database held embeddings for
    the vault rows it copied (issue_01M08YKNV185DFJ6832GV8ZY22, 2026-08-19).
    Nothing embeds anything locally any more.

    **Not alembic-managed, on purpose**, the same seam the tables' own DDL
    used to sit outside alembic for: a virtual table needs its module
    (sqlite-vec) loaded to be touched at all — including to drop it — and
    alembic's connection never loads it. `DROP TABLE IF EXISTS` here is the
    idempotent equivalent; once both tables are gone this is a permanent
    no-op, cheap enough to leave in place rather than removing later."""
    import sqlite3
    have_either = False
    with sqlite3.connect(str(db_path)) as probe:
        for name in ("library_vectors", "signal_vectors"):
            if probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone():
                have_either = True
                break
    if not have_either:
        return
    import sqlite_vec
    conn = sqlite3.connect(str(db_path))
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("DROP TABLE IF EXISTS library_vectors")
        conn.execute("DROP TABLE IF EXISTS signal_vectors")
        conn.commit()
    finally:
        conn.close()
