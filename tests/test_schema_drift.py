"""The model and the migrations are two copies of one schema, and this compares them.

**NOTHING ELSE IN THE REPO DOES.** There is no `alembic check` step in CI — the
workflow runs `ruff` and `pytest` and nothing else — so an index declared on a
model and never migrated (or the reverse) is invisible: the query returns the
same rows, just by scanning. It surfaces later as drift `alembic check` would
report and `--autogenerate` keeps proposing, long after whoever caused it moved
on.

**THE SCOPE IS DERIVED, NOT LISTED, and that is the whole point of this file.**
An earlier version lived in `test_epic_lease.py` and named its tables in a
literal — first `issue_leases` alone, which caught nothing when `lease_refusals`
arrived one slice later with exactly the defect the test existed to find; then
both, which a reviewer correctly pointed out would miss the third table the same
way (PR #160). A guard whose coverage depends on somebody remembering to extend
it has the coverage of whoever remembered last.

So it enumerates `Base.metadata.tables`. A table added in any later slice is
covered the moment its model exists.
"""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

import control_db as cdb
from models import Base

# Every mapped table, so a new one is covered by existing rather than by being
# added here. Sorted for a stable test-id order in the report.
ALL_TABLES = sorted(Base.metadata.tables)


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    """One migrated database for the whole module — every check is read-only, so
    paying the alembic upgrade once per table would be pure waiting."""
    import api_common  # noqa: F401  (import parity with the other suites)
    db = tmp_path_factory.mktemp("drift") / "control.db"
    old = (cdb.CONTROL_DB, cdb._engine, cdb._engine_url)
    cdb.CONTROL_DB, cdb._engine, cdb._engine_url = db, None, None
    cdb.init_db()
    try:
        yield inspect(cdb.get_engine())
    finally:
        cdb.CONTROL_DB, cdb._engine, cdb._engine_url = old


@pytest.mark.parametrize("table", ALL_TABLES)
def test_every_index_the_model_declares_exists_in_the_migrated_schema(table, migrated):
    """Both spellings of a declaration count: `Index(...)` in `__table_args__`
    and `index=True` on a column, which SQLAlchemy names `ix_<table>_<column>`.

    The direction checked is model → migration. A migration creating an index the
    model does not declare is the reverse drift and is far less costly: the index
    exists and works, it is merely undocumented, where this direction means the
    query someone designed an index for is scanning instead."""
    model = Base.metadata.tables[table]
    declared = {ix.name for ix in model.indexes} | {
        f"ix_{table}_{c.name}" for c in model.columns if c.index}
    got = {ix["name"] for ix in migrated.get_indexes(table)}
    missing = sorted(declared - got)
    assert not missing, (
        f"{table}: declared on the model but never migrated: {missing}. "
        f"Either add the create_index to the revision or drop the declaration — "
        f"a redundant one is still drift.")


def test_the_drift_check_covers_every_table_the_model_defines():
    """The guard on the guard.

    A test cannot notice its own scope shrinking: replace `ALL_TABLES` with a
    literal and the suite runs fewer cases, all green. That is exactly how the
    earlier version of this check went from covering one table to missing the
    next one silently, so the derivation itself is what needs asserting.

    Fails if anyone swaps the derived list for a hand-maintained one — which is
    the failure mode, not a hypothetical: it has already happened twice on this
    check."""
    assert set(ALL_TABLES) == set(Base.metadata.tables), (
        "the drift check's scope must be DERIVED from Base.metadata, not listed. "
        f"Not covered: {sorted(set(Base.metadata.tables) - set(ALL_TABLES))}")
