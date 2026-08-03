"""
One-time (but safely re-runnable) import of the legacy `entries` table into
the new `daily_figures` / `stock_adjustments` structure. Never runs
automatically — it's a deliberate, audited, admin-triggered action (see
webapp/routes/admin_legacy.py) because it makes judgment calls about
historical data that a human should be able to review first.

Never modifies or deletes a single row of the legacy `entries` table.
"""
import re

from webapp.extensions import db
from webapp.models.daily_figure import (
    OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING,
    DailyFigure,
    LegacyMigrationFlag,
    StockAdjustment,
)
from webapp.legacy_entries import get_db
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
            # running balance at all — always anchor-eligible (see
            # webapp/services/stock_service.py's Stage 8 correction and
            # production hotfix), never treated as an incidental/inherited
            # row that later carry-forward is free to recompute over.
            #
            # legacy_migrated_opening (final legacy-opening-migration
            # investigation) — NOT initial_manual: an earlier version of
            # this migration used initial_manual, which is *live-
            # revalidated* on every read and gets silently demoted (its
            # authoritative historical Opening discarded, no warning) the
            # moment ANY earlier finalized activity exists — including
            # another legacy row's own migrated Issued StockAdjustment,
            # which every product with more than one legacy row always
            # has. A legacy ledger row is already independently reconciled
            # by the business (Opening + Production + Returns - Issued =
            # Closing, verified before migration ever ran) and must be
            # trusted unconditionally, exactly like manual_correction —
            # never second-guessed by later-discovered history.
            opening_stock_source=OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING,
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


# =====================================================================
# Final legacy-opening-migration investigation — spreadsheet-style
# reconciliation, read-only audit, and safe repair.
# =====================================================================

# What run_legacy_migration() writes onto a migrated DailyFigure row's own
# `notes` field — the one reliable signal (besides opening_stock_source
# itself, which a later Reset/correction can legitimately change) that a
# row originated from this migration, used by both the audit and the
# repair command to find candidates without ever guessing.
LEGACY_MIGRATION_NOTES_PATTERN = re.compile(r"^Migrated from legacy entries\.id=(\d+)$")

# Classification labels — final legacy-opening-migration investigation,
# section 5. Every audited legacy row gets exactly one of these.
CLASS_NO_REPAIR_REQUIRED = "no_repair_required"
CLASS_MISSING_OPENING_ANCHOR = "missing_legacy_opening_anchor"
CLASS_EXISTING_MANUAL_CORRECTION = "existing_valid_manual_correction"
CLASS_ALREADY_MIGRATED_CORRECTLY = "legacy_row_already_migrated_correctly"
CLASS_DOES_NOT_RECONCILE = "legacy_equation_does_not_reconcile"
CLASS_GENUINE_NEGATIVE = "genuine_legacy_negative_stock"
CLASS_AMBIGUOUS_REVIEW = "ambiguous_requires_human_review"


def decode_legacy_row(row, rule):
    """
    Spreadsheet-style reconciliation, using exact integer base units
    throughout — final legacy-opening-migration investigation, section 4.
    `row` is a dict with the legacy `entries` table's own field names
    (opening/return_val/production/issued/closing, each the old "C.PP"
    book-notation REAL value — decoded via the SAME decode_legacy_value()
    run_legacy_migration() itself uses, never a second, competing parser).

    Returns a dict with every decoded field's exact base units, the
    recalculated Closing (opening + production + returns - issued), and
    whether that matches the legacy row's OWN stated Closing exactly — or
    an `errors` dict (field -> reason) if any field could not be decoded
    at all, in which case reconciliation cannot be determined.
    """
    fields = ("opening", "return_val", "production", "issued", "closing")
    decoded_cpp = {}
    base_units = {}
    errors = {}
    for field in fields:
        try:
            cpp = decode_legacy_value(row[field], rule)
            decoded_cpp[field] = cpp
            base_units[field] = to_base_units(*cpp, rule)
        except AmbiguousLegacyValue as e:
            errors[field] = str(e)

    if errors:
        return {"decoded_cpp": decoded_cpp, "base_units": base_units, "errors": errors, "reconciles": None}

    recalculated_closing = base_units["opening"] + base_units["production"] + base_units["return_val"] - base_units["issued"]
    return {
        "decoded_cpp": decoded_cpp,
        "base_units": base_units,
        "errors": {},
        "recalculated_closing_base_qty": recalculated_closing,
        "reconciles": recalculated_closing == base_units["closing"],
    }


