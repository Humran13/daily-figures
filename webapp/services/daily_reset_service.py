"""
Manager/Super-Administrator "Reset Daily Values" control (Stage 7 section
4, extended by the final pre-deployment correction). Narrowly scoped to
one Date + Shift, and either one Product or every active product for that
Date + Shift — never a wider blast radius than that, and never touches
another date or shift.

Two distinct, clearly separated modes:

  MODE_FIGURES_ONLY ("Reset Daily Figures Status Only") — clears only the
  Daily Figures workflow layer: a manually-entered Opening Stock (reset to
  zero, un-anchored — never deleted outright, the row/id/created_at stay
  intact), Notes, and the in-progress/completed review markers (see
  webapp/services/daily_entry_status_service.py). Existing finalized
  Dispatch/Returns/Production records are left completely untouched and
  continue affecting Daily Figures and carried stock exactly as before.

  MODE_FULL ("Full Reset — Start This Period Again") — does everything
  MODE_FIGURES_ONLY does, AND additionally neutralizes matching finalized
  (or abandoned-draft) Dispatch/Returns/Production activity for that exact
  Date + Shift(+Product) scope, using each source book's own established
  safe void mechanism (never a hard delete) — see
  _neutralize_source_for_product() below. A record with lines for other
  products too has only the target product's lines removed (via a
  reopen -> delete lines -> refinalize cycle when it was finalized);
  a record left with no lines at all is voided. This is destructive
  enough that the route layer requires typed confirmation text before
  calling it (see webapp/routes/daily_reset.py).

Either mode's product-scoped effect is completely independent of every
other product — a reset never touches a product outside its selected
scope, and Mode B's source-record surgery never touches another product's
lines within a shared multi-product record.

Urgent correction, "reset-created zero" investigation — both modes clear
Opening Stock the same way: to exactly 0, and to
OPENING_STOCK_SOURCE_RESET_CREATED, a *non-authoritative reset marker*
(never anchor-eligible — see webapp/models/daily_figure.py), NOT
`derived` as before. This is deliberate, not a quantity/authority change:
a reset-cleared row still self-heals on the very next view (see
stock_service.daily_figure_view() — a non-anchor-eligible source is
always live-re-derived from whatever real history precedes it, so the
Opening shown is immediately correct again, not 0). What changed is
durability of the SIGNAL: `reset_created` survives at least until the
next save, giving webapp/services/legacy_migration.py's audit tool a
reliable way to recognize a reset-created zero even if a later elevated
save mistakenly resubmits that same 0 as an explicit override — which
upsert_daily_figure() (correctly, given only what IT can see) would
otherwise lock in forever as an unconditionally-trusted manual_correction,
permanently blocking any legacy-anchor restoration. Reset itself has
never directly written manual_correction — that mislabeling can only
happen via a SEPARATE, later save — but the reset marker is what makes
that later mistake detectable and safely reversible instead of invisible.
Full Reset's own additional effect (source-record neutralization) is
unchanged and orthogonal to this: it removes this exact period's own
movement records; it does not, and never did, touch an earlier period's
Opening Stock anchor.
"""
from datetime import datetime, timedelta

from webapp.extensions import db
from webapp.models.daily_figure import (
    OPENING_STOCK_SOURCE_LEDGER_CUTOVER,
    OPENING_STOCK_SOURCE_RESET_CREATED,
    DailyFigure,
    StockAdjustment,
)
from webapp.models.dispatch import SHIFT_DAY, SHIFT_NIGHT, SHIFTS, STATUS_VOID as DISPATCH_VOID, Dispatch, DispatchLine
from webapp.models.product import Product
from webapp.models.production_record import (
    STATUS_VOID as PRODUCTION_VOID, ProductionLine, ProductionRecord,
)
from webapp.models.return_record import STATUS_VOID as RETURNS_VOID, ReturnLine, ReturnRecord
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import daily_entry_status_service, daily_review_service, dispatch_service, production_service, returns_service, stock_service as svc
from webapp.services.audit_service import record_audit
from webapp.services.quantity_format import qty_label

