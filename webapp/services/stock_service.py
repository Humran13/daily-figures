"""
Daily Figures stock math. The backend is the sole source of truth here:
Issued is always computed live from finalized dispatch lines (+ any manual
adjustments) — never cached, never hand-typed — and Closing is always
Opening + Return + Production - Issued, computed in exact integer base
units. Nothing here uses floating point.

Stage 8 — chronological carry-forward: stock is a running balance, not a
per-date island. A DailyFigure row only ever exists at a product's
first-ever period or at an explicit Manager/Super Admin correction (an
"anchor") — every period after that is derived, and previously that
derivation only looked at the anchor's own single-day closing, silently
ignoring any finalized Production/Returns/Dispatch activity recorded on
dates between the anchor and the date being viewed (see
get_prior_closing_base_qty()'s docstring below for the exact fix). This is
still fully computed live from source records on every read — no balance
is ever stored or cached, so a later correction to historical activity
ripples forward automatically the next time a later date is viewed.
"""
from datetime import datetime, timedelta

from webapp.extensions import db
from webapp.models.daily_figure import (
    OPENING_STOCK_SOURCE_DERIVED,
    OPENING_STOCK_SOURCE_INITIAL_MANUAL,
    OPENING_STOCK_SOURCE_MANUAL_CORRECTION,
    OPENING_STOCK_SOURCES_ANCHOR_ELIGIBLE,
    OPENING_STOCK_SOURCES_UNCONDITIONAL_ANCHOR,
    DailyFigure,
    StockAdjustment,
)
from webapp.models.dispatch import SHIFT_DAY, SHIFT_NIGHT, STATUS_FINALIZED, Dispatch, DispatchLine
from webapp.models.production_record import ProductionLine, ProductionRecord
from webapp.models.return_record import ReturnLine, ReturnRecord
from webapp.models.sales_category import SalesCategory
from webapp.services.customer_service import resolve_canonical, resolve_customer_ids_for_filter
from webapp.services.packaging import PackagingError, from_base_units, normalize, to_base_units
from webapp.services.quantity_format import qty_label

# The one centralized period-ordering rule (Stage 8 section 2) — Day always
# precedes Night on the same date; every date/shift comparison anywhere in
# this module (and nowhere else — never duplicated in a route or in
# frontend JS) goes through _sort_key().
SHIFT_ORDER = {"Day": 0, "Night": 1}

# Stage 8 hotfix (section 6) — used as the unbounded lower end of a
# "sum every finalized movement before this period" range when no Opening
# Stock anchor exists at all for a product. ISO date strings before this
# sort correctly with everything the app will ever produce.
_EPOCH_DATE = "0001-01-01"


def _user_label(user_id):
    """Mirrors daily_entry_status_service._user_label() — used by
    daily_figure_view() for the original-entry/corrector attribution
    fields (final pre-deployment correction, section 11)."""
    if user_id is None:
        return None
    from webapp.models.user import User
    user = db.session.get(User, user_id)
    return user.username if user else "a former user"


class StockError(ValueError):
    pass


def _sort_key(date, shift):
    return f"{date}-{SHIFT_ORDER.get(shift, 9)}"


def _shift_date(date_str, n):
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def _is_trusted_anchor(figure):
    """
    Whether THIS row's stored Opening Stock should be trusted right now —
    the single decision daily_figure_view() (for the exact row being
    viewed) and _find_anchor_figure() (scanning for an anchor before some
    later date) both defer to, so a row is never treated as authoritative
    in one context and stale in another for the same underlying reason.

    Stage 8 production hotfix (section 3/4): a plain "is this an override"
    boolean can't say WHY a row is trusted, which is exactly what let a
    real out-of-order data-entry case get stuck — a later period entered
    before its own history was recorded genuinely was this product's
    "first-ever period" at the moment it was saved, and correctly became
    an anchor (opening_stock_source=initial_manual) — but once finalized
    Production/Returns/Dispatch is later entered for an even earlier date,
    that row was never really the first period at all. Only a
    manual_correction (an elevated user's submission that provably
    differed from live derivation at that moment) is trusted
    unconditionally forever; every other anchor-eligible source
    (initial_manual, legacy_inferred) is re-checked against finalized
    history on every single read — never cached, never decided once and
    remembered — so a later-entered earlier movement demotes it
    automatically, with no reset, no reopening, and no new migration
    required.
    """
    if figure.opening_stock_source not in OPENING_STOCK_SOURCES_ANCHOR_ELIGIBLE:
        return False
    if figure.opening_stock_source in OPENING_STOCK_SOURCES_UNCONDITIONAL_ANCHOR:
        return True
    if figure.shift == SHIFT_NIGHT:
        end_date, end_shift = figure.date, SHIFT_DAY
    else:
        end_date, end_shift = _shift_date(figure.date, -1), SHIFT_NIGHT
    return not _any_finalized_activity_at_or_before(figure.product_id, end_date, end_shift)


def _find_anchor_figure(product_id, date, shift, exclude_id=None):
    """The most recent DailyFigure row strictly before (date, shift) for
    this product that is still a TRUSTED anchor right now (see
    _is_trusted_anchor()) — scanning backwards from just before the target
    so that a disqualified candidate (an initial_manual/legacy_inferred row
    that finalized history now predates) is skipped in favor of an older,
    still-valid one, rather than being mistaken for "no anchor exists."
    Every period between two real anchors — and every period after the
    latest one — has no row of its own by design (Stage 7's "do not create
    meaningless stock rows"), so this scan is bounded by the number of
    anchor-eligible rows ever created for this product, not by the number
    of days that have passed."""
    target_key = _sort_key(date, shift)
    candidates = DailyFigure.query.filter(
        DailyFigure.product_id == product_id,
        DailyFigure.opening_stock_source.in_(OPENING_STOCK_SOURCES_ANCHOR_ELIGIBLE),
    )
    if exclude_id is not None:
        candidates = candidates.filter(DailyFigure.id != exclude_id)

    eligible = sorted(
        (row for row in candidates.all() if _sort_key(row.date, row.shift) < target_key),
        key=lambda row: _sort_key(row.date, row.shift),
        reverse=True,
    )
    for row in eligible:
        if _is_trusted_anchor(row):
            return row
    return None


