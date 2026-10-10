"""drop the remaining vault tables

Revision ID: b031daf7f4c0
Revises: 927a6ab519c0
Create Date: 2026-08-19 00:30:00.000000

Orient stops holding a copy of the vaults (issue_01M08YKNV185DFJ6832GV8ZY22).
Drops the issue queue, signal memory, the Course, and the decision plane's
last table (decision_items — frozen and unwritten since the plane's removal;
its only reader, issue_api.py, is deleted in the same change). What survives:
reading_rooms, room_entries, room_requests, push_subscriptions — Orient's own
concerns under ADR-0022.

The vec0 virtual tables (`library_vectors`, `signal_vectors`) are NOT dropped
here — alembic's connection does not load the sqlite-vec extension a virtual
table needs to be touched at all, the same seam their own DDL always sat
outside alembic for. `control_db.init_db`'s `_drop_vector_tables_once` handles
them instead, on the same app-session connection that already carries the
extension.

Drop order is child-before-parent for every real foreign key; `lease_refusals`
and `signal_acks` declare none (by design — see their models) and are ordered
here to read as one deletion rather than for any FK reason.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b031daf7f4c0'
down_revision: Union[str, Sequence[str], None] = '927a6ab519c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table('issue_comments')
    op.drop_table('issue_events')
    op.drop_table('issue_leases')
    op.drop_table('lease_refusals')
    op.drop_table('issue_items')
    op.drop_table('course_events')
    op.drop_table('courses')
    op.drop_table('signal_acks')
    op.drop_table('signals')
    op.drop_table('decision_items')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table('decision_items',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('product', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('refs', sa.JSON(), nullable=False),
    sa.Column('blocked', sa.Boolean(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('directive', sa.Text(), nullable=True),
    sa.Column('snooze_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('once_key', sa.String(length=200), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('run_id', sa.String(length=100), nullable=True),
    sa.Column('outcome_note', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('once_key')
    )
    with op.batch_alter_table('decision_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_decision_items_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_decision_items_product'), ['product'], unique=False)
        batch_op.create_index(batch_op.f('ix_decision_items_state'), ['state'], unique=False)
        batch_op.create_index(batch_op.f('ix_decision_items_blocked'), ['blocked'], unique=False)

    op.create_table('signals',
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
    sa.Column('dedup_key', sa.String(length=300), nullable=True),
    sa.Column('produced_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('embedded_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dedup_key')
    )
    with op.batch_alter_table('signals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_signals_source'), ['source'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_stream'), ['stream'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_product'), ['product'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_privacy_class'), ['privacy_class'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_ingested_at'), ['ingested_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_signals_embedded_at'), ['embedded_at'], unique=False)

    op.create_table('signal_acks',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('signal_id', sa.String(length=40), nullable=False),
    sa.Column('identity', sa.String(length=200), nullable=False),
    sa.Column('acked_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('signal_id', 'identity', name='uq_signal_acks_signal_identity')
    )
    with op.batch_alter_table('signal_acks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_signal_acks_signal_id'), ['signal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_signal_acks_identity'), ['identity'], unique=False)

    op.create_table('courses',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('product', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('entries', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.String(length=200), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('courses', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_courses_product'), ['product'], unique=False)
        batch_op.create_index(batch_op.f('ix_courses_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_courses_created_at'), ['created_at'], unique=False)
        batch_op.create_index('ix_courses_live_product', ['product'],
                              unique=True, sqlite_where=sa.text("status = 'live'"))

    op.create_table('course_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('course_id', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=20), nullable=False),
    sa.Column('entries', sa.JSON(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('course_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_course_events_course_id'), ['course_id'], unique=False)

    op.create_table('issue_items',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('product', sa.String(length=64), nullable=False),
    sa.Column('parent_id', sa.String(length=40), nullable=True),
    sa.Column('topics', sa.JSON(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('refs', sa.JSON(), nullable=False),
    sa.Column('type', sa.String(length=20), nullable=False),
    sa.Column('needs', sa.String(length=20), nullable=True),
    sa.Column('maturity', sa.String(length=16), nullable=True),
    sa.Column('specification', sa.Text(), nullable=True),
    sa.Column('definition_of_done', sa.Text(), nullable=True),
    sa.Column('severity', sa.String(length=20), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('claimed_by', sa.String(length=200), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution', sa.String(length=20), nullable=True),
    sa.Column('resolved_by', sa.String(length=200), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome_note', sa.Text(), nullable=True),
    sa.Column('once_key', sa.String(length=300), nullable=True),
    sa.Column('park_reason', sa.Text(), nullable=True),
    sa.Column('unpark_condition', sa.Text(), nullable=True),
    sa.Column('parked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_by_kind', sa.String(length=10), nullable=True),
    sa.Column('embedded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('run_id', sa.String(length=100), nullable=True),
    sa.ForeignKeyConstraint(['parent_id'], ['issue_items.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('once_key')
    )
    with op.batch_alter_table('issue_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_issue_items_product'), ['product'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_items_parent_id'), ['parent_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_items_type'), ['type'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_items_needs'), ['needs'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_items_maturity'), ['maturity'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_items_severity'), ['severity'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_items_state'), ['state'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_items_embedded_at'), ['embedded_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_items_created_at'), ['created_at'], unique=False)

    op.create_table('issue_comments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('item_id', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('run_id', sa.String(length=100), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['issue_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('issue_comments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_issue_comments_item_id'), ['item_id'], unique=False)

    op.create_table('issue_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('item_id', sa.String(length=40), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('from_state', sa.String(length=20), nullable=True),
    sa.Column('to_state', sa.String(length=20), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['item_id'], ['issue_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('issue_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_issue_events_item_id'), ['item_id'], unique=False)

    op.create_table('issue_leases',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('root_id', sa.String(length=40), nullable=False),
    sa.Column('taken_for', sa.String(length=40), nullable=False),
    sa.Column('holder', sa.String(length=200), nullable=False),
    sa.Column('run_id', sa.String(length=100), nullable=True),
    sa.Column('taken_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('release_reason', sa.String(length=20), nullable=True),
    sa.Column('released_by', sa.String(length=200), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['root_id'], ['issue_items.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('issue_leases', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_issue_leases_root_id'), ['root_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_leases_taken_for'), ['taken_for'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_leases_holder'), ['holder'], unique=False)
        batch_op.create_index(batch_op.f('ix_issue_leases_release_reason'), ['release_reason'], unique=False)
        batch_op.create_index('ix_issue_leases_live_root', ['root_id'],
                              unique=True, sqlite_where=sa.text("released_at IS NULL"))

    op.create_table('lease_refusals',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('requested_item_id', sa.String(length=40), nullable=False),
    sa.Column('product', sa.String(length=64), nullable=False),
    sa.Column('requester', sa.String(length=200), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('blocking_lease_id', sa.String(length=40), nullable=False),
    sa.Column('blocking_root_id', sa.String(length=40), nullable=False),
    sa.Column('blocking_holder', sa.String(length=200), nullable=False),
    sa.Column('blocks_by', sa.String(length=20), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('lease_refusals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lease_refusals_at'), ['at'], unique=False)
        batch_op.create_index(batch_op.f('ix_lease_refusals_requested_item_id'), ['requested_item_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_lease_refusals_actor'), ['actor'], unique=False)
        batch_op.create_index(batch_op.f('ix_lease_refusals_blocking_root_id'), ['blocking_root_id'], unique=False)
        batch_op.create_index('ix_lease_refusals_product_at', ['product', 'at'], unique=False)
