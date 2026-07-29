"""add company_settings

Adds a single-row white-label branding/configuration table, seeded with a
safe default (display_name="Daily Figures", everything else blank) so an
existing installation keeps working unmodified until a Super Administrator
configures it. Purely additive — no other table changes.

Revision ID: 8d16f14e2b4a
Revises: 2a904d1ebe3b
Create Date: 2026-07-29 15:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '8d16f14e2b4a'
down_revision = '2a904d1ebe3b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'company_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('display_name', sa.String(length=160), nullable=False),
        sa.Column('legal_name', sa.String(length=160), nullable=True),
        sa.Column('logo_path', sa.String(length=255), nullable=True),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('phone', sa.String(length=40), nullable=True),
        sa.Column('email', sa.String(length=160), nullable=True),
        sa.Column('website', sa.String(length=200), nullable=True),
        sa.Column('currency_code', sa.String(length=10), nullable=True),
        sa.Column('tax_registration_number', sa.String(length=80), nullable=True),
        sa.Column('report_footer_text', sa.Text(), nullable=True),
        sa.Column('primary_contact_name', sa.String(length=120), nullable=True),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    bind = op.get_bind()
    table = sa.table(
        'company_settings',
        sa.column('id', sa.Integer),
        sa.column('display_name', sa.String),
        sa.column('updated_at', sa.DateTime),
    )
    bind.execute(table.insert().values(id=1, display_name='Daily Figures', updated_at=sa.func.now()))


def downgrade():
    op.drop_table('company_settings')
