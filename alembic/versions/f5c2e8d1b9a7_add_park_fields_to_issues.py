"""add park_reason, unpark_condition, parked_at to issue_items (ARCH-417)

A real slice of the queue is correctly open but cannot be drained — it is gated
on a condition that has not happened. Leaving those issues in `open` makes the
queue count read as a backlog when part of it is a waiting list, and makes every
nightly drain re-triage them forever.

`parked` is non-terminal but inactive: it leaves ISSUE_ACTIVE, `open`, and
`claimable`, so no drain sees it and the active count stops including it; it
stays reachable and can return to `open`. Parking requires BOTH a `park_reason`
and an `unpark_condition` — omitting either is rejected at the API layer.

NULL hazard (ARCH-415 lesson): SQL three-valued logic means `col != value` is
FALSE for NULL, so any query that excludes a value by inequality must also add
an explicit `is_(None)` branch or every pre-column row silently disappears from
the result. The claimable query already handles this for `needs`; the parked
exclusion in that query does the same for `parked_at` by using the state column
rather than `parked_at` directly (state='parked', which is a stored string, has
no NULL hazard). park_reason and unpark_condition are nullable with no default:
a row predating the column reads NULL on both, which is correct since it was
never parked.

Revision ID: f5c2e8d1b9a7
Revises: e4b2c7d9f3a1
Create Date: 2026-07-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5c2e8d1b9a7"
down_revision: Union[str, Sequence[str], None] = "e4b2c7d9f3a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("issue_items", sa.Column("park_reason", sa.Text(), nullable=True))
    op.add_column("issue_items", sa.Column("unpark_condition", sa.Text(), nullable=True))
    op.add_column("issue_items", sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("issue_items", "parked_at")
    op.drop_column("issue_items", "unpark_condition")
    op.drop_column("issue_items", "park_reason")