def _legacy_stored_movement_in_window(product_id, start_date, start_shift, end_date, end_shift):
    """
    Final stock-integrity investigation — a pre-Stage-5 migrated
    DailyFigure row can carry a Return/Production value directly on its
    own return_base_qty/production_base_qty columns (see
    closing_base_qty()'s `legacy_stored` parameter) instead of via the
    Returns/Production Book. Every row written by upsert_daily_figure()
    (Stage 5 onward) leaves these columns at their default of 0, so this
    can never double-count a Book-sourced entry — it only ever picks up
    genuinely legacy-only movement on an INTERMEDIATE row (never the
    anchor's own row, whose legacy_stored is already folded into
    anchor_closing by the caller; never the target's own row, which
    daily_figure_view() already handles directly — both are structurally
    outside the strictly-between window this function and its caller
    both use).

    Without this, a legacy row's stored value was visible only for as
    long as that exact row was being viewed directly — the moment any
    LATER period was computed, it silently vanished, because
    production_finalized_base_qty_range()/returns_finalized_base_qty_range()
    only ever look at the Production/Returns Book tables, never at this
    older, pre-Book column. This is the fix for that silent loss.
    """
    if _sort_key(start_date, start_shift) > _sort_key(end_date, end_shift):
        return 0, 0
    start_key = _sort_key(start_date, start_shift)
    end_key = _sort_key(end_date, end_shift)
    rows = DailyFigure.query.filter(
        DailyFigure.product_id == product_id,
        DailyFigure.date >= start_date, DailyFigure.date <= end_date,
    ).all()
    production = 0
    returns = 0
    for row in rows:
        if start_key <= _sort_key(row.date, row.shift) <= end_key:
            production += row.production_base_qty or 0
            returns += row.return_base_qty or 0
    return production, returns


def _movement_between_periods(product_id, start_date, start_shift, end_date, end_shift):
    """
    Sum of (Production, Returns, Issued) across every period from
    (start_date, start_shift) through (end_date, end_shift) INCLUSIVE — the
    caller always passes the period immediately after an anchor and the
    period immediately before a target, so "inclusive" here is exactly the
    open interval strictly between the anchor and the target. An empty
    range (the anchor and target are chronologically adjacent, e.g. a Day
    anchor carrying straight into that same date's Night, or a Night
    anchor carrying into the next date's Day) correctly contributes
    nothing. Uses the same finalized-only, range-query building blocks
    already used by date_range_summary() below — never a per-day Python
    loop, so this stays cheap regardless of how many days have elapsed
    since the anchor. Also includes any intermediate row's own legacy-
    stored Production/Returns column (see _legacy_stored_movement_in_window()
    above) — a second, independent query, never folded into the book-based
    piecewise logic below to keep that logic's existing, carefully-tuned
    boundary handling untouched.
    """
    if _sort_key(start_date, start_shift) > _sort_key(end_date, end_shift):
        return 0, 0, 0

    production = 0
    issued = 0
    returns = 0

    if start_date == end_date:
        shifts = (SHIFT_DAY, SHIFT_NIGHT) if (start_shift == SHIFT_DAY and end_shift == SHIFT_NIGHT) else (start_shift,)
        for s in shifts:
            production += production_finalized_base_qty(product_id, start_date, s)
            issued += dispatch_issued_base_qty(product_id, start_date, s) + adjustment_total_base_qty(product_id, start_date, s)
            if s == SHIFT_DAY:
                returns += returns_finalized_base_qty(product_id, start_date)
        legacy_production, legacy_returns = _legacy_stored_movement_in_window(product_id, start_date, start_shift, end_date, end_shift)
        return production + legacy_production, returns + legacy_returns, issued

    if start_shift == SHIFT_NIGHT:
        production += production_finalized_base_qty(product_id, start_date, SHIFT_NIGHT)
        issued += dispatch_issued_base_qty(product_id, start_date, SHIFT_NIGHT) + adjustment_total_base_qty(product_id, start_date, SHIFT_NIGHT)
        mid_from = _shift_date(start_date, 1)
    else:
        mid_from = start_date

    if end_shift == SHIFT_DAY:
        production += production_finalized_base_qty(product_id, end_date, SHIFT_DAY)
        issued += dispatch_issued_base_qty(product_id, end_date, SHIFT_DAY) + adjustment_total_base_qty(product_id, end_date, SHIFT_DAY)
        returns += returns_finalized_base_qty(product_id, end_date)
        mid_to = _shift_date(end_date, -1)
    else:
        mid_to = end_date

    if mid_from <= mid_to:
        production += production_finalized_base_qty_range(product_id, mid_from, mid_to)
        issued += dispatch_issued_base_qty_range(product_id, mid_from, mid_to) + adjustment_total_base_qty_range(product_id, mid_from, mid_to)
        returns += returns_finalized_base_qty_range(product_id, mid_from, mid_to)

    legacy_production, legacy_returns = _legacy_stored_movement_in_window(product_id, start_date, start_shift, end_date, end_shift)
    return production + legacy_production, returns + legacy_returns, issued


