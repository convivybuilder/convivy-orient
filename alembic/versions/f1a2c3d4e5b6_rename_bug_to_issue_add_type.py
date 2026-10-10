"""rename bug tables to issue + add type column

ARCH-361: the /bugs queue becomes /issues and widens to hold every kind of work
we'd previously have opened a GitHub issue for. Three changes:

  1. bug_items -> issue_items  and  bug_events -> issue_events
  2. the five ix_bug_* indexes renamed to ix_issue_*
  3. a new `type` column (bug | nit | improvement | feature | task)

Existing rows backfill to type='bug'. That default is honest rather than
convenient: every row predating this migration was captured by the bug queue,
so 'bug' is what they actually are.

Two SQLite notes:

- A table rename carries dependent FK references with it when legacy_alter_table
  is off (the SQLAlchemy default), so issue_events.item_id follows the parent to
  issue_items without a table rebuild.
- SQLite has no ALTER INDEX ... RENAME, and an index follows its table across a
  rename while KEEPING its old name. Left alone, issue_items would carry indexes
  still called ix_bug_*, drifting from the model's expected names and churning
  every future autogenerate. So each is dropped and recreated under its new name.

Revision ID: f1a2c3d4e5b6
Revises: c8d4a1f6b3e9
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f1a2c3d4e5b6"
down_revision = "c8d4a1f6b3e9"
branch_labels = None
depends_on = None

# (column, old index name, new index name) for the indexes carried by the rename.
_ITEM_INDEXES = [
    ("created_at", "ix_bug_items_created_at", "ix_issue_items_created_at"),
    ("product", "ix_bug_items_product", "ix_issue_items_product"),
    ("severity", "ix_bug_items_severity", "ix_issue_items_severity"),
    ("state", "ix_bug_items_state", "ix_issue_items_state"),
]
_EVENT_INDEX = ("item_id", "ix_bug_events_item_id", "ix_issue_events_item_id")


def upgrade() -> None:
    op.rename_table("bug_items", "issue_items")
    op.rename_table("bug_events", "issue_events")

    for column, old_name, new_name in _ITEM_INDEXES:
        op.drop_index(old_name, table_name="issue_items")
        op.create_index(new_name, "issue_items", [column])
    column, old_name, new_name = _EVENT_INDEX
    op.drop_index(old_name, table_name="issue_events")
    op.create_index(new_name, "issue_events", [column])

    # server_default backfills every existing row in one statement; the column is
    # NOT NULL because the drain routes on it and a null type routes nowhere.
    op.add_column(
        "issue_items",
        sa.Column("type", sa.String(length=20), nullable=False, server_default="bug"),
    )
    op.create_index("ix_issue_items_type", "issue_items", ["type"])


def downgrade() -> None:
    op.drop_index("ix_issue_items_type", table_name="issue_items")
    op.drop_column("issue_items", "type")

    column, old_name, new_name = _EVENT_INDEX
    op.drop_index(new_name, table_name="issue_events")
    op.create_index(old_name, "issue_events", [column])
    for column, old_name, new_name in _ITEM_INDEXES:
        op.drop_index(new_name, table_name="issue_items")
        op.create_index(old_name, "issue_items", [column])

    op.rename_table("issue_events", "bug_events")
    op.rename_table("issue_items", "bug_items")
