"""add feature_flags

Adds one row per module (dispatch, daily_figures, history_exports,
dashboard, customer_management, reporting), all seeded enabled=True so
every existing installation keeps working exactly as before until a Super
Administrator explicitly disables something. Purely additive.

Revision ID: e8a1576c5404
Revises: 8d16f14e2b4a
Create Date: 2026-07-30 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e8a1576c5404'
down_revision = '8d16f14e2b4a'
branch_labels = None
depends_on = None

MODULES = ["dispatch", "daily_figures", "history_exports", "dashboard", "customer_management", "reporting"]


def upgrade():
    op.create_table(
        'feature_flags',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('module_key', sa.String(length=40), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feature_flags_module_key', 'feature_flags', ['module_key'], unique=True)

    bind = op.get_bind()
    table = sa.table(
        'feature_flags',
        sa.column('module_key', sa.String),
        sa.column('enabled', sa.Boolean),
        sa.column('updated_at', sa.DateTime),
    )
    for module_key in MODULES:
        bind.execute(table.insert().values(module_key=module_key, enabled=True, updated_at=sa.func.now()))


def downgrade():
    op.drop_index('ix_feature_flags_module_key', table_name='feature_flags')
    op.drop_table('feature_flags')
