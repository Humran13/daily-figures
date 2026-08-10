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

Investigation finding, round 1 (see the "final stock-integrity
investigation" completion report): seven adversarial reproductions — a
historical correction after a later manual-correction anchor, a voided
dispatch, a reopened-but-never-refinalized production record, cross-
product isolation, genuine over-issuance, a Reset Daily Values Mode A
demotion of an anchor, and Night-before-Day chronology — all reconciled
EXACTLY against hand-computed expected values. No duplicate-counting,
status-filtering, boundary, provenance, adjustment, or cross-product
defect was found. The specific StockAdjustment arithmetic disagreement
reported afterward (Closing Stock disagreeing with the next period's
Opening Stock) did NOT reproduce when the exact reported record IDs,
deltas, and dates were recreated — every reconciliation check passed.

Investigation finding, round 2 (legacy-adjustment follow-up): while
proving round 1's non-reproduction, a genuinely different, real defect
was found and fixed in stock_service.py's _movement_between_periods() —
a pre-Stage-5 migrated DailyFigure row's own legacy-stored
production_base_qty/return_base_qty columns were correctly reflected
when that exact row's own Closing Stock was viewed directly, but were
NEVER carried into any LATER period's Opening Stock (silently lost the
moment a newer period was computed), because the range-based carry-
forward sum only ever looked at the Production/Returns Book tables, never
at this older, pre-Book column on an intermediate row. Fixed via
_legacy_stored_movement_in_window() in stock_service.py. This module's
own reconciliation check below (added the same round) would have caught
this immediately had it existed sooner — every period this diagnostic
now produces is verified, not merely displayed, and now also surfaces
legacy-stored Production/Returns explicitly as their own line items so
they are traceable, not hidden inside an aggregate total.

Reset Daily Values' preview is correctly scoped to only the exact target
Date+Shift, so it can look like "nothing here" for a period whose
negative balance was genuinely CARRIED FORWARD from an earlier period's
real movement — this ledger makes that distinction explicit (see
PERIOD_KIND_* below) instead of leaving a Manager to guess.

