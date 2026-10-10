"""add embedded_at to signals (semantic layer)

Revision ID: f4b7d2e9a5c1
Revises: e2a4f6c8b1d3
Create Date: 2026-07-16 02:05:00.000000

S0-b (ARCH-349): tracks when a signal's embedding landed in the vec0 index.
NULL = not yet embedded; the backfill sweeps NULLs. The vec0 virtual table itself
is NOT alembic-managed — control_db.init_db creates it idempotently, because
alembic's connection doesn't load the sqlite-vec extension (the same seam that
required init_db to persist WAL).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4b7d2e9a5c1'
down_revision: Union[str, Sequence[str], None] = 'e2a4f6c8b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('embedded_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_signals_embedded_at'), ['embedded_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_signals_embedded_at'))
        batch_op.drop_column('embedded_at')
