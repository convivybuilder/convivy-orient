"""add courses and course_events — the route to the bead

Revision ID: d4a1c6b90e73
Revises: b3e7f2a9c481
Create Date: 2026-08-06 08:00:00.000000

Keel Stage 1's closing piece (ADR-0029). `parent_id` says what contains a work
item and `maturity` says whether it has been agreed; the Course says what order
we intend to take things in. All three are the same layer of the Chart House.

TWO TABLES RATHER THAN A RANK COLUMN, which is the operator's ruling and the whole shape
of this migration. A rank on `issue_items` would make a reorder a write to every
affected row — each one an event in the audit trail that says nothing about the
work — and could not distinguish "not in the Course" from rank 0. The order is a
JSON array on its own object, so a reorder is one write and membership is
unambiguous.

NOTHING TO MIGRATE, and that is why it lands now. As of 2026-08-06 the store
holds 539 work items, 0 of them epics, and nothing holds a Course. Chart House
structure is cheapest to add against an empty tree — the same argument Phase 1
made for putting `parent_id` in before the lease depended on it.

THE PARTIAL UNIQUE INDEX IS THE CONSTRAINT, not a hint about it: one live Course
per product, enforced where two concurrent creates actually collide. Partial on
`status = 'live'` so superseded routes accumulate beside the live one without
blocking the next. It needs no PRAGMA to run, unlike the FK caveat ADR-0023
recorded — SQLite has supported partial indexes since 3.8.

DOWNGRADE DESTROYS EVERY PLAN AND ITS HISTORY. No other table references these
two, so going back down is safe for the system and unrecoverable for the routes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a1c6b90e73'
down_revision: Union[str, Sequence[str], None] = 'b3e7f2a9c481'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "courses",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("product", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_courses_product", "courses", ["product"])
    op.create_index("ix_courses_status", "courses", ["status"])
    op.create_index("ix_courses_created_at", "courses", ["created_at"])
    # One live Course per product. See the module docstring.
    op.create_index("ix_courses_live_product", "courses", ["product"],
                    unique=True, sqlite_where=sa.text("status = 'live'"))

    op.create_table(
        "course_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.String(length=40), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        # NULL means the order did not move in this event, which is a different
        # answer from an empty order — see the CourseEvent docstring.
        sa.Column("entries", sa.JSON(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_course_events_course_id", "course_events", ["course_id"])


def downgrade() -> None:
    op.drop_index("ix_course_events_course_id", table_name="course_events")
    op.drop_table("course_events")
    op.drop_index("ix_courses_live_product", table_name="courses")
    op.drop_index("ix_courses_created_at", table_name="courses")
    op.drop_index("ix_courses_status", table_name="courses")
    op.drop_index("ix_courses_product", table_name="courses")
    op.drop_table("courses")
