"""add embedded_at to issue_items (Library semantic layer)

Revision ID: a1b4c7e2d8f6
Revises: f5c2e8d1b9a7
Create Date: 2026-07-31 08:40:00.000000

ARCH-425 Phase 1: the Workshop joins the semantic index, so a work item needs the
same embed stamp a signal has. NULL = not yet embedded; the sweeper drains NULLs.

Every existing row lands NULL, which is deliberate — the running sweeper embeds the
Workshop's backlog on its own and no separate backfill script is needed.

The vec0 virtual table itself is NOT alembic-managed — control_db.init_db creates it
idempotently, because alembic's connection doesn't load the sqlite-vec extension.
The same init_db pass copies the pre-Phase-1 signal_vectors rows into the widened
index as the archives vault.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b4c7e2d8f6'
down_revision: Union[str, Sequence[str], None] = 'f5c2e8d1b9a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('issue_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('embedded_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f('ix_issue_items_embedded_at'), ['embedded_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('issue_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_issue_items_embedded_at'))
        batch_op.drop_column('embedded_at')