Investigation finding, round 3 (legacy Opening-Stock migration repair):
a real, previously-undiscovered defect in webapp/services/legacy_migration.py
— the original run_legacy_migration() wrote opening_stock_source=
initial_manual, which is *live-revalidated* on every read
(_is_trusted_anchor()) rather than trusted unconditionally. For any
product with more than one migrated legacy row, every row after the
chronologically-first one gets silently demoted the moment an earlier
legacy row's own migrated Issued StockAdjustment counts as "finalized
activity before it" — discarding that row's own authoritative,
already-reconciled historical Opening Stock with no warning. Fixed via a
new provenance, legacy_migrated_opening (webapp/models/daily_figure.py),
trusted unconditionally forever exactly like manual_correction. See
legacy_migration.py's audit_opening_migration()/preview_opening_repair()/
apply_opening_repair() (read-only audit, and a token-gated, idempotent,
provenance-only repair — never a quantity change) and the `flask
audit-legacy-opening-migration` / `repair-legacy-opening-migration` CLI
commands. This round also fixed a separate, unrelated defect: a negative
Closing Stock was being displayed as literal warning text ("negative —
check entries") instead of proper signed book notation (e.g. "-6.00
Ctns") in three places (stock_service.py, quantity_format.js, index.html)
— see qty_label_signed() below.
"""
import re
from datetime import datetime, timedelta

from webapp.extensions import db
from webapp.models.daily_figure import DailyFigure, StockAdjustment
from webapp.models.dispatch import SHIFT_DAY, SHIFT_NIGHT, SHIFTS, STATUS_FINALIZED as DISPATCH_FINALIZED, Dispatch, DispatchLine
from webapp.models.product import Product
from webapp.models.production_record import STATUS_FINALIZED as PRODUCTION_FINALIZED, ProductionLine, ProductionRecord
from webapp.models.return_record import STATUS_FINALIZED as RETURNS_FINALIZED, ReturnLine, ReturnRecord
from webapp.services import daily_entry_status_service, daily_review_service, returns_service, stock_service as svc


class LedgerError(ValueError):
    """User-facing validation problem — never raised for a data problem,
    only for a bad request (unknown product, bad date range, bad shift)."""


class LedgerReconciliationError(ValueError):
    """Raised instead of returning misleading output (final stock-
    integrity investigation, section 7) — if this ledger's own
    independently-queried component totals (production/returns/dispatch/
    adjustments, including legacy-stored columns) ever fail to reconcile
    exactly against stock_service.compute_closing()'s result for the same
    inputs, something about this diagnostic itself (never
    stock_service.py's own live calculation, which this never touches) is
    missing a contributing source — surfaced loudly, never silently."""


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
        # Same central rule stock_service.py's aggregate queries use (see
        # returns_service.is_return_stock_posting_eligible()) — a
        # Metro Sales return finalized on a non-Monday date is excluded
        # here too, so this diagnostic never disagrees with the actual
        # Daily Figures/Closing Stock it's supposed to reconcile against.
        included = returns_service.is_return_stock_posting_eligible(record)
        if included:
            total += line.base_unit_qty
        if record.status != RETURNS_FINALIZED:
            reason = f"{_record_status_label(record, 'remarks')} - excluded, never affects stock"
        elif not included:
            reason = "finalized Metro Sales return, non-Monday - excluded, posts to stock Monday only"
        else:
            reason = "finalized - counted"
        lines.append({
            "record_id": record.id, "line_id": line.id, "base_unit_qty": line.base_unit_qty,
            "status": _record_status_label(record, "remarks"),
            "included": included,
            "reason": reason,
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

    production_lines_total, production_lines = _production_lines(product.id, date, shift)
    returns_lines_total, returns_lines, returns_note = _returns_lines(product.id, date, shift)
    dispatch_total, dispatch_lines, dispatch_note = _dispatch_lines(product.id, date, shift)
    adjustment_total, adjustments = _adjustment_entries(product.id, date, shift)
    # Round 2 finding — a pre-Stage-5 migrated row can carry its own
    # legacy-stored Production/Returns value directly on the DailyFigure
    # row (see stock_service.closing_base_qty()'s `legacy_stored`
    # parameter) — surfaced as its own explicit, traceable component,
    # never silently folded into "production_lines"/"returns_lines" (which
    # only ever reflect Book table rows) and never lost.
    legacy_production = figure.production_base_qty if figure is not None else 0
    legacy_returns = figure.return_base_qty if figure is not None else 0
    production_total = production_lines_total + legacy_production
    returns_total = returns_lines_total + legacy_returns
    # Matches stock_service.issued_base_qty() exactly: adjustments are
    # signed and folded directly into Issued (a positive adjustment adds
    # to Issued, same direction as a Dispatch; a negative adjustment
    # reduces it) — never a separate +/- term in the Closing formula.
    issued_total = dispatch_total + adjustment_total

    opening_base = view["opening"]["base_qty"] if view["opening"] else None
    closing_base = view["closing"]["base_qty"] if view["closing"] else None

    # Final stock-integrity investigation, section 7 — never print
    # components that fail to reconcile with the displayed Closing Stock;
    # raise loudly instead of producing misleading output. Uses the exact
    # same compute_closing() every other screen uses, never a third,
    # independently-reinvented formula.
    formula_reconciles = None
    if opening_base is not None and closing_base is not None:
        expected_closing = svc.compute_closing(opening_base, production_total, returns_total, issued_total)
        if expected_closing != closing_base:
            raise LedgerReconciliationError(
                f"{product.name} {date} {shift}: components do not reconcile — "
                f"opening({opening_base}) + production({production_total}) + returns({returns_total}) "
                f"- issued({issued_total}) = {expected_closing}, but daily_figure_view() reports "
                f"closing={closing_base}. This diagnostic is missing a contributing source; it never "
                f"guesses or silently displays a mismatched total."
            )
        formula_reconciles = True  # LedgerReconciliationError above would have already raised otherwise

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
    if legacy_production or legacy_returns:
        warnings.append(
            f"This row carries legacy-stored Production/Returns (production={legacy_production}, returns={legacy_returns}) "
            "from before the Production/Returns Book existed, not a Book line."
        )

    return {
        "date": date, "shift": shift,
        "product_id": product.id, "product_name": product.name,
        "packaging_rule_id": rule.id if rule else None,
        "opening_base_qty": opening_base,
        "opening_label": svc.qty_label_signed(opening_base, rule),
        "opening_source": figure.opening_stock_source if figure is not None else None,
        # Final stock architecture — traces this exact period back to the
        # LedgerCutover that anchored it, when applicable. None for any
        # period not itself a cutover's own anchor row (a LATER period
        # that merely carries forward FROM a cutover has its own
        # anchor_row_id pointing at the cutover row instead — see below).
        "cutover_id": figure.cutover_id if figure is not None else None,
        "formula_reconciles": formula_reconciles,
        "anchor_row_id": anchor_row.id if anchor_row is not None else None,
        "anchor_trusted": anchor_trusted,
        "anchor_trust_reason": anchor_reason,
        "production_total": production_total, "production_lines": production_lines,
        "legacy_production": legacy_production,
        "returns_total": returns_total, "returns_lines": returns_lines, "returns_note": returns_note,
        "legacy_returns": legacy_returns,
        "dispatch_total": dispatch_total, "dispatch_lines": dispatch_lines, "dispatch_note": dispatch_note,
        "adjustment_total": adjustment_total, "adjustments": adjustments,
        "issued_total": issued_total,
        "closing_base_qty": closing_base,
        "closing_label": svc.qty_label_signed(closing_base, rule),
        "formula": "closing = opening + production + returns - issued_total, where issued_total = dispatch_total + adjustment_total "
                   "(adjustments are signed and folded directly into Issued - matches stock_service.issued_base_qty() exactly)",
        "entry_status": entry_status,
        "review_state": review_state,
        "period_kind": kind,
        "warnings": warnings,
    }


def calculate_period(product_id, date, shift):
    """
    Final stock architecture, section 4 — the one authoritative,
    chronological "what happened in this exact period" function every
    stock surface may call: Daily Figures, Dashboard, Dashboard Attention,
    History, Exports, Reset preview, the stock-ledger CLI, Reports, and
    low-stock warnings. Never reconstructs the formula independently —
    this is a thin, public wrapper around the same `_period_entry()` this
    module's own read-only diagnostic ledger (`build_ledger()`) has always
    used, itself built entirely from stock_service.py's shared functions.
    Raises LedgerError for an unknown product/shift, LedgerReconciliationError
    if the components fail to reconcile against the authoritative Closing.
    """
    product = db.session.get(Product, product_id)
    if product is None:
        raise LedgerError("product does not exist")
    if shift not in SHIFTS:
        raise LedgerError(f"shift must be one of {SHIFTS}")
    rule = product.current_packaging_rule()
    return _period_entry(product, date, shift, rule)


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


# Legacy-adjustment follow-up investigation — the reason string written by
# the migration that back-filled historical Issued figures as
# StockAdjustment rows (there being no "legacy issued" column on
# DailyFigure the way there is for Production/Returns — see
# stock_service._legacy_stored_movement_in_window()'s docstring for why
# THAT gap existed instead). Matched here for audit purposes only — never
# used to decide whether a row affects stock (every StockAdjustment
# always does, regardless of its reason text).
LEGACY_ADJUSTMENT_REASON_PATTERN = re.compile(r"Migrated legacy issued figure \(entries\.id=(\d+)\)")


def _legacy_entries_table_rows():
    """Best-effort read of the legacy `entries` table (webapp/legacy_entries.py
    — untouched raw-sqlite3, still queryable) so the audit can show the
    ORIGINAL pre-migration value next to what was migrated. Never raises —
    a missing/inaccessible legacy table just means this cross-reference is
    unavailable, not a reason to fail the whole audit."""
    try:
        from webapp.legacy_entries import get_db
        conn = get_db()
        rows = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM entries").fetchall()}
        conn.close()
        return rows
    except Exception:
        return {}


def _possible_duplicate_lines(product_id, date, shift, base_unit_qty):
    """Heuristic only (final stock-integrity investigation, section 5) —
    any Dispatch/Production/Returns line for this exact product/date/shift
    whose own base_unit_qty magnitude matches this adjustment's, regardless
    of that line's status. A match is a candidate worth a human's review,
    never proof by itself that the adjustment is redundant — a migrated
    legacy Issued correction and a genuinely separate, coincidentally
    equal-sized Dispatch line are indistinguishable from quantity alone."""
    target = abs(base_unit_qty)
    candidates = []
    for line, record in (
        db.session.query(ProductionLine, ProductionRecord)
        .join(ProductionRecord, ProductionRecord.id == ProductionLine.production_id)
        .filter(ProductionRecord.date == date, ProductionRecord.shift == shift, ProductionLine.product_id == product_id)
        .all()
    ):
        if line.base_unit_qty == target:
            candidates.append({"source_type": "production", "record_id": record.id, "line_id": line.id, "status": record.status})
    if shift == SHIFT_DAY:
        for line, record in (
            db.session.query(DispatchLine, Dispatch)
            .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
            .filter(Dispatch.date == date, Dispatch.shift == SHIFT_DAY, DispatchLine.product_id == product_id)
            .all()
        ):
            if line.base_unit_qty == target:
                candidates.append({"source_type": "dispatch", "record_id": record.id, "line_id": line.id, "status": record.status})
        for line, record in (
            db.session.query(ReturnLine, ReturnRecord)
            .join(ReturnRecord, ReturnRecord.id == ReturnLine.return_id)
            .filter(ReturnRecord.date == date, ReturnLine.product_id == product_id)
            .all()
        ):
            if line.base_unit_qty == target:
                candidates.append({"source_type": "returns", "record_id": record.id, "line_id": line.id, "status": record.status})
    return candidates


def audit_legacy_adjustments():
    """
    Final stock-integrity investigation, section 5/9 — a READ-ONLY,
    complete inventory of every StockAdjustment whose reason matches the
    legacy-migration pattern, across EVERY product (never a hard-coded
    handful of IDs). For each row: the original legacy entries.id and its
    stored value (cross-referenced from the untouched legacy `entries`
    table where still available), whether a Dispatch/Production/Returns
    line already exists that might represent the same movement (a
    candidate for human review, never an automatic conclusion), the
    current Closing Stock for that exact product/date/shift, and whether
    the balance would stay non-negative if this one adjustment were
    hypothetically removed. Never modifies anything — see
    `flask audit-legacy-adjustments` in webapp/cli.py for the CLI form,
    and the completion report for why no repair/apply command accompanies
    this: no evidence was found that any specific row is invalid,
    duplicated, or wrongly signed.
    """
    adjustments = StockAdjustment.query.order_by(StockAdjustment.date, StockAdjustment.shift, StockAdjustment.id).all()
    legacy_rows = [a for a in adjustments if a.reason and LEGACY_ADJUSTMENT_REASON_PATTERN.search(a.reason)]
    if not legacy_rows:
        return []

    product_ids = {a.product_id for a in legacy_rows}
    products_by_id = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()}
    legacy_entries = _legacy_entries_table_rows()

    report = []
    for a in legacy_rows:
        product = products_by_id.get(a.product_id)
        rule = product.current_packaging_rule() if product else None
        match = LEGACY_ADJUSTMENT_REASON_PATTERN.search(a.reason)
        legacy_entry_id = int(match.group(1)) if match else None
        legacy_entry_row = legacy_entries.get(legacy_entry_id) if legacy_entry_id is not None else None

        duplicates = _possible_duplicate_lines(a.product_id, a.date, a.shift, a.delta_base_qty)

        closing = None
        would_be_non_negative_without_this_row = None
        if product is not None and rule is not None:
            view = svc.daily_figure_view(product, a.date, a.shift)
            closing = view["closing"]["base_qty"] if view["closing"] else None
            if closing is not None:
                would_be_non_negative_without_this_row = (closing + a.delta_base_qty) >= 0

        report.append({
            "adjustment_id": a.id,
            "product_id": a.product_id, "product_name": product.name if product else None,
            "date": a.date, "shift": a.shift, "delta_base_qty": a.delta_base_qty, "reason": a.reason,
            "legacy_entries_id": legacy_entry_id,
            "legacy_entry_original_value": legacy_entry_row,
            "packaging_rule_id": rule.id if rule else None,
            "possible_duplicate_lines": duplicates,
            "current_closing_base_qty": closing,
            "creates_or_worsens_negative_balance": bool(closing is not None and closing < 0),
            "would_be_non_negative_without_this_row": would_be_non_negative_without_this_row,
        })
    return report
