"""add needs to issue_items (who can execute it)

An issue's `type` says what kind of work it is and `severity` says how much it
matters. Neither says WHO CAN DO IT, and until now nothing did.

The gap had a concrete cost on 2026-07-26. A vivy decision stated in plain words
that setting TOKEN_ENCRYPTION_KEY "needs an operator action in the Render
dashboard, so no dispatched coder can close it and the overnight queue cannot
drain it" — and when that decision was swept into the issue queue, there was no
field to carry the sentence. The only way to record it was a prose comment, which
no drain can read.

The failure that prevents is specific: a drain claims an issue no agent can
finish, the work fails, and the issue sits CLAIMED with no outcome — invisible to
the next pass until its 24h TTL expires, then claimed and abandoned again. The
items needing a human are precisely the ones that would be starved forever.

`operator` is excluded from the `claimable` query, so a drain never picks one up.
`either` stays claimable: an agent that can prepare a change for a human to apply
is doing real work.

Nullable with no server default, matching created_by_kind's precedent: a row
created before this column reads NULL, which the serialiser treats as "agent" —
the behaviour the queue already had, so no existing row changes meaning. No
backfill; an issue's executor should not be invented after the fact.

Revision ID: e4b2c7d9f3a1
Revises: d3f1a8c6e4b2
Create Date: 2026-07-26
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4b2c7d9f3a1"
down_revision: Union[str, Sequence[str], None] = "d3f1a8c6e4b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "issue_items",
        sa.Column("needs", sa.String(length=20), nullable=True),
    )
    op.create_index("ix_issue_items_needs", "issue_items", ["needs"])


def downgrade() -> None:
    op.drop_index("ix_issue_items_needs", table_name="issue_items")
    op.drop_column("issue_items", "needs")
