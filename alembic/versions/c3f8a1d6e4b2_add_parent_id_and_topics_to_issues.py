"""add parent_id and topics to issue_items — the epic model's schema

Revision ID: c3f8a1d6e4b2
Revises: b7e3d9c4a2f8
Create Date: 2026-08-04 18:40:00.000000

Phase 1 of the Keel build plan, implementing ADR-0023. Two columns, both nullable,
no data rewritten.

WHY THIS LANDS BEFORE CHECKOUT RATHER THAN WITH IT. `parent_id` is what the epic
LEASE attaches to. ADR-0020 holds that the lock's schema must exist from the start
because retrofitting it means migrating live lock state — moving a fence while
sessions are holding it. Adding the column now, while nothing leases anything, is
the cheap moment.

THE FOREIGN KEY IS REAL AND IT RUNS. `control_db` sets `PRAGMA foreign_keys=ON`
in a connect listener on every app connection, which is also what this schema's
existing CASCADE deletes depend on, so the constraint is enforced rather than
decorative. An earlier draft of this migration omitted it on the stated grounds
that the pragma was never set; that premise was simply false (reviewer, PR #154).

ON DELETE RESTRICT rather than CASCADE. CASCADE would delete an entire subtree
when its epic is deleted, silently, with no record of what went. RESTRICT refuses
the delete instead, matching how the rest of the model treats an epic with
dependants: re-parent or resolve the children first, as a decision about the work.

THE CONSTRAINT COVERS EXISTENCE AND NOTHING ELSE, which is worth stating so nobody
reads it as the whole guard. That the parent is an epic, that it sits in the same
product, and that the edge closes no cycle are checked in
`issue_api._validate_parent`, because a database has no opinion about any of them.
The constraint is defence in depth under that validation: a future write path that
forgets to call it still cannot store a dangling parent.

Note alembic's own connection does NOT get the pragma, so this migration's own
statements run unenforced. That is fine here — every existing row gets a NULL
`parent_id`, so there is nothing for the constraint to reject on the way in.

`topics` DEFAULTS TO AN EMPTY LIST, NOT NULL, for the reason `embedded_at`
deliberately does the opposite. A NULL `embedded_at` means "not yet embedded" and
the sweeper needs to find those rows, so NULL carries information there. A NULL
`topics` would mean nothing a reader could act on: they would have to distinguish
"this item has no topics" from "this item predates the column", and both answers
lead to the same behaviour. Backfilling `[]` removes a distinction nobody needs.

DOWNGRADE DROPS BOTH COLUMNS AND IS HONESTLY LOSSY. Any nesting established after
this migration is gone on the way back down, and so are any topics. That is
acceptable while nothing depends on them — which is true only until checkout
ships. Once a lease exists, a downgrade is a data-loss event and this docstring
stops being sufficient warning.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f8a1d6e4b2'
down_revision: Union[str, Sequence[str], None] = 'b7e3d9c4a2f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table because SQLite cannot ADD COLUMN with every modifier and
    # cannot ALTER at all; alembic rebuilds the table. Both columns are nullable,
    # so no server_default is needed to make the ADD succeed on existing rows.
    with op.batch_alter_table("issue_items") as batch:
        batch.add_column(sa.Column("parent_id", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("topics", sa.JSON(), nullable=True))
        # Self-referential and named, because SQLite's batch rebuild needs a name
        # to reproduce the constraint and an unnamed one cannot be dropped later.
        batch.create_foreign_key(
            "fk_issue_items_parent_id", "issue_items",
            ["parent_id"], ["id"], ondelete="RESTRICT")

    # Indexed because every lease operation walks it: the upward walk resolves an
    # item to the epic it leases, and the downward walk selects children by parent.
    # The downward walk is `WHERE parent_id IN (...)` per level, which without this
    # index is a full scan of the Chart House per level of depth.
    op.create_index("ix_issue_items_parent_id", "issue_items", ["parent_id"])

    # Backfill `[]` so no reader has to handle NULL. Done here rather than via a
    # server_default so the stored value is a real JSON array rather than a string
    # literal the JSON type would hand back unparsed.
    conn = op.get_bind()
    t = sa.table("issue_items", sa.column("topics", sa.JSON))
    conn.execute(t.update().where(t.c.topics.is_(None)).values(topics=[]))


def downgrade() -> None:
    op.drop_index("ix_issue_items_parent_id", table_name="issue_items")
    with op.batch_alter_table("issue_items") as batch:
        batch.drop_constraint("fk_issue_items_parent_id", type_="foreignkey")
        batch.drop_column("topics")
        batch.drop_column("parent_id")