def _any_finalized_activity_at_or_before(product_id, date, shift):
    """
    True if any finalized Production, Returns, Dispatch, or manual
    Adjustment record exists for this product at (date, shift) or any
    earlier period — with no lower bound. Existence, not a net sum: two
    movements that happen to cancel out (e.g. a Production later offset by
    a negative Adjustment) still count as real activity that can
    independently change again later, so a sum-based "is this zero" check
    would be wrong here.

    Used only when no Opening Stock anchor exists at all for a product
    (see get_prior_closing_base_qty()'s Stage 8 hotfix), to distinguish a
    genuinely blank first-ever period — still eligible to become an
    automatic anchor on an explicit save — from a period that happens to
    be a product's first DailyFigure row but follows real finalized
    activity, which must always be derived and must never auto-anchor
    (see upsert_daily_figure()).
    """
    if shift == SHIFT_NIGHT:
        at_or_before = lambda date_col, shift_col: db.or_(date_col < date, date_col == date)
    else:
        at_or_before = lambda date_col, shift_col: db.or_(
            date_col < date, db.and_(date_col == date, shift_col == SHIFT_DAY)
        )

    production_exists = db.session.query(
        db.session.query(ProductionLine.id)
        .join(ProductionRecord, ProductionRecord.id == ProductionLine.production_id)
        .filter(
            ProductionRecord.status == STATUS_FINALIZED,
            ProductionLine.product_id == product_id,
            at_or_before(ProductionRecord.date, ProductionRecord.shift),
        )
        .exists()
    ).scalar()
    if production_exists:
        return True

    dispatch_exists = db.session.query(
        db.session.query(DispatchLine.id)
        .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
        .filter(
            Dispatch.status == STATUS_FINALIZED,
            DispatchLine.product_id == product_id,
            at_or_before(Dispatch.date, Dispatch.shift),
        )
        .exists()
    ).scalar()
    if dispatch_exists:
        return True

    adjustment_exists = db.session.query(
        db.session.query(StockAdjustment.id)
        .filter(
            StockAdjustment.product_id == product_id,
            at_or_before(StockAdjustment.date, StockAdjustment.shift),
        )
        .exists()
    ).scalar()
    if adjustment_exists:
        return True

    # Returns has no shift column — a finalized Return always attributes
    # to that date's Day period (see return_base_qty()), so it counts as
    # "at or before" whenever its date is <= the boundary date.
    returns_exists = db.session.query(
        db.session.query(ReturnLine.id)
        .join(ReturnRecord, ReturnRecord.id == ReturnLine.return_id)
        .filter(
            ReturnRecord.status == STATUS_FINALIZED,
            ReturnLine.product_id == product_id,
            ReturnRecord.date <= date,
        )
        .exists()
    ).scalar()
    return bool(returns_exists)


def get_prior_closing_base_qty(product_id, date, shift, exclude_id=None):
    """
    The running stock balance carried into (date, shift) — the anchor's own
    closing PLUS every finalized Production/Returns/Issued movement on any
    period strictly between the anchor and (date, shift), even when none of
    those intervening periods ever got a DailyFigure row of their own (the
    normal case — see _find_anchor_figure()). Previously this returned only
    closing_base_qty(anchor), which is correct when the anchor is the
    immediately-preceding period but silently dropped every subsequent
    period's activity otherwise — the root cause of Opening Stock resetting
    to zero (or freezing at a stale value) weeks after the last anchor.

    Stage 8 hotfix (section 6): when there is no anchor at all, this used
    to unconditionally return None, which upsert_daily_figure() treats as
    "genuinely the first-ever period — an explicit value is required and
    automatically becomes the anchor." That was wrong whenever finalized
    Production/Returns/Dispatch activity for this product already existed
    before (date, shift) without any DailyFigure row ever having been
    created for it — the exact staging bug (Production finalized on 19
    July, no DailyFigure row until a stale zero one on 1 August). Now: only
    a product with NO anchor and NO finalized activity at all before
    (date, shift) returns None (a genuinely blank first period). Otherwise
    the balance is derived starting from zero base units, summing every
    finalized movement strictly before (date, shift) — exactly as if an
    anchor of 0 existed at the dawn of time.
    """
    anchor = _find_anchor_figure(product_id, date, shift, exclude_id=exclude_id)

    if shift == SHIFT_NIGHT:
        range_end_date, range_end_shift = date, SHIFT_DAY
    else:
        range_end_date, range_end_shift = _shift_date(date, -1), SHIFT_NIGHT

    if anchor is None:
        if not _any_finalized_activity_at_or_before(product_id, range_end_date, range_end_shift):
            return None
        production, returns, issued = _movement_between_periods(
            product_id, _EPOCH_DATE, SHIFT_DAY, range_end_date, range_end_shift
        )
        return production + returns - issued

    anchor_closing = closing_base_qty(anchor)

    if anchor.shift == SHIFT_DAY:
        range_start_date, range_start_shift = anchor.date, SHIFT_NIGHT
    else:
        range_start_date, range_start_shift = _shift_date(anchor.date, 1), SHIFT_DAY

    production, returns, issued = _movement_between_periods(
        product_id, range_start_date, range_start_shift, range_end_date, range_end_shift
    )
    return anchor_closing + production + returns - issued


def dispatch_issued_base_qty(product_id, date, shift):
    total = (
        db.session.query(db.func.coalesce(db.func.sum(DispatchLine.base_unit_qty), 0))
        .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
        .filter(
            Dispatch.status == STATUS_FINALIZED,
            Dispatch.date == date,
            Dispatch.shift == shift,
            DispatchLine.product_id == product_id,
        )
        .scalar()
    )
    return int(total or 0)


def adjustment_total_base_qty(product_id, date, shift):
    total = (
        db.session.query(db.func.coalesce(db.func.sum(StockAdjustment.delta_base_qty), 0))
        .filter(
            StockAdjustment.product_id == product_id,
            StockAdjustment.date == date,
            StockAdjustment.shift == shift,
        )
        .scalar()
    )
    return int(total or 0)