# How this reason string is written by the legacy Issued-figure migration
# (see the legacy-adjustment investigation's completion report) — used
# only to LABEL a row in the preview as "migrated legacy", never to
# change whether it affects stock (every StockAdjustment always does).
_LEGACY_MIGRATED_ADJUSTMENT_PREFIX = "Migrated legacy issued figure"

# Bounds how far back preview() will walk to name the exact period a
# carried-forward negative balance originated from (final stock-integrity
# investigation, section 8) — a preview must stay cheap even for "all
# products", so this is a safety cap, not a claim that no earlier
# movement could possibly exist beyond it; flask stock-ledger has no such
# cap for a deliberate, unbounded trace.
_CARRIED_NEGATIVE_LOOKBACK_DAYS = 60

MODE_FIGURES_ONLY = "figures_only"
MODE_FULL = "full"
MODES = [MODE_FIGURES_ONLY, MODE_FULL]

# One small registry so the neutralization logic below is written once and
# shared by all three source books, rather than three near-identical
# copies — see _remove_product_from_record().
_SOURCE_REGISTRY = {
    "dispatch": {
        "line_fk": "dispatch_id", "void_status": DISPATCH_VOID,
        "reopen": dispatch_service.reopen_dispatch, "finalize": dispatch_service.finalize_dispatch,
        "void": dispatch_service.void_dispatch, "error": dispatch_service.DispatchError,
    },
    "returns": {
        "line_fk": "return_id", "void_status": RETURNS_VOID,
        "reopen": returns_service.reopen_return, "finalize": returns_service.finalize_return,
        "void": returns_service.void_return, "error": returns_service.ReturnError,
    },
    "production": {
        "line_fk": "production_id", "void_status": PRODUCTION_VOID,
        "reopen": production_service.reopen_production, "finalize": production_service.finalize_production,
        "void": production_service.void_production, "error": production_service.ProductionError,
    },
}


class DailyResetError(ValueError):
    """User-facing validation problem — mapped to HTTP 400 by routes."""


def _require_manager_or_super_admin(actor):
    if actor.role not in (ROLE_MANAGER, ROLE_SUPER_ADMIN):
        raise DailyResetError("Only a Manager or Super Administrator may reset daily values")


def _target_products(product_id):
    if product_id is not None:
        product = db.session.get(Product, product_id)
        if product is None:
            raise DailyResetError("product does not exist")
        return [product]
    return Product.query.filter_by(active=True).order_by(Product.display_order, Product.name).all()


def _validate(date, shift, mode, actor):
    _require_manager_or_super_admin(actor)
    if not date:
        raise DailyResetError("date is required")
    if shift not in SHIFTS:
        raise DailyResetError(f"shift must be one of {SHIFTS}")
    if mode not in MODES:
        raise DailyResetError(f"mode must be one of {MODES}")


def _dispatch_rows_by_product(date, shift):
    """One query (not one per product) — every finalized-or-draft, non-void
    Dispatch line for this exact date+shift, grouped by product_id.
    Dispatch is Day-only, so a Night scope always yields nothing."""
    rows = {}
    if shift != SHIFT_DAY:
        return rows
    dispatches = Dispatch.query.filter(Dispatch.date == date, Dispatch.shift == SHIFT_DAY, Dispatch.status != DISPATCH_VOID).all()
    for d in dispatches:
        for line in d.lines:
            rows.setdefault(line.product_id, []).append({
                "record_id": d.id, "label": f"Dispatch #{d.dispatch_number}", "status": d.status,
                "quantity_label": qty_label(line.cartons, line.packs, line.pieces, line.packaging_rule),
                "base_unit_qty": line.base_unit_qty,
            })
    return rows


