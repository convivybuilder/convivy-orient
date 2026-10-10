"""add created_by_kind to issue_items (feature-gate authorization, ARCH-403 Tier 1.2)

The feature gate authorizes an overnight build off WHO filed the issue. Deriving
that from roles.json membership alone was string-based, so an agent token whose
`name` happened to match a human email in roles.json would serialize as "human"
and authorize an autonomous build (security-reviewer, PR #112). The identity's
KIND — human via CF Access, agent via bearer token — is the structural fact; this
column stores it at create time so the read-time gate can require BOTH the stored
kind AND live roles.json membership.

Nullable with no server default on purpose: a row created before this column
reads NULL, which the serialiser treats as "agent" — the safe direction. New
rows are written explicitly. No backfill: an existing issue's authorization
should not be invented after the fact.

Revision ID: d3f1a8c6e4b2
Revises: c9e6b3d8f1a2
Create Date: 2026-07-24
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3f1a8c6e4b2"
down_revision: Union[str, Sequence[str], None] = "c9e6b3d8f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "issue_items",
        sa.Column("created_by_kind", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("issue_items", "created_by_kind")
