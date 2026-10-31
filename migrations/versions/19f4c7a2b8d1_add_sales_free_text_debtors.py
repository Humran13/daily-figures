"""add sales free-text debtors and daily mobile money split

Revision ID: 19f4c7a2b8d1
Revises: de22eee9674b
Create Date: 2026-10-31 09:12:00.000000

Additive only.  Existing Sales invoices, payments, customer links, stock and
money rows are untouched: free-text debtors are a separate Sales ledger so a
driver can write down "Darius owes 500,000" without a Finished Goods Customer.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '19f4c7a2b8d1'
down_revision = 'de22eee9674b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('sales_debtors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sales_profile_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('name_key', sa.String(length=120), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['sales_profile_id'], ['sales_profiles.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sales_profile_id', 'name_key', name='uq_sales_debtor_profile_name')
    )
    with op.batch_alter_table('sales_debtors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_debtors_name_key'), ['name_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debtors_sales_profile_id'), ['sales_profile_id'], unique=False)

    op.create_table('sales_debts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('debtor_id', sa.Integer(), nullable=False),
    sa.Column('sales_profile_id', sa.Integer(), nullable=False),
    sa.Column('business_date', sa.String(length=10), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('amount_ugx', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('idempotency_key', sa.String(length=80), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('voided_by', sa.Integer(), nullable=True),
    sa.Column('voided_at', sa.DateTime(), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.CheckConstraint("kind IN ('sale', 'opening')", name='ck_sales_debt_kind'),
    sa.CheckConstraint('amount_ugx > 0', name='ck_sales_debt_positive'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['debtor_id'], ['sales_debtors.id'], ),
    sa.ForeignKeyConstraint(['sales_profile_id'], ['sales_profiles.id'], ),
    sa.ForeignKeyConstraint(['voided_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    with op.batch_alter_table('sales_debts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_debts_business_date'), ['business_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debts_debtor_id'), ['debtor_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debts_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debts_sales_profile_id'), ['sales_profile_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debts_status'), ['status'], unique=False)

    op.create_table('sales_debt_payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('debtor_id', sa.Integer(), nullable=False),
    sa.Column('sales_profile_id', sa.Integer(), nullable=False),
    sa.Column('payment_date', sa.String(length=10), nullable=False),
    sa.Column('amount_ugx', sa.Integer(), nullable=False),
    sa.Column('payment_method', sa.String(length=20), nullable=False),
    sa.Column('reference', sa.String(length=80), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('idempotency_key', sa.String(length=80), nullable=False),
    sa.Column('entered_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('voided_by', sa.Integer(), nullable=True),
    sa.Column('voided_at', sa.DateTime(), nullable=True),
    sa.Column('void_reason', sa.Text(), nullable=True),
    sa.CheckConstraint('amount_ugx > 0', name='ck_sales_debt_payment_positive'),
    sa.ForeignKeyConstraint(['debtor_id'], ['sales_debtors.id'], ),
    sa.ForeignKeyConstraint(['entered_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['sales_profile_id'], ['sales_profiles.id'], ),
    sa.ForeignKeyConstraint(['voided_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    with op.batch_alter_table('sales_debt_payments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sales_debt_payments_debtor_id'), ['debtor_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debt_payments_payment_date'), ['payment_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debt_payments_payment_method'), ['payment_method'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debt_payments_sales_profile_id'), ['sales_profile_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sales_debt_payments_status'), ['status'], unique=False)

    with op.batch_alter_table('sales_days', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mobile_money_from_sales_ugx', sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            'ck_sales_day_mobile_from_sales',
            'mobile_money_from_sales_ugx IS NULL OR mobile_money_from_sales_ugx >= 0')


def downgrade():
    with op.batch_alter_table('sales_days', schema=None) as batch_op:
        batch_op.drop_constraint('ck_sales_day_mobile_from_sales', type_='check')
        batch_op.drop_column('mobile_money_from_sales_ugx')

    with op.batch_alter_table('sales_debt_payments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_debt_payments_status'))
        batch_op.drop_index(batch_op.f('ix_sales_debt_payments_sales_profile_id'))
        batch_op.drop_index(batch_op.f('ix_sales_debt_payments_payment_method'))
        batch_op.drop_index(batch_op.f('ix_sales_debt_payments_payment_date'))
        batch_op.drop_index(batch_op.f('ix_sales_debt_payments_debtor_id'))
    op.drop_table('sales_debt_payments')

    with op.batch_alter_table('sales_debts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_debts_status'))
        batch_op.drop_index(batch_op.f('ix_sales_debts_sales_profile_id'))
        batch_op.drop_index(batch_op.f('ix_sales_debts_kind'))
        batch_op.drop_index(batch_op.f('ix_sales_debts_debtor_id'))
        batch_op.drop_index(batch_op.f('ix_sales_debts_business_date'))
    op.drop_table('sales_debts')

    with op.batch_alter_table('sales_debtors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_debtors_sales_profile_id'))
        batch_op.drop_index(batch_op.f('ix_sales_debtors_name_key'))
    op.drop_table('sales_debtors')

