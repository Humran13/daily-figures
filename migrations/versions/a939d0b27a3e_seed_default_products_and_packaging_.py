"""seed default products and packaging rules

Seeds the product list from the existing static/index.html PRODUCTS array
(same names, same order — nothing renamed) plus the packaging rules given
explicitly in the spec. Two products are intentionally left WITHOUT a
packaging rule because the spec never defines one for them, and guessing
a conversion ratio for a warehouse stock system is exactly the kind of
thing that must not be invented:
  - "Kantimba" — no rule given anywhere in the spec.
  - "Kitchen Towel" — only "Kitchen Towel Doubles" and "Kitchen Towel
    Singles" have defined rules; plain "Kitchen Towel" is seeded as their
    parent product (via parent_product_id) since that's an unambiguous
    grouping, but has no packaging rule of its own.
An admin must set these two via the packaging-rules admin screen before
dispatch entry for those products can be used.

Revision ID: a939d0b27a3e
Revises: a451e04281fc
Create Date: 2026-07-28 19:09:52.590783

"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'a939d0b27a3e'
down_revision = 'a451e04281fc'
branch_labels = None
depends_on = None

# (name, display_order, parent_name_or_None)
PRODUCTS = [
    ("Compact Corporate", 1, None),
    ("Compact Standard", 2, None),
    ("Lavex", 3, None),
    ("KingMax", 4, None),
    ("Premium", 5, None),
    ("JumboMax", 6, None),
    ("Compact Damage", 7, None),
    ("Lavex Damage", 8, None),
    ("Premium Damage", 9, None),
    ("Kantimba", 10, None),
    ("Mambo Damage", 11, None),
    ("JumboMax Damage", 12, None),
    ("Silky Damage", 13, None),
    ("KingMax Damage", 14, None),
    ("Napkins Corporate", 15, None),
    ("Napkins Standard", 16, None),
    ("Napkins Damage", 17, None),
    ("Straws", 18, None),
    ("Mambo", 19, None),
    ("Silky 4pack", 20, None),
    ("Kitchen Towel", 21, None),
    ("Kitchen Towel Doubles", 22, "Kitchen Towel"),
    ("Kitchen Towel Singles", 23, "Kitchen Towel"),
    ("Silky 10pack", 24, None),
]

# name -> (cartons_to_packs, packs_to_pieces, carton_to_pieces)
GROUP_A = (10, 10, None)      # Compact/Lavex/Premium/Mambo/Silky 10-pack + their damage variants
NAPKINS = (6, 10, None)
KINGMAX = (None, None, 60)
JUMBOMAX = (None, None, 24)

PACKAGING_RULES = {
    "Compact Corporate": GROUP_A,
    "Compact Standard": GROUP_A,
    "Lavex": GROUP_A,
    "Premium": GROUP_A,
    "Mambo": GROUP_A,
    "Silky 10pack": GROUP_A,
    "Compact Damage": GROUP_A,
    "Lavex Damage": GROUP_A,
    "Premium Damage": GROUP_A,
    "Mambo Damage": GROUP_A,
    "Silky Damage": GROUP_A,
    "Napkins Corporate": NAPKINS,
    "Napkins Standard": NAPKINS,
    "Napkins Damage": NAPKINS,
    "KingMax": KINGMAX,
    "KingMax Damage": KINGMAX,
    "JumboMax": JUMBOMAX,
    "JumboMax Damage": JUMBOMAX,
    "Straws": (12, 100, None),
    "Silky 4pack": (25, 4, None),
    "Kitchen Towel Doubles": (12, 2, None),
    "Kitchen Towel Singles": (None, None, 24),
    # "Kantimba" and "Kitchen Towel" deliberately absent — see module docstring.
}


def _products_table():
    return sa.table(
        "products",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("display_order", sa.Integer),
        sa.column("active", sa.Boolean),
        sa.column("parent_product_id", sa.Integer),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )


def _packaging_rules_table():
    return sa.table(
        "packaging_rules",
        sa.column("product_id", sa.Integer),
        sa.column("cartons_to_packs", sa.Integer),
        sa.column("packs_to_pieces", sa.Integer),
        sa.column("carton_to_pieces", sa.Integer),
        sa.column("effective_from", sa.DateTime),
        sa.column("effective_to", sa.DateTime),
        sa.column("created_by", sa.Integer),
        sa.column("created_at", sa.DateTime),
    )


def upgrade():
    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC — matches webapp/models' convention
    products = _products_table()
    packaging_rules = _packaging_rules_table()

    name_to_id = {}
    for name, display_order, _parent_name in PRODUCTS:
        bind.execute(
            products.insert().values(
                name=name, display_order=display_order, active=True,
                parent_product_id=None, created_at=now, updated_at=now,
            )
        )
        name_to_id[name] = bind.execute(
            sa.select(products.c.id).where(products.c.name == name)
        ).scalar_one()

    for name, display_order, parent_name in PRODUCTS:
        if parent_name is not None:
            bind.execute(
                products.update()
                .where(products.c.id == name_to_id[name])
                .values(parent_product_id=name_to_id[parent_name])
            )

    for name, (cartons_to_packs, packs_to_pieces, carton_to_pieces) in PACKAGING_RULES.items():
        bind.execute(
            packaging_rules.insert().values(
                product_id=name_to_id[name],
                cartons_to_packs=cartons_to_packs,
                packs_to_pieces=packs_to_pieces,
                carton_to_pieces=carton_to_pieces,
                effective_from=now,
                effective_to=None,
                created_by=None,
                created_at=now,
            )
        )


def downgrade():
    bind = op.get_bind()
    products = _products_table()
    packaging_rules = _packaging_rules_table()
    names = [name for name, _, _ in PRODUCTS]

    seeded_ids = [
        row[0] for row in bind.execute(
            sa.select(products.c.id).where(products.c.name.in_(names))
        )
    ]
    if seeded_ids:
        bind.execute(packaging_rules.delete().where(packaging_rules.c.product_id.in_(seeded_ids)))
        bind.execute(products.delete().where(products.c.id.in_(seeded_ids)))