def issued_base_qty(product_id, date, shift):
    """
    Final stock-integrity investigation, section 3 — the one documented
    StockAdjustment sign convention, used everywhere in this codebase and
    nowhere overridden: a StockAdjustment is an ISSUED CORRECTION (option
    B), not a separate direct stock delta. It is folded directly into
    Issued alongside Dispatch, both subtracted together in
    compute_closing() below — never added back separately, never
    subtracted a second time, never given the opposite sign anywhere. A
    positive delta_base_qty means "more actually left the warehouse than
    Dispatch alone accounts for" (reduces Closing Stock); a negative
    delta_base_qty reduces Issued (increases Closing Stock).
    """
    return dispatch_issued_base_qty(product_id, date, shift) + adjustment_total_base_qty(product_id, date, shift)


def returns_finalized_base_qty(product_id, date):
    total = (
        db.session.query(db.func.coalesce(db.func.sum(ReturnLine.base_unit_qty), 0))
        .join(ReturnRecord, ReturnRecord.id == ReturnLine.return_id)
        .filter(
            ReturnRecord.status == STATUS_FINALIZED,
            ReturnRecord.date == date,
            ReturnLine.product_id == product_id,
        )
        .scalar()
    )
    return int(total or 0)


def production_finalized_base_qty(product_id, date, shift):
    total = (
        db.session.query(db.func.coalesce(db.func.sum(ProductionLine.base_unit_qty), 0))
        .join(ProductionRecord, ProductionRecord.id == ProductionLine.production_id)
        .filter(
            ProductionRecord.status == STATUS_FINALIZED,
            ProductionRecord.date == date,
            ProductionRecord.shift == shift,
            ProductionLine.product_id == product_id,
        )
        .scalar()
    )
    return int(total or 0)


def return_base_qty(product_id, date, shift, legacy_stored=0):
    """
    Returns is a day-only workflow (see the Stage 5 spec's shift rules): a
    finalized Returns Book entry only ever contributes to that date's Day
    Daily Figures, never Night. `legacy_stored` is whatever was written
    directly onto a DailyFigure row before Returns became its own book —
    pre-Stage-5 data (including any historical Night value, which the old
    workflow allowed) is preserved exactly as before; only the NEW,
    book-sourced contribution is restricted to Day.
    """
    finalized = returns_finalized_base_qty(product_id, date) if shift == SHIFT_DAY else 0
    return legacy_stored + finalized


def production_base_qty(product_id, date, shift, legacy_stored=0):
    """
    Production has always been shift-based, so there is no Day-only
    restriction here — just add whatever the Production Book has finalized
    for this exact date+shift to whatever was legacy-stored on the
    DailyFigure row before Production became its own book.
    """
    return legacy_stored + production_finalized_base_qty(product_id, date, shift)


def compute_closing(opening_base_qty, production_total, returns_total, issued_total):
    """
    THE one authoritative Closing Stock formula (final stock-integrity
    investigation, section 6) — every caller that needs a Closing Stock
    number (closing_base_qty() below for one DailyFigure row,
    daily_figure_view() for one product/date/shift, date_range_summary()
    for a Dashboard/Reports whole-date-range row, and
    stock_ledger_service.py's own reconciliation check) goes through this
    exact function, so "closing = opening + production + returns -
    issued" is expressed in exactly one place in the codebase and can
    never independently drift between screens.

    `issued_total` is expected to already be the COMBINED Dispatch +
    StockAdjustment figure (see issued_base_qty() below) — the one
    documented sign convention (final stock-integrity investigation,
    section 3): a StockAdjustment is an ISSUED CORRECTION, not a separate
    direct stock delta. A positive delta_base_qty means "more was issued
    than Dispatch alone shows" (reduces Closing Stock, same direction as
    a Dispatch); a negative delta_base_qty reduces Issued (increases
    Closing Stock). There is no second, opposite-signed convention
    anywhere in this codebase — verified by
    tests/test_final_legacy_adjustment_investigation.py's cross-surface
    reconciliation tests.

    Exact integer base units — Python ints throughout, never float.
    """
    return opening_base_qty + production_total + returns_total - issued_total


def qty_label_signed(base_qty, rule):
    """
    Packaging-aware book-notation label for ANY quantity, including a
    genuinely negative balance — the one shared function every surface
    (Daily Figures, Dashboard, History, exports, the stock-ledger CLI, the
    legacy-migration audit) uses whenever a raw base_qty might be
    negative, so they can never disagree. Delegates the actual sign/split
    handling to _split_or_none() + quantity_format.qty_label() (which
    detects a negative `cartons` and formats accordingly) — never a
    second, competing negative-number formatter.
    """
    if base_qty is None:
        return None
    if rule is None:
        return f"{base_qty} pc"
    split = _split_or_none(base_qty, rule)
    return qty_label(split["cartons"], split["packs"], split["pieces"], rule)


def closing_base_qty(figure):
    issued = issued_base_qty(figure.product_id, figure.date, figure.shift)
    return_total = return_base_qty(figure.product_id, figure.date, figure.shift, figure.return_base_qty)
    production_total = production_base_qty(figure.product_id, figure.date, figure.shift, figure.production_base_qty)
    return compute_closing(figure.opening_base_qty, production_total, return_total, issued)


def opening_base_qty_at(product_id, date, shift=SHIFT_DAY):
    """
    The resolved Opening Stock balance carried into (date, shift): the
    stored value on an explicit DailyFigure row exactly there ONLY when
    that row is a currently-trusted anchor (_is_trusted_anchor() — see
    daily_figure_view()'s identical rule), else the running balance
    derived live (get_prior_closing_base_qty()), or 0 for a product with
    no anchor and no finalized activity at all yet. The single shared
    "what is Opening Stock right now, at this point in time" answer used
    by date_range_summary() (and, through it, the Dashboard and any report
    built on it) — never a second, duplicated derivation, and never a
    second source of truth that could disagree with Daily Figures for the
    same product/date/shift.
    """
    figure = DailyFigure.query.filter_by(product_id=product_id, date=date, shift=shift).first()
    if figure is not None and _is_trusted_anchor(figure):
        return figure.opening_base_qty
    prior = get_prior_closing_base_qty(product_id, date, shift, exclude_id=figure.id if figure else None)
    return prior if prior is not None else 0


