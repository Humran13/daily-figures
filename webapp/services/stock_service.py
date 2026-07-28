"""
Daily Figures stock math. The backend is the sole source of truth here:
Issued is always computed live from finalized dispatch lines (+ any manual
adjustments) — never cached, never hand-typed — and Closing is always
Opening + Return + Production - Issued, computed in exact integer base
units. Nothing here uses floating point.
"""
from webapp.extensions import db
from webapp.models.daily_figure import DailyFigure, StockAdjustment
from webapp.models.dispatch import STATUS_FINALIZED, Dispatch, DispatchLine
from webapp.models.sales_category import SalesCategory
from webapp.services.customer_service import resolve_canonical, resolve_customer_ids_for_filter
from webapp.services.packaging import PackagingError, from_base_units, normalize, to_base_units

SHIFT_ORDER = {"Day": 0, "Night": 1}


class StockError(ValueError):
    pass


def _sort_key(date, shift):
    return f"{date}-{SHIFT_ORDER.get(shift, 9)}"


def get_prior_closing_base_qty(product_id, date, shift, exclude_id=None):
    """The most recent DailyFigure strictly before (date, shift) for this product, if any."""
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
    if best is None:
        return None
    return closing_base_qty(best)


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


def closing_base_qty(figure):
    issued = issued_base_qty(figure.product_id, figure.date, figure.shift)
    return figure.opening_base_qty + figure.return_base_qty + figure.production_base_qty - issued


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

    if figure is not None:
        opening_base = figure.opening_base_qty
        return_base = figure.return_base_qty
        production_base = figure.production_base_qty
        opening_editable = get_prior_closing_base_qty(product.id, date, shift, exclude_id=figure.id) is None
        notes = figure.notes
    else:
        prior = get_prior_closing_base_qty(product.id, date, shift)
        opening_base = prior if prior is not None else 0
        return_base = 0
        production_base = 0
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
        "return_": {"base_qty": return_base, **(_split_or_none(return_base, rule) or {})} if rule else None,
        "production": {"base_qty": production_base, **(_split_or_none(production_base, rule) or {})} if rule else None,
        "issued": {
            "base_qty": issued,
            **(_split_or_none(issued, rule) or {}),
            "from_dispatches": dispatch_issued_base_qty(product.id, date, shift),
            "from_adjustments": adjustment_total_base_qty(product.id, date, shift),
        } if rule else None,
        "closing": {"base_qty": closing_base, **(_split_or_none(closing_base, rule) or {"warning": "negative — check entries"})} if rule else None,
        "notes": notes,
    }


def upsert_daily_figure(*, product, date, shift, opening, return_, production, notes, user):
    """
    opening/return_/production are each {cartons, packs, pieces} dicts (or
    None to leave opening as-is when it's locked to the prior period's
    closing). Raises StockError for anything the user needs to fix.
    """
    rule = product.current_packaging_rule()
    if rule is None:
        raise StockError(
            f"'{product.name}' has no packaging rule configured yet — "
            "set one in Admin > Products first"
        )

    figure = DailyFigure.query.filter_by(product_id=product.id, date=date, shift=shift).first()
    opening_locked = get_prior_closing_base_qty(
        product.id, date, shift, exclude_id=figure.id if figure else None
    )

    try:
        if opening_locked is not None:
            opening_base = opening_locked
        else:
            if opening is None:
                raise StockError("Opening stock is required for this product's first-ever entry")
            oc, op, opc = normalize(opening.get("cartons", 0), opening.get("packs", 0), opening.get("pieces", 0), rule)
            opening_base = to_base_units(oc, op, opc, rule)

        rc, rp, rpc = normalize(return_.get("cartons", 0), return_.get("packs", 0), return_.get("pieces", 0), rule)
        return_base = to_base_units(rc, rp, rpc, rule)

        pc, pp, ppc = normalize(production.get("cartons", 0), production.get("packs", 0), production.get("pieces", 0), rule)
        production_base = to_base_units(pc, pp, ppc, rule)
    except PackagingError as e:
        raise StockError(str(e)) from e

    if figure is None:
        figure = DailyFigure(product_id=product.id, date=date, shift=shift, created_by=user.id)
        db.session.add(figure)

    if opening_locked is None:
        figure.opening_cartons, figure.opening_packs, figure.opening_pieces = oc, op, opc
        figure.opening_base_qty = opening_base
    else:
        # keep whatever's already stored (or, for a brand-new row inheriting
        # a locked opening, store the prior period's closing split for display)
        if figure.id is None:
            split = from_base_units(opening_base, rule)
            figure.opening_cartons, figure.opening_packs, figure.opening_pieces = split
        figure.opening_base_qty = opening_base

    figure.return_cartons, figure.return_packs, figure.return_pieces = rc, rp, rpc
    figure.return_base_qty = return_base
    figure.production_cartons, figure.production_packs, figure.production_pieces = pc, pp, ppc
    figure.production_base_qty = production_base
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

        if not rows and total_issued == 0:
            continue

        rule = product.current_packaging_rule()
        opening_base = rows[0].opening_base_qty if rows else 0
        total_return = sum(r.return_base_qty for r in rows)
        total_production = sum(r.production_base_qty for r in rows)
        closing_base = opening_base + total_return + total_production - total_issued

        results.append({
            "product_id": product.id,
            "product_name": product.name,
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
