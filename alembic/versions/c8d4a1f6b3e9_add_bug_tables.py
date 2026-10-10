"""add bug tables (ARCH-359 step 2a)

Revision ID: c8d4a1f6b3e9
Revises: e2f3a4b5c6d7
Create Date: 2026-07-17 20:00:00.000000

The in-box bug queue's schema: bug_items (one addressable object per bug,
mirroring decision_items) + bug_events (its append-only audit trail, mirroring
decision_events). States: open -> claimed -> (fixed | wontfix | duplicate).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d4a1f6b3e9'
down_revision: Union[str, Sequence[str], None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'bug_items',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('product', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('refs', sa.JSON(), nullable=False),
        sa.Column('severity', sa.String(length=20), nullable=False),
        sa.Column('state', sa.String(length=20), nullable=False),
        sa.Column('claimed_by', sa.String(length=200), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolution', sa.String(length=20), nullable=True),
        sa.Column('resolved_by', sa.String(length=200), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('outcome_note', sa.Text(), nullable=True),
        sa.Column('once_key', sa.String(length=300), nullable=True),
        sa.Column('created_by', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('run_id', sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('once_key'),
    )
    with op.batch_alter_table('bug_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bug_items_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_bug_items_product'), ['product'], unique=False)
        batch_op.create_index(batch_op.f('ix_bug_items_severity'), ['severity'], unique=False)
        batch_op.create_index(batch_op.f('ix_bug_items_state'), ['state'], unique=False)

    op.create_table(
        'bug_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('item_id', sa.String(length=40), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('from_state', sa.String(length=20), nullable=True),
        sa.Column('to_state', sa.String(length=20), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['bug_items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('bug_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bug_events_item_id'), ['item_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('bug_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bug_events_item_id'))
    op.drop_table('bug_events')

    with op.batch_alter_table('bug_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bug_items_state'))
        batch_op.drop_index(batch_op.f('ix_bug_items_severity'))
        batch_op.drop_index(batch_op.f('ix_bug_items_product'))
        batch_op.drop_index(batch_op.f('ix_bug_items_created_at'))
    op.drop_table('bug_items')