def _returns_rows_by_product(date, shift):
    """Returns has no shift column at all — Day-only by omission, exactly
    like Dispatch, so a Night scope always yields nothing here too."""
    rows = {}
    if shift != SHIFT_DAY:
        return rows
    records = ReturnRecord.query.filter(ReturnRecord.date == date, ReturnRecord.status != RETURNS_VOID).all()
    for r in records:
        for line in r.lines:
            rows.setdefault(line.product_id, []).append({
                "record_id": r.id, "label": f"Return #{r.id}", "status": r.status,
                "quantity_label": qty_label(line.cartons, line.packs, line.pieces, line.packaging_rule),
                "base_unit_qty": line.base_unit_qty,
            })
    return rows


def _production_rows_by_product(date, shift):
    """Production supports both Day and Night — filtered to the exact
    selected shift, never the other one."""
    rows = {}
    records = ProductionRecord.query.filter(ProductionRecord.date == date, ProductionRecord.shift == shift, ProductionRecord.status != PRODUCTION_VOID).all()
    for p in records:
        for line in p.lines:
            rows.setdefault(line.product_id, []).append({
                "record_id": p.id, "label": f"Production #{p.id}", "status": p.status,
                "quantity_label": qty_label(line.cartons, line.packs, line.pieces, line.packaging_rule),
                "base_unit_qty": line.base_unit_qty,
            })
    return rows


def _adjustment_rows_by_product(date, shift):
    """Final stock-integrity investigation, section 8 — the preview
    previously never looked at StockAdjustment at all, so a period whose
    entire stock effect came from an adjustment (a legacy-migrated Issued
    correction, or an ordinary manual one) misleadingly reported
    "no matching Production/Returns/Dispatch records" even though it
    genuinely affects stock. One query (not one per product), grouped by
    product_id — StockAdjustment has both a date and shift column, so
    unlike Dispatch/Returns this is never Day-only by construction."""
    rows = {}
    adjustments = StockAdjustment.query.filter_by(date=date, shift=shift).all()
    for a in adjustments:
        rows.setdefault(a.product_id, []).append({
            "id": a.id, "delta_base_qty": a.delta_base_qty, "reason": a.reason,
            "created_by": a.created_by, "created_at": a.created_at.isoformat() if a.created_at else None,
            "is_legacy_migrated": bool(a.reason) and a.reason.startswith(_LEGACY_MIGRATED_ADJUSTMENT_PREFIX),
        })
    return rows


def _carried_negative_info(product, date, shift, has_current_period_movement):
    """Final stock-integrity investigation, section 8 — never describe a
    carried-forward negative as though it were current-period activity.
    If this exact period has no movement of its own (no Production/
    Returns/Dispatch/Adjustment) but its Closing Stock is negative, name
    the exact period the negative first appeared in, bounded by
    _CARRIED_NEGATIVE_LOOKBACK_DAYS so an "all products" preview always
    stays cheap — flask stock-ledger has no such bound for a deliberate,
    unbounded trace. Returns None when there is nothing carried (either
    genuine current-period movement, or a non-negative balance)."""
    if has_current_period_movement:
        return None
    view = svc.daily_figure_view(product, date, shift)
    closing = view["closing"]["base_qty"] if view["closing"] else None
    if closing is None or closing >= 0:
        return None

    from webapp.services import stock_ledger_service
    lookback_start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=_CARRIED_NEGATIVE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    anchor = svc._find_anchor_figure(product.id, date, shift)
    scan_start = max(anchor.date, lookback_start) if anchor is not None else lookback_start
    try:
        entries = stock_ledger_service.build_ledger(product.id, scan_start, date)
    except stock_ledger_service.LedgerError:
        entries = []
    origin = stock_ledger_service.first_negative_period(entries)
    if origin is None:
        return {
            "message": (
                f"No activity in this period. This balance was carried from before "
                f"{scan_start} — reload with a wider range or use `flask stock-ledger` for the exact origin."
            ),
            "originating_date": None, "originating_shift": None,
        }
    return {
        "message": f"No activity in this period. This balance was carried from {origin['date']} {origin['shift']}.",
        "originating_date": origin["date"], "originating_shift": origin["shift"],
    }


