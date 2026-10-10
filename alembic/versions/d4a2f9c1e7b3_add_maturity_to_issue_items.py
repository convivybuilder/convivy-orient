"""add maturity to issue_items — the bead accounting's bucket

Revision ID: d4a2f9c1e7b3
Revises: c3f8a1d6e4b2
Create Date: 2026-08-05 22:45:00.000000

Phase 2 of the Keel build plan, answering the question ADR-0024 left open and the operator
decided on 2026-08-05. One nullable column, no data rewritten.

WHAT IT CARRIES. `built | approved | proposed | superseded` — the four buckets a
bead's §6 accounting sorts capabilities into. `type` × `state` cannot express
them: that gives bug/feature/task against open/claimed/fixed/wontfix, which
cannot distinguish "approved but not built" from "still proposed".

WHY A COLUMN BESIDE `state` RATHER THAN INSIDE IT. `state` is LIFECYCLE and the
drain routes on it. An approved-but-unbuilt item is still `open` — the two axes
are orthogonal, and folding maturity into `state` would put an unknown value in
neither the fix lane nor the decision lane. The alternative of DERIVING the bucket
from evidence (approved = has a linked ADR, built = has a recorded caller) was
rejected because it works only where anchors exist, and missing anchors are
precisely the finding a bead exists to surface; §6.4 superseded has no evidence
signal to derive from at all.

**NULL IS "NOT YET ASSESSED" AND IT IS NOT `proposed`.** This is the whole reason
there is no backfill here, and getting it wrong would be quietly destructive: the
505 rows that predate this column have never been assessed against a bead, and
writing `proposed` into them would report the entire existing backlog as an
unapproved idea nobody has agreed to. A bead rendered from that would be
confidently wrong about the one thing it exists to be honest about.

So this is the OPPOSITE of what `topics` did one migration ago, and deliberately.
`topics` backfilled `[]` because a NULL there forced a reader to distinguish "no
topics" from "predates the column" and both answers led to identical behaviour.
Here the two answers lead to DIFFERENT behaviour: an unassessed item is not a
proposal, and an accounting must be able to say "I have not looked at this yet".

NO DEFAULT AND NO SERVER_DEFAULT, for the same reason. A default would silently
assign a bucket to every row created by every existing caller, none of which knows
about this field yet.

DOWNGRADE DROPS THE COLUMN AND IS LOSSY, honestly. Any bucket assigned after this
migration is gone on the way back down. That is acceptable while nothing reads it;
once a bead's accounting is rendered from this column rather than hand-written,
a downgrade stops being recoverable from the document.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a2f9c1e7b3'
down_revision: Union[str, Sequence[str], None] = 'c3f8a1d6e4b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A PLAIN add_column, NOT batch_alter_table, and the difference matters on a
    # 210MB table. SQLite can ADD COLUMN natively for a nullable column with no
    # default — `e4b2c7d9f3a1` (needs) does exactly this. Batch mode is for what
    # SQLite cannot do in place (dropping a column, adding a constraint), and it
    # works by rebuilding the whole table and re-reflecting its indexes. Paying
    # for a rebuild here would buy nothing and put every index and the vec0
    # neighbour tables through a reflection round-trip for no reason
    # (reviewer, PR #157; an earlier draft used batch and justified it with the
    # false premise that SQLite cannot ALTER at all).
    op.add_column("issue_items",
                  sa.Column("maturity", sa.String(length=16), nullable=True))

    # Indexed because the bead's accounting selects by bucket per project — four
    # queries of the shape `WHERE product = ? AND maturity = ?` to render §6.1
    # through §6.4. Without it each is a full scan of the Chart House.
    op.create_index("ix_issue_items_maturity", "issue_items", ["maturity"])

    # DELIBERATELY NO BACKFILL. See the docstring: NULL means "not yet assessed"
    # and must stay distinguishable from `proposed`.


def downgrade() -> None:
    op.drop_index("ix_issue_items_maturity", table_name="issue_items")
    # Batch HERE, unlike the upgrade, because dropping a column is the thing
    # SQLite genuinely cannot do in place — this is what batch mode is for.
    with op.batch_alter_table("issue_items") as batch:
        batch.drop_column("maturity")
