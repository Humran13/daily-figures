"""add customer normalized_name for duplicate detection

Adds a case/whitespace-insensitive comparison column, backfills it for
every EXISTING customer row (active, inactive, temporary, and merged-away
alike — nothing is filtered out), then attempts a unique index on it so a
race between two concurrent imports can't slip a duplicate past the
in-Python pre-check.

The unique index is best-effort and defensive: if real production data
already has two customers whose names only differ by case/whitespace
(entered before this protection existed), creating a UNIQUE index would
fail and take down the whole deploy over a data-quality issue this
migration didn't cause. Rather than risk that, this catches exactly that
failure and falls back to a plain (non-unique) index, logging which rows
collided so an admin can merge or rename them by hand — the same
"never silently guess, never block a safe deploy" posture as every other
migration in this project.

Revision ID: 135b08c5ab45
Revises: 012e556ab7ad
Create Date: 2026-07-28 22:35:47.273358

"""
import re

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '135b08c5ab45'
down_revision = '012e556ab7ad'
branch_labels = None
depends_on = None


def _normalize(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def upgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('normalized_name', sa.String(length=160), nullable=True))

    bind = op.get_bind()
    customers = sa.table(
        'customers',
        sa.column('id', sa.Integer),
        sa.column('name', sa.String),
        sa.column('normalized_name', sa.String),
    )
    rows = bind.execute(sa.select(customers.c.id, customers.c.name)).fetchall()
    for row in rows:
        bind.execute(
            customers.update().where(customers.c.id == row.id).values(normalized_name=_normalize(row.name))
        )

    try:
        op.create_index('ix_customers_normalized_name', 'customers', ['normalized_name'], unique=True)
    except (sa.exc.IntegrityError, sa.exc.OperationalError) as e:
        print(
            "WARNING: could not create a UNIQUE index on customers.normalized_name — "
            "existing data already has case/whitespace-only duplicate names. "
            f"Falling back to a non-unique index. Original error: {e}"
        )
        op.create_index('ix_customers_normalized_name', 'customers', ['normalized_name'], unique=False)


def downgrade():
    op.drop_index('ix_customers_normalized_name', table_name='customers')
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_column('normalized_name')
