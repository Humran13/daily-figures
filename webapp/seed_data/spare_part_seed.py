"""
Pure, zero-Flask-app-context seeding of the Spare Parts master catalogue
from a committed JSON artifact (spare_parts_master_seed.json) — the
original source workbook is deliberately NOT read here and is never
required on a production server. This module has no dependency on any
spreadsheet library, the ORM models, or Flask: it takes a plain
SQLAlchemy Connection and reflects the live `spare_parts` table via
`sa.Table(..., autoload_with=conn)`, exactly like this project's other
data migrations — so it carries zero schema-drift risk and can be called
identically from an Alembic migration's `op.get_bind()` or directly from
a test's `db.engine`/`db.session.connection()` (this project's tests
build their schema via `db.create_all()`, never real Alembic migrations —
see tests/conftest.py — so sharing this exact function is what lets the
test suite exercise precisely the same code path the migration runs).

Insert-only, by design: an existing row matching a seed record's
identity (normalized name + specifications) is left completely
untouched — never updated, regardless of what the seed contains — so a
user's own edits (including a manually-entered Selling Price) can never
be clobbered by re-seeding. Never creates a SparePartMovement, never
sets a Buying Price, never sets a current/opening stock quantity.
"""
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import sqlalchemy as sa

SEED_FILE = Path(__file__).parent / "spare_parts_master_seed.json"


def _normalize_key(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def load_seed_records(path=None):
    path = path or SEED_FILE
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def seed_spare_parts_master(conn, records=None):
    """
    Inserts any seed record not already matched by an existing
    spare_parts row (normalized name + specifications). Returns the
    number of rows actually inserted. Safe to call more than once —
    a second call always returns 0 once every record has been seeded
    (or already existed under a matching identity).
    """
    records = records if records is not None else load_seed_records()
    meta = sa.MetaData()
    spare_parts = sa.Table("spare_parts", meta, autoload_with=conn)

    existing_keys = set()
    for row in conn.execute(sa.select(spare_parts.c.name, spare_parts.c.specifications)):
        existing_keys.add((_normalize_key(row.name), _normalize_key(row.specifications)))

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    inserted = 0
    for rec in records:
        key = (_normalize_key(rec.get("name")), _normalize_key(rec.get("specifications")))
        if key in existing_keys:
            continue

        selling_price = None
        raw_price = rec.get("selling_price")
        if raw_price not in (None, ""):
            try:
                selling_price = Decimal(str(raw_price))
            except InvalidOperation:
                selling_price = None

        values = dict(
            code=None,
            name=rec.get("name"),
            model=None,
            size=None,
            specifications=rec.get("specifications"),
            category_id=None,
            unit="pcs",
            minimum_stock=None,
            location=None,
            notes=None,
            buying_price=None,
            selling_price=selling_price,
            active=True,
            current_stock_cache=0,
            created_at=now,
            updated_at=now,
        )
        # `critical` was added by a later migration than this seed's own
        # (c3d5e7f9a1b2 vs. this function's a7c9d1e3f5b8). When Alembic
        # replays history from scratch, the table reflected here at THIS
        # point in the chain does not have the column yet — only include
        # it when the live connection's schema already does (e.g. this
        # project's tests, which build the full current schema via
        # db.create_all() and call this exact function directly).
        if "critical" in spare_parts.c:
            values["critical"] = False
        if "critical_note" in spare_parts.c:
            values["critical_note"] = None
        result = conn.execute(spare_parts.insert().values(**values))
        new_id = result.inserted_primary_key[0]
        conn.execute(
            spare_parts.update().where(spare_parts.c.id == new_id).values(code=f"SP-{new_id:05d}")
        )
        existing_keys.add(key)
        inserted += 1

    return inserted
