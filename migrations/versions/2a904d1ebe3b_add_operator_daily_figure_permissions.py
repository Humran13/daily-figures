"""add operator_daily_figure_permissions

Adds a single-row, role-wide table controlling whether the Operator role
may edit Opening/Return/Production on Daily Figures or create manual stock
adjustments. All four flags default to False — Operator is read-only on
Daily Figures until a Super Administrator explicitly enables a field. This
never affects Manager or Super Admin (their existing write access is
enforced purely by role, unrelated to this table) and never affects Viewer
(who must never gain write access from these flags).

Purely additive: one new table, one seeded default row. Nothing else in
the schema changes.

Revision ID: 2a904d1ebe3b
Revises: 135b08c5ab45
Create Date: 2026-07-29 02:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '2a904d1ebe3b'
down_revision = '135b08c5ab45'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'operator_daily_figure_permissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('can_edit_opening', sa.Boolean(), nullable=False),
        sa.Column('can_edit_production', sa.Boolean(), nullable=False),
        sa.Column('can_edit_returns', sa.Boolean(), nullable=False),
        sa.Column('can_create_adjustments', sa.Boolean(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    bind = op.get_bind()
    table = sa.table(
        'operator_daily_figure_permissions',
        sa.column('id', sa.Integer),
        sa.column('can_edit_opening', sa.Boolean),
        sa.column('can_edit_production', sa.Boolean),
        sa.column('can_edit_returns', sa.Boolean),
        sa.column('can_create_adjustments', sa.Boolean),
        sa.column('updated_at', sa.DateTime),
    )
    bind.execute(
        table.insert().values(
            id=1,
            can_edit_opening=False,
            can_edit_production=False,
            can_edit_returns=False,
            can_create_adjustments=False,
            updated_at=sa.func.now(),
        )
    )


def downgrade():
    op.drop_table('operator_daily_figure_permissions')
