"""
One-time (but safely re-runnable) import of the legacy `entries` table into
the new `daily_figures` / `stock_adjustments` structure. Never runs
automatically — it's a deliberate, audited, admin-triggered action (see
webapp/routes/admin_legacy.py) because it makes judgment calls about
historical data that a human should be able to review first.

Never modifies or deletes a single row of the legacy `entries` table.
"""
from webapp.extensions import db
from webapp.legacy_entries import get_db
from webapp.models.daily_figure import DailyFigure, LegacyMigrationFlag, StockAdjustment
from webapp.models.product import Product
from webapp.services.legacy_decode import AmbiguousLegacyValue, decode_legacy_value
from webapp.services.packaging import to_base_units

# Maps product names as they appeared in the app BEFORE the shorthand ->
# full-name rename, for any legacy rows written before that change.
# "Smalls" has no confident mapping and is deliberately left out — those
# rows will be flagged for manual review rather than guessed.
LEGACY_NAME_ALIASES = {
    "Compac Cot": "Compact Corporate",
    "Compac Old": "Compact Standard",
    "K.max": "KingMax",
    "J.max": "JumboMax",
    "Napkins Cot": "Napkins Corporate",
    "Napkins Old": "Napkins Standard",
    "Napkins D": "Napkins Damage",
    "Silky 4pk": "Silky 4pack",
    "Doubles": "Kitchen Towel Doubles",
    "Singles": "Kitchen Towel Singles",
    "Silky 10pk": "Silky 10pack",
}

FIELDS = ["opening", "return_val", "production", "issued"]


def _resolve_product(raw_name):
    product = Product.query.filter_by(name=raw_name).first()
    if product is not None:
        return product
    alias = LEGACY_NAME_ALIASES.get(raw_name)
    if alias:
        return Product.query.filter_by(name=alias).first()
    return None


def _flag(entries_row, field, raw_value, reason):
    existing = LegacyMigrationFlag.query.filter_by(entries_row_id=entries_row["id"], field=field).first()
    if existing is not None:
        return existing  # already flagged by a previous run — idempotent
    flag = LegacyMigrationFlag(
        entries_row_id=entries_row["id"],
        date=entries_row["date"],
        shift=entries_row["shift"],
        product_name=entries_row["product"],
        field=field,
        raw_value=raw_value if raw_value is not None else -1.0,
        reason=reason,
    )
    db.session.add(flag)
    return flag


def run_legacy_migration(user):
    """
    Returns a summary dict: {migrated: N, flagged: N, skipped_already_migrated: N}.
    Safe to run more than once — rows already migrated (a DailyFigure
    already exists for that date/shift/product) or already flagged are
    left alone.
    """
    conn = get_db()
    legacy_rows = conn.execute("SELECT * FROM entries ORDER BY date, shift, product").fetchall()
    conn.close()

    migrated = 0
    flagged = 0
    skipped = 0

    for row in legacy_rows:
        row = dict(row)

        product = _resolve_product(row["product"])
        if product is None:
            for field in FIELDS:
                _flag(row, field, None,
                      f"product name '{row['product']}' does not match any current product "
                      "(checked exact name and known legacy aliases)")
            flagged += 1
            continue

        if DailyFigure.query.filter_by(product_id=product.id, date=row["date"], shift=row["shift"]).first():
            skipped += 1
            continue

        rule = product.current_packaging_rule()
        if rule is None:
            for field in FIELDS:
                _flag(row, field, None, f"'{product.name}' has no packaging rule configured yet")
            flagged += 1
            continue

        decoded = {}
        any_failed = False
        raw_values = {"opening": row["opening"], "return_val": row["return_val"],
                      "production": row["production"], "issued": row["issued"]}
        for field, raw_value in raw_values.items():
            try:
                decoded[field] = decode_legacy_value(raw_value, rule)
            except AmbiguousLegacyValue as e:
                _flag(row, field, raw_value, str(e))
                any_failed = True

        if any_failed:
            flagged += 1
            continue

        opening_cpp = decoded["opening"]
        return_cpp = decoded["return_val"]
        production_cpp = decoded["production"]
        issued_cpp = decoded["issued"]

        figure = DailyFigure(
            product_id=product.id, date=row["date"], shift=row["shift"],
            opening_cartons=opening_cpp[0], opening_packs=opening_cpp[1], opening_pieces=opening_cpp[2],
            opening_base_qty=to_base_units(*opening_cpp, rule),
            # A legacy `entries` row is a genuine standalone historical
            # record from before this app derived Opening Stock from a
            # running balance at all — always trust it as an authoritative
            # anchor (see webapp/services/stock_service.py's Stage 8
            # correction), never treat it as an incidental/inherited row
            # that later carry-forward is free to recompute over.
            opening_stock_is_override=True,
            return_cartons=return_cpp[0], return_packs=return_cpp[1], return_pieces=return_cpp[2],
            return_base_qty=to_base_units(*return_cpp, rule),
            production_cartons=production_cpp[0], production_packs=production_cpp[1], production_pieces=production_cpp[2],
            production_base_qty=to_base_units(*production_cpp, rule),
            packaging_rule_id=rule.id,
            notes=f"Migrated from legacy entries.id={row['id']}",
            created_by=user.id, updated_by=user.id,
        )
        db.session.add(figure)

        issued_base = to_base_units(*issued_cpp, rule)
        if issued_base != 0:
            db.session.add(StockAdjustment(
                product_id=product.id, date=row["date"], shift=row["shift"],
                delta_base_qty=issued_base,
                reason=f"Migrated legacy issued figure (entries.id={row['id']}) — "
                       "no dispatch records exist for this pre-Phase-3 date",
                created_by=user.id,
            ))
        migrated += 1

    db.session.flush()
    return {"migrated": migrated, "flagged": flagged, "skipped_already_migrated": skipped}
