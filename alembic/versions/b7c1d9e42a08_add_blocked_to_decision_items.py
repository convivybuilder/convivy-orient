"""add blocked flag to decision_items

Revision ID: b7c1d9e42a08
Revises: c69664130577
Create Date: 2026-07-16 00:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c1d9e42a08'
down_revision: Union[str, Sequence[str], None] = 'c69664130577'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the blocked flag (WS1.1, ARCH-349). server_default false so any existing
    rows backfill to False under SQLite's batch (table-rebuild) ALTER."""
    with op.batch_alter_table('decision_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('blocked', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.create_index(batch_op.f('ix_decision_items_blocked'), ['blocked'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('decision_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_decision_items_blocked'))
        batch_op.drop_column('blocked')
