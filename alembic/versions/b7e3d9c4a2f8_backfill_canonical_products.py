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

ONE ENTRY ("Substrate" -> "substrate") IS A FROZEN LITERAL, NOT DERIVED, AND
STAYS THAT WAY. It is a casing bug, not a scope: one issue filed through the
team-site form, invisible to every view asking for `substrate`, the bug that
motivated this whole step. No registry unit is keyed `substrate` (it has no
repos of its own to resolve through), so there is nothing for the registry to
tell this migration about it, and a data migration recording a historical fact
must do the same thing on every database it is ever applied to — including one
restored from backup a year from now — so this one stays pinned exactly as it
read on 2026-07-31.

THE REPO-NAME ENTRIES ARE GONE (CQ-171, the 0.7.0 name gate). This revision
also used to fold three repo names a filer had typed as a project into the unit
that owns each. Those names were per-install values, which a shipped file may
not carry. Removing them changes no data that survives: both tables this
revision touches are dropped by b031daf7f4c0 later in the same chain, so on
every database the rows this backfill rewrote are deleted after it runs.
Orient's own database applied the original version on 2026-07-31 and is past
b031daf7f4c0.

DOWNGRADE RAISES, deliberately: the original version folded two repos into
one project, so a database migrated by it cannot be reversed, and a casing fix
has no meaningful reverse either.
"""
from collections.abc import Sequence
from typing import Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e3d9c4a2f8'
down_revision: Union[str, Sequence[str], None] = 'a1b4c7e2d8f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen as of 2026-07-31 — see the module docstring: this one entry must NOT
# be derived from the live registry, because no registry unit is keyed this way.
_CASE_FIX = {"Substrate": "substrate"}

# Only the vaults that arbitrate. `signals` is deliberately absent.
_TABLES = ("issue_items", "decision_items")


def _canonical_mapping(present: set[str]) -> dict[str, str]:
    """The backfill mapping for exactly the non-canonical values `present`
    in the database — never more than that, so a value this migration
    doesn't recognize is left alone rather than guessed at.

    No target may also be a source. The loop in `upgrade()` applies each
    mapping entry in turn against the live table, so a chained entry — say a
    target also appearing as a key — would promote rows TWICE: a row
    rewritten by an earlier iteration would then be caught by the later one
    and moved again. This asserts that rather than leaving it to whoever
    edits the source lists next (reviewer, PR #144)."""
    mapping = {k: v for k, v in _CASE_FIX.items() if k in present}
    assert not (set(mapping) & set(mapping.values())), (
        "canonical mapping is chained: a target value is also a source key, "
        "which would rewrite the same rows twice")
    return mapping


def upgrade() -> None:
    conn = op.get_bind()
    known_sources = set(_CASE_FIX)
    present: set[str] = set()
    for table in _TABLES:
        # Built from SQLAlchemy table/column objects rather than an f-string. The
        # table name is a frozen constant here so string formatting would be safe,
        # but the SHAPE of an interpolated table name is the shape of SQL
        # injection, and it is the shape that gets copied into the next migration —
        # where the name may not be a constant (security-reviewer, PR #144).
        t = sa.table(table, sa.column("product", sa.String))
        present |= {v for v in conn.execute(sa.select(t.c.product).distinct()).scalars()
                    if v in known_sources}
    if not present:
        return
    mapping = _canonical_mapping(present)
    for table in _TABLES:
        t = sa.table(table, sa.column("product", sa.String))
        for old, new in mapping.items():
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
        "b7e3d9c4a2f8 is a one-way migration: its original version folded two "
        "repos into one project, so no reverse mapping exists. To undo it you "
        "need the pre-migration backup.")
