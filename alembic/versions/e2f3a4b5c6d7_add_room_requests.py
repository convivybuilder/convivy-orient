"""add room_requests (Reading Room concierge, ARCH-357 fast-follow)

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-17 18:00:00.000000

The concierge request→edit loop's audit table: one row per member request to
change their own room — the raw body, the model's action plan, the outcome, the
reply shown, and (when a grant-widening ask escalates) the DecisionItem id.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'room_requests',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('room_id', sa.String(length=40), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('plan', sa.JSON(), nullable=False),
        sa.Column('reply', sa.Text(), nullable=False),
        sa.Column('decision_id', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['room_id'], ['reading_rooms.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_room_requests_room_id'), 'room_requests', ['room_id'], unique=False)
    op.create_index(op.f('ix_room_requests_created_at'), 'room_requests', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_room_requests_created_at'), table_name='room_requests')
    op.drop_index(op.f('ix_room_requests_room_id'), table_name='room_requests')
    op.drop_table('room_requests')
