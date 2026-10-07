"""add China and Local Uganda spare-part buying prices

Revision ID: b9d4e6f8a1c3
Revises: a7c9d1e3f5b8
Create Date: 2026-10-07 00:00:00.000000

The old buying_price is intentionally retained and left unchanged.  Its
source cannot be inferred safely, so migrated rows keep using it as their
current-cost fallback until a Super Administrator explicitly selects China
or Local Uganda with a usable source price.
"""
import sqlalchemy as sa
from alembic import op


revision = "b9d4e6f8a1c3"
down_revision = "a7c9d1e3f5b8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("spare_parts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("china_buying_price", sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column("local_buying_price", sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column("preferred_cost_source", sa.String(length=20), nullable=True))
        batch_op.create_check_constraint(
            "ck_spare_parts_preferred_cost_source",
            "preferred_cost_source IS NULL OR preferred_cost_source IN ('china', 'local_uganda')",
        )


def downgrade():
    with op.batch_alter_table("spare_parts", schema=None) as batch_op:
        batch_op.drop_constraint("ck_spare_parts_preferred_cost_source", type_="check")
        batch_op.drop_column("preferred_cost_source")
        batch_op.drop_column("local_buying_price")
        batch_op.drop_column("china_buying_price")
