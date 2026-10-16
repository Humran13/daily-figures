"""add spare_parts.safety_stock_override

Revision ID: d4f6a8b0c2e4
Revises: c3d5e7f9a1b2
Create Date: 2026-10-17 00:00:00.000000

Design review follow-up: Consumption & Procurement Planning initially
reused `minimum_stock` as its safety-stock override. minimum_stock/reorder
level and safety stock are different concepts (minimum_stock drives the
existing low-stock alert; safety stock is a planning buffer added on top
of projected demand) and must not be conflated. This adds a dedicated,
nullable, additive column instead. `minimum_stock` and every existing
calculation that reads it (is_low_stock(), stock_status_for(), etc.) are
completely unchanged by this migration.

NULL (the default for every existing row) means "use the application
default safety-stock rule" (see spare_part_planning_service.safety_stock_for());
an explicit value overrides that default for just that part.
"""
import sqlalchemy as sa
from alembic import op


revision = "d4f6a8b0c2e4"
down_revision = "c3d5e7f9a1b2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("spare_parts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("safety_stock_override", sa.Numeric(precision=12, scale=3), nullable=True))


def downgrade():
    with op.batch_alter_table("spare_parts", schema=None) as batch_op:
        batch_op.drop_column("safety_stock_override")
