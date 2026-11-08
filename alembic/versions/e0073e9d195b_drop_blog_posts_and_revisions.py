"""drop blog_posts and blog_post_revisions (CQ·170)

Revision ID: e0073e9d195b
Revises: f43c0829608e
Create Date: 2026-10-24 19:00:00.000000

The Archives are the store for a Writing Desk post, as of CQ·168
(issue_01M5GBHESAC479QFB2VHK5J6AK) and CQ·170's own publish route
(issue_01M5GBJ3H5BFX0CR83019T737D). Every post and revision these two tables
ever held was copied out through `bin/copy_posts_to_archives.py` before this
migration runs, so this is cleanup, not loss — Orient has no post table any
more, matching the vault tables dropped on
issue_01M08YKNV185DFJ6832GV8ZY22.

Downgrade recreates both tables empty, matching `c3d1f7a20e84`'s own
`upgrade()` exactly: a downgrade restores the schema, never the rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e0073e9d195b'
down_revision: Union[str, Sequence[str], None] = 'f43c0829608e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f('ix_blog_post_revisions_slug'), table_name='blog_post_revisions')
    op.drop_table('blog_post_revisions')
    op.drop_index(op.f('ix_blog_posts_published_at'), table_name='blog_posts')
    op.drop_index(op.f('ix_blog_posts_status'), table_name='blog_posts')
    op.drop_table('blog_posts')


def downgrade() -> None:
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
        sa.Column('actor', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['slug'], ['blog_posts.slug'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_blog_post_revisions_slug'), 'blog_post_revisions', ['slug'])