def _split_or_none(base_qty, rule):
    """
    Final legacy-migration investigation, section 9 — a negative base_qty
    is expressible in book notation: split the ABSOLUTE quantity normally
    (packs/pieces are always non-negative positional digits — a sign has
    no meaning at that level) and carry the sign on the most-significant
    nonzero component, e.g. -600 base units on a 10x10 rule becomes
    cartons=-6, packs=0, pieces=0. A magnitude smaller than one whole
    carton (e.g. -50 base units on a 100-per-carton rule) would split to
    cartons=0 — and `-0 == 0` for an int, silently losing the sign if it
    were forced onto `cartons` regardless — so the sign instead falls onto
    `packs` (or `pieces`, if packs is also 0). At most one of the three is
    ever negative. quantity_format.qty_label()/quantity_format.js's
    qtyLabel() both detect any negative component and render the whole
    label with one leading minus sign ("-6.00 Ctns" or "-0.50 Ctns"), never
    a raw negative base-unit number relabeled as cartons, and never a text
    warning replacing the number outright. Returns None only when base_qty
    itself is None (no figure exists), never merely because the value is
    negative — a genuine negative balance is still a real, displayable
    quantity.
    """
    if base_qty is None:
        return None
    cartons, packs, pieces = from_base_units(abs(base_qty), rule)
    if base_qty < 0:
        if cartons != 0:
            cartons = -cartons
        elif packs != 0:
            packs = -packs
        else:
            pieces = -pieces
    return {"cartons": cartons, "packs": packs, "pieces": pieces}


def daily_figure_view(product, date, shift):
    """
    Full computed view for one product/date/shift, whether or not a
    DailyFigure row has been saved yet — Issued (and its drill-down) must
    be visible even before Opening/Return/Production are entered.

    Stage 8 correction, extended by the production hotfix: a row existing
    exactly at (date, shift) is only ever trusted for its Opening Stock
    value when _is_trusted_anchor() says so right now — a non-anchor-
    eligible row (opening_stock_source="derived", one that a save happened
    to touch without actually correcting Opening, or one cleared by Reset
    Daily Values), OR an anchor-eligible row that's been live-revalidated
    away (an "initial_manual"/"legacy_inferred" row that finalized history
    has since been entered before — see _is_trusted_anchor()) has its
    stored opening_base_qty ignored entirely and is treated exactly like
    "no row exists here". Only "manual_correction" is exempt from that
    revalidation. Either way, a later correction to earlier Production/
    Returns/Dispatch, or a later real anchor, is picked up live on every
    read no matter how the row came to exist or when its history was
    entered relative to it. Completion status (Stage 7's DailyEntryStatus
    — "No Activity Today"/completed) never enters this function at all, so
    it can never freeze or bypass this calculation.
    """
    rule = product.current_packaging_rule()
    figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()

    issued = issued_base_qty(product.id, date, shift)
    legacy_return = figure.return_base_qty if figure is not None else 0
    legacy_production = figure.production_base_qty if figure is not None else 0
    return_base = return_base_qty(product.id, date, shift, legacy_return)
    production_base = production_base_qty(product.id, date, shift, legacy_production)

    if figure is not None and _is_trusted_anchor(figure):
        opening_base = figure.opening_base_qty
        opening_editable = get_prior_closing_base_qty(product.id, date, shift, exclude_id=figure.id) is None
        notes = figure.notes
    else:
        prior = get_prior_closing_base_qty(product.id, date, shift, exclude_id=figure.id if figure else None)
        opening_base = prior if prior is not None else 0
        opening_editable = prior is None
        notes = figure.notes if figure is not None else None

    closing_base = compute_closing(opening_base, production_base, return_base, issued)

    return {
        "product_id": product.id,
        "product_name": product.name,
        "date": date,
        "shift": shift,
        "has_entry": figure is not None,
        "opening_editable": opening_editable,
        "packaging_rule": rule.to_dict() if rule else None,
        "opening": {"base_qty": opening_base, **(_split_or_none(opening_base, rule) or {})} if rule else None,
        "return_": {
            "base_qty": return_base,
            **(_split_or_none(return_base, rule) or {}),
            "from_returns_book": returns_finalized_base_qty(product.id, date) if shift == SHIFT_DAY else 0,
            "legacy": legacy_return,
        } if rule else None,
        "production": {
            "base_qty": production_base,
            **(_split_or_none(production_base, rule) or {}),
            "from_production_book": production_finalized_base_qty(product.id, date, shift),
            "legacy": legacy_production,
        } if rule else None,
        "issued": {
            "base_qty": issued,
            **(_split_or_none(issued, rule) or {}),
            "from_dispatches": dispatch_issued_base_qty(product.id, date, shift),
            "from_adjustments": adjustment_total_base_qty(product.id, date, shift),
        } if rule else None,
        "closing": {"base_qty": closing_base, **(_split_or_none(closing_base, rule) or {})} if rule else None,
        "notes": notes,
        # Final pre-deployment correction (Manager/Super Administrator
        # review workflow, section 11): who originally entered this row
        # and who most recently touched it — never overwritten, so a
        # later elevated correction is always distinguishable from the
        # original entry rather than looking like a second independent
        # one. created_by/created_at never change once the row exists;
        # updated_by/updated_at reflect the most recent save (which may
        # be the same person, or a correction by someone else).
        "created_by": figure.created_by if figure is not None else None,
        "created_by_username": _user_label(figure.created_by) if figure is not None else None,
        "created_at": figure.created_at.isoformat() if figure is not None and figure.created_at else None,
        "updated_by": figure.updated_by if figure is not None else None,
        "updated_by_username": _user_label(figure.updated_by) if figure is not None else None,
        "updated_at": figure.updated_at.isoformat() if figure is not None and figure.updated_at else None,
    }


