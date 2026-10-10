"""add signals table (signal-memory plane)

Revision ID: e2a4f6c8b1d3
Revises: b7c1d9e42a08
Create Date: 2026-07-16 01:10:00.000000

The signal-memory datastore (S0, ARCH-349): one row per captured communication
event, on the same control DB as the decision plane. The embedding column + the
sqlite-vec index land in a later migration (S0-b) once the embedding source and
its dimension are fixed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2a4f6c8b1d3'
down_revision: Union[str, Sequence[str], None] = 'b7c1d9e42a08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'signals',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('source', sa.String(length=40), nullable=False),
        sa.Column('stream', sa.String(length=200), nullable=True),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('ingested_by', sa.String(length=200), nullable=False),
        sa.Column('product', sa.String(length=64), nullable=False),
        sa.Column('refs', sa.JSON(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('meta', sa.JSON(), nullable=False),
        sa.Column('privacy_class', sa.String(length=20), nullable=False),
        sa.Column('produced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_signals_source'), ['source'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_stream'), ['stream'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_product'), ['product'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_privacy_class'), ['privacy_class'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_ingested_at'), ['ingested_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_signals_ingested_at'))
        batch_op.drop_index(batch_op.f('ix_signals_privacy_class'))
        batch_op.drop_index(batch_op.f('ix_signals_product'))
        batch_op.drop_index(batch_op.f('ix_signals_stream'))
        batch_op.drop_index(batch_op.f('ix_signals_source'))
    op.drop_table('signals')
