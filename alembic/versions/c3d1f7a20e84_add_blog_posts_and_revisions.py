"""add blog_posts and blog_post_revisions (Field Notes store of truth)

Revision ID: c3d1f7a20e84
Revises: b031daf7f4c0
Create Date: 2026-08-22 18:20:00.000000

Orient becomes the store of truth for Field Notes, with every public surface a
renderer (`issue_01M0N650ZJHPQYPF54HD22RJEH`). Until now the corpus was files in
`convivy-lab/content/field-notes/`, and that repository is being deleted — so
this table is what makes "Orient holds the posts" true rather than aspirational.

`blog_post_revisions` is append-only and exists because git held the history
until now. It stops holding it when the repo goes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d1f7a20e84'
down_revision: Union[str, Sequence[str], None] = 'b031daf7f4c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'blog_posts',
        sa.Column('slug', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('author', sa.String(length=200), nullable=False),
        sa.Column('authorship', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('slug'),
    )
    op.create_index(op.f('ix_blog_posts_status'), 'blog_posts', ['status'])
    op.create_index(op.f('ix_blog_posts_published_at'), 'blog_posts', ['published_at'])

    op.create_table(
        'blog_post_revisions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('slug', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('author', sa.String(length=200), nullable=False),
        sa.Column('authorship', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=20), nullable=False),
        # NULLABLE, and only for `ingest`: an imported post predates this store
        # and has no attested actor. Recording an inferred one would be exactly
        # the fabrication the attestation rule exists to prevent.
        sa.Column('actor', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['slug'], ['blog_posts.slug'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_blog_post_revisions_slug'), 'blog_post_revisions', ['slug'])


def downgrade() -> None:
    op.drop_index(op.f('ix_blog_post_revisions_slug'), table_name='blog_post_revisions')
    op.drop_table('blog_post_revisions')
    op.drop_index(op.f('ix_blog_posts_published_at'), table_name='blog_posts')
    op.drop_index(op.f('ix_blog_posts_status'), table_name='blog_posts')
    op.drop_table('blog_posts')