def upsert_daily_figure(*, product, date, shift, opening, notes, user):
    """
    opening is a {cartons, packs, pieces} dict (or None to leave opening
    as-is when it's locked to the prior period's running balance) — the
    only quantity Daily Figures still accepts directly. Return and
    Production are no longer entered here: Stage 5 moved them to the
    dedicated Returns Book / Production Book, and they're always read live
    from there (see return_base_qty()/production_base_qty() above) — Daily
    Figures is a calculated summary for both, the same way Issued already
    was. Raises StockError for anything the user needs to fix.

    Stage 8 section 3 — Opening Stock anchors, corrected: an Operator
    submitting `opening` for a period whose Opening is derived (not this
    product's first-ever period) has it silently ignored in favor of the
    derived running balance, exactly as before — Operators never create a
    new anchor. A Manager/Super Administrator submitting an explicit
    `opening` only becomes a new anchor when it actually DIFFERS from what
    pure carry-forward would otherwise produce — a routine save that
    happens to leave the pre-filled (already-correct) value unchanged is
    never treated as a correction. This is the fix for a real regression:
    treating every elevated submission as an override meant an elevated
    user simply viewing-and-saving an already-derived-correct period (e.g.
    while paging through, or completing "No Activity Today" on a page that
    also showed an editable Opening field) could silently freeze that
    date's Opening Stock forever at whatever was on screen at that moment
    — later Production/Returns/Dispatch corrections upstream would then
    never ripple past it.

    Production hotfix, section 3/7 — WHICH kind of anchor a row becomes is
    now recorded via opening_stock_source, not just a bare True/False:
      - opening_locked is None (nothing before this period at all, not
        even finalized movement) -> initial_manual. Any role may set
        this; it's the genuine starting point. Only trusted live for as
        long as it truly has nothing before it -- see _is_trusted_anchor()
        -- so a later out-of-order historical entry doesn't leave it
        stuck.
      - An elevated user's submission genuinely differs from live
        derivation -> manual_correction. Trusted unconditionally, forever
        -- a deliberate correction is never second-guessed by history.
      - Everything else -> derived. Never an anchor.
    See daily_figure_view()'s _is_trusted_anchor() call, which is the one
    place that actually decides whether a row's stored Opening is used.
    """
    from webapp.models.user import ROLE_MANAGER, ROLE_SUPER_ADMIN

    rule = product.current_packaging_rule()
    if rule is None:
        raise StockError(
            f"'{product.name}' has no packaging rule configured yet — "
            "set one in Admin > Products first"
        )

    is_elevated = user.role in (ROLE_MANAGER, ROLE_SUPER_ADMIN)
    figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()
    opening_locked = get_prior_closing_base_qty(
        product.id, date, shift, exclude_id=figure.id if figure else None
    )

    oc = op = opc = submitted_base_qty = None
    if opening is not None:
        try:
            oc, op, opc = normalize(opening.get("cartons", 0), opening.get("packs", 0), opening.get("pieces", 0), rule)
            submitted_base_qty = to_base_units(oc, op, opc, rule)
        except PackagingError as e:
            raise StockError(str(e)) from e

    if opening_locked is None:
        # Genuinely the first-ever period — an explicit value is required,
        # from any role, and always becomes the anchor.
        if submitted_base_qty is None:
            raise StockError("Opening stock is required for this product's first-ever entry")
        new_source = OPENING_STOCK_SOURCE_INITIAL_MANUAL
        opening_base = submitted_base_qty
    elif is_elevated and submitted_base_qty is not None and submitted_base_qty != opening_locked:
        new_source = OPENING_STOCK_SOURCE_MANUAL_CORRECTION
        opening_base = submitted_base_qty
    else:
        new_source = OPENING_STOCK_SOURCE_DERIVED
        opening_base = opening_locked

    if figure is None:
        figure = DailyFigure(product_id=product.id, date=date, shift=shift, created_by=user.id)
        db.session.add(figure)

    if new_source != OPENING_STOCK_SOURCE_DERIVED:
        figure.opening_cartons, figure.opening_packs, figure.opening_pieces = oc, op, opc
        figure.opening_base_qty = opening_base
        figure.opening_stock_source = new_source
        figure.opening_stock_is_override = True
    else:
        # Not a correction — always refresh the display split to the
        # current live-derived truth (self-healing: any write to a
        # non-manual_correction row, even an unrelated notes edit,
        # corrects a previously-stale value AND its stored provenance --
        # an initial_manual/legacy_inferred row that's since been
        # live-disqualified by earlier history (see _is_trusted_anchor())
        # gets its stored label corrected to "derived" too, the moment
        # anyone next saves it. Only manual_correction is protected from
        # this -- only a fresh override write (above) or Reset Daily
        # Values may retire a real correction.
        try:
            split = from_base_units(opening_base, rule)
        except PackagingError:
            # Negative-stock policy (see _split_or_none() — a negative
            # value is fully displayable, split by absolute value with a
            # signed cartons component) applies to LIVE views, not these
            # stored display columns: a derived opening can legitimately
            # be negative, and from_base_units() itself still can't split
            # a negative count directly. These columns aren't consulted
            # for a non-override row anyway (daily_figure_view() always
            # recomputes it live from opening_base_qty's derivation,
            # never from these three columns, using _split_or_none()) —
            # 0/0/0 is a harmless placeholder; opening_base_qty below
            # still carries the true (negative) value.
            split = (0, 0, 0)
        figure.opening_cartons, figure.opening_packs, figure.opening_pieces = split
        figure.opening_base_qty = opening_base
        if figure.opening_stock_source != OPENING_STOCK_SOURCE_MANUAL_CORRECTION:
            figure.opening_stock_source = OPENING_STOCK_SOURCE_DERIVED
            figure.opening_stock_is_override = False

    figure.packaging_rule_id = rule.id
    figure.notes = notes
    figure.updated_by = user.id

    db.session.flush()
    return figure


