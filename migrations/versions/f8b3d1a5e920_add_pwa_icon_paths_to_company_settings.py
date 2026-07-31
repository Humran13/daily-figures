"""add pwa icon paths to company_settings

Stage 6: derived PWA install-icon variants (192/512/512-maskable) generated
from the uploaded company logo — see
webapp/services/branding_service.py's _generate_derived_icons(). Purely
additive and nullable; every existing installation has no logo-derived
icons yet, which correctly falls back to the generic ledger icon (see
webapp/routes/pwa.py's manifest()) until a Super Administrator next
uploads or replaces the logo.

Revision ID: f8b3d1a5e920
Revises: d4e7a2c9f631
Create Date: 2026-07-31 09:30:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'f8b3d1a5e920'
down_revision = 'd4e7a2c9f631'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('company_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('icon_192_path', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('icon_512_path', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('icon_512_maskable_path', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('company_settings', schema=None) as batch_op:
        batch_op.drop_column('icon_512_maskable_path')
        batch_op.drop_column('icon_512_path')
        batch_op.drop_column('icon_192_path')
