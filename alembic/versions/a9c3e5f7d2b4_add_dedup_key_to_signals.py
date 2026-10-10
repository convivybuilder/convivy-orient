"""add dedup_key to signals (idempotent capture)

Revision ID: a9c3e5f7d2b4
Revises: f4b7d2e9a5c1
Create Date: 2026-07-16 03:05:00.000000

S1 (ARCH-349): repeated capture runs (the GitHub review backfill + its incremental
poller) must be idempotent — a crash-and-rerun or an overlapping since-window must
not duplicate rows. dedup_key is the signals analogue of decision once_key: a
globally-unique caller-supplied key; the create API returns the existing row on
collision (with the same cross-product no-leak rule).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c3e5f7d2b4'
down_revision: Union[str, Sequence[str], None] = 'f4b7d2e9a5c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dedup_key', sa.String(length=300), nullable=True))
        batch_op.create_unique_constraint('uq_signals_dedup_key', ['dedup_key'])


def downgrade() -> None:
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.drop_constraint('uq_signals_dedup_key', type_='unique')
        batch_op.drop_column('dedup_key')
