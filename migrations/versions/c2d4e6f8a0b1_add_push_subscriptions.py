"""add push_subscriptions table

Full targeted Operator correction/void/requests/notification package —
Web Push infrastructure did not exist before this round (confirmed by
inspection: no PushManager/VAPID/push-event code anywhere in the
codebase). Purely additive: one new table, no existing table's data is
touched.

Revision ID: c2d4e6f8a0b1
Revises: b7c9d1e3f5a7
Create Date: 2026-08-13 09:05:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'c2d4e6f8a0b1'
down_revision = 'b7c9d1e3f5a7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'push_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.String(length=500), nullable=False),
        sa.Column('p256dh', sa.String(length=255), nullable=False),
        sa.Column('auth', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint'),
    )
    with op.batch_alter_table('push_subscriptions', schema=None) as batch_op:
        batch_op.create_index('ix_push_subscriptions_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_push_subscriptions_endpoint', ['endpoint'], unique=False)


def downgrade():
    with op.batch_alter_table('push_subscriptions', schema=None) as batch_op:
        batch_op.drop_index('ix_push_subscriptions_endpoint')
        batch_op.drop_index('ix_push_subscriptions_user_id')
    op.drop_table('push_subscriptions')