def preview(date, shift, product_id, actor, mode=MODE_FIGURES_ONLY):
    """
    What a reset would affect, without changing anything — shown to the
    Manager/Super Administrator before they confirm. Queries the exact
    same authoritative records execute() acts on (DailyFigure,
    DailyEntryStatus, and — since the final pre-deployment correction —
    finalized Dispatch/Returns/Production activity too), so this can never
    report "no affected products" while matching source-book activity
    actually exists — the previous defect was that this function only
    ever looked at DailyFigure/DailyEntryStatus and never checked the
    source books at all.
    """
    _validate(date, shift, mode, actor)
    products = _target_products(product_id)

    dispatch_by_product = _dispatch_rows_by_product(date, shift)
    returns_by_product = _returns_rows_by_product(date, shift)
    production_by_product = _production_rows_by_product(date, shift)
    adjustment_by_product = _adjustment_rows_by_product(date, shift)
    # Safety correction, item 2 — the preview must also show whether the
    # selected scope touches a submitted/in-progress review session or any
    # already-reviewed/edited/skipped product, so a Manager/Super
    # Administrator sees that consequence before confirming a reset.
    review_info = daily_review_service.preview_review_state(date, shift, [p.id for p in products])

    rows = []
    for product in products:
        figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()
        status = daily_entry_status_service.status_view(date, shift, product.id)
        has_manual_opening = figure is not None and (
            figure.opening_cartons or figure.opening_packs or figure.opening_pieces or figure.opening_base_qty
        )
        dispatch_rows = dispatch_by_product.get(product.id, [])
        returns_rows = returns_by_product.get(product.id, [])
        production_rows = production_by_product.get(product.id, [])
        adjustment_rows = adjustment_by_product.get(product.id, [])
        has_source_activity = bool(dispatch_rows or returns_rows or production_rows)
        has_adjustment_activity = bool(adjustment_rows)
        review_state = review_info["product_states"].get(product.id, "not_reviewed")
        carried_negative = _carried_negative_info(
            product, date, shift, has_current_period_movement=has_source_activity or has_adjustment_activity
        )

        rows.append({
            "product_id": product.id,
            "product_name": product.name,
            "has_manual_opening_entry": bool(has_manual_opening),
            "opening_qty": {
                "cartons": figure.opening_cartons, "packs": figure.opening_packs, "pieces": figure.opening_pieces,
            } if figure is not None else None,
            "is_opening_override": bool(figure and figure.opening_stock_is_override),
            "opening_stock_source": figure.opening_stock_source if figure is not None else None,
            # Legacy-opening-migration investigation, section 10 — an
            # authoritative legacy ledger anchor (never a Manager-typed
            # value) shown as its own explicit category, plus the row's
            # own legacy-stored Production/Returns columns (pre-Stage-5
            # data — see stock_service.closing_base_qty()'s
            # `legacy_stored` parameter) broken out separately so the
            # preview never lumps a genuine historical ledger figure in
            # with an ordinary Manager entry.
            "is_legacy_migrated_opening": bool(figure and figure.opening_stock_source == "legacy_migrated_opening"),
            # Urgent correction, "reset-created zero" investigation — a row
            # still literally carrying the non-authoritative reset marker
            # (see OPENING_STOCK_SOURCE_RESET_CREATED), shown here so a
            # Manager previewing ANOTHER reset over an already-reset period
            # can see that plainly, rather than it looking like an ordinary
            # derived row.
            "is_reset_created": bool(figure and figure.opening_stock_source == "reset_created"),
            "legacy_production_component": figure.production_base_qty if figure is not None else 0,
            "legacy_returns_component": figure.return_base_qty if figure is not None else 0,
            "has_notes": bool(figure and figure.notes),
            "review_status": status["status"],
            "finalized_dispatch": dispatch_rows,
            "finalized_returns": returns_rows,
            "finalized_production": production_rows,
            "has_source_activity": has_source_activity,
            # Final stock-integrity investigation, section 8 — a
            # StockAdjustment (including a legacy-migrated Issued
            # correction) genuinely affects stock but is a DIFFERENT
            # category from has_source_activity above: neither reset mode
            # currently touches StockAdjustment at all (see execute()),
            # so this is shown for visibility only, never folded into
            # preserved_in_mode_a/neutralized_in_mode_b below, which must
            # keep accurately describing only what a reset actually does.
            "stock_adjustments": adjustment_rows,
            "has_adjustment_activity": has_adjustment_activity,
            # Mode A never touches source books — always preserved there.
            # Mode B neutralizes exactly what's listed above, nothing else
            # — StockAdjustment rows are never neutralized by either mode.
            "preserved_in_mode_a": has_source_activity,
            "neutralized_in_mode_b": has_source_activity,
            # Never None unless there is genuinely nothing carried — see
            # _carried_negative_info()'s docstring. Distinguishes "this
            # period's own movement caused the negative" (has_source_activity
            # or has_adjustment_activity is True) from "nothing happened
            # here, the negative is inherited."
            "carried_negative": carried_negative,
            # Daily Figures review workflow state this reset would clear —
            # "not_reviewed" means the reset has nothing to clear for this
            # product, regardless of what the review session's own status is.
            "review_state": review_state,
        })

    any_affected = any(
        r["has_manual_opening_entry"] or r["has_notes"] or r["review_status"] != "not_started"
        or r["has_source_activity"] or r["has_adjustment_activity"] or r["review_state"] != "not_reviewed"
        for r in rows
    )
    return {
        "date": date, "shift": shift, "mode": mode, "any_affected": any_affected, "products": rows,
        "review_session_status": review_info["session_status"],
        "affects_submitted_review": review_info["session_status"] == daily_review_service.REVIEW_STATUS_SUBMITTED,
        "affects_in_progress_review": review_info["session_status"] in (
            daily_review_service.REVIEW_STATUS_IN_PROGRESS, daily_review_service.REVIEW_STATUS_REOPENED,
        ),
    }


