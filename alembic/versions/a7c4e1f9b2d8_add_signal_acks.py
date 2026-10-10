"""add signal_acks (per-identity alert acknowledgment)

Revision ID: a7c4e1f9b2d8
Revises: f1a2c3d4e5b6
Create Date: 2026-07-19 05:30:00.000000

ARCH-362 Phase 4a. Acknowledgment is the read/ack state the Alerts page needs
before any alert-class Slack post can be retired: the site is pull-only, so
"The operator has seen this" has to be recordable somewhere or removing the Slack post
strips a property nothing replaces.

PER IDENTITY, not global, and that is the load-bearing choice. Alerts are not
addressed to "the team" — an infra alert is the operator's to clear, and a contributor
acknowledging it must not clear the operator's badge. A global ack column on `signals`
would have been simpler and would have quietly made one person's reading
silence everyone's queue.

Its own table rather than columns on `signals` for the same reason: `signals` is
the generic capture primitive for every source, and acknowledgment is a
per-viewer relationship to a row, not a property of the row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c4e1f9b2d8'
down_revision: Union[str, Sequence[str], None] = 'f1a2c3d4e5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'signal_acks',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('signal_id', sa.String(length=40), nullable=False),
        # The CF-Access-verified email. Not a display label — the label is
        # cosmetic and changeable; the email is what authz resolves against.
        sa.Column('identity', sa.String(length=200), nullable=False),
        sa.Column('acked_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        # One ack per (signal, identity). Re-acking is idempotent rather than a
        # duplicate row, so a double-tap on a phone cannot inflate anything.
        sa.UniqueConstraint('signal_id', 'identity', name='uq_signal_acks_signal_identity'),
    )
    op.create_index(op.f('ix_signal_acks_signal_id'), 'signal_acks', ['signal_id'], unique=False)
    op.create_index(op.f('ix_signal_acks_identity'), 'signal_acks', ['identity'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_signal_acks_identity'), table_name='signal_acks')
    op.drop_index(op.f('ix_signal_acks_signal_id'), table_name='signal_acks')
    op.drop_table('signal_acks')
