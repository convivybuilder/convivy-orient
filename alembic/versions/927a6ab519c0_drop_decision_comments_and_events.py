"""drop decision_comments and decision_events

Revision ID: 927a6ab519c0
Revises: e5b2d8f31c04
Create Date: 2026-08-18 22:30:00.000000

The decision plane is removed from Orient (the operator, 2026-08-18: "we're getting rid
of the concept of decisions"). `decision_api.py` and `decisions_build.py` are
deleted in the same change, and they were the only code that read or wrote
`decision_comments` and `decision_events` — nothing else in this repository
touches either table, so both go.

`decision_items` DOES NOT move in this migration, even though the epic that
removes the decision plane asks for it too
(issue_01M0AW26VCCNMZ0PP02GKEP722's definition of done). `issue_api.py` still
reads it directly — a real, pending, same-product decision is what a
feature-gate escalation names (`escalate_issue`) and what the claimable-features
query joins against (`create_issue`'s Tier 1.2 gate) — and `issue_api.py` is
itself still mounted and live. Dropping `decision_items` out from under a live
reader is the second-order failure this epic's own text warns against ("check
the second-order effects before assuming one line is the whole fix"), so this
table is deliberately left in place, unwritten from now on (nothing left in
this codebase can write to it once `decision_api.py` is gone), until
`issue_api.py` itself is deleted — the next epic on Orient's Course
(issue_01M08YKNV185DFJ6832GV8ZY22).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '927a6ab519c0'
down_revision: Union[str, Sequence[str], None] = 'e5b2d8f31c04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('decision_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_decision_events_item_id'))
    op.drop_table('decision_events')

    with op.batch_alter_table('decision_comments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_decision_comments_item_id'))
    op.drop_table('decision_comments')


def downgrade() -> None:
    """Downgrade schema."""
    import sqlalchemy as sa

    op.create_table('decision_comments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('item_id', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['decision_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('decision_comments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_decision_comments_item_id'), ['item_id'], unique=False)

    op.create_table('decision_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('item_id', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('from_state', sa.String(length=20), nullable=True),
    sa.Column('to_state', sa.String(length=20), nullable=False),
    sa.Column('directive', sa.Text(), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['decision_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('decision_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_decision_events_item_id'), ['item_id'], unique=False)
