"""
Urgent stock-integrity investigation — a read-only diagnostic ledger.

This module never writes anything. It exists to answer, for one product
across a chronological Date+Shift range, exactly what stock_service.py's
carry-forward math (get_prior_closing_base_qty()/daily_figure_view(),
completely UNCHANGED by this module) actually did — which records
contributed, which were excluded and why, what the Opening Stock anchor
was and whether it was trusted, and the exact arithmetic that produced
each period's Closing Stock. It is the tool a Manager/Super Administrator
uses to trace WHERE a surprising balance (including a negative one)
actually originated, rather than guessing.

Investigation finding (see the "final stock-integrity investigation"
completion report): seven adversarial reproductions — a historical
correction after a later manual-correction anchor, a voided dispatch, a
reopened-but-never-refinalized production record, cross-product
isolation, genuine over-issuance, a Reset Daily Values Mode A demotion of
an anchor, and Night-before-Day chronology — all reconciled EXACTLY
against hand-computed expected values. No duplicate-counting, status-
filtering, boundary, provenance, adjustment, unit-conversion, or cross-
product defect was found in stock_service.py. A negative Closing Stock
this app can currently produce is always a genuine, exactly-reconciling
consequence of real source records (typically Issued exceeding Produced+
Returned+anchor, i.e. real over-issuance or a real data-entry mistake in
a source record) — never a software double-count. The actual gap this
module fills: Reset Daily Values' preview is correctly scoped to only the
exact target Date+Shift, so it can look like "nothing here" for a period
whose negative balance was genuinely CARRIED FORWARD from an earlier
period's real movement — this ledger makes that distinction explicit
(see PERIOD_KIND_* below) instead of leaving a Manager to guess.
"""
from datetime import datetime, timedelta

from webapp.extensions import db
from webapp.models.daily_figure import DailyFigure, StockAdjustment
from webapp.models.dispatch import SHIFT_DAY, SHIFT_NIGHT, SHIFTS, STATUS_FINALIZED as DISPATCH_FINALIZED, Dispatch, DispatchLine
from webapp.models.product import Product
from webapp.models.production_record import STATUS_FINALIZED as PRODUCTION_FINALIZED, ProductionLine, ProductionRecord
from webapp.models.return_record import STATUS_FINALIZED as RETURNS_FINALIZED, ReturnLine, ReturnRecord
from webapp.services import daily_entry_status_service, daily_review_service, stock_service as svc
from webapp.services.quantity_format import qty_label


class LedgerError(ValueError):
    """User-facing validation problem — never raised for a data problem,
    only for a bad request (unknown product, bad date range, bad shift)."""


# What KIND of period this is, from the ledger's own point of view — the
# explicit distinction section 11 of the investigation asked for, so a
# Manager never has to guess whether a negative balance came from this
# exact period's own movement or was carried forward from an earlier one.
PERIOD_KIND_NO_MOVEMENT = "no_movement"                    # nothing at all this period; balance == prior balance
PERIOD_KIND_MOVEMENT_HERE = "movement_here"                 # this period's own records changed the balance
PERIOD_KIND_NEGATIVE_CARRIED = "negative_carried_forward"   # balance is negative but nothing moved this period — it's inherited


def _shift_date(date_str, n):
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def _iter_dates(date_from, date_to):
    d = date_from
    while d <= date_to:
        yield d
        d = _shift_date(d, 1)


def _record_status_label(record, reopen_marker_field=None):
    """Only three statuses exist in this schema (draft/finalized/void) —
    "reopened" is not a separate stored state, it's a finalized record
    that went back to draft with a note appended to its own remarks/notes
    field (see production_service.reopen_production() etc.) — detected
    heuristically here for display only, never used to decide inclusion
    (status alone already does that correctly)."""
    if record.status == "void":
        return "Voided"
    if record.status == "finalized":
        return "Finalized"
    marker = getattr(record, reopen_marker_field, None) if reopen_marker_field else None
    if marker and "Reopened:" in marker:
        return "Draft (reopened, not yet refinalized)"
    return "Draft"


def _production_lines(product_id, date, shift):
    rows = (
        db.session.query(ProductionLine, ProductionRecord)
        .join(ProductionRecord, ProductionRecord.id == ProductionLine.production_id)
        .filter(ProductionRecord.date == date, ProductionRecord.shift == shift, ProductionLine.product_id == product_id)
        .all()
    )
    lines = []
    total = 0
    for line, record in rows:
        included = record.status == PRODUCTION_FINALIZED
        if included:
            total += line.base_unit_qty
        lines.append({
            "record_id": record.id, "line_id": line.id, "base_unit_qty": line.base_unit_qty,
            "status": _record_status_label(record, "remarks"),
            "included": included,
            "reason": "finalized - counted" if included else f"{_record_status_label(record, 'remarks')} - excluded, never affects stock",
        })
    return total, lines


