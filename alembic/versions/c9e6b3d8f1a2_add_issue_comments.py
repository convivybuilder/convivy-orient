"""add issue_comments (the diagnosis thread, ARCH-373)

The issue queue had nowhere to put a DIAGNOSIS. The Slack flow it replaces
carried state in reactions, the diagnosis in a threaded reply, and the
back-and-forth around it in the rest of the thread. IssueItem already modelled
state and resolution; without this table, migrating bug-investigator would move
the state and strand the handoff that bug-fixer actually consumes.

Mirrors decision_comments, plus `kind` — so `diagnosis` is findable rather than
inferred from prose, and a decline note cannot be mistaken for one.

Revision ID: c9e6b3d8f1a2
Revises: b8d5f2a7c3e1
Create Date: 2026-07-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9e6b3d8f1a2"
down_revision: Union[str, Sequence[str], None] = "b8d5f2a7c3e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "issue_comments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False,
                  server_default="note"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["item_id"], ["issue_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_issue_comments_item_id", "issue_comments", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_issue_comments_item_id", table_name="issue_comments")
    op.drop_table("issue_comments")
