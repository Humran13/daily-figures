"""add spare part stock count audit values

Revision ID: e5a7c9d1f3b4
Revises: d4f6a8b0c2e4
"""
from alembic import op
import sqlalchemy as sa


revision = "e5a7c9d1f3b4"
down_revision = "d4f6a8b0c2e4"
branch_labels = None
depends_on = None


def upgrade():
    # Additive ALTER TABLE operations avoid SQLite batch-copy temp tables.
    op.add_column("spare_part_movements", sa.Column("system_quantity_before", sa.Numeric(12, 3), nullable=True))
    op.add_column("spare_part_movements", sa.Column("counted_quantity", sa.Numeric(12, 3), nullable=True))


def downgrade():
    # SQLite needs batch mode only for the optional downgrade path.
    with op.batch_alter_table("spare_part_movements") as batch_op:
        batch_op.drop_column("counted_quantity")
        batch_op.drop_column("system_quantity_before")