def _legacy_entries_id_from_notes(notes):
    if not notes:
        return None
    match = LEGACY_MIGRATION_NOTES_PATTERN.match(notes.strip())
    return int(match.group(1)) if match else None


def audit_opening_migration():
    """
    Read-only, complete audit of every legacy `entries` row's Opening
    Stock migration — final legacy-opening-migration investigation,
    section 5. Never modifies anything. For every legacy row: decodes and
    reconciles the full spreadsheet equation, cross-references the
    CURRENT DailyFigure/StockAdjustment state for that exact
    product/date/shift, and classifies it into exactly one of the
    CLASS_* categories above.

    "Missing legacy Opening anchor" (CLASS_MISSING_OPENING_ANCHOR) is the
    ONLY category the repair command ever acts on: a DailyFigure row that
    (a) still carries the migration's own notes marker, (b) is not
    already legacy_migrated_opening, (c) is not a genuine later
    manual_correction, and (d) still holds a stored opening_base_qty
    matching the decoded legacy value exactly (i.e. nothing has touched
    it since migration) — repairing it is a pure provenance reclassification,
    never a quantity change.
    """
    conn = get_db()
    legacy_rows = conn.execute("SELECT * FROM entries ORDER BY date, shift, product").fetchall()
    conn.close()

    from webapp.services import stock_service as svc

    report = []
    for raw_row in legacy_rows:
        row = dict(raw_row)
        entry = {
            "entries_id": row["id"], "date": row["date"], "shift": row["shift"],
            "legacy_product_name": row["product"],
            "legacy_raw": {k: row[k] for k in ("opening", "return_val", "production", "issued", "closing")},
        }

        product = _resolve_product(row["product"])
        if product is None:
            entry.update({
                "product_id": None, "product_name": None, "classification": CLASS_AMBIGUOUS_REVIEW,
                "reason": f"product name '{row['product']}' does not match any current product",
            })
            report.append(entry)
            continue
        entry["product_id"], entry["product_name"] = product.id, product.name

        rule = product.current_packaging_rule()
        if rule is None:
            entry.update({"classification": CLASS_AMBIGUOUS_REVIEW, "reason": f"'{product.name}' has no packaging rule configured"})
            report.append(entry)
            continue
        entry["packaging_rule_id"] = rule.id

        recon = decode_legacy_row(row, rule)
        if recon["errors"]:
            entry.update({"classification": CLASS_AMBIGUOUS_REVIEW, "reason": f"legacy value(s) could not be decoded: {recon['errors']}"})
            report.append(entry)
            continue

        entry["legacy_base_units"] = recon["base_units"]
        entry["legacy_recalculated_closing_base_qty"] = recon["recalculated_closing_base_qty"]
        entry["legacy_equation_reconciles"] = recon["reconciles"]
        entry["legacy_opening_label"] = svc.qty_label_signed(recon["base_units"]["opening"], rule)
        entry["legacy_closing_label"] = svc.qty_label_signed(recon["base_units"]["closing"], rule)

        if not recon["reconciles"]:
            entry.update({
                "classification": CLASS_DOES_NOT_RECONCILE,
                "reason": (
                    f"recalculated Closing ({recon['recalculated_closing_base_qty']}) does not match the legacy "
                    f"row's own stated Closing ({recon['base_units']['closing']}) — never auto-repaired"
                ),
            })
            report.append(entry)
            continue

        figure = DailyFigure.query.filter_by(product_id=product.id, date=row["date"], shift=row["shift"]).first()
        legacy_opening_base = recon["base_units"]["opening"]

        entry["existing_daily_figure_id"] = figure.id if figure else None
        entry["existing_opening_stock_source"] = figure.opening_stock_source if figure else None
        entry["existing_opening_stock_is_override"] = figure.opening_stock_is_override if figure else None
        entry["existing_opening_base_qty"] = figure.opening_base_qty if figure else None
        entry["existing_trusted_anchor"] = svc._is_trusted_anchor(figure) if figure else None
        entry["existing_migrated_adjustment_ids"] = [
            a.id for a in StockAdjustment.query.filter_by(
                product_id=product.id, date=row["date"], shift=row["shift"],
            ).all()
            if a.reason and f"entries.id={row['id']})" in a.reason
        ]
        entry["current_closing_base_qty"] = svc.daily_figure_view(product, row["date"], row["shift"])["closing"]["base_qty"] if figure else None

        if figure is None:
            entry.update({
                "classification": CLASS_AMBIGUOUS_REVIEW,
                "reason": "no DailyFigure row exists for this legacy row yet — it was never migrated "
                          "(run the ordinary legacy migration first, not this repair tool)",
            })
            report.append(entry)
            continue

        migration_entries_id = _legacy_entries_id_from_notes(figure.notes)
        looks_migration_created = migration_entries_id == row["id"]
        entry["existing_row_looks_migration_created"] = looks_migration_created

        if figure.opening_stock_source == "manual_correction" and not looks_migration_created:
            entry.update({
                "classification": CLASS_EXISTING_MANUAL_CORRECTION,
                "reason": "a real user's later manual correction exists for this exact period — never touched",
                "proposed_repair_action": "none",
            })
        elif looks_migration_created and figure.opening_base_qty == legacy_opening_base and figure.opening_stock_source != OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING:
            projected_closing = svc.closing_base_qty(figure)
            entry.update({
                "classification": CLASS_MISSING_OPENING_ANCHOR,
                "reason": (
                    f"still holds its originally-migrated opening ({legacy_opening_base}) but is source="
                    f"'{figure.opening_stock_source}' (currently trusted={entry['existing_trusted_anchor']}) — "
                    "eligible for a pure provenance repair (reclassify only, no quantity change)"
                ),
                "proposed_repair_action": "reclassify opening_stock_source to legacy_migrated_opening",
                "projected_closing_after_repair": projected_closing,
                "projected_closing_after_repair_label": svc.qty_label_signed(projected_closing, rule),
                "current_vs_projected_delta": (
                    (projected_closing - entry["current_closing_base_qty"]) if entry["current_closing_base_qty"] is not None else None
                ),
            })
        elif entry["current_closing_base_qty"] is not None and entry["current_closing_base_qty"] < 0:
            # NOTE: the legacy row's OWN decoded fields (base_units) can
            # never individually be negative here — decode_legacy_value()
            # rejects any negative raw legacy float before we ever reach
            # this point (old paper book-notation cannot record a negative
            # number at all). A genuine negative balance only ever shows up
            # in the CURRENT, live-computed closing (which incorporates
            # every dispatch/adjustment recorded since migration, not just
            # this one legacy row) — e.g. real over-issuance against a
            # correctly-trusted opening anchor. That is not a migration
            # defect, so it is reported for visibility and never repaired.
            entry.update({
                "classification": CLASS_GENUINE_NEGATIVE,
                "reason": (
                    f"current computed Closing ({entry['current_closing_base_qty']}) is genuinely negative "
                    "even with a correctly-trusted opening anchor — reflects real recorded activity, not a "
                    "migration defect — never forced to zero or positive"
                ),
                "proposed_repair_action": "none",
            })
        elif figure.opening_stock_source == OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING and figure.opening_base_qty == legacy_opening_base:
            entry.update({
                "classification": CLASS_ALREADY_MIGRATED_CORRECTLY,
                "reason": "already legacy_migrated_opening with the correct value — no repair required",
                "proposed_repair_action": "none",
            })
        elif figure.opening_base_qty == legacy_opening_base and svc._is_trusted_anchor(figure):
            entry.update({
                "classification": CLASS_NO_REPAIR_REQUIRED,
                "reason": "current value matches the legacy record and is already trusted",
                "proposed_repair_action": "none",
            })
        else:
            entry.update({
                "classification": CLASS_AMBIGUOUS_REVIEW,
                "reason": (
                    f"stored opening_base_qty ({figure.opening_base_qty}) does not match the decoded legacy "
                    f"opening ({legacy_opening_base}), or provenance is unclear — requires a human to review, "
                    "never auto-repaired"
                ),
                "proposed_repair_action": "none",
            })

        report.append(entry)

    return report