def _remove_product_from_record(source_type, record, product_id, actor, reason):
    """
    Removes `product_id`'s line(s) from one Dispatch/ReturnRecord/
    ProductionRecord (Mode B only). If other products' lines remain, a
    finalized record is safely reopened, has just this product's lines
    deleted, and is refinalized — the exact same reopen/edit/finalize
    cycle a manual correction would use, just automated for this reset.
    If no lines remain at all, the record is voided via its own
    established safe void mechanism (never a hard delete — status +
    reason, fully traceable). Returns a small summary dict, or None if
    this record had nothing for this product at all.
    """
    cfg = _SOURCE_REGISTRY[source_type]
    matching_lines = [line for line in record.lines if line.product_id == product_id]
    if not matching_lines:
        return None

    remaining_lines = [line for line in record.lines if line.product_id != product_id]
    # The caller only ever queries non-void records, so status here is
    # always "draft" or "finalized" — a literal comparison, not another
    # import, since all three source-book modules use this exact string.
    was_finalized = record.status == "finalized"
    error_cls = cfg["error"]
    try:
        if was_finalized and remaining_lines:
            cfg["reopen"](record, actor, f"Daily reset (full): {reason}")
        for line in matching_lines:
            # record.lines.remove(...), not db.session.delete(...) directly
            # — the relationship's cascade="all, delete-orphan" still
            # deletes the row on flush, but ALSO keeps the in-memory
            # collection itself correctly synchronized. A direct
            # session.delete() bypasses the collection-level event, so a
            # second call against the same (identity-mapped) record later
            # in the same request — e.g. the next product in an
            # "all products" reset — would still see the already-deleted
            # line in record.lines and misjudge whether any lines remain.
            record.lines.remove(line)
        db.session.flush()

        from webapp.services import product_usage_service
        if remaining_lines:
            if was_finalized:
                cfg["finalize"](record, actor)
            remaining_product_ids = {line.product_id for line in remaining_lines}
            product_usage_service.record_usage(source_type, record.id, remaining_product_ids)
            return {"record_id": record.id, "action": "product_line_removed"}
        else:
            if record.status != cfg["void_status"]:
                cfg["void"](record, actor, f"Daily reset (full): {reason}")
            product_usage_service.remove_usage(source_type, record.id)
            return {"record_id": record.id, "action": "voided_empty"}
    except error_cls as e:
        raise DailyResetError(str(e)) from e