def _dispatch_lines(product_id, date, shift):
    if shift != SHIFT_DAY:
        return 0, [], "Dispatch is a Day-only workflow - no Dispatch record can be dated this shift."
    rows = (
        db.session.query(DispatchLine, Dispatch)
        .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
        .filter(Dispatch.date == date, Dispatch.shift == SHIFT_DAY, DispatchLine.product_id == product_id)
        .all()
    )
    lines = []
    total = 0
    for line, record in rows:
        included = record.status == DISPATCH_FINALIZED
        if included:
            total += line.base_unit_qty
        lines.append({
            "record_id": record.id, "line_id": line.id, "base_unit_qty": line.base_unit_qty,
            "status": _record_status_label(record, "notes"),
            "included": included,
            "reason": "finalized - counted" if included else f"{_record_status_label(record, 'notes')} - excluded, never affects stock",
        })
    return total, lines, None


def _returns_lines(product_id, date, shift):
    if shift != SHIFT_DAY:
        return 0, [], "Returns has no shift column - a finalized Return only ever contributes to this date's Day period."
    rows = (
        db.session.query(ReturnLine, ReturnRecord)
        .join(ReturnRecord, ReturnRecord.id == ReturnLine.return_id)
        .filter(ReturnRecord.date == date, ReturnLine.product_id == product_id)
        .all()
    )
    lines = []
    total = 0
    for line, record in rows:
        included = record.status == RETURNS_FINALIZED
        if included:
            total += line.base_unit_qty
        lines.append({
            "record_id": record.id, "line_id": line.id, "base_unit_qty": line.base_unit_qty,
            "status": _record_status_label(record, "remarks"),
            "included": included,
            "reason": "finalized - counted" if included else f"{_record_status_label(record, 'remarks')} - excluded, never affects stock",
        })
    return total, lines, None


def _adjustment_entries(product_id, date, shift):
    rows = StockAdjustment.query.filter_by(product_id=product_id, date=date, shift=shift).all()
    total = sum(a.delta_base_qty for a in rows)
    entries = [{"id": a.id, "delta_base_qty": a.delta_base_qty, "reason": a.reason} for a in rows]
    return total, entries


def _anchor_trust_reason(figure, is_trusted):
    if figure is None:
        return "no anchor-eligible row"
    if figure.opening_stock_source not in ("initial_manual", "manual_correction", "legacy_inferred"):
        return f"opening_stock_source={figure.opening_stock_source!r} is not anchor-eligible"
    if figure.opening_stock_source == "manual_correction":
        return "manual_correction - trusted unconditionally forever, regardless of any earlier history entered since"
    if is_trusted:
        return f"{figure.opening_stock_source} - trusted: no finalized activity exists before it"
    return f"{figure.opening_stock_source} - NOT trusted: finalized activity now exists before it, so it is treated as if it did not exist"


