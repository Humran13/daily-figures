"""add spare part critical flag and a movement analytics index

Revision ID: c3d5e7f9a1b2
Revises: b9d4e6f8a1c3
Create Date: 2026-10-16 00:00:00.000000

Additive only. `critical`/`critical_note` default every existing row to
"not critical" with no note, matching what was previously implicit (no
criticality concept existed). The new index speeds up Consumption &
Procurement Planning's consumption-window aggregate queries
(filter on movement_type/status/business_date, grouped by spare_part_id)
without changing any existing query's results.
"""
import sqlalchemy as sa
from alembic import op


revision = "c3d5e7f9a1b2"
down_revision = "b9d4e6f8a1c3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("spare_parts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("critical", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("critical_note", sa.Text(), nullable=True))

    op.create_index(
        "ix_spare_part_movements_consumption_window",
        "spare_part_movements",
        ["movement_type", "status", "business_date", "spare_part_id"],
    )


def downgrade():
    op.drop_index("ix_spare_part_movements_consumption_window", table_name="spare_part_movements")
    with op.batch_alter_table("spare_parts", schema=None) as batch_op:
        batch_op.drop_column("critical_note")
        batch_op.drop_column("critical")