def _neutralize_source_for_product(date, shift, product_id, actor, reason):
    """Mode B: scans every matching, currently-active (non-void) Dispatch/
    Returns/Production record for this exact date+shift and removes this
    one product's contribution from each. Respects the same Day-only
    (Dispatch/Returns) / Day-or-Night (Production) rules as everywhere
    else in the app — never invents a Night Dispatch/Returns scope."""
    summary = {"dispatch": [], "returns": [], "production": []}

    if shift == SHIFT_DAY:
        for d in Dispatch.query.filter(Dispatch.date == date, Dispatch.shift == SHIFT_DAY, Dispatch.status != DISPATCH_VOID).all():
            result = _remove_product_from_record("dispatch", d, product_id, actor, reason)
            if result:
                summary["dispatch"].append(result)
        for r in ReturnRecord.query.filter(ReturnRecord.date == date, ReturnRecord.status != RETURNS_VOID).all():
            result = _remove_product_from_record("returns", r, product_id, actor, reason)
            if result:
                summary["returns"].append(result)

    for p in ProductionRecord.query.filter(ProductionRecord.date == date, ProductionRecord.shift == shift, ProductionRecord.status != PRODUCTION_VOID).all():
        result = _remove_product_from_record("production", p, product_id, actor, reason)
        if result:
            summary["production"].append(result)

    return summary


