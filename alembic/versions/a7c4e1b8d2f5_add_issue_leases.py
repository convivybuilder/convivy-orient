"""add issue_leases — the epic lease's table

Revision ID: a7c4e1b8d2f5
Revises: d4a2f9c1e7b3
Create Date: 2026-08-05 17:10:00.000000

Phase 2 of the Keel build plan, implementing the checkout half of ARCH-424/428.
One new table, no existing row touched, no column added to `issue_items`.

THE LEASE IS ITS OWN TABLE RATHER THAN COLUMNS ON `issue_items`, and the reason is
that a lease and a claim are records about different objects. Checking out an
issue under epic E claims the ISSUE and leases the EPIC; storing the lease in
E's `claimed_by` would make an epic nobody is working on read as claimed in every
queue, every listing and every dashboard that already keys on that column.

ONE LIVE LEASE PER ROOT IS A PARTIAL UNIQUE INDEX, which is the mutual exclusion
itself rather than a hint about it. Two workers checking out siblings under the
same epic race; declaring the invariant to the database means the loser's INSERT
fails rather than the loser's SELECT happening to run first. `WHERE released_at
IS NULL` is what lets released rows stay for the override-rate history — ARCH-424
makes "how often did someone break a live lease" the measurement that says whether
epics are sized right, and a deleted row cannot carry it.

Unlike ADR-0023's foreign key, this constraint needs no pragma to run: SQLite has
enforced partial unique indexes since 3.8, and alembic's own connection enforces
it too. So the dry-run against a copy of the live database exercises the real
thing rather than a decorative version of it.

ON DELETE RESTRICT on `root_id`, matching `parent_id`. Deleting an item out from
under a live lease should refuse rather than silently drop a fence its holder is
still working inside.

THE 505 EXISTING ROWS GET NOTHING, deliberately. A lease is taken by an act, and
no act has happened yet. There is no backfill to get wrong, which is the whole
reason Phase 1 put `parent_id` in first: the schema the lease attaches to landed
while nothing held anything, so this migration adds a table to an empty problem.

DOWNGRADE DROPS LIVE LOCK STATE, and this is the migration Phase 1's docstring
warned about. Going back down while any lease is held unfences every subtree at
once with no signal to the holders still working inside them, and destroys the
release history the override rate is computed from. Release every lease first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c4e1b8d2f5'
down_revision: Union[str, Sequence[str], None] = 'd4a2f9c1e7b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "issue_leases",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("root_id", sa.String(length=40), nullable=False),
        sa.Column("taken_for", sa.String(length=40), nullable=False),
        sa.Column("holder", sa.String(length=200), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=20), nullable=True),
        sa.Column("released_by", sa.String(length=200), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["root_id"], ["issue_items.id"],
            name="fk_issue_leases_root_id", ondelete="RESTRICT"),
    )
    op.create_index("ix_issue_leases_root_id", "issue_leases", ["root_id"])
    # `taken_for` is what the `state=claimable` listing correlates on, once per
    # outer row. Unindexed it is a full scan of this table per issue on the
    # drain's hottest query — tolerable while the table is empty, and a full scan
    # of every released lease ever taken once the history accumulates (reviewer,
    # PR #158).
    op.create_index("ix_issue_leases_taken_for", "issue_leases", ["taken_for"])
    op.create_index("ix_issue_leases_holder", "issue_leases", ["holder"])
    # Reads that ask "how did leases end" — the override rate — scan on this.
    op.create_index("ix_issue_leases_release_reason", "issue_leases", ["release_reason"])
    # The invariant. See the module docstring for why it is partial and why it
    # belongs in the schema rather than in a Python guard.
    op.create_index("ix_issue_leases_live_root", "issue_leases", ["root_id"],
                    unique=True, sqlite_where=sa.text("released_at IS NULL"))


def downgrade() -> None:
    op.drop_index("ix_issue_leases_live_root", table_name="issue_leases")
    op.drop_index("ix_issue_leases_release_reason", table_name="issue_leases")
    op.drop_index("ix_issue_leases_holder", table_name="issue_leases")
    op.drop_index("ix_issue_leases_taken_for", table_name="issue_leases")
    op.drop_index("ix_issue_leases_root_id", table_name="issue_leases")
    op.drop_table("issue_leases")
