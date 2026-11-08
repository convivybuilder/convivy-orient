"""The b7e3d9c4a2f8 backfill fixes one casing bug and touches nothing else.
Its repo-name entries were removed for the 0.7.0 name gate (CQ-171): both
tables it rewrites are dropped by b031daf7f4c0 later in the chain, so they
changed no data that survives, and it no longer reads the registry at all.

Exercised against a throwaway table rather than the real `issue_items`/
`decision_items` schema — this migration's own logic (which values it
resolves and how) is what's under test, not the rest of the vault schema,
and `test_schema_drift.py`'s `migrated` fixture already proves a fresh,
empty database passes through this migration (and every other) with no
registry dependency at all, since there is nothing for it to backfill."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

import crew_registry

_MIGRATION_PATH = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
                   / "b7e3d9c4a2f8_backfill_canonical_products.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("b7e3d9c4a2f8", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration(monkeypatch):
    """The migration module, with `_TABLES` repointed at one throwaway table
    this test creates and seeds itself, so the test owns the schema rather
    than depending on the real vault tables (which a later migration drops)."""
    mod = _load_migration()
    monkeypatch.setattr(mod, "_TABLES", ("fake_items",))
    return mod


@pytest.fixture
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE fake_items (id TEXT PRIMARY KEY, product TEXT)"))
    with engine.connect() as c:
        yield c


def _seed(conn, rows: dict[str, str]) -> None:
    t = sa.table("fake_items", sa.column("id", sa.String), sa.column("product", sa.String))
    for item_id, product in rows.items():
        conn.execute(t.insert().values(id=item_id, product=product))
    conn.commit()


def _run_upgrade(migration, conn) -> None:
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        migration.upgrade()


def _products(conn) -> dict[str, str]:
    t = sa.table("fake_items", sa.column("id", sa.String), sa.column("product", sa.String))
    return dict(conn.execute(sa.select(t.c.id, t.c.product)).all())


def test_the_case_fix_applies_and_nothing_else_is_touched(migration, conn, monkeypatch):
    """`Substrate` becomes `substrate`. Every other value, including a repo
    name the original version folded into its unit, is left as it was, and
    the registry is never read."""
    def _boom():
        raise AssertionError("this migration must not read the registry")
    monkeypatch.setattr(crew_registry, "units", _boom)
    _seed(conn, {"i1": "Substrate", "i2": "alpha-services", "i3": "substrate",
                 "i4": "unrelated"})
    _run_upgrade(migration, conn)
    assert _products(conn) == {"i1": "substrate", "i2": "alpha-services",
                               "i3": "substrate", "i4": "unrelated"}


def test_nothing_to_backfill_writes_nothing(migration, conn):
    _seed(conn, {"i1": "alpha", "i2": "substrate"})
    _run_upgrade(migration, conn)
    assert _products(conn) == {"i1": "alpha", "i2": "substrate"}


def test_downgrade_raises(migration):
    with pytest.raises(NotImplementedError, match="one-way"):
        migration.downgrade()
