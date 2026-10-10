"""add reading room tables (ARCH-357)

Revision ID: d1e2f3a4b5c6
Revises: a9c3e5f7d2b4
Create Date: 2026-07-17 16:00:00.000000

The Reading Room curation store (ARCH-357): reading_rooms (one per member) +
room_entries (the curated docs, ordered, sectioned, annotated). The GRANT — what a
member may see — is never stored here: it is computed live from tiers.json +
roles.json + audience frontmatter (kb_index.room_entitlement). These tables hold
only the per-member CURATION, so there is no materialized grant to drift.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'a9c3e5f7d2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reading_rooms',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('email', sa.String(length=200), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('landing_note', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_by', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_reading_rooms_email'), 'reading_rooms', ['email'], unique=True)
    op.create_index(op.f('ix_reading_rooms_status'), 'reading_rooms', ['status'], unique=False)
    op.create_table(
        'room_entries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('room_id', sa.String(length=40), nullable=False),
        sa.Column('doc_id', sa.String(length=200), nullable=False),
        sa.Column('section', sa.String(length=120), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('pinned', sa.Boolean(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('added_by', sa.String(length=200), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['room_id'], ['reading_rooms.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('room_id', 'doc_id', name='uq_room_entry_doc'),
    )
    op.create_index(op.f('ix_room_entries_room_id'), 'room_entries', ['room_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_room_entries_room_id'), table_name='room_entries')
    op.drop_table('room_entries')
    op.drop_index(op.f('ix_reading_rooms_status'), table_name='reading_rooms')
    op.drop_index(op.f('ix_reading_rooms_email'), table_name='reading_rooms')
    op.drop_table('reading_rooms')
