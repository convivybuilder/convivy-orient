"""add alert_policies (dashboard alert-threshold policy)

Revision ID: a2c5e8f1d4b7
Revises: c3d1f7a20e84
Create Date: 2026-09-17 03:00:00.000000

Lifts the three in-code dashboard alert thresholds
(`code_health_build.ci_status`, `dashboard_build.overage_status`,
`kb_build.cls_recall`) into settable state, so a person reading a dashboard
can set the policy it alerts against from the page showing the metric
(issue_01M17W6XGXVHNDWCFRTQ38Y2RK). A missing row means "never set"; the
evaluator falls back to the hardcoded default (`alert_policy_store.DEFAULTS`).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2c5e8f1d4b7'
down_revision: Union[str, Sequence[str], None] = 'c3d1f7a20e84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'alert_policies',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('values', sa.JSON(), nullable=False),
        sa.Column('updated_by', sa.String(length=200), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('alert_policies')
