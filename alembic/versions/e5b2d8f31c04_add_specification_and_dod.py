"""add specification and definition_of_done to issue_items

Revision ID: e5b2d8f31c04
Revises: d4a1c6b90e73
Create Date: 2026-08-06 17:10:00.000000

ADR-0030: an epic cannot become `approved` while either is empty.

`approved` became load-bearing when the Course shipped — it is what the unplanned
invariant fires on, and ADR-0024 makes it the bucket that becomes the build queue.
Until now the only requirement was the `decide` capability, so the field recorded
WHO said yes and nothing about WHAT they said yes to.

BOTH ARE NULLABLE AND NOTHING IS BACKFILLED. Every existing row predates the
requirement, and inventing a specification for one would be worse than the gap:
it would read as something an operator had approved. The 9 epics in the store on
2026-08-06 are all `proposed`, so none of them is grandfathered past a gate — the
first approval of each is where the requirement bites, which is the intent.

NO INDEXES. Both are prose read one row at a time, on an item already fetched by
id. An index would be drift for the schema-drift test to catch and nothing else.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b2d8f31c04'
down_revision: Union[str, Sequence[str], None] = 'd4a1c6b90e73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("issue_items", sa.Column("specification", sa.Text(), nullable=True))
    op.add_column("issue_items",
                  sa.Column("definition_of_done", sa.Text(), nullable=True))


def downgrade() -> None:
    # Dropping these destroys every specification an operator approved, and with
    # them the only record of what an `approved` epic was approved FOR. The
    # maturity values survive and become unbacked claims.
    op.drop_column("issue_items", "definition_of_done")
    op.drop_column("issue_items", "specification")
