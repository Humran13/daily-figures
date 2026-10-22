"""add spare part fabrication history

Revision ID: a6c8e4922fbc
Revises: e5a7c9d1f3b4
Create Date: 2026-10-22 00:00:00.000000

Additive only: one new table, spare_part_fabrications. Super-Admin-only
financial history of fabricating a spare part in-house (cost vs. what it
was sold to the factory for), one row per fabrication event, many rows
per spare part — see webapp/models/spare_part_fabrication.py. No change
to any existing table.
"""
import sqlalchemy as sa
from alembic import op

revision = "a6c8e4922fbc"
down_revision = "e5a7c9d1f3b4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "spare_part_fabrications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spare_part_id", sa.Integer(), sa.ForeignKey("spare_parts.id"), nullable=False),
        sa.Column("fabrication_date", sa.String(length=10), nullable=False),
        sa.Column("fabrication_cost_ugx", sa.Integer(), nullable=False),
        sa.Column("sold_to_factory_ugx", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_spare_part_fabrications_spare_part_id", "spare_part_fabrications", ["spare_part_id"],
    )
    op.create_index(
        "ix_spare_part_fabrications_fabrication_date", "spare_part_fabrications", ["fabrication_date"],
    )


def downgrade():
    op.drop_index("ix_spare_part_fabrications_fabrication_date", table_name="spare_part_fabrications")
    op.drop_index("ix_spare_part_fabrications_spare_part_id", table_name="spare_part_fabrications")
    op.drop_table("spare_part_fabrications")