# =====================================================================
# Safe repair — dry-run by default, token-gated apply.
# =====================================================================

class LegacyMigrationRepairConflict(ValueError):
    """The database changed since the dry-run preview (or no valid token
    was supplied) — mapped to a non-zero CLI exit code, never silently
    applied against different data than what was previewed."""


def _repair_candidates(product_id=None):
    """Exactly the CLASS_MISSING_OPENING_ANCHOR rows from
    audit_opening_migration() — the ONLY category this repair ever acts
    on: a pure provenance reclassification (opening_stock_source ->
    legacy_migrated_opening), never a quantity change, never touching a
    genuine later manual_correction, a reconciliation mismatch, or a
    genuinely negative legacy balance."""
    report = audit_opening_migration()
    candidates = [r for r in report if r["classification"] == CLASS_MISSING_OPENING_ANCHOR]
    if product_id is not None:
        candidates = [r for r in candidates if r["product_id"] == product_id]
    return candidates


def _fingerprint(candidates):
    """A stable fingerprint of exactly which DailyFigure rows would be
    repaired and their current state (id, updated_at, source,
    opening_base_qty) — the preview token. Recomputing it at apply time
    and requiring an exact match is what makes apply refuse to run
    against data that changed since the dry run (final legacy-opening-
    migration investigation, section 8)."""
    import hashlib
    parts = []
    for c in sorted(candidates, key=lambda c: c["existing_daily_figure_id"]):
        figure = db.session.get(DailyFigure, c["existing_daily_figure_id"])
        parts.append(
            f"{figure.id}:{figure.updated_at.isoformat() if figure.updated_at else ''}:"
            f"{figure.opening_stock_source}:{figure.opening_base_qty}"
        )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def preview_opening_repair(product_id=None):
    """Dry run — never modifies anything. Returns the exact candidates a
    subsequent --apply would touch, plus the token that apply must be
    given back verbatim."""
    candidates = _repair_candidates(product_id)
    return {"candidates": candidates, "preview_token": _fingerprint(candidates), "count": len(candidates)}


