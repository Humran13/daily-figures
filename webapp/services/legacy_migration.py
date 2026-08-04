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
    OPENING_STOCK_SOURCE_MANUAL_CORRECTION,
    OPENING_STOCK_SOURCE_RESET_CREATED,
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
# Urgent correction, "reset-created zero" investigation — a Reset Daily
# Values action (see daily_reset_service.execute()) zeroes Opening Stock
# and marks it OPENING_STOCK_SOURCE_RESET_CREATED, a *non-authoritative*
# marker. If nobody has touched the row since, it is still literally
# `reset_created` (CLASS_RESET_CREATED_ZERO). If an elevated user's next
# save nonetheless resubmitted the reset-left value unchanged (almost
# always 0), the row is now labeled `manual_correction` — indistinguishable
# from a genuine correction UNLESS the row's own audit history is
# consulted (see _reset_evidence_for_period()): a `reset_daily_values`
# AuditLog record for this exact date+shift+product, at or before this
# row's own updated_at, with the figure still sitting at exactly 0. When a
# reconciling legacy row PROVES a real, non-zero Opening should be there,
# this is CLASS_MISSING_ANCHOR_AFTER_RESET — the one new category the
# repair command may act on, restoring the QUANTITY itself (not just
# provenance, since it was actually overwritten) — never guessed, always
# gated by legacy-equation reconciliation and a matched reset record.
CLASS_RESET_CREATED_ZERO = "reset_created_zero"
CLASS_MISSING_ANCHOR_AFTER_RESET = "missing_legacy_opening_anchor_after_reset"

# Classifications that mean "this row's CURRENT Opening/Closing cannot yet
# be trusted" — used to gate CLASS_GENUINE_NEGATIVE on LATER periods of the
# same product (final legacy-migration investigation, urgent correction
# section 7/8): a later period's own Closing can look negative purely
# because it derives from an earlier, still-unrepaired anchor — that is
# not "genuine" until the chain above it is proven correct. See the
# `product_needs_upstream_repair` tracking in audit_opening_migration().
_UPSTREAM_POISONING_CLASSES = frozenset({
    CLASS_DOES_NOT_RECONCILE, CLASS_MISSING_OPENING_ANCHOR,
    CLASS_MISSING_ANCHOR_AFTER_RESET, CLASS_RESET_CREATED_ZERO,
})
# Classifications that represent a fresh, trusted anchor point — reaching
# one of these for a product resets the upstream-poisoning flag, since
# everything from here on derives from THIS row, not the earlier issue.
_UPSTREAM_CLEARING_CLASSES = frozenset({
    CLASS_ALREADY_MIGRATED_CORRECTLY, CLASS_NO_REPAIR_REQUIRED, CLASS_EXISTING_MANUAL_CORRECTION,
})


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