def _period_entry(product, date, shift, rule):
    view = svc.daily_figure_view(product, date, shift)

    figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()
    figure_is_trusted = figure is not None and svc._is_trusted_anchor(figure)
    prior_anchor = svc._find_anchor_figure(product.id, date, shift, exclude_id=figure.id if figure else None)

    if figure_is_trusted:
        anchor_row, anchor_trusted, anchor_reason = figure, True, _anchor_trust_reason(figure, True)
    elif prior_anchor is not None:
        anchor_row, anchor_trusted, anchor_reason = prior_anchor, True, _anchor_trust_reason(prior_anchor, True)
    else:
        anchor_row, anchor_trusted, anchor_reason = None, False, "no anchor-eligible row exists before this period"

    production_total, production_lines = _production_lines(product.id, date, shift)
    returns_total, returns_lines, returns_note = _returns_lines(product.id, date, shift)
    dispatch_total, dispatch_lines, dispatch_note = _dispatch_lines(product.id, date, shift)
    adjustment_total, adjustments = _adjustment_entries(product.id, date, shift)
    # Matches stock_service.issued_base_qty() exactly: adjustments are
    # signed and folded directly into Issued (a positive adjustment adds
    # to Issued, same direction as a Dispatch; a negative adjustment
    # reduces it) — never a separate +/- term in the Closing formula.
    issued_total = dispatch_total + adjustment_total

    opening_base = view["opening"]["base_qty"] if view["opening"] else None
    closing_base = view["closing"]["base_qty"] if view["closing"] else None

    movement_here = bool(production_total or returns_total or dispatch_total or adjustment_total)
    if not movement_here:
        kind = PERIOD_KIND_NEGATIVE_CARRIED if (closing_base is not None and closing_base < 0) else PERIOD_KIND_NO_MOVEMENT
    else:
        kind = PERIOD_KIND_MOVEMENT_HERE

    entry_status = daily_entry_status_service.status_view(date, shift, product.id)
    review_session = daily_review_service.get_session(date, shift)
    review_state = None
    if review_session is not None:
        from webapp.models.daily_review_session import DailyReviewProductState
        row = DailyReviewProductState.query.filter_by(
            review_session_id=review_session.id, product_id=product.id
        ).first()
        review_state = {"session_status": review_session.status, "product_state": row.state if row else "not_reviewed"}

    warnings = []
    if closing_base is not None and closing_base < 0:
        warnings.append(f"Closing Stock is negative ({closing_base} base units).")
    if rule is None:
        warnings.append("No packaging rule configured for this product - quantities cannot be labeled.")
    if kind == PERIOD_KIND_NEGATIVE_CARRIED:
        warnings.append("Negative balance carried forward from an earlier period - no movement occurred in this exact period.")

    return {
        "date": date, "shift": shift,
        "product_id": product.id, "product_name": product.name,
        "packaging_rule_id": rule.id if rule else None,
        "opening_base_qty": opening_base,
        "opening_label": _label(opening_base, rule),
        "opening_source": figure.opening_stock_source if figure is not None else None,
        "anchor_row_id": anchor_row.id if anchor_row is not None else None,
        "anchor_trusted": anchor_trusted,
        "anchor_trust_reason": anchor_reason,
        "production_total": production_total, "production_lines": production_lines,
        "returns_total": returns_total, "returns_lines": returns_lines, "returns_note": returns_note,
        "dispatch_total": dispatch_total, "dispatch_lines": dispatch_lines, "dispatch_note": dispatch_note,
        "adjustment_total": adjustment_total, "adjustments": adjustments,
        "issued_total": issued_total,
        "closing_base_qty": closing_base,
        "closing_label": _label(closing_base, rule),
        "formula": "closing = opening + production + returns - issued_total, where issued_total = dispatch_total + adjustment_total "
                   "(adjustments are signed and folded directly into Issued - matches stock_service.issued_base_qty() exactly)",
        "entry_status": entry_status,
        "review_state": review_state,
        "period_kind": kind,
        "warnings": warnings,
    }


def _label(base_qty, rule):
    """Packaging-aware book-notation label for a non-negative quantity —
    for a genuinely negative balance, this deliberately does NOT run the
    raw base_qty through the cartons formatter (qty_label() has no
    negative-cartons notion, and doing so would silently mislabel a raw
    negative PIECE count as though it were a cartons figure — exactly the
    "raw piece value displayed as cartons" defect class this
    investigation was asked to check for). A negative balance is reported
    as an explicit, honest base-unit count instead."""
    if base_qty is None:
        return None
    if base_qty < 0:
        return f"{base_qty} base units (negative - not expressible in carton notation)"
    if rule is None:
        return f"{base_qty} base units (no packaging rule configured)"
    cartons, packs, pieces = svc.from_base_units(base_qty, rule)
    return qty_label(cartons, packs, pieces, rule)


def build_ledger(product_id, date_from, date_to, shift=None):
    """The read-only diagnostic ledger — one entry per chronological
    Date+Shift period (Day before Night on the same date, this date's
    Night before the next date's Day — the exact same SHIFT_ORDER
    stock_service.py itself uses, never a second ordering rule). Never
    writes anything; every number comes straight from
    stock_service.daily_figure_view() (the same function every other
    screen already calls), so this can never itself disagree with what
    the application shows elsewhere."""
    product = db.session.get(Product, product_id)
    if product is None:
        raise LedgerError("product does not exist")
    if date_from > date_to:
        raise LedgerError("date_from must not be after date_to")
    if shift is not None and shift not in SHIFTS:
        raise LedgerError(f"shift must be one of {SHIFTS}")

    rule = product.current_packaging_rule()
    shifts = [shift] if shift else [SHIFT_DAY, SHIFT_NIGHT]

    entries = []
    for date in _iter_dates(date_from, date_to):
        for s in shifts:
            entries.append(_period_entry(product, date, s, rule))
    return entries


def first_negative_period(entries):
    """The first chronological entry whose Closing Stock is negative, or
    None — the direct answer to "what was the first exact Date+Shift
    where the stock changed from the expected value to an incorrect
    negative value" (investigation section 2)."""
    for entry in entries:
        if entry["closing_base_qty"] is not None and entry["closing_base_qty"] < 0:
            return entry
    return None