def execute(date, shift, product_id, reason, actor, mode=MODE_FIGURES_ONLY, confirmation_text=None):
    """Transactional: every affected product (and, in Mode B, every
    affected source record) is cleared together, or (on any failure) none
    of them are — the caller's route commits once at the end and rolls
    back the whole session on any exception here."""
    _validate(date, shift, mode, actor)
    if not reason:
        raise DailyResetError("A reason is required to reset daily values")
    if mode == MODE_FULL:
        expected_confirmation = f"FULL RESET {date} {shift.upper()}"
        if confirmation_text != expected_confirmation:
            raise DailyResetError(f'Full reset requires typed confirmation matching exactly: "{expected_confirmation}"')

    products = _target_products(product_id)

    # Safety correction, item 2 — cleared BEFORE the per-product/source
    # work below (not after) so that a later failure anywhere in this same
    # transaction — a bad product, a source-book conflict during Mode B
    # neutralization, anything — rolls this back too, exactly like every
    # other part of the reset. See clear_product_states_for_reset()'s
    # docstring; a no-op if this date+shift was never reviewed.
    review_reset = daily_review_service.clear_product_states_for_reset(
        date, shift, [p.id for p in products], actor, reason, mode
    )

    affected = []
    source_summary = {"dispatch": [], "returns": [], "production": []}
    for product in products:
        figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()
        before = None
        if figure is not None:
            before = {
                "opening_cartons": figure.opening_cartons, "opening_packs": figure.opening_packs,
                "opening_pieces": figure.opening_pieces, "opening_base_qty": figure.opening_base_qty,
                "opening_stock_is_override": figure.opening_stock_is_override,
                "opening_stock_source": figure.opening_stock_source,
                "notes": figure.notes,
            }
            # Final reset-safety correction, section 2/3 — MODE_FIGURES_ONLY
            # ("Daily Figures Status Only") no longer touches a single
            # Opening Stock column, Production/Returns/Issued, or any
            # source-book record: ONLY the workflow layer below (entry
            # status, review state) is cleared. This is the fix for the
            # actual root cause of recurring corruption — even the
            # non-authoritative `reset_created` marker from the prior
            # round could still be resubmitted and misread as a deliberate
            # edit by a later save; the real fix is to never zero Opening
            # Stock at all for a status-only reset in the first place. See
            # stock_service.upsert_daily_figure()'s explicit-edit-intent
            # gating for the complementary, second half of this fix (a
            # stale/routine resave can no longer promote ANY row —
            # including one MODE_FULL below still legitimately clears —
            # into manual_correction).
            #
            # MODE_FULL ("Full Reset") still clears Opening Stock — that
            # is its whole point, starting the period over — but never to
            # an authoritative zero: it is RECALCULATED from the real
            # previous chronological Closing Stock (0 only if genuinely
            # nothing precedes it), stored as the non-authoritative
            # `reset_created` marker (never anchor-eligible, always
            # live-revalidated, exactly like `derived` — see
            # OPENING_STOCK_SOURCES_UNCONDITIONAL_ANCHOR). The live API
            # response already reflects this correctly-derived value
            # immediately, and a later routine save can never promote it
            # into an authoritative correction (see upsert_daily_figure()).
            if mode == MODE_FULL:
                # Final stock architecture, ledger boundary rule — a Full
                # Reset must never alter an activated cutover's own anchor
                # row. Any OTHER (later) period's recalculation already
                # respects the boundary automatically (get_prior_closing_
                # base_qty() below stops at the nearest unconditionally-
                # trusted anchor, which is the cutover row itself for any
                # period at or after it) — this guard only needs to catch
                # the one case that isn't automatic: resetting the exact
                # cutover period itself.
                if figure.opening_stock_source == OPENING_STOCK_SOURCE_LEDGER_CUTOVER:
                    raise DailyResetError(
                        f"'{product.name}' on {date} {shift} is the activated ledger cutover's own anchor — "
                        "a Full Reset cannot alter it. Cancel/supersede the cutover explicitly instead."
                    )
                from webapp.services import stock_service as svc
                prior_closing = svc.get_prior_closing_base_qty(product.id, date, shift, exclude_id=figure.id)
                recalculated_opening = prior_closing if prior_closing is not None else 0
                rule = product.current_packaging_rule()
                try:
                    split = svc.from_base_units(recalculated_opening, rule) if rule else (0, 0, 0)
                except svc.PackagingError:
                    # Same negative-stock policy as upsert_daily_figure()'s
                    # own DERIVED branch — the stored display columns
                    # can't split a negative count directly; opening_base_qty
                    # below still carries the true (possibly negative) value.
                    split = (0, 0, 0)
                figure.opening_cartons, figure.opening_packs, figure.opening_pieces = split
                figure.opening_base_qty = recalculated_opening
                figure.opening_stock_source = OPENING_STOCK_SOURCE_RESET_CREATED
                figure.opening_stock_is_override = False
                figure.notes = None
                figure.updated_by = actor.id
            # MODE_FIGURES_ONLY: no DailyFigure column is written at all —
            # not Opening Stock, not notes, nothing.

        daily_entry_status_service.clear_for_reset(date, shift, product.id, actor, reason)

        if mode == MODE_FULL:
            product_summary = _neutralize_source_for_product(date, shift, product.id, actor, reason)
            source_summary["dispatch"].extend(product_summary["dispatch"])
            source_summary["returns"].extend(product_summary["returns"])
            source_summary["production"].extend(product_summary["production"])

        affected.append({"product_id": product.id, "product_name": product.name, "before": before})

    db.session.flush()
    record_audit(
        actor, "reset_daily_values", "daily_figure_reset",
        entity_id=f"{date}|{shift}|{product_id if product_id is not None else 'all'}",
        before={"products": [a["product_id"] for a in affected], "mode": mode},
        after={
            "reason": reason, "product_count": len(affected), "mode": mode,
            "actor_role": actor.role, "source_records_affected": source_summary,
            "review_reset": review_reset,
        },
    )
    return {
        "date": date, "shift": shift, "mode": mode,
        "products": [a["product_name"] for a in affected],
        "source_records_affected": source_summary,
        "review_reset": review_reset,
    }
