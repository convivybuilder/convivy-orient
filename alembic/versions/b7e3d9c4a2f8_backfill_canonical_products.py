"""backfill non-canonical product values in the arbitrated vaults

Revision ID: b7e3d9c4a2f8
Revises: a1b4c7e2d8f6
Create Date: 2026-07-31 21:05:00.000000

ARCH-426 step 4a. Step 3 made the WRITE path store canonical project slugs; this
fixes the rows written before it. 21 rows across two tables — the whole real
migration, because the alias mechanism already absorbs the rest.

WHY SIGNALS ARE NOT TOUCHED, which is the load-bearing decision here.

1,275 signal rows carry a non-canonical product and every one of them is left
alone. The Archives are specified append-only and "read for context, never
arbitrated" (ARCH-425), and a backfill is arbitration. Rewriting the historical
attribution of 1,275 captured signals to tidy a vocabulary would trade the one
property that makes that vault trustworthy for a cosmetic gain. Signal reads
resolve through the registry instead, which costs nothing and changes no history.

The Workshop and the decision queue are different: both arbitrate by design —
they hold state that is claimed, resolved and acted on — so correcting a scope
value in them is maintenance rather than revision.

THE MAPPING IS FROZEN, NOT DERIVED. It would be one line to import
project_registry and resolve each value live, and that would be wrong. A data
migration is a historical fact: it must do the same thing on every database it is
ever applied to, including one restored from backup a year from now. Deriving the
mapping from a registry that keeps changing makes the migration's effect depend on
when it runs. So the four mappings below are written out, taken from the registry
as it stands on 2026-07-31 and pinned here permanently.

DOWNGRADE IS A NO-OP, deliberately, and this is a genuine one-way door rather than
laziness. `convivy-app-kit` and `convivy-services` both fold to `svc`, so the
reverse is ambiguous — an `svc` row cannot say which repo it came from. Recording
the pre-image to make it reversible would mean a side table carrying a value the
system has decided is not canonical, kept forever, for a downgrade nobody runs.
The forward direction loses only which of two repos a filer named for 9 rows
total.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e3d9c4a2f8'
down_revision: Union[str, Sequence[str], None] = 'a1b4c7e2d8f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen as of 2026-07-31. See the module docstring: this must NOT be derived from
# the live registry.
#
#   Substrate        → a casing bug, not a scope. One issue filed through the
#                      team-site form, invisible to every view asking for
#                      `substrate`. The bug that motivated the whole step.
#   convivy-app-kit  → svc is ONE project over both repos (ARCH-426 amendment):
#   convivy-services   one co, one crew, two tightly-coupled parts.
#   selector-backend → one product over a frontend and a backend, which
#                      issue_api's own comment already assumed.
_CANONICAL = {
    "Substrate": "substrate",
    "convivy-app-kit": "svc",
    "convivy-services": "svc",
    "selector-backend": "selector",
}

# No target may also be a source. The loop below applies each mapping in turn
# against the live table, so a chained entry — say `svc` also appearing as a KEY —
# would promote rows TWICE: a row rewritten to `svc` by an earlier iteration would
# then be caught by the later one and moved again. The current mapping is safe, and
# this asserts it rather than leaving it to whoever edits the dict next. Module
# scope, so it fails at import rather than mid-migration (reviewer, PR #144).
assert not (set(_CANONICAL) & set(_CANONICAL.values())), (
    "_CANONICAL is chained: a target value is also a source key, which would "
    "rewrite the same rows twice")

# Only the vaults that arbitrate. `signals` is deliberately absent.
_TABLES = ("issue_items", "decision_items")


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        # Built from SQLAlchemy table/column objects rather than an f-string. The
        # table name is a frozen constant here so string formatting would be safe,
        # but the SHAPE of an interpolated table name is the shape of SQL
        # injection, and it is the shape that gets copied into the next migration —
        # where the name may not be a constant (security-reviewer, PR #144).
        t = sa.table(table, sa.column("product", sa.String))
        for old, new in _CANONICAL.items():
            conn.execute(t.update().where(t.c.product == old).values(product=new))


def downgrade() -> None:
    """RAISES rather than doing nothing, and the difference matters.

    An empty downgrade is not a no-op from the operator's side: alembic removes
    the revision from `alembic_version` afterwards, so `alembic downgrade -1`
    prints nothing, reports success, and leaves every canonicalised row exactly as
    it was. The operator is told the rollback worked when the data did not move.

    Raising surfaces the one-way-ness at the moment someone tries to use it, which
    is the only moment the reasoning is worth anything (reviewer, PR #144)."""
    raise NotImplementedError(
        "b7e3d9c4a2f8 is a one-way migration: convivy-app-kit and convivy-services "
        "both fold to svc, so an svc row cannot say which repo it came from and no "
        "reverse mapping exists. To undo it you need the pre-migration backup.")