def _reset_evidence_for_period(product, date, shift):
    """
    Urgent correction, "reset-created zero" investigation — the most
    recent `reset_daily_values` AuditLog record (see
    daily_reset_service.execute()) that touched this exact product's
    exact date+shift, whether it was a single-product reset (entity_id
    ends in the product's own id) or an "all products" reset (entity_id
    ends in "all"). Returns the AuditLog row itself (actor/timestamp/
    mode/reason are all on or inside it) or None if no reset ever touched
    this period. Read-only — never used to change anything by itself.
    """
    from webapp.models.audit_log import AuditLog
    return (
        AuditLog.query.filter(
            AuditLog.entity_type == "daily_figure_reset",
            AuditLog.entity_id.in_([f"{date}|{shift}|{product.id}", f"{date}|{shift}|all"]),
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )


def _reset_evidence_summary(reset_record):
    """JSON-safe summary of a reset AuditLog record for the audit report —
    actor, timestamp, mode, and reason, straight from the reset's own
    audited `after` payload (see daily_reset_service.execute()'s
    record_audit call). None if there is no reset evidence at all."""
    if reset_record is None:
        return None
    after = reset_record.to_dict()["after"] or {}
    return {
        "audit_log_id": reset_record.id,
        "actor_username": reset_record.username,
        "reset_at": reset_record.created_at.isoformat() if reset_record.created_at else None,
        "mode": after.get("mode"),
        "reason": after.get("reason"),
    }


def _projected_closing_with_opening(figure, opening_base_qty):
    """What figure.closing would be if `opening_base_qty` were trusted for
    this exact row, WITHOUT actually changing anything — used to project
    the after-repair Closing for CLASS_MISSING_ANCHOR_AFTER_RESET, where
    (unlike CLASS_MISSING_OPENING_ANCHOR) the row's own stored
    opening_base_qty is currently wrong (zeroed by a reset), so
    stock_service.closing_base_qty(figure) — which reads figure's own
    stored opening directly — cannot be used as-is."""
    from webapp.services import stock_service as svc
    issued = svc.issued_base_qty(figure.product_id, figure.date, figure.shift)
    return_total = svc.return_base_qty(figure.product_id, figure.date, figure.shift, figure.return_base_qty)
    production_total = svc.production_base_qty(figure.product_id, figure.date, figure.shift, figure.production_base_qty)
    return svc.compute_closing(opening_base_qty, production_total, return_total, issued)


def audit_opening_migration():
    """
    Read-only, complete audit of every legacy `entries` row's Opening
    Stock migration — final legacy-opening-migration investigation,
    section 5. Never modifies anything. For every legacy row: decodes and
    reconciles the full spreadsheet equation, cross-references the
    CURRENT DailyFigure/StockAdjustment state for that exact
    product/date/shift, and classifies it into exactly one of the
    CLASS_* categories above.

    "Missing legacy Opening anchor" (CLASS_MISSING_OPENING_ANCHOR) is a
    category the repair command acts on: a DailyFigure row that
    (a) still carries the migration's own notes marker, (b) is not
    already legacy_migrated_opening, (c) is not a genuine later
    manual_correction, and (d) still holds a stored opening_base_qty
    matching the decoded legacy value exactly (i.e. nothing has touched
    it since migration) — repairing it is a pure provenance reclassification,
    never a quantity change.

    Urgent correction, "reset-created zero" investigation — a SECOND,
    riskier repairable category, CLASS_MISSING_ANCHOR_AFTER_RESET: a row
    whose Opening was cleared by a real Reset Daily Values action (proven
    via an AuditLog `reset_daily_values` record for this exact
    product/date/shift — see _reset_evidence_for_period()), currently
    sitting at 0 (whether still literally `reset_created`, or already
    mislabeled `manual_correction` by an unwitting later resave of that
    same 0), where the legacy ledger proves a real non-zero Opening
    belongs there instead. Unlike CLASS_MISSING_OPENING_ANCHOR, repairing
    this ALSO restores the quantity itself (opening_base_qty and its
    cartons/packs/pieces split), not just provenance — because the reset
    actually overwrote it, whereas CLASS_MISSING_OPENING_ANCHOR's value
    was only ever mistrusted, never overwritten. CLASS_RESET_CREATED_ZERO
    is the same reset-evidence check but with nothing to restore (the
    legacy Opening was itself 0) — flagged for visibility, never repaired.

    A later period's own negative Closing is only ever classified
    CLASS_GENUINE_NEGATIVE once every EARLIER period for that exact
    product has been proven clean (see `product_needs_upstream_repair`
    below) — otherwise it is CLASS_AMBIGUOUS_REVIEW, since it may simply
    be inheriting an unrepaired or reset-damaged anchor further up the
    chain (e.g. a Night shift's Closing looking negative purely because
    the same date's Day shift was reset and not yet repaired).
    """
    conn = get_db()
    legacy_rows = conn.execute("SELECT * FROM entries ORDER BY date, shift, product").fetchall()
    conn.close()

    from webapp.services import stock_service as svc

    # Urgent correction, "reset-created zero" investigation, section 7/8 —
    # per-product running flag: has an EARLIER row for this exact product
    # (rows are visited in chronological date/shift order, interleaved
    # with other products, but always chronological WITHIN one product)
    # been left in a state we cannot yet trust? If so, a LATER row's own
    # negative Closing cannot be confirmed "genuine" — it may simply be
    # inheriting the earlier, still-broken anchor. See
    # _UPSTREAM_POISONING_CLASSES/_UPSTREAM_CLEARING_CLASSES above.
    product_needs_upstream_repair = {}

    report = []

    def _finish(entry, product):
        # Urgent correction, "reset-created zero" investigation, section
        # 7/8 — update the per-product upstream-trust flag based on what
        # this row was classified as, then record it. Must run for EVERY
        # exit point of the loop (including the early product/rule/decode
        # failures), so a later row for the same product always sees an
        # accurate picture of whether anything upstream is still broken.
        if product is not None:
            cls = entry.get("classification")
            if cls in _UPSTREAM_POISONING_CLASSES:
                product_needs_upstream_repair[product.id] = True
            elif cls in _UPSTREAM_CLEARING_CLASSES:
                product_needs_upstream_repair[product.id] = False
        report.append(entry)

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
            _finish(entry, product)
            continue
        entry["product_id"], entry["product_name"] = product.id, product.name

        rule = product.current_packaging_rule()
        if rule is None:
            entry.update({"classification": CLASS_AMBIGUOUS_REVIEW, "reason": f"'{product.name}' has no packaging rule configured"})
            _finish(entry, product)
            continue
        entry["packaging_rule_id"] = rule.id

        recon = decode_legacy_row(row, rule)
        if recon["errors"]:
            entry.update({"classification": CLASS_AMBIGUOUS_REVIEW, "reason": f"legacy value(s) could not be decoded: {recon['errors']}"})
            _finish(entry, product)
            continue

        entry["legacy_base_units"] = recon["base_units"]
        entry["legacy_opening_cpp"] = recon["decoded_cpp"]["opening"]
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
            _finish(entry, product)
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
            _finish(entry, product)
            continue

        migration_entries_id = _legacy_entries_id_from_notes(figure.notes)
        looks_migration_created = migration_entries_id == row["id"]
        entry["existing_row_looks_migration_created"] = looks_migration_created

        # Urgent correction, "reset-created zero" investigation, sections
        # 1-4 — reset evidence for this EXACT period, cross-referenced
        # regardless of the figure's current opening_stock_source: still
        # literally `reset_created` (untouched since the reset) is
        # self-evident; already promoted to `manual_correction` needs the
        # AuditLog trail (a reset record for this scope, at or before this
        # row's own updated_at, with the figure still sitting at exactly
        # the value the reset left — 0).
        reset_record = _reset_evidence_for_period(product, row["date"], row["shift"])
        entry["reset_evidence"] = _reset_evidence_summary(reset_record)
        currently_reset_created = figure.opening_stock_source == OPENING_STOCK_SOURCE_RESET_CREATED
        promoted_reset_created_zero = (
            figure.opening_stock_source == OPENING_STOCK_SOURCE_MANUAL_CORRECTION
            and figure.opening_base_qty == 0
            and reset_record is not None
            and figure.updated_at is not None
            and figure.updated_at >= reset_record.created_at
        )
        is_reset_created_zero = currently_reset_created or promoted_reset_created_zero
        entry["reset_already_promoted_to_manual_correction"] = promoted_reset_created_zero

        if figure.opening_stock_source == OPENING_STOCK_SOURCE_MANUAL_CORRECTION and not looks_migration_created and not is_reset_created_zero:
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
        elif is_reset_created_zero and legacy_opening_base != 0:
            # The one category the repair command may restore a QUANTITY
            # for (not just provenance) — Reset Daily Values zeroed this
            # row (proven via reset_record above), and the legacy ledger
            # PROVES a real, non-zero Opening belongs here instead.
            projected_closing = _projected_closing_with_opening(figure, legacy_opening_base)
            entry.update({
                "classification": CLASS_MISSING_ANCHOR_AFTER_RESET,
                "reason": (
                    f"Opening Stock was cleared by a Reset Daily Values action"
                    + (f" ({entry['reset_evidence']['mode']} mode, by {entry['reset_evidence']['actor_username']}"
                       f" on {entry['reset_evidence']['reset_at']})" if entry["reset_evidence"] else "")
                    + (" and later resaved unchanged as a manual_correction" if promoted_reset_created_zero else "")
                    + f" — the legacy ledger proves the real Opening was {legacy_opening_base}, not 0 — "
                    "eligible for a repair that RESTORES the quantity (not just provenance)"
                ),
                "proposed_repair_action": "restore opening_base_qty/cartons/packs/pieces to the legacy value and reclassify to legacy_migrated_opening",
                "projected_closing_after_repair": projected_closing,
                "projected_closing_after_repair_label": svc.qty_label_signed(projected_closing, rule),
                "current_vs_projected_delta": (
                    (projected_closing - entry["current_closing_base_qty"]) if entry["current_closing_base_qty"] is not None else None
                ),
            })
        elif is_reset_created_zero:
            entry.update({
                "classification": CLASS_RESET_CREATED_ZERO,
                "reason": (
                    "Opening Stock was cleared by a Reset Daily Values action — a non-authoritative marker, "
                    "never an authoritative correction — but the legacy ledger's own Opening for this period "
                    "is also 0, so there is nothing to restore"
                ),
                "proposed_repair_action": "none",
            })
        elif product_needs_upstream_repair.get(product.id) and entry["current_closing_base_qty"] is not None and entry["current_closing_base_qty"] < 0:
            entry.update({
                "classification": CLASS_AMBIGUOUS_REVIEW,
                "reason": (
                    "current Closing is negative, but an EARLIER period for this exact product has its own "
                    "unrepaired/reset-damaged Opening anchor — this negative cannot be confirmed genuine until "
                    "that earlier period is repaired and the audit is re-run; never auto-repaired"
                ),
                "proposed_repair_action": "none",
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
            # Only reached once every EARLIER period for this product is
            # proven clean (see product_needs_upstream_repair above) — the
            # correct prior Opening Stock chain has been reconstructed.
            entry.update({
                "classification": CLASS_GENUINE_NEGATIVE,
                "reason": (
                    f"current computed Closing ({entry['current_closing_base_qty']}) is genuinely negative "
                    "even with a correctly-trusted opening anchor, and every earlier period for this product "
                    "is proven clean — reflects real recorded activity, not a migration defect — never repaired"
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

        _finish(entry, product)

    return report


# =====================================================================
# Safe repair — dry-run by default, token-gated apply.
# =====================================================================

class LegacyMigrationRepairConflict(ValueError):
    """The database changed since the dry-run preview (or no valid token
    was supplied) — mapped to a non-zero CLI exit code, never silently
    applied against different data than what was previewed."""


_REPAIRABLE_CLASSES = (CLASS_MISSING_OPENING_ANCHOR, CLASS_MISSING_ANCHOR_AFTER_RESET)


def _repair_candidates(product_id=None):
    """Exactly the CLASS_MISSING_OPENING_ANCHOR and, since the urgent
    "reset-created zero" correction, CLASS_MISSING_ANCHOR_AFTER_RESET rows
    from audit_opening_migration() — the ONLY two categories this repair
    ever acts on. Never touches a genuine later manual_correction, a
    reconciliation mismatch, a genuinely negative legacy balance, a plain
    reset-created zero with nothing to restore, or anything still
    ambiguous (including a negative Closing pending an earlier period's
    own repair — see product_needs_upstream_repair)."""
    report = audit_opening_migration()
    candidates = [r for r in report if r["classification"] in _REPAIRABLE_CLASSES]
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
    audit_log entry per repaired row. Never touches a source book, a
    StockAdjustment, a genuine later manual_correction, or the reset
    AuditLog record itself (its actor/timestamp/mode/reason are preserved
    exactly as they were — this repair only ever ADDS a new audit_log
    entry, never edits or deletes an existing one).

    Two distinct repair actions, one per candidate classification:
      - CLASS_MISSING_OPENING_ANCHOR: pure provenance reclassification —
        opening_stock_source only, never opening_base_qty/cartons/packs/
        pieces (the stored value was only ever mistrusted, never touched).
      - CLASS_MISSING_ANCHOR_AFTER_RESET (urgent "reset-created zero"
        correction): the stored value WAS actually overwritten (by Reset
        Daily Values, to 0), so this ALSO restores opening_base_qty and
        its cartons/packs/pieces split to the proven legacy value, in
        addition to the same provenance reclassification. Still never
        touches Production/Returns/Issued/StockAdjustment quantities.
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
        before = {
            "opening_stock_source": figure.opening_stock_source,
            "opening_base_qty": figure.opening_base_qty,
            "opening_cartons": figure.opening_cartons, "opening_packs": figure.opening_packs,
            "opening_pieces": figure.opening_pieces,
        }
        if c["classification"] == CLASS_MISSING_ANCHOR_AFTER_RESET:
            cartons, packs, pieces = c["legacy_opening_cpp"]
            figure.opening_cartons, figure.opening_packs, figure.opening_pieces = cartons, packs, pieces
            figure.opening_base_qty = c["legacy_base_units"]["opening"]
            figure.opening_stock_is_override = True
        figure.opening_stock_source = OPENING_STOCK_SOURCE_LEGACY_MIGRATED_OPENING
        figure.updated_by = actor.id
        db.session.flush()
        after = {
            "opening_stock_source": figure.opening_stock_source, "opening_base_qty": figure.opening_base_qty,
            "opening_cartons": figure.opening_cartons, "opening_packs": figure.opening_packs,
            "opening_pieces": figure.opening_pieces,
        }
        record_audit(
            actor, "repair_legacy_opening_migration", "daily_figure", entity_id=figure.id,
            before={**before, "entries_id": c["entries_id"], "date": c["date"], "shift": c["shift"], "classification": c["classification"]},
            after=after,
        )
        repaired.append({
            "daily_figure_id": figure.id, "entries_id": c["entries_id"],
            "product_id": c["product_id"], "product_name": c["product_name"],
            "date": c["date"], "shift": c["shift"], "classification": c["classification"],
            "projected_closing_after_repair": c.get("projected_closing_after_repair"),
        })

    db.session.flush()
    return {"repaired": repaired, "count": len(repaired)}