def create_adjustment(*, product, date, shift, delta_base_qty, reason, user):
    if not reason:
        raise StockError("A reason is required for a manual stock adjustment")
    if delta_base_qty == 0:
        raise StockError("Adjustment delta cannot be zero")
    adjustment = StockAdjustment(
        product_id=product.id, date=date, shift=shift,
        delta_base_qty=delta_base_qty, reason=reason, created_by=user.id,
    )
    db.session.add(adjustment)
    db.session.flush()
    return adjustment


def dispatch_issued_base_qty_range(product_id, date_from, date_to):
    total = (
        db.session.query(db.func.coalesce(db.func.sum(DispatchLine.base_unit_qty), 0))
        .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
        .filter(
            Dispatch.status == STATUS_FINALIZED,
            Dispatch.date >= date_from,
            Dispatch.date <= date_to,
            DispatchLine.product_id == product_id,
        )
        .scalar()
    )
    return int(total or 0)


def adjustment_total_base_qty_range(product_id, date_from, date_to):
    total = (
        db.session.query(db.func.coalesce(db.func.sum(StockAdjustment.delta_base_qty), 0))
        .filter(
            StockAdjustment.product_id == product_id,
            StockAdjustment.date >= date_from,
            StockAdjustment.date <= date_to,
        )
        .scalar()
    )
    return int(total or 0)


def returns_finalized_base_qty_range(product_id, date_from, date_to):
    """
    Whole-range total, shift-agnostic — ReturnRecord has no shift column at
    all (see return_base_qty()'s docstring on why), so unlike Issued/
    Production there is nothing to filter by here.
    """
    total = (
        db.session.query(db.func.coalesce(db.func.sum(ReturnLine.base_unit_qty), 0))
        .join(ReturnRecord, ReturnRecord.id == ReturnLine.return_id)
        .filter(
            ReturnRecord.status == STATUS_FINALIZED,
            ReturnRecord.date >= date_from,
            ReturnRecord.date <= date_to,
            ReturnLine.product_id == product_id,
        )
        .scalar()
    )
    return int(total or 0)


def production_finalized_base_qty_range(product_id, date_from, date_to):
    """Whole-range total across both shifts — matches total_production below, which also sums both shifts' DailyFigure rows together."""
    total = (
        db.session.query(db.func.coalesce(db.func.sum(ProductionLine.base_unit_qty), 0))
        .join(ProductionRecord, ProductionRecord.id == ProductionLine.production_id)
        .filter(
            ProductionRecord.status == STATUS_FINALIZED,
            ProductionRecord.date >= date_from,
            ProductionRecord.date <= date_to,
            ProductionLine.product_id == product_id,
        )
        .scalar()
    )
    return int(total or 0)


def date_range_summary(date_from, date_to):
    """
    One row per product touched during [date_from, date_to]: total
    Return/Production/Issued across the whole range, plus the range's
    Opening (earliest entered figure) and a derived Closing — everything
    in exact integer base units. Products with no activity in the range
    are left out rather than padding the report with all-zero rows.
    """
    from webapp.models.product import Product  # local import avoids a circular import at module load time

    results = []
    for product in Product.query.filter_by(active=True).order_by(Product.display_order, Product.name).all():
        rows = (
            DailyFigure.query.filter(
                DailyFigure.product_id == product.id,
                DailyFigure.date >= date_from,
                DailyFigure.date <= date_to,
            )
            .order_by(DailyFigure.date, DailyFigure.shift)
            .all()
        )
        total_issued = dispatch_issued_base_qty_range(product.id, date_from, date_to) + \
            adjustment_total_base_qty_range(product.id, date_from, date_to)
        total_return = sum(r.return_base_qty for r in rows) + \
            returns_finalized_base_qty_range(product.id, date_from, date_to)
        total_production = sum(r.production_base_qty for r in rows) + \
            production_finalized_base_qty_range(product.id, date_from, date_to)

        # Stage 8: the range's Opening is the carried-forward running
        # balance at date_from (see opening_base_qty_at()), not merely
        # whichever DailyFigure row happens to be earliest within the
        # range — a date range with no row of its own (the normal case for
        # every period after a product's first) previously always reported
        # Opening as a hard-coded 0, silently discarding all the finalized
        # activity that happened between the last anchor and date_from.
        opening_base = opening_base_qty_at(product.id, date_from, SHIFT_DAY)

        # A product genuinely untouched — no carried balance, no rows, no
        # movement in range — is still left out, exactly as before, rather
        # than padding the report with all-zero rows. A product with a real
        # carried balance (even a zero-activity day) is now correctly kept.
        if not rows and total_issued == 0 and total_return == 0 and total_production == 0 and opening_base == 0:
            continue

        rule = product.current_packaging_rule()
        closing_base = compute_closing(opening_base, total_production, total_return, total_issued)

        results.append({
            "product_id": product.id,
            "product_name": product.name,
            "packaging_rule": rule.to_dict() if rule else None,
            "opening": _split_or_none(opening_base, rule) if rule else None,
            "return_": _split_or_none(total_return, rule) if rule else None,
            "production": _split_or_none(total_production, rule) if rule else None,
            "issued": _split_or_none(total_issued, rule) if rule else None,
            "closing": _split_or_none(closing_base, rule) if rule else None,
            "opening_base_qty": opening_base, "return_base_qty": total_return,
            "production_base_qty": total_production, "issued_base_qty": total_issued,
            "closing_base_qty": closing_base,
        })
    return results


