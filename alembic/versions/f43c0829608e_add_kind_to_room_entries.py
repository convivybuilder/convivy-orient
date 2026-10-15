"""add kind to room_entries (CQ·147, E22)

Revision ID: f43c0829608e
Revises: a2c5e8f1d4b7
Create Date: 2026-10-15 08:40:45.000000

The Reading Room widens from KB docs alone to also hold Chart House work
items and Archives entries (E22, issue_01M4WXCQWBXSXP1FJ7DR3V76S2). `kind`
says which store `doc_id` names — doc | item | signal — and the uniqueness
that used to be (room_id, doc_id) becomes (room_id, kind, doc_id), since the
same id text can in principle name a doc and an item without colliding.

server_default so every existing row backfills to 'doc' under SQLite's batch
(table-rebuild) ALTER, the same convention `b7c1d9e42a08` (decision_items.
blocked) uses — every row written before this migration was a KB doc, and
there is no other kind it could have been.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f43c0829608e'
down_revision: Union[str, Sequence[str], None] = 'a2c5e8f1d4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('room_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=20), nullable=False,
                                      server_default='doc'))
        batch_op.drop_constraint('uq_room_entry_doc', type_='unique')
        batch_op.create_unique_constraint('uq_room_entry_kind_doc',
                                          ['room_id', 'kind', 'doc_id'])


def downgrade() -> None:
    with op.batch_alter_table('room_entries', schema=None) as batch_op:
        batch_op.drop_constraint('uq_room_entry_kind_doc', type_='unique')
        batch_op.create_unique_constraint('uq_room_entry_doc', ['room_id', 'doc_id'])
        batch_op.drop_column('kind')
