"""add product_usage_events

Adds the product_usage_events table (Stage 8 Part 2): one row per
(source, source_id, product_id) recording a finalized Dispatch/Returns/
Production record's use of a product, timestamped so the global "quick
selection" ranking can be recomputed fresh (frequency + recency) from the
current date on every read, with no scheduled job and no stored score to
go stale. Purely additive — no existing table is touched.

Revision ID: c7a4e29f1b83
Revises: b6f2a913c7d4
Create Date: 2026-08-15 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c7a4e29f1b83'
down_revision = 'b6f2a913c7d4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'product_usage_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'source_id', 'product_id', name='uq_product_usage_event_source_product'),
    )
    with op.batch_alter_table('product_usage_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_product_usage_events_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_product_usage_events_used_at'), ['used_at'], unique=False)


def downgrade():
    with op.batch_alter_table('product_usage_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_product_usage_events_used_at'))
        batch_op.drop_index(batch_op.f('ix_product_usage_events_product_id'))
    op.drop_table('product_usage_events')