def recipient_totals(date_from, date_to, group_by):
    """
    Total Issued (finalized dispatches only) and dispatch count, grouped
    either by sales category or by recipient. Recipient grouping resolves
    merges (via customer_service.resolve_canonical) so a customer's
    history recorded both before and after a merge is combined under the
    canonical name — merging must never split a recipient's totals.

    Dashboard safety correction: `total_issued_base_qty` is a raw base-unit
    sum across every product in the group — different products have
    different carton capacities, so this must never be presented to a user
    as one carton figure (existing exports that show it as a plain,
    explicitly-labeled "(pieces)" number are unaffected — this field's
    shape and meaning are unchanged). `products` is the additive fix: one
    packaging-aware line per product actually contributing to this group
    (product id/name, exact integer base_qty, its packaging rule, and a
    backend-formatted book-notation label via the one centralized
    qty_label() — never a second, drifting reimplementation), sorted by
    descending quantity, omitting any product whose total is zero. Product
    lookups are batched into one extra query for every distinct product
    touched across all groups combined, never one query per group/dispatch
    line.
    """
    from webapp.models.product import Product  # local import avoids a circular import at module load time

    if group_by not in ("category", "recipient"):
        raise ValueError("group_by must be 'category' or 'recipient'")

    rows = (
        db.session.query(Dispatch, DispatchLine)
        .join(DispatchLine, DispatchLine.dispatch_id == Dispatch.id)
        .filter(Dispatch.status == STATUS_FINALIZED, Dispatch.date >= date_from, Dispatch.date <= date_to)
        .all()
    )

    groups = {}
    if group_by == "category":
        category_cache = {}
        for dispatch, line in rows:
            key = dispatch.sales_category_id
            if key not in category_cache:
                category = db.session.get(SalesCategory, key) if key else None
                category_cache[key] = category.name if category else "Uncategorized"
            group = groups.setdefault(key, {
                "group_id": key, "group_name": category_cache[key], "dispatch_ids": set(), "total": 0, "product_totals": {},
            })
            group["dispatch_ids"].add(dispatch.id)
            group["total"] += line.base_unit_qty
            group["product_totals"][line.product_id] = group["product_totals"].get(line.product_id, 0) + line.base_unit_qty
    else:
        canonical_cache = {}
        for dispatch, line in rows:
            customer = dispatch.customer
            if customer is None:
                key, name = None, dispatch.customer_name_snapshot or "Unknown"
            else:
                if customer.id not in canonical_cache:
                    canonical_cache[customer.id] = resolve_canonical(customer)
                canonical = canonical_cache[customer.id]
                key, name = canonical.id, canonical.name
            group = groups.setdefault(key, {
                "group_id": key, "group_name": name, "dispatch_ids": set(), "total": 0, "product_totals": {},
            })
            group["dispatch_ids"].add(dispatch.id)
            group["total"] += line.base_unit_qty
            group["product_totals"][line.product_id] = group["product_totals"].get(line.product_id, 0) + line.base_unit_qty

    all_product_ids = {pid for g in groups.values() for pid in g["product_totals"]}
    products_by_id = (
        {p.id: p for p in Product.query.filter(Product.id.in_(all_product_ids)).all()} if all_product_ids else {}
    )

    results = []
    for g in groups.values():
        product_lines = []
        for pid, qty in g["product_totals"].items():
            if qty <= 0:
                continue
            product = products_by_id.get(pid)
            if product is None:
                continue
            rule = product.current_packaging_rule()
            if rule:
                cartons, packs, pieces = from_base_units(qty, rule)
                label = qty_label(cartons, packs, pieces, rule)
            else:
                # No packaging rule configured for this product right now —
                # never guess a "Ctns" figure without one to derive it from.
                cartons = packs = pieces = None
                label = f"{qty} pc"
            product_lines.append({
                "product_id": pid, "product_name": product.name, "base_qty": qty,
                "packaging_rule": rule.to_dict() if rule else None,
                "cartons": cartons, "packs": packs, "pieces": pieces,
                "quantity_label": label,
            })
        product_lines.sort(key=lambda r: -r["base_qty"])

        results.append({
            "group_id": g["group_id"], "group_name": g["group_name"],
            "dispatch_count": len(g["dispatch_ids"]), "total_issued_base_qty": g["total"],
            "products": product_lines,
        })
    results.sort(key=lambda r: -r["total_issued_base_qty"])
    return results


def issued_detail(product, date, shift, sales_category_id=None, customer_id=None):
    """
    Dispatches (and adjustments) contributing to this product's Issued
    total for one date/shift. Optionally scoped to a sales category or a
    recipient (resolving merged-away recipients so a canonical customer's
    drill-down still includes history recorded under a merged name).
    """
    query = (
        db.session.query(Dispatch, DispatchLine)
        .join(DispatchLine, DispatchLine.dispatch_id == Dispatch.id)
        .filter(
            Dispatch.status == STATUS_FINALIZED,
            Dispatch.date == date,
            Dispatch.shift == shift,
            DispatchLine.product_id == product.id,
        )
    )
    if sales_category_id is not None:
        query = query.filter(Dispatch.sales_category_id == sales_category_id)
    if customer_id is not None:
        query = query.filter(Dispatch.customer_id.in_(resolve_customer_ids_for_filter(customer_id)))

    rows = query.order_by(Dispatch.dispatch_number).all()
    dispatches = [{
        "dispatch_id": dispatch.id,
        "dispatch_number": dispatch.dispatch_number,
        "customer_id": dispatch.customer_id,
        "customer_name": dispatch.customer_name_snapshot or (dispatch.customer.name if dispatch.customer else None),
        "sales_category_id": dispatch.sales_category_id,
        "sales_category_name": dispatch.sales_category_name_snapshot,
        "cartons": line.cartons, "packs": line.packs, "pieces": line.pieces,
        "base_unit_qty": line.base_unit_qty,
    } for dispatch, line in rows]

    adjustments = StockAdjustment.query.filter_by(product_id=product.id, date=date, shift=shift).all()

    return {
        "dispatches": dispatches,
        "adjustments": [a.to_dict() for a in adjustments],
        "total_from_dispatches": sum(d["base_unit_qty"] for d in dispatches),
        "total_from_adjustments": sum(a.delta_base_qty for a in adjustments),
    }
