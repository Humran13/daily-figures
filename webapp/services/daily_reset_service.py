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
"""
from webapp.extensions import db
from webapp.models.daily_figure import OPENING_STOCK_SOURCE_DERIVED, DailyFigure
from webapp.models.dispatch import SHIFT_DAY, SHIFTS, STATUS_VOID as DISPATCH_VOID, Dispatch, DispatchLine
from webapp.models.product import Product
from webapp.models.production_record import (
    STATUS_VOID as PRODUCTION_VOID, ProductionLine, ProductionRecord,
)
from webapp.models.return_record import STATUS_VOID as RETURNS_VOID, ReturnLine, ReturnRecord
from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN
from webapp.services import daily_entry_status_service, daily_review_service, dispatch_service, production_service, returns_service
from webapp.services.audit_service import record_audit
from webapp.services.quantity_format import qty_label

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
        has_source_activity = bool(dispatch_rows or returns_rows or production_rows)
        review_state = review_info["product_states"].get(product.id, "not_reviewed")

        rows.append({
            "product_id": product.id,
            "product_name": product.name,
            "has_manual_opening_entry": bool(has_manual_opening),
            "opening_qty": {
                "cartons": figure.opening_cartons, "packs": figure.opening_packs, "pieces": figure.opening_pieces,
            } if figure is not None else None,
            "is_opening_override": bool(figure and figure.opening_stock_is_override),
            "opening_stock_source": figure.opening_stock_source if figure is not None else None,
            "has_notes": bool(figure and figure.notes),
            "review_status": status["status"],
            "finalized_dispatch": dispatch_rows,
            "finalized_returns": returns_rows,
            "finalized_production": production_rows,
            "has_source_activity": has_source_activity,
            # Mode A never touches source books — always preserved there.
            # Mode B neutralizes exactly what's listed above, nothing else.
            "preserved_in_mode_a": has_source_activity,
            "neutralized_in_mode_b": has_source_activity,
            # Daily Figures review workflow state this reset would clear —
            # "not_reviewed" means the reset has nothing to clear for this
            # product, regardless of what the review session's own status is.
            "review_state": review_state,
        })

    any_affected = any(
        r["has_manual_opening_entry"] or r["has_notes"] or r["review_status"] != "not_started"
        or r["has_source_activity"] or r["review_state"] != "not_reviewed"
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
            figure.opening_cartons = 0
            figure.opening_packs = 0
            figure.opening_pieces = 0
            figure.opening_base_qty = 0
            # The repair mechanism for a stuck/stale row (Stage 8
            # correction): clearing the anchor classification, not just
            # the quantity, means carry-forward stops trusting this row at
            # all and goes back to deriving this period live from whatever
            # real anchor precedes it. This is the one place besides an
            # explicit correction write that may retire a manual_correction
            # too — a Manager/Super Administrator reset is itself a
            # deliberate, audited override of whatever was there before.
            figure.opening_stock_source = OPENING_STOCK_SOURCE_DERIVED
            figure.opening_stock_is_override = False
            figure.notes = None
            figure.updated_by = actor.id

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
