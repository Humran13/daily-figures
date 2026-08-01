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
from webapp.models.daily_figure import DailyFigure, StockAdjustment
from webapp.models.dispatch import SHIFT_DAY, SHIFT_NIGHT, STATUS_FINALIZED, Dispatch, DispatchLine
from webapp.models.production_record import ProductionLine, ProductionRecord
from webapp.models.return_record import ReturnLine, ReturnRecord
from webapp.models.sales_category import SalesCategory
from webapp.services.customer_service import resolve_canonical, resolve_customer_ids_for_filter
from webapp.services.packaging import PackagingError, from_base_units, normalize, to_base_units

# The one centralized period-ordering rule (Stage 8 section 2) — Day always
# precedes Night on the same date; every date/shift comparison anywhere in
# this module (and nowhere else — never duplicated in a route or in
# frontend JS) goes through _sort_key().
SHIFT_ORDER = {"Day": 0, "Night": 1}


class StockError(ValueError):
    pass


def _sort_key(date, shift):
    return f"{date}-{SHIFT_ORDER.get(shift, 9)}"


def _shift_date(date_str, n):
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def _find_anchor_figure(product_id, date, shift, exclude_id=None):
    """The most recent DailyFigure row strictly before (date, shift) for
    this product, if any — the last point Opening Stock was explicitly set
    (a product's first-ever period) or corrected (an authorized Manager/
    Super Admin anchor, see upsert_daily_figure()). Every period between
    two anchors — and every period after the latest one — has no row of
    its own by design (Stage 7's "do not create meaningless stock rows"),
    so this is a small scan bounded by the number of anchors ever created
    for this product, not by the number of days that have passed."""
    target_key = _sort_key(date, shift)
    candidates = DailyFigure.query.filter(DailyFigure.product_id == product_id)
    if exclude_id is not None:
        candidates = candidates.filter(DailyFigure.id != exclude_id)

    best = None
    best_key = None
    for row in candidates.all():
        key = _sort_key(row.date, row.shift)
        if key < target_key and (best_key is None or key > best_key):
            best = row
            best_key = key
    return best


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
    since the anchor.
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
        return production, returns, issued

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

    return production, returns, issued


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
    Returns None only when there is no anchor at all (the product's actual
    first-ever period, unchanged from before).
    """
    anchor = _find_anchor_figure(product_id, date, shift, exclude_id=exclude_id)
    if anchor is None:
        return None

    anchor_closing = closing_base_qty(anchor)

    if anchor.shift == SHIFT_DAY:
        range_start_date, range_start_shift = anchor.date, SHIFT_NIGHT
    else:
        range_start_date, range_start_shift = _shift_date(anchor.date, 1), SHIFT_DAY

    if shift == SHIFT_NIGHT:
        range_end_date, range_end_shift = date, SHIFT_DAY
    else:
        range_end_date, range_end_shift = _shift_date(date, -1), SHIFT_NIGHT

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


def closing_base_qty(figure):
    issued = issued_base_qty(figure.product_id, figure.date, figure.shift)
    return_total = return_base_qty(figure.product_id, figure.date, figure.shift, figure.return_base_qty)
    production_total = production_base_qty(figure.product_id, figure.date, figure.shift, figure.production_base_qty)
    return figure.opening_base_qty + return_total + production_total - issued


def opening_base_qty_at(product_id, date, shift=SHIFT_DAY):
    """
    The resolved Opening Stock balance carried into (date, shift): whatever
    explicit DailyFigure row exists exactly there, else the running balance
    derived from the latest prior anchor (get_prior_closing_base_qty()), or
    0 for a product with no anchor at all yet. The single shared
    "what is Opening Stock right now, at this point in time" answer used
    by date_range_summary() (and, through it, the Dashboard and any report
    built on it) — never a second, duplicated derivation.
    """
    figure = DailyFigure.query.filter_by(product_id=product_id, date=date, shift=shift).first()
    if figure is not None:
        return figure.opening_base_qty
    prior = get_prior_closing_base_qty(product_id, date, shift)
    return prior if prior is not None else 0


def _split_or_none(base_qty, rule):
    if base_qty is None or base_qty < 0:
        return None
    cartons, packs, pieces = from_base_units(base_qty, rule)
    return {"cartons": cartons, "packs": packs, "pieces": pieces}


def daily_figure_view(product, date, shift):
    """
    Full computed view for one product/date/shift, whether or not a
    DailyFigure row has been saved yet — Issued (and its drill-down) must
    be visible even before Opening/Return/Production are entered.
    """
    rule = product.current_packaging_rule()
    figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()

    issued = issued_base_qty(product.id, date, shift)
    legacy_return = figure.return_base_qty if figure is not None else 0
    legacy_production = figure.production_base_qty if figure is not None else 0
    return_base = return_base_qty(product.id, date, shift, legacy_return)
    production_base = production_base_qty(product.id, date, shift, legacy_production)

    if figure is not None:
        opening_base = figure.opening_base_qty
        opening_editable = get_prior_closing_base_qty(product.id, date, shift, exclude_id=figure.id) is None
        notes = figure.notes
    else:
        prior = get_prior_closing_base_qty(product.id, date, shift)
        opening_base = prior if prior is not None else 0
        opening_editable = prior is None
        notes = None

    closing_base = opening_base + return_base + production_base - issued

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
        "closing": {"base_qty": closing_base, **(_split_or_none(closing_base, rule) or {"warning": "negative — check entries"})} if rule else None,
        "notes": notes,
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

    Stage 8 section 3 — Opening Stock anchors: an Operator submitting
    `opening` for a period whose Opening is derived (not this product's
    first-ever period) has it silently ignored in favor of the derived
    running balance, exactly as before — Operators never create a new
    anchor. A Manager/Super Administrator submitting an explicit `opening`
    for ANY period — including one that already has a derived value —
    IS honored: that value becomes a new stock-balance anchor, its own
    Closing recalculates from it, and every later period (which has no row
    of its own) picks it up automatically the next time it's viewed, via
    get_prior_closing_base_qty() finding this row as the new nearest
    anchor. The audit trail (recorded by the route, before/after) captures
    the correction; this function never restores an older value over it.
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

    submits_new_anchor = is_elevated and opening is not None
    oc = op = opc = None
    try:
        if opening_locked is not None and not submits_new_anchor:
            opening_base = opening_locked
        elif opening is None:
            if opening_locked is not None:
                opening_base = opening_locked  # elevated user touching this period without changing Opening
            else:
                raise StockError("Opening stock is required for this product's first-ever entry")
        else:
            oc, op, opc = normalize(opening.get("cartons", 0), opening.get("packs", 0), opening.get("pieces", 0), rule)
            opening_base = to_base_units(oc, op, opc, rule)
    except PackagingError as e:
        raise StockError(str(e)) from e

    if figure is None:
        figure = DailyFigure(product_id=product.id, date=date, shift=shift, created_by=user.id)
        db.session.add(figure)

    if oc is not None:
        # An explicit value was submitted and accepted (first-ever entry,
        # or an elevated correction) — store the exact split provided.
        figure.opening_cartons, figure.opening_packs, figure.opening_pieces = oc, op, opc
        figure.opening_base_qty = opening_base
    else:
        # Locked to the derived running balance — keep whatever's already
        # stored (or, for a brand-new row inheriting a locked opening,
        # store the split purely for display).
        if figure.id is None:
            split = from_base_units(opening_base, rule)
            figure.opening_cartons, figure.opening_packs, figure.opening_pieces = split
        figure.opening_base_qty = opening_base

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
        closing_base = opening_base + total_return + total_production - total_issued

        results.append({
            "product_id": product.id,
            "product_name": product.name,
            "packaging_rule": rule.to_dict() if rule else None,
            "opening": _split_or_none(opening_base, rule) if rule else None,
            "return_": _split_or_none(total_return, rule) if rule else None,
            "production": _split_or_none(total_production, rule) if rule else None,
            "issued": _split_or_none(total_issued, rule) if rule else None,
            "closing": _split_or_none(closing_base, rule) if rule and closing_base >= 0 else {"warning": "negative"},
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
    """
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
            group = groups.setdefault(key, {"group_id": key, "group_name": category_cache[key], "dispatch_ids": set(), "total": 0})
            group["dispatch_ids"].add(dispatch.id)
            group["total"] += line.base_unit_qty
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
            group = groups.setdefault(key, {"group_id": key, "group_name": name, "dispatch_ids": set(), "total": 0})
            group["dispatch_ids"].add(dispatch.id)
            group["total"] += line.base_unit_qty

    results = [
        {"group_id": g["group_id"], "group_name": g["group_name"],
         "dispatch_count": len(g["dispatch_ids"]), "total_issued_base_qty": g["total"]}
        for g in groups.values()
    ]
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