def apply_opening_repair(actor, product_id=None, preview_token=None):
    """
    Applies the repair — idempotent (a second run finds nothing left to
    repair, since repaired rows reclassify to
    CLASS_ALREADY_MIGRATED_CORRECTLY), token-gated (refuses if the live
    candidate set no longer matches what was previewed), and creates one
    audit_log entry per repaired row. Only ever changes
    opening_stock_source — never opening_base_qty/cartons/packs/pieces,
    never a source book, never a StockAdjustment, never a genuine later
    manual_correction.
    """
    from webapp.services.audit_service import record_audit

    candidates = _repair_candidates(product_id)
    current_token = _fingerprint(candidates)
    if not preview_token or current_token != preview_token:
        raise LegacyMigrationRepairConflict(
            "The database has changed since the preview (or no valid --preview-token was supplied) — "
            "run the dry run again and pass its exact token."
        )

    repaired = []
    for c in candidates:
        figure = db.session.get(DailyFigure, c["existing_daily_figure_id"])
        before = {"opening_stock_source": figure.opening_stock_source}
        figure.opening_stock_source = OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING
        figure.updated_by = actor.id
        db.session.flush()
        record_audit(
            actor, "repair_legacy_opening_migration", "daily_figure", entity_id=figure.id,
            before={**before, "entries_id": c["entries_id"], "date": c["date"], "shift": c["shift"]},
            after={"opening_stock_source": figure.opening_stock_source, "opening_base_qty": figure.opening_base_qty},
        )
        repaired.append({
            "daily_figure_id": figure.id, "entries_id": c["entries_id"],
            "product_id": c["product_id"], "product_name": c["product_name"],
            "date": c["date"], "shift": c["shift"],
            "projected_closing_after_repair": c.get("projected_closing_after_repair"),
        })

    db.session.flush()
    return {"repaired": repaired, "count": len(repaired)}
