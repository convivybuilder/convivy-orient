"""add push_subscriptions (Web Push, ARCH-362 Phase 4b)

Revision ID: b8d5f2a7c3e1
Revises: a7c4e1f9b2d8
Create Date: 2026-07-19 06:00:00.000000

The second half of the Phase 4 gate. Acknowledgment (4a) records that the operator has
seen an alert; push is what makes the alert reach them when they are away from a
browser at all. Until both exist, no alert-class Slack post may be retired,
because the site is pull-only and removing the post would silently strip a
property nothing replaces.

PER SUBSCRIPTION, keyed by endpoint. One person has several browsers; each
registration is its own row, so revoking one device does not silence the others.
`endpoint` is unique because it IS the browser's identifier for the subscription
— re-subscribing the same browser must update the existing row rather than
accumulate duplicates that each deliver the same notification.

The keys stored here (`p256dh`, `auth`) are the browser's PUBLIC encryption
material, not secrets of ours: they let the server encrypt a payload only that
browser can read. The VAPID PRIVATE key is deliberately NOT in the database —
it lives in ~/.orient-control/vapid.json at 0600, the same posture as the
capture token, so a database read never yields the ability to sign as us.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d5f2a7c3e1'
down_revision: Union[str, Sequence[str], None] = 'a7c4e1f9b2d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'push_subscriptions',
        sa.Column('id', sa.String(length=40), nullable=False),
        # The CF-Access-verified email that registered this browser. Push is
        # addressed per person, so a notification is only ever sent to the rows
        # belonging to the identity entitled to see the signal.
        sa.Column('identity', sa.String(length=200), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh', sa.String(length=200), nullable=False),
        sa.Column('auth', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        # Delivery health. A push service returns 404/410 for a subscription the
        # browser has discarded; those are pruned rather than retried forever.
        sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failures', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint', name='uq_push_subscriptions_endpoint'),
    )
    op.create_index(op.f('ix_push_subscriptions_identity'), 'push_subscriptions',
                    ['identity'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_push_subscriptions_identity'), table_name='push_subscriptions')
    op.drop_table('push_subscriptions')
