"""add lease_refusals — the collisions the fence caught

Revision ID: b3e7f2a9c481
Revises: a7c4e1b8d2f5
Create Date: 2026-08-05 22:10:00.000000

Keel Phase 2 slice 4, implementing the half of ADR-0019's instrumentation that
needed a build rather than a query.

THE LEASE RECORDED WHAT IT HELD AND NOT WHAT IT STOPPED. Slice 2 shipped the
refusal — a 409 naming the holder and the expiry — and stored nothing about it.
ADR-0019 asks for the duplicate-filing rate "alongside the epic lease's refusals:
a refused sibling claim is a collision the fence caught, where a duplicate filing
is one it did not." With only the duplicates countable, the mechanism would be
measured entirely on its failures.

IT CANNOT BE BACKFILLED, which is why it lands now rather than when someone first
wants the number. A refusal changes nothing: no state moves, no lease is created,
no event is appended. Every refusal issued between slice 2 shipping and this
migration is gone, and no later query can recover them.

APPEND-ONLY AND UNCONSTRAINED BY DESIGN. There is deliberately no foreign key on
`blocking_lease_id` or `requested_item_id`: the blocking lease will be released
and the item may be deleted long before anyone reads this, and a record that
disappears when its cause is cleaned up is not a record. `product` is copied in
for the same reason — a refusal states what was true when it happened, and
re-deriving it through the item would silently follow a later product change.

DOWNGRADE DROPS THE MEASUREMENT AND NOTHING ELSE. No other table references this
one, so going back down is safe for the system and destroys the collision history
permanently.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e7f2a9c481'
down_revision: Union[str, Sequence[str], None] = 'a7c4e1b8d2f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lease_refusals",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_item_id", sa.String(length=40), nullable=False),
        sa.Column("product", sa.String(length=64), nullable=False),
        sa.Column("requester", sa.String(length=200), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("blocking_lease_id", sa.String(length=40), nullable=False),
        sa.Column("blocking_root_id", sa.String(length=40), nullable=False),
        sa.Column("blocking_holder", sa.String(length=200), nullable=False),
        sa.Column("blocks_by", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lease_refusals_at", "lease_refusals", ["at"])
    op.create_index("ix_lease_refusals_actor", "lease_refusals", ["actor"])
    op.create_index("ix_lease_refusals_requested_item_id", "lease_refusals",
                    ["requested_item_id"])
    op.create_index("ix_lease_refusals_blocking_root_id", "lease_refusals",
                    ["blocking_root_id"])
    # The measure is a rate over time, per product.
    op.create_index("ix_lease_refusals_product_at", "lease_refusals", ["product", "at"])


def downgrade() -> None:
    op.drop_index("ix_lease_refusals_product_at", table_name="lease_refusals")
    op.drop_index("ix_lease_refusals_blocking_root_id", table_name="lease_refusals")
    op.drop_index("ix_lease_refusals_requested_item_id", table_name="lease_refusals")
    op.drop_index("ix_lease_refusals_actor", table_name="lease_refusals")
    op.drop_index("ix_lease_refusals_at", table_name="lease_refusals")
    op.drop_table("lease_refusals")
