"""Authoritative calculations for Sales vehicle stock, receivables and money.

Nothing in this module trusts client totals.  All quantities and balances are
re-derived from immutable/safely-voided source rows on every read, which makes
historical corrections propagate without rewriting unrelated transactions.
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import func, or_

from webapp.extensions import db
from webapp.models.customer import Customer
from webapp.models.dispatch import Dispatch, DispatchLine, STATUS_FINALIZED
from webapp.models.packaging_rule import PackagingRule
from webapp.models.product import Product
from webapp.models.sales import (
    ACTIVE, DAY_CLOSED, DAY_DRAFT, DAY_REOPENED, DEBT_KINDS, DEBT_OPENING, DEBT_SALE,
    MONEY_ACCOUNTS, PAYMENT_METHODS, SALE_UNITS, VOID,
    SalesBaseline, SalesCashAdjustment, SalesDay, SalesDebt, SalesDebtPayment, SalesDebtor,
    SalesExpense, SalesInvoice, SalesInvoiceLine, SalesMoneyTransfer, SalesOpeningStockLine,
    SalesPayment, SalesProfile, SalesStockAdjustment, SalesVehicleClosingLine,
)
from webapp.services.audit_service import record_audit
from webapp.services.business_calendar import business_today, utcnow


class SalesError(ValueError):
    pass


class DayNotSaved(SalesError):
    """Something tried to persist part of a working day before Close Day.

    The Sales screen keeps an unfinished day in the browser, so a separate
    endpoint writing one piece of it is either an outdated client or a caller
    that should be using Close Day.  Correcting a day that was really closed is
    a different operation, with its own authorization, reason and audit trail,
    and is still allowed.
    """


def valid_date(value, field="business_date"):
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise SalesError(f"{field} must be a valid YYYY-MM-DD date")
    return parsed.isoformat()


def whole_ugx(value, field, *, positive=False, nonnegative=True):
    if isinstance(value, bool) or not isinstance(value, int):
        raise SalesError(f"{field} must be a whole UGX integer")
    if positive and value <= 0:
        raise SalesError(f"{field} must be greater than zero")
    if nonnegative and value < 0:
        raise SalesError(f"{field} cannot be negative")
    return value


def saved_day(profile_id, business_date):
    """The stored Sales Day for a date, or ``None`` if nothing was ever saved.

    Save and Close Day are the only two ways this row is created, so any row
    found here is real, whichever status it is in - including an open,
    unbalanced day a salesperson Saved and has not yet closed.  Only a date
    where neither action has ever been taken has no day at all.
    """
    return SalesDay.query.filter_by(sales_profile_id=profile_id,
                                    business_date=valid_date(business_date)).first()


def _rule_for(product_id, business_date):
    end = datetime.combine(date.fromisoformat(valid_date(business_date)), time.max)
    rule = (
        PackagingRule.query.filter(
            PackagingRule.product_id == product_id,
            PackagingRule.effective_from <= end,
            or_(PackagingRule.effective_to.is_(None), PackagingRule.effective_to > end),
        )
        .order_by(PackagingRule.effective_from.desc())
        .first()
    )
    if rule is None:
        # Historical reconciliation can legitimately start before this app's
        # first PackagingRule row was created.  In that one bootstrap case,
        # use the nearest known rule and snapshot its id on the new record;
        # later rule changes can therefore never reinterpret the transaction.
        rule = (PackagingRule.query.filter(PackagingRule.product_id == product_id,
                                           PackagingRule.effective_from <= end)
                .order_by(PackagingRule.effective_from.desc()).first())
    if rule is None:
        rule = (PackagingRule.query.filter_by(product_id=product_id)
                .order_by(PackagingRule.effective_from.asc()).first())
    if rule is None:
        raise SalesError("No packaging rule exists for this product")
    return rule


def unit_to_base(quantity, unit, rule):
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise SalesError("quantity must be a positive whole number")
    if unit not in SALE_UNITS:
        raise SalesError(f"unit must be one of {SALE_UNITS}")
    if unit == "carton":
        return quantity * rule.pieces_per_carton
    if unit == "pack":
        if not rule.has_pack_tier:
            raise SalesError("This product does not support packs")
        return quantity * rule.pieces_per_pack
    return quantity


# ---- Quantities -------------------------------------------------------------
# Stock counts are whole base units.  A sales card, however, may price a part
# of a unit ("0.5 cartons", "0.25", "1.5"), so card rows carry an exact
# three-decimal Decimal instead of a float: comparing a card against Sold Today
# must never depend on binary rounding.
QTY_STEP = Decimal("0.001")


def as_quantity(value, field="quantity", *, positive=False):
    """One card quantity as an exact three-decimal Decimal.

    A sales card may price a part of a unit ("0.5"), so decimals are accepted
    here; the exact Decimal keeps every comparison against Sold Today exact.
    """
    message = f"{field} must be a positive whole number or a decimal fraction such as 0.5"
    if isinstance(value, bool) or value is None:
        raise SalesError(message)
    try:
        qty = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise SalesError(message)
    if not qty.is_finite():
        raise SalesError(message)
    if -qty.as_tuple().exponent > 3:
        raise SalesError(f"{field} can have at most three decimal places")
    if qty < 0 or (positive and qty <= 0):
        raise SalesError(message)
    return qty.quantize(QTY_STEP)


def quantity_number(value):
    """A quantity as a JSON-safe number: whole values stay ints, fractions float."""
    if value is None:
        return None
    qty = value if isinstance(value, Decimal) else Decimal(str(value))
    if qty == qty.to_integral_value():
        return int(qty)
    return float(qty)


def sales_row_base(quantity, unit, rule):
    """Base units priced by one sales card row.  Decimals are allowed here."""
    qty = as_quantity(quantity, "quantity", positive=True)
    if unit not in SALE_UNITS:
        raise SalesError(f"unit must be one of {SALE_UNITS}")
    if unit == "carton":
        return qty * rule.pieces_per_carton
    if unit == "pack":
        if not rule.has_pack_tier:
            raise SalesError("This product does not support packs")
        return qty * rule.pieces_per_pack
    return qty


def line_total_ugx(quantity, unit_price_ugx):
    """Whole UGX for one priced row, rounded half-up from the exact Decimal."""
    return int((Decimal(str(quantity)) * int(unit_price_ugx)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _quantity_text(value):
    """A quantity as plain text: 3 stays '3', 0.5 stays '0.5'."""
    qty = Decimal(str(value))
    if qty == qty.to_integral_value():
        return str(int(qty))
    return format(qty.quantize(QTY_STEP).normalize(), "f")


def _split_quantity(base_qty, rule):
    """(negative, cartons, packs, pieces) for a base quantity, whole or fractional."""
    total = Decimal(str(base_qty))
    negative = total < 0
    total = abs(total)
    per_carton = Decimal(rule.pieces_per_carton or 1)
    cartons = int(total // per_carton) if total >= per_carton else 0
    rest = total - cartons * per_carton
    packs = 0
    if rule.has_pack_tier and rule.pieces_per_pack:
        per_pack = Decimal(rule.pieces_per_pack)
        packs = int(rest // per_pack) if rest >= per_pack else 0
        rest -= packs * per_pack
    return negative, cartons, packs, rest


def quantity_dict(base_qty, rule):
    if base_qty is None:
        return None
    _, cartons, packs, pieces = _split_quantity(base_qty, rule)
    return {
        "base_unit_qty": quantity_number(base_qty),
        "cartons": cartons,
        "packs": packs,
        "pieces": quantity_number(pieces),
        "negative": Decimal(str(base_qty)) < 0,
        "label": _quantity_label(base_qty, rule),
    }


def _quantity_label(base_qty, rule):
    negative, cartons, packs, pieces = _split_quantity(base_qty, rule)
    sign = "-" if negative else ""
    parts = []
    if cartons:
        parts.append(f"{cartons} carton{'s' if cartons != 1 else ''}")
    if packs:
        parts.append(f"{packs} pack{'s' if packs != 1 else ''}")
    if pieces or not parts:
        parts.append(f"{_quantity_text(pieces)} piece{'s' if pieces != 1 else ''}")
    return sign + " ".join(parts)


def profile_dict(profile):
    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "username": profile.user.username if profile.user else None,
        "display_name": profile.display_name,
        "sales_category_id": profile.sales_category_id,
        "sales_category_name": profile.sales_category.name if profile.sales_category else None,
        "recipient_customer_id": profile.recipient_customer_id,
        "recipient_customer_name": profile.recipient_customer.name if profile.recipient_customer else None,
        "vehicle_label": profile.vehicle_label,
        "active": profile.active,
    }


def baseline_for(profile_id, business_date):
    return (
        SalesBaseline.query.filter(
            SalesBaseline.sales_profile_id == profile_id,
            SalesBaseline.effective_date <= valid_date(business_date),
        )
        .order_by(SalesBaseline.effective_date.desc(), SalesBaseline.id.desc())
        .first()
    )


def opening_stock(profile_id, product_id, business_date):
    business_date = valid_date(business_date)
    prior = (
        db.session.query(SalesVehicleClosingLine, SalesDay)
        .join(SalesDay, SalesDay.id == SalesVehicleClosingLine.sales_day_id)
        .filter(
            SalesDay.sales_profile_id == profile_id,
            SalesDay.business_date < business_date,
            SalesVehicleClosingLine.product_id == product_id,
            # A product can be put on the vehicle before its return is counted.
            # Such a row only says "it is on the van today"; its placeholder
            # zero is not a counted return, so it never carries forward.
            SalesVehicleClosingLine.return_counted.is_(True),
            # A line a Manager removed from a closed day is history, not stock:
            # it must never carry forward into the next day either.
            SalesVehicleClosingLine.removed_at.is_(None),
        )
        .order_by(SalesDay.business_date.desc(), SalesVehicleClosingLine.id.desc())
        .first()
    )
    if prior:
        return prior[0].base_unit_qty, "previous_closing", prior[1].business_date, prior[0].packaging_rule_id
    baseline = baseline_for(profile_id, business_date)
    if baseline:
        line = SalesOpeningStockLine.query.filter_by(baseline_id=baseline.id, product_id=product_id).first()
        if line:
            return line.base_unit_qty, "baseline", baseline.effective_date, line.packaging_rule_id
        # A trusted starting point exists for this vehicle, so a product with no
        # row there was simply not carried yet: that is an explicit zero, not a
        # missing value.  Without this, stock given for a newly added product
        # could never be reconciled.
        return 0, "baseline", baseline.effective_date, None
    return None, "required", None, None


def dispatch_loads(profile, business_date):
    if not profile.recipient_customer_id:
        return {}
    rows = (
        db.session.query(DispatchLine.product_id, func.coalesce(func.sum(DispatchLine.base_unit_qty), 0))
        .join(Dispatch, Dispatch.id == DispatchLine.dispatch_id)
        .filter(
            Dispatch.customer_id == profile.recipient_customer_id,
            Dispatch.date == valid_date(business_date),
            Dispatch.status == STATUS_FINALIZED,
        )
        .group_by(DispatchLine.product_id)
        .all()
    )
    return {product_id: int(total) for product_id, total in rows}


def adjustment_loads(profile_id, business_date):
    rows = (
        db.session.query(SalesStockAdjustment.product_id, func.sum(SalesStockAdjustment.base_unit_delta))
        .filter_by(sales_profile_id=profile_id, business_date=valid_date(business_date), status=ACTIVE)
        .group_by(SalesStockAdjustment.product_id)
        .all()
    )
    return {product_id: int(total) for product_id, total in rows}


def optional_base(quantity, unit, rule, field):
    """Convert one optional Vehicle Stock quantity, allowing an explicit zero.

    Blank/None means "leave this figure on the system value"; zero is a real
    answer ("none here").  Only whole numbers are accepted, exactly like every
    other stock entry in Sales.
    """
    if quantity is None or quantity == "":
        return None
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise SalesError(f"{field} must be a whole number")
    if quantity < 0:
        raise SalesError(f"{field} cannot be negative")
    if quantity == 0:
        return 0
    return unit_to_base(quantity, unit, rule)


def resolve_vehicle_stock(profile, business_date, product_id, line=None, dispatch=None, adjustments=None):
    """The four working Vehicle Stock figures for one product on one day.

    Every figure has a system default and every figure can be corrected for the
    day being worked on:

      Previous Return = the most recent counted return (or the trusted opening)
      Given Stock     = finalized Dispatch base units + active adjustments
      Total Stock     = Previous Return + Given Stock
      Return Today    = the counted return, or None until it is counted

    A correction is stored on THIS day's row and wins for THIS day only.
    Earlier days are read from their own rows, so correcting today's Previous
    Return can never rewrite yesterday's counted return.
    """
    business_date = valid_date(business_date)
    opening, source, source_date, opening_rule_id = opening_stock(profile.id, product_id, business_date)
    if dispatch is None:
        dispatch = dispatch_loads(profile, business_date)
    if adjustments is None:
        adjustments = adjustment_loads(profile.id, business_date)
    previous_override = line.previous_return_base_qty if line is not None else None
    issued_override = line.issued_base_qty if line is not None else None
    total_override = line.total_stock_base_qty if line is not None else None
    issued_derived = dispatch.get(product_id, 0) + adjustments.get(product_id, 0)
    previous = opening if previous_override is None else previous_override
    issued = issued_derived if issued_override is None else issued_override
    total_derived = None if previous is None else previous + issued
    total = total_derived if total_override is None else total_override
    counted = bool(line is not None and line.return_counted)
    closing = line.base_unit_qty if counted else None
    sold = None if total is None or closing is None else total - closing
    return {
        "opening": opening, "opening_source": source, "opening_source_date": source_date,
        "opening_rule_id": opening_rule_id,
        "previous": previous, "previous_derived": opening,
        "previous_overridden": previous_override is not None,
        "issued": issued, "issued_derived": issued_derived,
        "issued_overridden": issued_override is not None,
        "dispatch": dispatch.get(product_id, 0), "adjustment": adjustments.get(product_id, 0),
        "total": total, "total_derived": total_derived,
        "total_overridden": total_override is not None,
        "closing": closing, "return_counted": counted, "sold": sold,
    }


def recorded_quantities(profile_id, business_date):
    rows = (
        db.session.query(SalesInvoiceLine.product_id, func.sum(SalesInvoiceLine.base_unit_qty))
        .join(SalesInvoice, SalesInvoice.id == SalesInvoiceLine.invoice_id)
        .filter(
            SalesInvoice.sales_profile_id == profile_id,
            SalesInvoice.business_date == valid_date(business_date),
            SalesInvoice.status == ACTIVE,
            SalesInvoice.is_opening_receivable.is_(False),
            SalesInvoiceLine.product_id.is_not(None),
        )
        .group_by(SalesInvoiceLine.product_id)
        .all()
    )
    return {product_id: Decimal(str(total or 0)).quantize(QTY_STEP) for product_id, total in rows}


def stock_reconciliation(profile, business_date, *, day=None):
    business_date = valid_date(business_date)
    dispatch = dispatch_loads(profile, business_date)
    adjustments = adjustment_loads(profile.id, business_date)
    recorded = recorded_quantities(profile.id, business_date)
    day = day if day is not None else saved_day(profile.id, business_date)
    # A line a Manager removed from a closed day is kept for the audit trail but
    # is no longer part of the day: it is not counted, not shown, and never
    # carries forward.
    closings = ({line.product_id: line for line in day.closing_lines if line.removed_at is None}
                if day else {})
    product_ids = set(dispatch) | set(adjustments) | set(recorded) | set(closings)
    baseline = baseline_for(profile.id, business_date)
    if baseline:
        # A baseline row of zero exists only to say "none carried"; it should
        # not clutter the driver's daily list.
        product_ids.update(line.product_id for line in baseline.stock_lines if line.base_unit_qty > 0)
    # The salesperson declares the starting stock themselves on the day the
    # vehicle balance starts.  From the next date the previous counted return
    # is authoritative, so the starting point is never re-declared (and never
    # duplicated) on a later day.
    opening_locked = baseline is not None and baseline.effective_date != business_date
    # A closed day is read-only: the starting stock may only be corrected while
    # the day is open (reopening needs a Manager).
    day_open = day is None or day.status != DAY_CLOSED
    # Only products that actually belong to this vehicle balance are shown:
    # opening stock carried from an earlier count, stock loaded today, a
    # recorded adjustment, a sale, or a closing count.  A driver must never be
    # forced to type zero for every unrelated product in the company.
    for (product_id,) in db.session.query(SalesVehicleClosingLine.product_id).join(
        SalesDay, SalesDay.id == SalesVehicleClosingLine.sales_day_id,
    ).filter(
        SalesDay.sales_profile_id == profile.id,
        SalesDay.business_date < business_date,
        SalesVehicleClosingLine.base_unit_qty > 0,
        SalesVehicleClosingLine.removed_at.is_(None),
    ).distinct():
        product_ids.add(product_id)
    products = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()} if product_ids else {}
    result = []
    for product_id in sorted(product_ids, key=lambda pid: (products[pid].display_order, products[pid].name)):
        product = products[product_id]
        close_line = closings.get(product_id)
        figures = resolve_vehicle_stock(profile, business_date, product_id, close_line, dispatch, adjustments)
        rule_id = close_line.packaging_rule_id if close_line else figures["opening_rule_id"]
        # db.session.get(Model, None) warns about a NULL primary key; skip the
        # lookup entirely when there is no recorded rule to load.
        rule = db.session.get(PackagingRule, rule_id) if rule_id else None
        rule = rule or _rule_for(product_id, business_date)
        # The four Vehicle Stock figures are the salesperson's own working
        # numbers: corrected means "today's row carries this figure", default
        # means "the system figure still applies".
        previous = figures["previous"]
        issued = figures["issued"]
        available = figures["total"]
        closing = figures["closing"]
        derived = figures["sold"]
        recorded_qty = recorded.get(product_id, 0)
        variance = None if derived is None else derived - recorded_qty
        result.append({
            "product_id": product_id,
            "product_name": product.name,
            "packaging_rule_id": rule.id,
            "supports_packs": rule.has_pack_tier,
            # Display-only conversion factors so the Daily screen can preview
            # "Sold" from the quantity the driver typed before saving.  The
            # server still recalculates and validates the real figures.
            "pieces_per_carton": rule.pieces_per_carton,
            "pieces_per_pack": rule.pieces_per_pack,
            "opening_source": figures["opening_source"],
            "opening_source_date": figures["opening_source_date"],
            "can_set_opening": day_open and figures["opening_source"] != "previous_closing" and not opening_locked,
            "rule": {
                "cartons_to_packs": rule.cartons_to_packs,
                "packs_to_pieces": rule.packs_to_pieces,
                "pieces_per_carton": rule.pieces_per_carton,
                "pieces_per_pack": rule.pieces_per_pack,
                "has_pack_tier": rule.has_pack_tier,
            },
            # Previous Return / Given Stock / Total Stock / Return Today: the
            # editable Vehicle Stock card, plus the system figure each one
            # falls back to and whether today overrides it.
            "previous_return": quantity_dict(previous, rule),
            "previous_return_default": quantity_dict(figures["previous_derived"], rule),
            "previous_overridden": figures["previous_overridden"],
            "issued_today": quantity_dict(issued, rule),
            "issued_today_default": quantity_dict(figures["issued_derived"], rule),
            "issued_overridden": figures["issued_overridden"],
            "total_stock": quantity_dict(available, rule),
            "total_stock_default": quantity_dict(figures["total_derived"], rule),
            "total_overridden": figures["total_overridden"],
            "return_counted": figures["return_counted"],
            # Kept for every later step, export and historical reader.
            "opening": quantity_dict(previous, rule),
            "dispatch_loaded": quantity_dict(figures["dispatch"], rule),
            "manual_adjustment": quantity_dict(figures["adjustment"], rule),
            "added": quantity_dict(issued, rule),
            "available": quantity_dict(available, rule),
            "closing": quantity_dict(closing, rule),
            "derived_sold": quantity_dict(derived, rule),
            "recorded_sold": quantity_dict(recorded_qty, rule),
            "variance": quantity_dict(variance, rule),
            "reconciled": variance == 0 if variance is not None else False,
        })
    return result


def vehicle_stock_options(profile, business_date):
    """Every active product, with today's system Vehicle Stock figures.

    This is what the Daily screen's Product dropdown is built from.  The rows
    come straight from the product catalogue, so a product added to the
    catalogue later is selectable with no frontend change.  ``on_vehicle`` says
    which products already have a card, so the screen can offer one card per
    product per day.
    """
    business_date = valid_date(business_date)
    dispatch = dispatch_loads(profile, business_date)
    adjustments = adjustment_loads(profile.id, business_date)
    day = saved_day(profile.id, business_date)
    lines = ({line.product_id: line for line in day.closing_lines if line.removed_at is None}
             if day else {})
    on_vehicle = {row["product_id"] for row in stock_reconciliation(profile, business_date)}
    products = []
    for product in Product.query.filter_by(active=True).order_by(Product.display_order, Product.name):
        try:
            rule = _rule_for(product.id, business_date)
        except SalesError:
            # No packaging rule yet, so nothing can be counted in units for it.
            continue
        figures = resolve_vehicle_stock(profile, business_date, product.id, lines.get(product.id),
                                        dispatch, adjustments)
        products.append({
            "id": product.id, "name": product.name,
            "on_vehicle": product.id in on_vehicle,
            "rule": {
                "cartons_to_packs": rule.cartons_to_packs,
                "packs_to_pieces": rule.packs_to_pieces,
                "pieces_per_carton": rule.pieces_per_carton,
                "pieces_per_pack": rule.pieces_per_pack,
                "has_pack_tier": rule.has_pack_tier,
            },
            "units": ["carton", "pack", "piece"] if rule.has_pack_tier else ["carton", "piece"],
            "previous_return": quantity_dict(figures["previous"], rule),
            "issued_today": quantity_dict(figures["issued"], rule),
        })
    return {"business_date": business_date, "products": products}


def invoice_total(invoice):
    return sum(line.line_total_ugx for line in invoice.lines)


def paid_total(invoice, as_of=None):
    q = SalesPayment.query.filter_by(invoice_id=invoice.id, status=ACTIVE)
    if as_of:
        q = q.filter(SalesPayment.payment_date <= valid_date(as_of, "as_of"))
    return sum(p.amount_ugx for p in q.all())


def outstanding(invoice, as_of=None):
    return invoice_total(invoice) - paid_total(invoice, as_of)


def invoice_dict(invoice, as_of=None, include_payments=True):
    data = {
        "id": invoice.id, "reference": invoice.reference, "sales_profile_id": invoice.sales_profile_id,
        "salesperson": invoice.profile.display_name if invoice.profile else None,
        "customer_id": invoice.customer_id, "customer_name": invoice.customer.name if invoice.customer else None,
        "business_date": invoice.business_date, "status": invoice.status,
        "is_opening_receivable": invoice.is_opening_receivable, "notes": invoice.notes,
        "total_ugx": invoice_total(invoice), "paid_ugx": paid_total(invoice),
        "outstanding_ugx": outstanding(invoice),
        "outstanding_as_of_ugx": outstanding(invoice, as_of) if as_of else None,
        "lines": [{
            "id": line.id, "product_id": line.product_id,
            "product_name": line.product.name if line.product else "Opening receivable",
            "quantity": quantity_number(line.quantity), "unit": line.sale_unit,
            "base_unit_qty": quantity_number(line.base_unit_qty),
            "unit_price_ugx": line.unit_price_ugx, "line_total_ugx": line.line_total_ugx,
        } for line in invoice.lines],
    }
    if include_payments:
        data["payments"] = [payment_dict(p) for p in SalesPayment.query.filter_by(invoice_id=invoice.id).order_by(SalesPayment.payment_date, SalesPayment.id)]
    return data


def payment_dict(payment):
    return {
        "id": payment.id, "invoice_id": payment.invoice_id, "payment_date": payment.payment_date,
        "amount_ugx": payment.amount_ugx, "payment_method": payment.payment_method,
        "reference": payment.reference, "notes": payment.notes, "status": payment.status,
    }


def _sum(query):
    return int(query.scalar() or 0)


# ---------------------------------------------------------------------------
# Free-text debtors: the salesperson's own memory of who still owes money.
# These rows never touch the Finished Goods ``customers`` table.
# ---------------------------------------------------------------------------

def debtor_name_key(name):
    return " ".join(str(name or "").split()).casefold()


def clean_debtor_name(value):
    name = " ".join(str(value or "").split())
    if not name:
        raise SalesError("name is required")
    if len(name) > 120:
        raise SalesError("name must be 120 characters or fewer")
    return name


def debtor_dict(debtor):
    return {
        "id": debtor.id, "sales_profile_id": debtor.sales_profile_id, "name": debtor.name,
        "salesperson": debtor.profile.display_name if debtor.profile else None,
    }


def resolve_debtor(profile_id, name, actor):
    clean = clean_debtor_name(name)
    key = debtor_name_key(clean)
    debtor = SalesDebtor.query.filter_by(sales_profile_id=profile_id, name_key=key).first()
    if debtor is None:
        debtor = SalesDebtor(sales_profile_id=profile_id, name=clean, name_key=key, created_by=actor.id)
        db.session.add(debtor)
        db.session.flush()
        record_audit(actor, "create", "sales_debtor", debtor.id, after=debtor_dict(debtor))
    elif debtor.name != clean:
        # Keep the spelling used most recently; the normalized identity never
        # changes, so "Darius"/"darius"/"DARIUS" remain one suggestion.
        debtor.name = clean
    return debtor


def debtor_names(profile_ids=None, limit=50):
    q = SalesDebtor.query
    if profile_ids is not None:
        q = q.filter(SalesDebtor.sales_profile_id.in_(profile_ids))
    rows = q.order_by(SalesDebtor.updated_at.desc(), SalesDebtor.id.desc()).limit(max(1, int(limit))).all()
    seen, names = set(), []
    for row in rows:
        if row.name_key in seen:
            continue
        seen.add(row.name_key)
        names.append(row.name)
    return names


def debtor_totals(profile_ids=None, as_of=None):
    """Owed / paid / outstanding per free-text debtor."""
    as_of = valid_date(as_of, "as_of") if as_of else None
    owed_q = db.session.query(
        SalesDebt.debtor_id, func.sum(SalesDebt.amount_ugx), func.min(SalesDebt.business_date),
    ).filter(SalesDebt.status == ACTIVE)
    paid_q = db.session.query(
        SalesDebtPayment.debtor_id, func.sum(SalesDebtPayment.amount_ugx),
    ).filter(SalesDebtPayment.status == ACTIVE)
    if profile_ids is not None:
        owed_q = owed_q.filter(SalesDebt.sales_profile_id.in_(profile_ids))
        paid_q = paid_q.filter(SalesDebtPayment.sales_profile_id.in_(profile_ids))
    if as_of:
        owed_q = owed_q.filter(SalesDebt.business_date <= as_of)
        paid_q = paid_q.filter(SalesDebtPayment.payment_date <= as_of)
    owed = {debtor_id: (int(amount or 0), first_date)
            for debtor_id, amount, first_date in owed_q.group_by(SalesDebt.debtor_id).all()}
    paid = {debtor_id: int(amount or 0)
            for debtor_id, amount in paid_q.group_by(SalesDebtPayment.debtor_id).all()}
    return {
        debtor_id: {
            "owed_ugx": amount, "paid_ugx": paid.get(debtor_id, 0),
            "outstanding_ugx": amount - paid.get(debtor_id, 0), "first_date": first_date,
        }
        for debtor_id, (amount, first_date) in owed.items()
    }


def debtor_balance(debtor, as_of=None):
    totals = debtor_totals([debtor.sales_profile_id], as_of).get(debtor.id)
    return 0 if totals is None else totals["outstanding_ugx"]


# ---------------------------------------------------------------------------
# Tender split: sales minus unpaid debts minus Mobile Money equals cash.
# ---------------------------------------------------------------------------

def sale_invoices(profile_id, business_date):
    return [i for i in SalesInvoice.query.filter_by(
        sales_profile_id=profile_id, business_date=valid_date(business_date), status=ACTIVE,
    ).all() if not i.is_opening_receivable]


def tender_split(profile_id, business_date, *, day=None):
    """How one day's sales were collected.

    Simplified workflow: everything is treated as cash unless the salesperson
    records a Mobile Money amount for the day or writes an unpaid amount down
    in Debtors.  Legacy rows keep their original per-sale payments and
    receivables semantics, so deployed history reconciles exactly as before
    and nothing is counted twice.

    ``day`` is passed explicitly by Close Day, which is still building the row
    in its own transaction; every other reader gets the stored closed day.
    """
    business_date = valid_date(business_date)
    invoices = sale_invoices(profile_id, business_date)
    total_sales = sum(invoice_total(i) for i in invoices)
    legacy_credit = sum(max(0, outstanding(i, business_date)) for i in invoices if i.customer_id is not None)
    ledger_debt = _sum(db.session.query(func.sum(SalesDebt.amount_ugx)).filter(
        SalesDebt.sales_profile_id == profile_id, SalesDebt.business_date == business_date,
        SalesDebt.kind == DEBT_SALE, SalesDebt.status == ACTIVE,
    ))
    day = day if day is not None else saved_day(profile_id, business_date)
    explicit_mobile = day.mobile_money_from_sales_ugx if day else None
    legacy_mobile = 0
    if explicit_mobile is None:
        legacy_mobile = sum(p.amount_ugx for p in SalesPayment.query.filter_by(
            sales_profile_id=profile_id, payment_date=business_date, status=ACTIVE,
            payment_method="mobile_money",
        ).all() if p.invoice and p.invoice.business_date == business_date)
    mobile = explicit_mobile if explicit_mobile is not None else legacy_mobile
    new_debt = legacy_credit + ledger_debt
    return {
        "business_date": business_date,
        "total_sales_ugx": total_sales,
        "legacy_receivables_ugx": legacy_credit,
        "new_debt_ugx": new_debt,
        "mobile_money_from_sales_ugx": mobile,
        "mobile_money_recorded": explicit_mobile is not None,
        "cash_from_sales_ugx": total_sales - new_debt - mobile,
        "exceeds_total_sales": (new_debt + mobile) > total_sales,
    }


def assert_tender_within_sales(profile_id, business_date, extra_debt=0, extra_mobile=None, day=None):
    split = tender_split(profile_id, business_date, day=day)
    mobile = split["mobile_money_from_sales_ugx"] if extra_mobile is None else whole_ugx(
        extra_mobile, "mobile_money_from_sales_ugx")
    new_debt = split["new_debt_ugx"] + extra_debt
    if new_debt + mobile > split["total_sales_ugx"]:
        raise SalesError(
            "Unpaid debts and Mobile Money cannot exceed Total Sales of "
            f"UGX {split['total_sales_ugx']:,} for {split['business_date']}."
        )
    return split


def debt_collections(profile_id, business_date, account=None):
    """Money received on a date for debts that arose before that date."""
    business_date = valid_date(business_date)
    legacy_q = SalesPayment.query.join(SalesInvoice, SalesInvoice.id == SalesPayment.invoice_id).filter(
        SalesPayment.sales_profile_id == profile_id, SalesPayment.payment_date == business_date,
        SalesPayment.status == ACTIVE, SalesInvoice.business_date < business_date,
        # A sale without a customer is already counted as collected in cash on
        # its own date, so a later row against it must never be counted again.
        SalesInvoice.customer_id.is_not(None),
    )
    ledger_q = SalesDebtPayment.query.filter(
        SalesDebtPayment.sales_profile_id == profile_id, SalesDebtPayment.payment_date == business_date,
        SalesDebtPayment.status == ACTIVE,
    )
    if account:
        legacy_q = legacy_q.filter(SalesPayment.payment_method == account)
        ledger_q = ledger_q.filter(SalesDebtPayment.payment_method == account)
    legacy = sum(p.amount_ugx for p in legacy_q.all())
    ledger = _sum(ledger_q.with_entities(func.sum(SalesDebtPayment.amount_ugx)))
    return legacy + ledger


def _money_collections_between(profile_id, date_from, date_to, account):
    legacy = _sum(db.session.query(func.sum(SalesPayment.amount_ugx)).join(
        SalesInvoice, SalesInvoice.id == SalesPayment.invoice_id,
    ).filter(
        SalesPayment.sales_profile_id == profile_id, SalesPayment.status == ACTIVE,
        SalesPayment.payment_method == account,
        SalesPayment.payment_date >= date_from, SalesPayment.payment_date <= date_to,
        SalesInvoice.business_date < SalesPayment.payment_date,
        SalesInvoice.customer_id.is_not(None),
    ))
    ledger = _sum(db.session.query(func.sum(SalesDebtPayment.amount_ugx)).filter(
        SalesDebtPayment.sales_profile_id == profile_id, SalesDebtPayment.status == ACTIVE,
        SalesDebtPayment.payment_method == account,
        SalesDebtPayment.payment_date >= date_from, SalesDebtPayment.payment_date <= date_to,
    ))
    return legacy + ledger


def _range_tender(profile_id, date_from, date_to):
    """Cash and Mobile Money from sales over a whole range, in a few queries.

    Summed per day this equals:
        Total Sales − unpaid amounts created that day − Mobile Money that day
    because a legacy invoice's unpaid amount is measured as of its own date
    (its same-day payments are already part of the tender).
    """
    in_range = (
        SalesInvoice.business_date >= date_from, SalesInvoice.business_date <= date_to,
    )
    total_sales = _sum(db.session.query(func.sum(SalesInvoiceLine.line_total_ugx)).join(
        SalesInvoice, SalesInvoice.id == SalesInvoiceLine.invoice_id,
    ).filter(
        SalesInvoice.sales_profile_id == profile_id, SalesInvoice.status == ACTIVE,
        SalesInvoice.is_opening_receivable.is_(False), *in_range,
    ))
    legacy_total = _sum(db.session.query(func.sum(SalesInvoiceLine.line_total_ugx)).join(
        SalesInvoice, SalesInvoice.id == SalesInvoiceLine.invoice_id,
    ).filter(
        SalesInvoice.sales_profile_id == profile_id, SalesInvoice.status == ACTIVE,
        SalesInvoice.is_opening_receivable.is_(False),
        SalesInvoice.customer_id.is_not(None), *in_range,
    ))
    legacy_same_day_paid = _sum(db.session.query(func.sum(SalesPayment.amount_ugx)).join(
        SalesInvoice, SalesInvoice.id == SalesPayment.invoice_id,
    ).filter(
        SalesPayment.sales_profile_id == profile_id, SalesPayment.status == ACTIVE,
        SalesInvoice.customer_id.is_not(None),
        SalesPayment.payment_date == SalesInvoice.business_date, *in_range,
    ))
    ledger_debts = _sum(db.session.query(func.sum(SalesDebt.amount_ugx)).filter(
        SalesDebt.sales_profile_id == profile_id, SalesDebt.status == ACTIVE,
        SalesDebt.kind == DEBT_SALE,
        SalesDebt.business_date >= date_from, SalesDebt.business_date <= date_to,
    ))
    explicit_rows = db.session.query(SalesDay.business_date, SalesDay.mobile_money_from_sales_ugx).filter(
        SalesDay.sales_profile_id == profile_id,
        SalesDay.business_date >= date_from, SalesDay.business_date <= date_to,
        SalesDay.mobile_money_from_sales_ugx.is_not(None),
    ).all()
    explicit_dates = {value for value, _amount in explicit_rows}
    explicit_mobile = sum(int(amount or 0) for _value, amount in explicit_rows)
    legacy_mobile = sum(
        amount for amount, invoice_date in db.session.query(
            SalesPayment.amount_ugx, SalesInvoice.business_date,
        ).join(SalesInvoice, SalesInvoice.id == SalesPayment.invoice_id).filter(
            SalesPayment.sales_profile_id == profile_id, SalesPayment.status == ACTIVE,
            SalesPayment.payment_method == "mobile_money",
            SalesPayment.payment_date == SalesInvoice.business_date, *in_range,
        ).all() if invoice_date not in explicit_dates
    )
    mobile = explicit_mobile + legacy_mobile
    cash = total_sales - (legacy_total - legacy_same_day_paid) - ledger_debts - mobile
    return cash, mobile


def money_anchor(profile_id, business_date, account, *, day=None):
    """Where the running money balance starts, and how much it carried.

    A declared starting point always wins.  When the salesperson has not
    declared one yet, the books start at their first Sales Day carrying
    nothing: Cash at Hand is derived from the day's own activity instead of
    waiting for a Manager to unlock it.

    ``day`` is passed while Close Day is still building its own row (a draft in
    an uncommitted transaction); every other reader ignores draft leftovers so
    an abandoned working day can never anchor the books.
    """
    business_date = valid_date(business_date)
    baseline = baseline_for(profile_id, business_date)
    if baseline is not None:
        opening = baseline.opening_cash_ugx if account == "cash" else baseline.opening_mobile_money_ugx
        return baseline.effective_date, int(opening or 0)
    query = db.session.query(func.min(SalesDay.business_date)).filter(
        SalesDay.sales_profile_id == profile_id, SalesDay.business_date <= business_date,
    )
    if day is None:
        query = query.filter(SalesDay.status != DAY_DRAFT)
    first_date = query.scalar()
    if first_date is None:
        return None, 0
    return valid_date(first_date, "business_date"), 0


def money_balance(profile_id, business_date, account, *, upto=None, day=None):
    business_date = valid_date(business_date)
    end = valid_date(upto, "upto") if upto else business_date
    start_date, opening = money_anchor(profile_id, business_date, account, day=day)
    if start_date is None:
        return None
    if end < start_date:
        return opening
    cash_tender, mobile_tender = _range_tender(profile_id, start_date, end)
    tender = cash_tender if account == "cash" else mobile_tender
    collections = _money_collections_between(profile_id, start_date, end, account)
    expenses = _sum(db.session.query(func.sum(SalesExpense.amount_ugx)).filter(
        SalesExpense.sales_profile_id == profile_id, SalesExpense.status == ACTIVE,
        SalesExpense.business_date >= start_date, SalesExpense.business_date <= end,
        SalesExpense.payment_method == account,
    ))
    incoming = _sum(db.session.query(func.sum(SalesMoneyTransfer.amount_ugx)).filter(
        SalesMoneyTransfer.sales_profile_id == profile_id, SalesMoneyTransfer.status == ACTIVE,
        SalesMoneyTransfer.business_date >= start_date, SalesMoneyTransfer.business_date <= end,
        SalesMoneyTransfer.to_account == account,
    ))
    outgoing = _sum(db.session.query(func.sum(SalesMoneyTransfer.amount_ugx)).filter(
        SalesMoneyTransfer.sales_profile_id == profile_id, SalesMoneyTransfer.status == ACTIVE,
        SalesMoneyTransfer.business_date >= start_date, SalesMoneyTransfer.business_date <= end,
        SalesMoneyTransfer.from_account == account,
    ))
    adjustments = _sum(db.session.query(func.sum(SalesCashAdjustment.amount_delta_ugx)).filter(
        SalesCashAdjustment.sales_profile_id == profile_id,
        SalesCashAdjustment.business_date >= start_date,
        SalesCashAdjustment.business_date <= end,
        SalesCashAdjustment.account == account,
    ))
    return opening + tender + collections + incoming - expenses - outgoing + adjustments


def carried_money_balance(profile_id, business_date, account):
    """What Cash / Mobile Money carried INTO a date, before that date's own work.

    An unfinished day is expected to be worked from this figure in the browser,
    so it is what Cash at Hand starts at when the salesperson begins typing.
    """
    business_date = valid_date(business_date)
    previous = (date.fromisoformat(business_date) - timedelta(days=1)).isoformat()
    return money_balance(profile_id, business_date, account, upto=previous)


def daily_summary(profile, business_date, *, day=None):
    business_date = valid_date(business_date)
    day = day if day is not None else saved_day(profile.id, business_date)
    # Nothing exists for a date until Save or Close Day has stored it.  A date
    # with no saved day therefore reports an empty ledger - no invoices, no
    # expenses, no debts, no counted money - instead of whatever an abandoned,
    # never-saved working day may have left in the browser.  The Vehicle Stock
    # context below is still real: it is the carry-forward and Finished Goods
    # load the salesperson must work from.
    invoices = (SalesInvoice.query.filter_by(
        sales_profile_id=profile.id, business_date=business_date, status=ACTIVE).all()
        if day else [])
    split = tender_split(profile.id, business_date, day=day) if day else {
        "business_date": business_date, "total_sales_ugx": 0, "legacy_receivables_ugx": 0,
        "new_debt_ugx": 0, "mobile_money_from_sales_ugx": 0, "mobile_money_recorded": False,
        "cash_from_sales_ugx": 0, "exceeds_total_sales": False,
    }
    total_sales = split["total_sales_ugx"]
    expenses = (SalesExpense.query.filter_by(
        sales_profile_id=profile.id, business_date=business_date, status=ACTIVE).all()
        if day else [])
    total_expenses = sum(e.amount_ugx for e in expenses)
    # Only these two accounts exist, and an expense paid from one never touches
    # the other.  Keeping them apart is what makes Cash at Hand correct.
    cash_expenses = sum(e.amount_ugx for e in expenses if e.payment_method == "cash")
    mobile_expenses = sum(e.amount_ugx for e in expenses if e.payment_method == "mobile_money")
    # Everything collected on this date that belongs to an earlier debt is
    # counted here, on the date it was actually received.
    cash_receipts = split["cash_from_sales_ugx"]
    mobile_receipts = split["mobile_money_from_sales_ugx"]
    old_debt = debt_collections(profile.id, business_date) if day else 0
    old_debt_cash = debt_collections(profile.id, business_date, "cash") if day else 0
    old_debt_mobile = debt_collections(profile.id, business_date, "mobile_money") if day else 0
    new_credit = split["new_debt_ugx"]
    # What the date starts with, before any of today's work.  The browser adds
    # today's working figures to these; a closed day's expected figures below are
    # the closed balances, exactly as they were when the day was closed.
    opening_balances = {
        "cash_ugx": carried_money_balance(profile.id, business_date, "cash"),
        "mobile_money_ugx": carried_money_balance(profile.id, business_date, "mobile_money"),
    }
    expected_cash = (money_balance(profile.id, business_date, "cash", day=day) if day
                     else opening_balances["cash_ugx"])
    expected_mobile = (money_balance(profile.id, business_date, "mobile_money", day=day) if day
                       else opening_balances["mobile_money_ugx"])
    actual_cash = day.actual_cash_count_ugx if day else None
    actual_mobile = day.actual_mobile_money_ugx if day else None
    stock = stock_reconciliation(profile, business_date, day=day)
    stock_variance = any(r["variance"] is not None and r["variance"]["base_unit_qty"] != 0 for r in stock)
    uncounted = any(r["closing"] is None for r in stock)
    # The same three conditions Close Day itself requires (every product
    # counted, the stock count matches the sales entered, cash counted),
    # computed here so a day's status is never guessed from client arithmetic.
    # A day that already passes this is "balanced" whether or not it has been
    # closed yet; one that does not is "unbalanced" even if it was Saved.
    balanced = (not uncounted) and (not stock_variance) and actual_cash is not None
    if day is None:
        status_code = "not_saved"
    elif day.status == DAY_CLOSED:
        status_code = "closed"
    else:
        status_code = "open"      # Saved/open (draft) or reopened for correction
    baseline = baseline_for(profile.id, business_date)
    # The starting return is the salesperson's own entry, never an approval.
    # This block only says whether there is still something for them to type;
    # nothing in it blocks returns, sales, expenses or cash.
    opening_required = baseline is None or any(r["opening"] is None for r in stock)
    opening = {
        "required": opening_required,
        # A closed day is read-only for everyone: the starting stock can only be
        # corrected while the day is still open (reopen needs a Manager).
        "can_declare": (day is None or day.status != DAY_CLOSED) and (
            baseline is None or baseline.effective_date == business_date),
        "started_on": baseline.effective_date if baseline else None,
        "unset_products": [r["product_name"] for r in stock if r["opening"] is None],
    }
    # One card per product: the price rows the salesperson already saved today.
    # Daily screens read this instead of rebuilding totals on the client.
    product_cards = {}
    for invoice in invoices:
        for line in invoice.lines:
            card = product_cards.setdefault(
                line.product_id, {"product_id": line.product_id, "rows": [], "card_total_ugx": 0})
            card["rows"].append({
                "quantity": quantity_number(line.quantity), "unit": line.sale_unit,
                "base_unit_qty": quantity_number(line.base_unit_qty),
                "unit_price_ugx": line.unit_price_ugx,
                "line_total_ugx": line.line_total_ugx,
            })
            card["card_total_ugx"] += line.line_total_ugx
    # Plain-language mismatch warnings: the driver should never have to read a
    # ledger, only "stock count says X, sales entered say Y".
    stock_warnings = [
        f"{r['product_name']}: stock count says {r['derived_sold']['label']} sold, "
        f"sales entered say {r['recorded_sold']['label']} (difference {r['variance']['label']})."
        for r in stock if r["variance"] is not None and r["variance"]["base_unit_qty"] != 0
    ]
    debts = (SalesDebt.query.filter_by(
        sales_profile_id=profile.id, business_date=business_date, status=ACTIVE,
    ).order_by(SalesDebt.id).all() if day else [])
    debt_payments = (SalesDebtPayment.query.filter_by(
        sales_profile_id=profile.id, payment_date=business_date, status=ACTIVE,
    ).order_by(SalesDebtPayment.id).all() if day else [])
    # Outstanding balances for the Debtors card: what is still owed today, from
    # every earlier closed day.  Collections typed on an unfinished day are only
    # in the browser, so nothing here can include them.
    debtor_balances = []
    for debtor_id, totals in debtor_totals([profile.id], as_of=business_date).items():
        if totals["outstanding_ugx"] <= 0:
            continue
        debtor = db.session.get(SalesDebtor, debtor_id)
        if debtor is None:
            continue
        debtor_balances.append({
            "debtor_id": debtor_id, "name": debtor.name,
            "owed_ugx": totals["owed_ugx"], "paid_ugx": totals["paid_ugx"],
            "outstanding_ugx": totals["outstanding_ugx"], "first_date": totals["first_date"],
        })
    debtor_balances.sort(key=lambda row: (row["name"] or "").casefold())

    # Previous descriptions for this salesperson make the expense form's
    # suggestions useful without inventing any values: newest first, unique
    # regardless of capitals, and only ever descriptions this profile itself
    # already recorded.  A cancelled expense still remembers a real description,
    # so voided rows count too: the memory is about the words, not the money.
    seen_descriptions = set()
    expense_suggestions = []
    for (description,) in db.session.query(SalesExpense.description).filter(
        SalesExpense.sales_profile_id == profile.id,
        SalesExpense.business_date <= business_date,
    ).order_by(SalesExpense.business_date.desc(), SalesExpense.id.desc()).limit(200):
        label = (description or "").strip()
        key = label.casefold()
        if not label or key in seen_descriptions:
            continue
        seen_descriptions.add(key)
        expense_suggestions.append(label)
        if len(expense_suggestions) >= 12:
            break
    return {
        "business_date": business_date, "profile": profile_dict(profile),
        "day": None if day is None else {
            "id": day.id, "status": day.status, "notes": day.notes,
            "mobile_money_from_sales_ugx": day.mobile_money_from_sales_ugx,
            "actual_cash_count_ugx": actual_cash, "actual_mobile_money_ugx": actual_mobile,
            "close_override_reason": day.close_override_reason,
        },
        # "not_saved": no row exists yet (browser-only, discarded on leave).
        # "open": Saved (a draft row) or reopened for correction - editable.
        # "closed": Close Day succeeded.  ``balanced`` is the same reconciliation
        # Close Day itself requires, so it is meaningful for an open day too.
        "status_code": status_code,
        "balanced": balanced,
        "stock": stock,
        "stock_warnings": stock_warnings,
        "invoices": [invoice_dict(i, as_of=business_date) for i in invoices],
        "expenses": [{"id": e.id, "description": e.description, "amount_ugx": e.amount_ugx,
                      "payment_method": e.payment_method, "notes": e.notes, "status": e.status} for e in expenses],
        "transfers": [{"id": t.id, "from_account": t.from_account, "to_account": t.to_account,
                       "amount_ugx": t.amount_ugx, "reference": t.reference, "notes": t.notes, "status": t.status}
                      for t in SalesMoneyTransfer.query.filter_by(sales_profile_id=profile.id, business_date=business_date, status=ACTIVE)],
        # The day's actual receipts, each on its own receipt date, so a late
        # payment never rewrites the day it was collected for a previous debt.
        "payments": [{
            "id": p.id, "invoice_id": p.invoice_id,
            "invoice_reference": p.invoice.reference if p.invoice else None,
            "customer_name": p.invoice.customer.name if p.invoice and p.invoice.customer else None,
            "amount_ugx": p.amount_ugx, "payment_method": p.payment_method,
            "reference": p.reference, "notes": p.notes, "status": p.status,
        } for p in SalesPayment.query.filter_by(
            sales_profile_id=profile.id, payment_date=business_date, status=ACTIVE)],
        "debts": [{
            "id": d.id, "debtor_id": d.debtor_id,
            "name": d.debtor.name if d.debtor else None,
            "amount_ugx": d.amount_ugx, "kind": d.kind, "notes": d.notes, "status": d.status,
        } for d in debts],
        "debt_payments": [{
            "id": p.id, "debtor_id": p.debtor_id, "name": p.debtor.name if p.debtor else None,
            "amount_ugx": p.amount_ugx, "payment_method": p.payment_method,
            "reference": p.reference, "notes": p.notes, "status": p.status,
        } for p in debt_payments],
        "debtor_names": debtor_names([profile.id]),
        "totals": {
            "total_sales_ugx": total_sales, "total_expenses_ugx": total_expenses,
            "net_sales_after_expenses_ugx": total_sales - total_expenses,
            "cash_from_sales_ugx": cash_receipts, "cash_receipts_ugx": cash_receipts,
            "mobile_money_from_sales_ugx": mobile_receipts, "mobile_money_receipts_ugx": mobile_receipts,
            "old_debt_collections_ugx": old_debt,
            "old_debt_cash_ugx": old_debt_cash, "old_debt_mobile_money_ugx": old_debt_mobile,
            "new_debt_ugx": new_credit, "new_customer_receivables_ugx": new_credit,
            "expected_cash_ugx": expected_cash, "actual_cash_ugx": actual_cash,
            "cash_variance_ugx": None if expected_cash is None or actual_cash is None else actual_cash - expected_cash,
            "cash_at_hand_ugx": expected_cash,
            "cash_expenses_ugx": cash_expenses, "mobile_money_expenses_ugx": mobile_expenses,
            "expected_mobile_money_ugx": expected_mobile, "actual_mobile_money_ugx": actual_mobile,
            "mobile_variance_ugx": None if expected_mobile is None or actual_mobile is None else actual_mobile - expected_mobile,
            "tender_exceeds_sales": split["exceeds_total_sales"],
            "mobile_money_recorded": split["mobile_money_recorded"],
        },
        "opening": opening,
        "opening_balances": opening_balances,
        "debtor_balances": debtor_balances,
        "product_cards": [product_cards[r["product_id"]] for r in stock if r["product_id"] in product_cards],
        "baseline_required": opening_required,
        "needs_reconciliation": stock_variance,
        "expense_suggestions": expense_suggestions,
    }


def create_invoice(profile, payload, actor, *, opening=False):
    key = str(payload.get("idempotency_key") or "").strip()
    if not key:
        raise SalesError("idempotency_key is required")
    existing = SalesInvoice.query.filter_by(idempotency_key=key).first()
    if existing:
        if existing.sales_profile_id != profile.id:
            raise SalesError("idempotency key is already in use")
        return existing, False
    business_date = valid_date(payload.get("business_date"))
    customer_id = payload.get("customer_id")
    customer = db.session.get(Customer, customer_id) if customer_id else None
    if customer_id and (customer is None or not customer.active or customer.merged_into_id):
        raise SalesError("customer is not available")
    invoice = SalesInvoice(
        reference=f"SAL-{uuid4().hex[:10].upper()}", sales_profile_id=profile.id,
        customer_id=customer.id if customer else None, business_date=business_date,
        notes=(payload.get("notes") or "").strip() or None, idempotency_key=key,
        created_by=actor.id, is_opening_receivable=opening,
    )
    db.session.add(invoice)
    db.session.flush()
    if opening:
        if customer is None:
            raise SalesError("Opening receivable must have a real customer")
        amount = whole_ugx(payload.get("amount_ugx"), "amount_ugx", positive=True)
        invoice.lines.append(SalesInvoiceLine(quantity=1, sale_unit="piece", unit_price_ugx=amount, line_total_ugx=amount))
    else:
        lines = payload.get("lines")
        if not isinstance(lines, list) or not lines:
            raise SalesError("at least one sale line is required")
        for item in lines:
            product = db.session.get(Product, item.get("product_id"))
            if product is None or not product.active:
                raise SalesError("an active product is required")
            rule = _rule_for(product.id, business_date)
            quantity = item.get("quantity")
            unit = item.get("unit")
            base_qty = unit_to_base(quantity, unit, rule)
            price = whole_ugx(item.get("unit_price_ugx"), "unit_price_ugx")
            invoice.lines.append(SalesInvoiceLine(
                product_id=product.id, quantity=quantity, sale_unit=unit, base_unit_qty=base_qty,
                packaging_rule_id=rule.id, unit_price_ugx=price, line_total_ugx=quantity * price,
            ))
    db.session.flush()
    supplied_payments = payload.get("payments") or []
    record_audit(actor, "create", "sales_invoice", invoice.id, after=invoice_dict(invoice, include_payments=False))
    for p in supplied_payments:
        create_payment(invoice, p, actor, default_date=business_date, allow_batch_partial=True)
    # An ordinary paid sale needs no customer and no per-sale payment split.
    # Unpaid amounts are recorded separately in the Debtors workspace, so a
    # sale without a customer never becomes a customer receivable.
    return invoice, True


def create_payment(invoice, payload, actor, default_date=None, allow_batch_partial=False):
    key = str(payload.get("idempotency_key") or "").strip()
    if not key:
        raise SalesError("payment idempotency_key is required")
    existing = SalesPayment.query.filter_by(idempotency_key=key).first()
    if existing:
        if existing.invoice_id != invoice.id:
            raise SalesError("idempotency key is already in use")
        return existing, False
    if invoice.status != ACTIVE:
        raise SalesError("Cannot pay a void invoice")
    method = payload.get("payment_method")
    if method not in PAYMENT_METHODS:
        raise SalesError(f"payment_method must be one of {PAYMENT_METHODS}")
    amount = whole_ugx(payload.get("amount_ugx"), "amount_ugx", positive=True)
    # Re-query under the same SQLite write transaction immediately before insert.
    # SQLite serializes writers; this prevents two committed allocations exceeding
    # the invoice even when requests race.
    remaining = outstanding(invoice)
    if amount > remaining:
        raise SalesError(f"Payment exceeds the outstanding balance by UGX {amount - remaining:,}.")
    if invoice.customer_id is None and amount < remaining and not allow_batch_partial:
        raise SalesError("A customer is required before leaving any amount on credit")
    payment_date = valid_date(payload.get("payment_date") or default_date, "payment_date")
    if payment_date < invoice.business_date:
        raise SalesError("payment_date cannot be before the invoice date")
    payment = SalesPayment(
        invoice_id=invoice.id, customer_id=invoice.customer_id, sales_profile_id=invoice.sales_profile_id,
        payment_date=payment_date,
        amount_ugx=amount, payment_method=method,
        reference=(payload.get("reference") or "").strip() or None,
        notes=(payload.get("notes") or "").strip() or None, idempotency_key=key, entered_by=actor.id,
    )
    db.session.add(payment)
    db.session.flush()
    record_audit(actor, "create", "sales_payment", payment.id, after=payment_dict(payment))
    return payment, True


def _aging_bucket(age):
    return "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "90+"


def create_debt(profile, payload, actor, *, kind=DEBT_SALE, day=None, enforce_tender=True):
    """Record an unpaid amount against a free-text debtor name.

    ``enforce_tender=False`` skips the check that this debt (with every other
    debt and Mobile Money amount already counted today) cannot exceed Total
    Sales.  Save uses this: sales may still be partly unpriced when a debt is
    typed, so that check can only be authoritative at Close Day.
    """
    if kind not in DEBT_KINDS:
        raise SalesError(f"kind must be one of {DEBT_KINDS}")
    key = str(payload.get("idempotency_key") or "").strip()
    if not key:
        raise SalesError("idempotency_key is required")
    existing = SalesDebt.query.filter_by(idempotency_key=key).first()
    if existing:
        if existing.sales_profile_id != profile.id:
            raise SalesError("idempotency key is already in use")
        return existing, False
    business_date = valid_date(payload.get("business_date") or business_today())
    amount = whole_ugx(payload.get("amount_ugx"), "amount_ugx", positive=True)
    if kind == DEBT_SALE and enforce_tender:
        # The sale is already counted; the debt may only explain money that was
        # not collected in cash today.  It can never create extra sales.
        assert_tender_within_sales(profile.id, business_date, extra_debt=amount, day=day)
    debtor = resolve_debtor(profile.id, payload.get("name"), actor)
    debt = SalesDebt(
        debtor_id=debtor.id, sales_profile_id=profile.id, business_date=business_date, kind=kind,
        amount_ugx=amount, notes=(payload.get("notes") or "").strip() or None,
        idempotency_key=key, created_by=actor.id,
    )
    db.session.add(debt)
    db.session.flush()
    record_audit(actor, "create", "sales_debt", debt.id, after={
        "debtor": debtor.name, "amount_ugx": amount, "kind": kind, "business_date": business_date,
    })
    return debt, True


def record_debt_payment(debtor, payload, actor, default_date=None):
    key = str(payload.get("idempotency_key") or "").strip()
    if not key:
        raise SalesError("idempotency_key is required")
    existing = SalesDebtPayment.query.filter_by(idempotency_key=key).first()
    if existing:
        if existing.debtor_id != debtor.id:
            raise SalesError("idempotency key is already in use")
        return existing, False
    method = payload.get("payment_method")
    if method not in PAYMENT_METHODS:
        raise SalesError(f"payment_method must be one of {PAYMENT_METHODS}")
    amount = whole_ugx(payload.get("amount_ugx"), "amount_ugx", positive=True)
    balance = debtor_balance(debtor)
    if amount > balance:
        raise SalesError(f"Payment exceeds the outstanding balance by UGX {amount - balance:,}.")
    payment_date = valid_date(payload.get("payment_date") or default_date or business_today(), "payment_date")
    first_date = (debtor_totals([debtor.sales_profile_id]).get(debtor.id) or {}).get("first_date")
    if first_date and payment_date < first_date:
        raise SalesError("payment_date cannot be before the debt date")
    payment = SalesDebtPayment(
        debtor_id=debtor.id, sales_profile_id=debtor.sales_profile_id, payment_date=payment_date,
        amount_ugx=amount, payment_method=method,
        reference=(payload.get("reference") or "").strip() or None,
        notes=(payload.get("notes") or "").strip() or None,
        idempotency_key=key, entered_by=actor.id,
    )
    db.session.add(payment)
    db.session.flush()
    record_audit(actor, "create", "sales_debt_payment", payment.id, after={
        "debtor": debtor.name, "amount_ugx": amount, "payment_method": method, "payment_date": payment_date,
    })
    return payment, True


def debtors(profile_ids=None, as_of=None):
    as_of_date = valid_date(as_of, "as_of") if as_of else business_today()
    as_of_value = date.fromisoformat(as_of_date)
    rows = []
    # The salesperson's own free-text debts (no Customer record required).
    totals = debtor_totals(profile_ids, as_of_date)
    if totals:
        q = SalesDebtor.query.filter(SalesDebtor.id.in_(list(totals)))
        if profile_ids is not None:
            q = q.filter(SalesDebtor.sales_profile_id.in_(profile_ids))
        for debtor in q.order_by(SalesDebtor.name).all():
            balance = totals.get(debtor.id)
            if not balance or balance["outstanding_ugx"] <= 0:
                continue
            first_date = balance["first_date"] or as_of_date
            age = max(0, (as_of_value - date.fromisoformat(first_date)).days)
            rows.append({
                "id": debtor.id, "kind": "debt", "debtor_id": debtor.id,
                "name": debtor.name, "customer_name": debtor.name,
                "reference": "Unpaid", "business_date": first_date,
                "salesperson": debtor.profile.display_name if debtor.profile else None,
                "profile_id": debtor.sales_profile_id,
                "total_ugx": balance["owed_ugx"], "paid_ugx": balance["paid_ugx"],
                "outstanding_ugx": balance["outstanding_ugx"],
                "days_outstanding": age, "aging_bucket": _aging_bucket(age),
            })
    # Customer-linked credit created by the first implementation stays readable
    # and collectable exactly as before (backward compatibility).
    q = SalesInvoice.query.filter(SalesInvoice.status == ACTIVE, SalesInvoice.customer_id.is_not(None))
    if profile_ids is not None:
        q = q.filter(SalesInvoice.sales_profile_id.in_(profile_ids))
    for invoice in q.order_by(SalesInvoice.business_date, SalesInvoice.id).all():
        balance = outstanding(invoice, as_of)
        if balance <= 0:
            continue
        row = invoice_dict(invoice, as_of=as_of)
        age = max(0, (as_of_value - date.fromisoformat(invoice.business_date)).days)
        row["days_outstanding"] = age
        row["aging_bucket"] = _aging_bucket(age)
        row["kind"] = "invoice"
        row["debtor_id"] = None
        row["name"] = row.get("customer_name")
        row["profile_id"] = invoice.sales_profile_id
        rows.append(row)
    rows.sort(key=lambda item: (item["business_date"], item["id"]))
    return rows


def save_vehicle_stock(day, lines, actor, elevated=False, allow_new=True):
    """Save the salesperson's own Vehicle Stock sheet for one day.

    Each line is one product card: the unit it was counted in, plus Previous
    Return, Given Stock, Total Stock and Return Today.  All four are the
    salesperson's working figures and any of them may be corrected - it is
    their own vehicle to reconcile, so no Manager approval is involved.

    A correction is stored on TODAY's row (and audited).  Earlier days are read
    from their own rows, so correcting today's Previous Return never rewrites
    yesterday's counted return.

    A product may be put on the vehicle here for the first time, and any active
    product from the existing catalogue is eligible: a product added to the
    catalogue later needs no frontend change.  ``allow_new=False`` keeps the
    older single-product endpoint's rule that Return Today may only be saved
    for a product the vehicle already has.
    """
    if day.status == DAY_CLOSED:
        raise SalesError("A closed Sales Day must be reopened before it can be edited")
    if not isinstance(lines, list) or not lines:
        raise SalesError("Add at least one product to the vehicle stock.")
    business_date = day.business_date
    on_vehicle = {row["product_id"] for row in stock_reconciliation(day.profile, business_date)}
    dispatch = dispatch_loads(day.profile, business_date)
    adjustments = adjustment_loads(day.profile.id, business_date)
    rows = {row.product_id: row for row in SalesVehicleClosingLine.query.filter_by(sales_day_id=day.id).all()}
    # Validate every card before anything is written, so one bad line cannot
    # leave half the sheet saved.
    prepared, seen = [], set()
    for item in lines:
        if not isinstance(item, dict):
            raise SalesError("Each vehicle stock card needs a product and a quantity.")
        product = db.session.get(Product, item.get("product_id"))
        if product is None:
            raise SalesError("product not found")
        if not product.active:
            raise SalesError(f"{product.name} is not an active product.")
        if product.id in seen:
            # One card per product: two cards would split the same stock and
            # the driver could never tell which one counts.
            raise SalesError(f"{product.name} is already on the vehicle stock. Keep one card per product.")
        seen.add(product.id)
        if not allow_new and product.id not in on_vehicle:
            raise SalesError(f"{product.name} is not part of this vehicle's stock. "
                             "Add it to Vehicle stock first.")
        rule = _rule_for(product.id, business_date)
        unit = item.get("unit") or "carton"
        if unit not in SALE_UNITS:
            raise SalesError(f"unit must be one of {SALE_UNITS}")
        row = rows.get(product.id)
        # Older callers send quantity/unit (Return Today); the Vehicle Stock
        # sheet sends all four figures explicitly.
        previous = optional_base(item.get("previous_qty"), item.get("previous_unit") or unit, rule,
                                 "Previous Return")
        issued = optional_base(item.get("issued_qty"), item.get("issued_unit") or unit, rule, "Given Stock")
        total = optional_base(item.get("total_qty"), item.get("total_unit") or unit, rule, "Total Stock")
        returned = optional_base(item.get("return_qty", item.get("quantity")),
                                 item.get("return_unit") or unit, rule, "Return Today")
        figures = resolve_vehicle_stock(day.profile, business_date, product.id, row, dispatch, adjustments)
        previous_working = figures["previous"] if previous is None else previous
        if previous_working is None:
            # Zero is never silently assumed for a vehicle that has never been
            # counted: the salesperson says what they started with.
            raise SalesError(f"Enter the starting stock (Previous Return) for {product.name} first: "
                             "nothing has been counted for it on an earlier day.")
        issued_working = figures["issued"] if issued is None else issued
        total_working = total if total is not None else previous_working + issued_working
        if returned is not None and returned > total_working:
            raise SalesError(f"{product.name}: Return Today cannot exceed Total Stock "
                             f"({_quantity_label(total_working, rule)}).")
        prepared.append({
            "product": product, "rule": rule, "row": row,
            "previous": previous, "issued": issued, "total": total, "returned": returned,
            "previous_default": figures["previous_derived"],
            "issued_default": figures["issued_derived"],
            "total_default": previous_working + issued_working,
            "reason": (item.get("reason") or "").strip(),
        })
    saved = []
    for card in prepared:
        row, rule = card["row"], card["rule"]
        # Keep a figure on "follow the system value" while it still equals it,
        # so a corrected earlier day keeps carrying forward.  A different
        # number is today's own correction and is stored as such.
        previous_store = (None if card["previous"] is None or card["previous"] == card["previous_default"]
                          else card["previous"])
        issued_store = (None if card["issued"] is None or card["issued"] == card["issued_default"]
                        else card["issued"])
        total_store = (None if card["total"] is None or card["total"] == card["total_default"]
                       else card["total"])
        counted = card["returned"] is not None
        base_qty = card["returned"] or 0
        before = None if row is None else {
            "base_unit_qty": row.base_unit_qty,
            "previous_return_base_qty": row.previous_return_base_qty,
            "issued_base_qty": row.issued_base_qty,
            "total_stock_base_qty": row.total_stock_base_qty,
            "return_counted": row.return_counted,
        }
        unchanged = row is not None and (
            row.base_unit_qty == base_qty and row.return_counted == counted
            and row.previous_return_base_qty == previous_store
            and row.issued_base_qty == issued_store
            and row.total_stock_base_qty == total_store
        )
        if unchanged:
            saved.append(row)
            continue
        if row is None:
            row = SalesVehicleClosingLine(sales_day_id=day.id, product_id=card["product"].id,
                                          base_unit_qty=base_qty, packaging_rule_id=rule.id)
            db.session.add(row)
        else:
            if day.status != DAY_DRAFT and not elevated:
                raise SalesError("A closed Sales Day must be reopened by a Manager or Super Administrator")
            # The four Vehicle Stock figures belong to the salesperson: on an
            # open day correcting them is ordinary work, not a correction that
            # needs a Manager verdict.  A reason is still recorded when one is
            # supplied (reopened/administrative work keeps its audit trail),
            # and a closed day is refused above.
            if card["reason"]:
                row.corrected_by = actor.id
                row.correction_reason = card["reason"]
        row.base_unit_qty = base_qty
        row.return_counted = counted
        row.previous_return_base_qty = previous_store
        row.issued_base_qty = issued_store
        row.total_stock_base_qty = total_store
        row.packaging_rule_id = rule.id
        # Writing the line again - a correction on a reopened day - puts it back
        # on the vehicle.  The removal it carried stays in the audit trail.
        row.removed_at = None
        row.removed_by = None
        row.removal_reason = None
        db.session.flush()
        record_audit(actor, "correct" if before else "create", "sales_vehicle_closing", row.id,
                     before=before,
                     after={"base_unit_qty": base_qty,
                            "previous_return_base_qty": previous_store,
                            "issued_base_qty": issued_store,
                            "total_stock_base_qty": total_store,
                            "return_counted": counted,
                            "reason": card["reason"] or None})
        saved.append(row)
    return saved


def set_closing_stock(day, payload, actor, elevated=False):
    """Return Today for one product, through the same Vehicle Stock path.

    Kept for the existing single-product and returns-today endpoints; the Daily
    screen saves the whole sheet with ``save_vehicle_stock``.  An older caller
    only ever supplies quantity/unit, so Return Today is always counted here.
    """
    quantity = payload.get("quantity")
    line = dict(payload)
    line["return_qty"] = quantity if quantity not in (None, "") else 0
    line["return_unit"] = payload.get("unit") or payload.get("return_unit") or "carton"
    line.setdefault("unit", payload.get("unit") or "carton")
    return save_vehicle_stock(day, [line], actor, elevated=elevated, allow_new=False)[0]


def save_returns_today(day, lines, actor, elevated=False):
    """Save today's counted return for several products in one action.

    The daily screen counts the whole vehicle at once, so one click must be
    able to record every line.  Each line still goes through the same
    validation as a single save.
    """
    if not isinstance(lines, list) or not lines:
        raise SalesError("Enter the return today for at least one product.")
    saved = []
    for item in lines:
        if not isinstance(item, dict):
            raise SalesError("Each return line needs a product and a quantity.")
        saved.append(set_closing_stock(day, item, actor, elevated=elevated))
    return saved


def set_opening_return(profile, business_date, product_id, quantity, unit, actor):
    """The salesperson's own starting stock, entered on the first day.

    This is data entry, not an approval: whoever holds the stock declares what
    they are starting with.  It is only allowed while no earlier day has
    carried a counted return forward, because from then on the previous
    closing is authoritative.
    """
    business_date = valid_date(business_date)
    product = db.session.get(Product, product_id)
    if product is None or not product.active:
        raise SalesError("an active product is required")
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
    if day is None:
        raise SalesError("Create the Sales Day first.")
    if day.status == DAY_CLOSED:
        raise SalesError("A closed Sales Day must be reopened before it can be edited")
    baseline = baseline_for(profile.id, business_date)
    if baseline is not None and baseline.effective_date != business_date:
        raise SalesError(
            f"The starting stock was declared on {baseline.effective_date} and carries forward. "
            "Correct that day's counted return instead."
        )
    prior = (
        db.session.query(SalesVehicleClosingLine.id)
        .join(SalesDay, SalesDay.id == SalesVehicleClosingLine.sales_day_id)
        .filter(
            SalesDay.sales_profile_id == profile.id,
            SalesDay.business_date < business_date,
            SalesVehicleClosingLine.product_id == product.id,
        )
        .first()
    )
    if prior:
        raise SalesError(f"{product.name} already carries a counted return forward from an earlier day.")
    if recorded_quantities(profile.id, business_date).get(product.id, 0):
        raise SalesError(f"Sales are already recorded for {product.name}. "
                         "Void them before changing its starting stock.")
    rule = _rule_for(product.id, business_date)
    base_qty = 0 if quantity in (None, "", 0) else unit_to_base(quantity, unit, rule)
    if baseline is None:
        baseline = SalesBaseline(
            sales_profile_id=profile.id, effective_date=business_date,
            opening_cash_ugx=0, opening_mobile_money_ugx=0,
            notes="Starting stock declared by the salesperson", created_by=actor.id,
        )
        db.session.add(baseline)
        db.session.flush()
    line = SalesOpeningStockLine.query.filter_by(baseline_id=baseline.id, product_id=product.id).first()
    before = None if line is None else {"base_unit_qty": line.base_unit_qty}
    if line is None:
        line = SalesOpeningStockLine(baseline_id=baseline.id, product_id=product.id,
                                     base_unit_qty=base_qty, packaging_rule_id=rule.id)
        db.session.add(line)
    else:
        line.base_unit_qty = base_qty
        line.packaging_rule_id = rule.id
    db.session.flush()
    record_audit(actor, "correct" if before else "create", "sales_opening_stock", line.id,
                 before=before,
                 after={"base_unit_qty": base_qty, "business_date": business_date,
                        "entered_by_role": getattr(actor, "role", None)})
    return line



def product_sales_rows(profile_id, business_date, product_id):
    """Active sale invoices for one date that price this product."""
    return [
        invoice for invoice in sale_invoices(profile_id, business_date)
        if any(line.product_id == product_id for line in invoice.lines)
    ]


def live_vehicle_stock(profile, business_date, lines):
    """Sold Today taken from the Vehicle Stock sheet on the salesperson's screen.

    A sales card has to price exactly the Sold Today the salesperson can see, but
    the sheet is stored once - when the day is closed - so a card saved during
    the day cannot read it back from the database.  This reads the submitted
    figures the same way ``resolve_vehicle_stock`` reads a saved row, with the
    same fallbacks, the same rounding and the same refusals, and writes nothing
    at all.

    Returns ``{product_id: {"rule", "total", "returned", "sold"}}`` in base units.
    """
    business_date = valid_date(business_date)
    if not isinstance(lines, list) or not lines:
        raise SalesError("Add at least one product to the vehicle stock.")
    dispatch = dispatch_loads(profile, business_date)
    adjustments = adjustment_loads(profile.id, business_date)
    day = saved_day(profile.id, business_date)
    stored = ({line.product_id: line for line in day.closing_lines if line.removed_at is None}
              if day else {})
    figures = {}
    for item in lines:
        if not isinstance(item, dict):
            raise SalesError("Each vehicle stock card needs a product and a quantity.")
        product = db.session.get(Product, item.get("product_id"))
        if product is None:
            raise SalesError("product not found")
        if product.id in figures:
            # One card per product: two cards would split the same stock.
            raise SalesError(f"{product.name} is already on the vehicle stock. Keep one card per product.")
        rule = _rule_for(product.id, business_date)
        unit = item.get("unit") or "carton"
        if unit not in SALE_UNITS:
            raise SalesError(f"unit must be one of {SALE_UNITS}")
        previous = optional_base(item.get("previous_qty"), item.get("previous_unit") or unit, rule,
                                 "Previous Return")
        issued = optional_base(item.get("issued_qty"), item.get("issued_unit") or unit, rule,
                               "Given Stock")
        total = optional_base(item.get("total_qty"), item.get("total_unit") or unit, rule,
                              "Total Stock")
        returned = optional_base(item.get("return_qty", item.get("quantity")),
                                 item.get("return_unit") or unit, rule, "Return Today")
        # A blank figure means "the day's own row still applies", exactly as
        # save_vehicle_stock() treats it, so a half-typed sheet is validated
        # against the same numbers it will be stored with.
        working = resolve_vehicle_stock(profile, business_date, product.id,
                                        stored.get(product.id), dispatch, adjustments)
        previous_working = working["previous"] if previous is None else previous
        if previous_working is None:
            raise SalesError(f"Enter the starting stock (Previous Return) for {product.name} first: "
                             "nothing has been counted for it on an earlier day.")
        issued_working = working["issued"] if issued is None else issued
        total_working = previous_working + issued_working if total is None else total
        if returned is not None and returned > total_working:
            raise SalesError(f"{product.name}: Return Today cannot exceed Total Stock "
                             f"({_quantity_label(total_working, rule)}).")
        figures[product.id] = {
            "rule": rule, "total": total_working, "returned": returned,
            "sold": None if returned is None else total_working - returned,
        }
    return figures


def save_product_sales(profile, business_date, product_id, rows, actor, vehicle_stock=None,
                       require_balance=True):
    """Save one product's sales card as a set of price rows.

    The card is authoritative for one product on one day.  It is replaced as a
    whole, so re-saving the same numbers changes nothing and an edited card can
    never be counted twice.

    ``require_balance=True`` (Close Day) still requires the rows to add up to
    the stock sold exactly: an under- or over-priced card is refused with the
    difference spelled out rather than silently balanced.  ``require_balance=
    False`` (Save) skips that comparison - and the requirement that Sold Today
    even be known yet - so an unfinished card is simply stored as it stands;
    only the shape of each row (a valid unit, a positive quantity, a positive
    price) is still checked either way.

    ``vehicle_stock`` is the Vehicle Stock sheet the salesperson can see, sent
    by the Sales screen so the card is priced against the Sold Today on screen
    rather than a saved figure.  It is validated and never stored: Close Day is
    the one action that writes the sheet, and it re-checks this card against the
    figures it stores then.
    """
    business_date = valid_date(business_date)
    product = db.session.get(Product, product_id)
    if product is None or not product.active:
        raise SalesError("an active product is required")
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
    if day is None:
        raise SalesError("Create the Sales Day first.")
    if day.status == DAY_CLOSED:
        raise SalesError("A closed Sales Day must be reopened before it can be edited")
    sold = None
    if vehicle_stock is not None:
        # The Sales screen sends the sheet it is showing, so the card is priced
        # against the Sold Today on screen.  Nothing is written here: Close Day
        # stores the sheet and re-checks this card against those figures, so the
        # rule that a card must price exactly what was sold is never weakened.
        live = live_vehicle_stock(profile, business_date, vehicle_stock).get(product.id)
        if live is None:
            raise SalesError(f"{product.name} is not part of this vehicle's stock for {business_date}.")
        sold, rule = live["sold"], live["rule"]
        if sold is None and require_balance:
            raise SalesError(f"Enter the return today for {product.name} first, "
                             "then its sold quantity is known.")
    else:
        rec = next((r for r in stock_reconciliation(profile, business_date)
                    if r["product_id"] == product.id), None)
        if rec is None:
            raise SalesError(f"{product.name} is not part of this vehicle's stock for {business_date}.")
        if rec["available"] is None:
            raise SalesError(f"Save the starting stock for {product.name} first.")
        rule = (db.session.get(PackagingRule, rec["packaging_rule_id"])
                if rec["packaging_rule_id"] else None) or _rule_for(product.id, business_date)
        if rec["derived_sold"] is None:
            if require_balance:
                raise SalesError(f"Save the return today for {product.name} first, then its sold quantity is known.")
        else:
            sold = rec["derived_sold"]["base_unit_qty"]
    if not isinstance(rows, list):
        raise SalesError("rows must be a list of price rows")
    parsed = []
    priced = Decimal(0)
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            raise SalesError(f"Row {index} is not a valid price row.")
        unit = item.get("unit")
        if unit not in SALE_UNITS:
            raise SalesError(f"Row {index}: unit must be one of {SALE_UNITS}.")
        # A row may price a fraction of a unit (0.5 cartons), so the quantity is
        # read as an exact Decimal and every comparison below stays exact.
        try:
            quantity = as_quantity(item.get("quantity"), "quantity", positive=True)
        except SalesError as exc:
            raise SalesError(f"Row {index}: {exc}")
        base_qty = sales_row_base(quantity, unit, rule)
        price = whole_ugx(item.get("unit_price_ugx"), f"Row {index} selling price")
        if price <= 0:
            raise SalesError(f"Row {index}: the selling price must be greater than zero.")
        priced += base_qty
        parsed.append({"product_id": product.id, "quantity": quantity,
                       "unit": unit, "unit_price_ugx": price})
    if require_balance:
        sold = Decimal(str(sold))
        if priced != sold:
            if priced < sold:
                raise SalesError(
                    f"{product.name}: the rows price {_quantity_label(priced, rule)} "
                    f"but {_quantity_label(sold, rule)} were sold. "
                    f"Add another row for the remaining {_quantity_label(sold - priced, rule)}."
                )
            raise SalesError(
                f"{product.name}: the rows price {_quantity_label(priced, rule)} "
                f"but only {_quantity_label(sold, rule)} were sold. "
                f"Remove {_quantity_label(priced - sold, rule)}."
            )
    existing = product_sales_rows(profile.id, business_date, product.id)
    signature = [(p["quantity"], p["unit"], p["unit_price_ugx"]) for p in parsed]
    if len(existing) == 1:
        current = [(line.quantity, line.sale_unit, line.unit_price_ugx) for line in existing[0].lines]
        if current == signature:
            return existing[0], "unchanged"
    for invoice in existing:
        if any(line.product_id != product.id for line in invoice.lines):
            raise SalesError(
                f"{product.name}: sale {invoice.reference} also prices another product. "
                "Void that sale first, then save this product's card."
            )
        void_record(invoice, actor, f"Replaced by the {product.name} sales card for {business_date}")
    if not parsed:
        db.session.flush()
        record_audit(actor, "update", "sales_card", day.id,
                     before={"product_id": product.id, "business_date": business_date},
                     after={"product_id": product.id, "business_date": business_date, "rows": []})
        return None, "cleared"
    invoice = SalesInvoice(
        reference=f"SAL-{uuid4().hex[:10].upper()}", sales_profile_id=profile.id,
        business_date=business_date, notes=f"{product.name} sales card",
        idempotency_key=f"card-{profile.id}-{business_date}-{product.id}-{uuid4().hex[:8]}",
        created_by=actor.id, is_opening_receivable=False,
    )
    db.session.add(invoice)
    db.session.flush()
    for p in parsed:
        base_qty = sales_row_base(p["quantity"], p["unit"], rule)
        invoice.lines.append(SalesInvoiceLine(
            product_id=product.id, quantity=p["quantity"], sale_unit=p["unit"],
            base_unit_qty=base_qty, packaging_rule_id=rule.id,
            unit_price_ugx=p["unit_price_ugx"],
            line_total_ugx=line_total_ugx(p["quantity"], p["unit_price_ugx"]),
        ))
    db.session.flush()
    record_audit(actor, "create", "sales_card", invoice.id,
                 after={"product_id": product.id, "business_date": business_date, "rows": parsed,
                        "card_total_ugx": invoice_total(invoice)})
    return invoice, "created"


def save_sales_cards(profile, business_date, cards, actor, vehicle_stock=None, require_balance=True):
    """Store every sales card the salesperson is holding, in this transaction.

    Save and Close Day both send the complete working state this way: the
    Vehicle stock sheet plus one card per product.  Each card is priced by the
    same ``save_product_sales()`` path, against the same submitted sheet, so
    the rule that a card must price exactly Sold Today (``require_balance``)
    is unchanged for Close Day and relaxed only for Save.  Nothing here
    commits: the caller's transaction commits it all or nothing.
    """
    if cards is None:
        return []
    if not isinstance(cards, list):
        raise SalesError("sales_cards must be a list of sales cards")
    seen = set()
    saved = []
    for item in cards:
        if not isinstance(item, dict):
            raise SalesError("Each sales card needs a product and its price rows.")
        product = db.session.get(Product, item.get("product_id"))
        if product is None:
            raise SalesError("product not found")
        if product.id in seen:
            raise SalesError(f"{product.name} appears twice in the sales cards. Keep one card per product.")
        seen.add(product.id)
        rows = item.get("rows") or []
        if not isinstance(rows, list):
            raise SalesError(f"{product.name}: rows must be a list of price rows.")
        invoice, action = save_product_sales(profile, business_date, product.id, rows, actor,
                                             vehicle_stock=vehicle_stock, require_balance=require_balance)
        saved.append({"product_id": product.id, "action": action,
                      "invoice_id": invoice.id if invoice else None})
    return saved


def close_day(day, actor, override_reason=None, elevated=False):
    # The close validates the day it is about to store, so it reads the row it is
    # still building rather than waiting for it to be marked closed.
    summary = daily_summary(day.profile, day.business_date, day=day)
    uncounted = [r for r in summary["stock"] if r["closing"] is None]
    if uncounted:
        raise SalesError(f"Enter the return today for {uncounted[0]['product_name']} before closing.")
    # Accountability is the point of the day close: a normal Operator must
    # count the cash in hand first.  Expected cash stays ledger-derived.
    if day.actual_cash_count_ugx is None and not (elevated and (override_reason or "").strip()):
        raise SalesError("Enter the cash you counted (Cash Counted) before closing the day.")
    mismatches = [r for r in summary["stock"] if r["variance"] and r["variance"]["base_unit_qty"] != 0]
    if mismatches and not (elevated and (override_reason or "").strip()):
        first = mismatches[0]
        raise SalesError(
            f"{first['product_name']}: stock says {first['derived_sold']['label']} sold, sales entered say "
            f"{first['recorded_sold']['label']} (difference {first['variance']['label']}). "
            "Correct the count or the sales before closing."
        )
    day.status = DAY_CLOSED
    day.closed_by = actor.id
    day.closed_at = utcnow()
    day.close_override_reason = (override_reason or "").strip() or None
    record_audit(actor, "close", "sales_day", day.id, after={"business_date": day.business_date, "override_reason": day.close_override_reason})


def void_record(record, actor, reason):
    reason = (reason or "").strip()
    if not reason:
        raise SalesError("reason is required")
    if getattr(record, "status", None) == VOID:
        return record
    before = {"status": record.status}
    record.status = VOID
    record.voided_by = actor.id
    record.voided_at = utcnow()
    record.void_reason = reason
    record_audit(actor, "void", record.__tablename__, record.id, before=before, after={"status": VOID, "reason": reason})
    return record


def remove_vehicle_line(day, product_id, actor, reason):
    """Take a product off a CLOSED day without destroying the history.

    The counted row stays exactly as it was recorded; it is marked removed with
    who, when and why, and the audit trail keeps the before/after.  Everything
    that reads the day - stock reconciliation, carry-forward, the totals - skips
    a removed line, so the closed day recalculates as if that product had never
    been on the vehicle, while the evidence of the correction remains.
    """
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise SalesError("A reason is required to remove a product from a closed day.")
    if day.status == DAY_DRAFT:
        raise DayNotSaved("Nothing is saved before Close Day, so there is nothing to remove.")
    line = SalesVehicleClosingLine.query.filter_by(sales_day_id=day.id, product_id=product_id).first()
    if line is None:
        raise SalesError("That product is not on this day's vehicle stock.")
    if line.removed_at is not None:
        return line
    before = {"base_unit_qty": line.base_unit_qty, "return_counted": line.return_counted,
              "removed_at": None}
    line.removed_at = utcnow()
    line.removed_by = actor.id
    line.removal_reason = reason
    db.session.flush()
    record_audit(actor, "remove", "sales_vehicle_closing", line.id, before=before,
                 after={"base_unit_qty": line.base_unit_qty, "return_counted": line.return_counted,
                        "removed_at": line.removed_at.isoformat(), "reason": reason})
    return line


def create_expense(day, payload, actor):
    """One expense row for the day being closed."""
    description = " ".join(str(payload.get("description") or "").split())
    if not description:
        raise SalesError("Every expense needs a description.")
    if len(description) > 160:
        raise SalesError("Expense descriptions must be 160 characters or fewer.")
    method = payload.get("payment_method") or "cash"
    if method not in ("cash", "mobile_money", "other"):
        raise SalesError("invalid expense payment method")
    amount = whole_ugx(payload.get("amount_ugx"), "amount_ugx", positive=True)
    row = SalesExpense(
        sales_profile_id=day.sales_profile_id, business_date=day.business_date,
        description=description, amount_ugx=amount, payment_method=method,
        notes=(payload.get("notes") or "").strip() or None,
        idempotency_key=str(payload.get("idempotency_key") or f"close-{uuid4().hex}"),
        created_by=actor.id,
    )
    db.session.add(row)
    db.session.flush()
    record_audit(actor, "create", "sales_expense", row.id,
                 after={"description": description, "amount_ugx": amount, "payment_method": method})
    return row


def create_transfer(day, payload, actor):
    """One money movement for the day being closed.  Not an expense."""
    source = payload.get("from_account")
    target = payload.get("to_account")
    if source not in MONEY_ACCOUNTS or target not in MONEY_ACCOUNTS:
        raise SalesError("invalid money account")
    if source == target:
        raise SalesError("Choose two different accounts for a money transfer.")
    amount = whole_ugx(payload.get("amount_ugx"), "amount_ugx", positive=True)
    row = SalesMoneyTransfer(
        sales_profile_id=day.sales_profile_id, business_date=day.business_date,
        from_account=source, to_account=target, amount_ugx=amount,
        reference=(payload.get("reference") or "").strip() or None,
        notes=(payload.get("notes") or "").strip() or None,
        idempotency_key=str(payload.get("idempotency_key") or f"close-{uuid4().hex}"),
        created_by=actor.id,
    )
    db.session.add(row)
    db.session.flush()
    record_audit(actor, "create", "sales_money_transfer", row.id,
                 after={"from_account": source, "to_account": target, "amount_ugx": amount})
    return row


def _resolve_debtor_reference(profile, payload):
    """The debtor a collection names, by id or by free-text name."""
    debtor = None
    debtor_id = payload.get("debtor_id")
    if debtor_id:
        debtor = db.session.get(SalesDebtor, debtor_id)
    if debtor is None:
        name = " ".join(str(payload.get("name") or "").split())
        if name:
            debtor = SalesDebtor.query.filter_by(sales_profile_id=profile.id,
                                                 name_key=debtor_name_key(name)).first()
    if debtor is None:
        raise SalesError("Choose the debtor this money came from.")
    if debtor.sales_profile_id != profile.id:
        raise SalesError("That debtor belongs to another salesperson.")
    return debtor


def record_collection(profile, business_date, payload, actor):
    """Money received today against a debt that already exists.

    The receipt date is the day being closed, which is the date the money
    actually arrived; the debt keeps the date it was created.  A collection is
    never a product sale, so it never touches today's Total Sales.
    """
    business_date = valid_date(business_date)
    debtor = _resolve_debtor_reference(profile, payload)
    payment, _created = record_debt_payment(
        debtor, {**payload, "payment_date": business_date,
                 "idempotency_key": payload.get("idempotency_key") or f"close-{uuid4().hex}"},
        actor, default_date=business_date)
    return payment


# ---------------------------------------------------------------------------
# Replacing a day's expenses, transfers, debts and collections as a whole.
#
# Save and Close Day both send the complete list for each of these sections
# every time, exactly like a Sales Card's price rows.  Each helper compares
# that list against what is already stored for the day and only touches the
# database when something is actually different, so pressing Save repeatedly
# with no changes writes nothing new and a later Save or Close Day replaces
# the set rather than ever appending a duplicate.
# ---------------------------------------------------------------------------

def _clean_expense_item(item):
    if not isinstance(item, dict):
        raise SalesError("Each expense needs a description and an amount.")
    description = " ".join(str(item.get("description") or "").split())
    if not description:
        raise SalesError("Every expense needs a description.")
    if len(description) > 160:
        raise SalesError("Expense descriptions must be 160 characters or fewer.")
    method = item.get("payment_method") or "cash"
    if method not in ("cash", "mobile_money", "other"):
        raise SalesError("invalid expense payment method")
    amount = whole_ugx(item.get("amount_ugx"), "amount_ugx", positive=True)
    return {"description": description, "amount_ugx": amount, "payment_method": method,
            "notes": (item.get("notes") or "").strip() or None}


def replace_day_expenses(day, items, actor):
    if not isinstance(items, list):
        raise SalesError("expenses must be a list.")
    cleaned = [_clean_expense_item(item) for item in items]
    new_sig = sorted((c["description"], c["amount_ugx"], c["payment_method"], c["notes"]) for c in cleaned)
    existing_rows = SalesExpense.query.filter_by(
        sales_profile_id=day.sales_profile_id, business_date=day.business_date, status=ACTIVE).all()
    old_sig = sorted((r.description, r.amount_ugx, r.payment_method, r.notes) for r in existing_rows)
    if new_sig == old_sig:
        return existing_rows
    for row in existing_rows:
        void_record(row, actor, f"Replaced by Save on {day.business_date}")
    db.session.flush()
    return [create_expense(day, c, actor) for c in cleaned]


def _clean_transfer_item(item):
    if not isinstance(item, dict):
        raise SalesError("Each money transfer needs two accounts and an amount.")
    source, target = item.get("from_account"), item.get("to_account")
    if source not in MONEY_ACCOUNTS or target not in MONEY_ACCOUNTS:
        raise SalesError("invalid money account")
    if source == target:
        raise SalesError("Choose two different accounts for a money transfer.")
    amount = whole_ugx(item.get("amount_ugx"), "amount_ugx", positive=True)
    return {"from_account": source, "to_account": target, "amount_ugx": amount,
            "reference": (item.get("reference") or "").strip() or None,
            "notes": (item.get("notes") or "").strip() or None}


def replace_day_transfers(day, items, actor):
    if not isinstance(items, list):
        raise SalesError("transfers must be a list.")
    cleaned = [_clean_transfer_item(item) for item in items]
    new_sig = sorted((c["from_account"], c["to_account"], c["amount_ugx"], c["reference"], c["notes"])
                     for c in cleaned)
    existing_rows = SalesMoneyTransfer.query.filter_by(
        sales_profile_id=day.sales_profile_id, business_date=day.business_date, status=ACTIVE).all()
    old_sig = sorted((r.from_account, r.to_account, r.amount_ugx, r.reference, r.notes) for r in existing_rows)
    if new_sig == old_sig:
        return existing_rows
    for row in existing_rows:
        void_record(row, actor, f"Replaced by Save on {day.business_date}")
    db.session.flush()
    return [create_transfer(day, c, actor) for c in cleaned]


def _clean_debt_item(item):
    if not isinstance(item, dict):
        raise SalesError("Each unpaid sale needs a name and an amount.")
    name = clean_debtor_name(item.get("name"))
    amount = whole_ugx(item.get("amount_ugx"), "amount_ugx", positive=True)
    return {"name": name, "amount_ugx": amount, "notes": (item.get("notes") or "").strip() or None}


def replace_day_debts(profile, day, items, actor, require_balance=True):
    if not isinstance(items, list):
        raise SalesError("debts must be a list.")
    cleaned = [_clean_debt_item(item) for item in items]
    new_sig = sorted((debtor_name_key(c["name"]), c["amount_ugx"], c["notes"]) for c in cleaned)
    existing_rows = SalesDebt.query.filter_by(
        sales_profile_id=profile.id, business_date=day.business_date, kind=DEBT_SALE, status=ACTIVE).all()
    old_sig = sorted((debtor_name_key(r.debtor.name) if r.debtor else "", r.amount_ugx, r.notes)
                     for r in existing_rows)
    if new_sig == old_sig:
        return existing_rows
    for row in existing_rows:
        void_record(row, actor, f"Replaced by Save on {day.business_date}")
    db.session.flush()
    created = []
    for c in cleaned:
        debt, _created = create_debt(
            profile, {**c, "business_date": day.business_date, "idempotency_key": f"save-{uuid4().hex}"},
            actor, kind=DEBT_SALE, day=day, enforce_tender=require_balance)
        created.append(debt)
    return created


def replace_day_debt_payments(profile, day, items, actor):
    if not isinstance(items, list):
        raise SalesError("debt_payments must be a list.")
    resolved = []
    for item in items:
        if not isinstance(item, dict):
            raise SalesError("Each collection needs a debtor and an amount.")
        amount = whole_ugx(item.get("amount_ugx"), "amount_ugx", positive=True)
        method = item.get("payment_method")
        if method not in PAYMENT_METHODS:
            raise SalesError(f"payment_method must be one of {PAYMENT_METHODS}")
        debtor = _resolve_debtor_reference(profile, item)
        reference = (item.get("reference") or "").strip() or None
        resolved.append((debtor, amount, method, reference, item))
    new_sig = sorted((debtor.id, amount, method, reference)
                     for debtor, amount, method, reference, _item in resolved)
    existing_rows = SalesDebtPayment.query.filter_by(
        sales_profile_id=profile.id, payment_date=day.business_date, status=ACTIVE).all()
    old_sig = sorted((r.debtor_id, r.amount_ugx, r.payment_method, r.reference) for r in existing_rows)
    if new_sig == old_sig:
        return existing_rows
    for row in existing_rows:
        void_record(row, actor, f"Replaced by Save on {day.business_date}")
    db.session.flush()
    created = []
    for debtor, amount, method, reference, item in resolved:
        payment = record_collection(profile, day.business_date, {
            **item, "debtor_id": debtor.id, "amount_ugx": amount,
            "payment_method": method, "reference": reference,
            "idempotency_key": f"save-{uuid4().hex}",
        }, actor)
        created.append(payment)
    return created


def _open_or_create_working_day(profile, business_date, actor, *, action):
    """The Sales Day row Save or Close Day is about to write.

    Both actions share one rule: a day that is genuinely closed can only be
    written again after a Manager reopens it.  Everything else - a brand new
    date, or one already Saved as an open, unbalanced day - is writable by the
    salesperson themselves, by either action.
    """
    existing = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
    if existing is not None and existing.status == DAY_CLOSED:
        raise SalesError(
            f"This Sales Day is already closed. A Manager must reopen it before it can be {action} again."
        )
    day = existing if existing is not None else SalesDay(
        sales_profile_id=profile.id, business_date=business_date, status=DAY_DRAFT, created_by=actor.id)
    if existing is None:
        db.session.add(day)
    return day, existing is None


def _write_working_day(day, is_new, profile, business_date, payload, actor, elevated, require_balance, source):
    """Write the complete working day payload onto ``day``, in one transaction.

    Everything the salesperson can see arrives together - the Vehicle Stock
    sheet, every Sales Card, expenses, money movements, Mobile Money, the
    counted cash, new unpaid amounts and the collections of older ones.  Save
    and Close Day both call this; only ``require_balance`` tells it whether a
    Sales Card must price exactly Sold Today.  Nothing here commits: the
    caller's transaction (and, for Close Day, ``close_day()`` below it) commits
    it all together, so a refusal leaves nothing written.
    """
    lines = payload.get("vehicle_stock")
    if not isinstance(lines, list) or not lines:
        verb = "closing" if require_balance else "saving"
        raise SalesError(f"Add at least one product to the vehicle stock before {verb}.")
    cards = payload.get("sales_cards")
    if cards is not None and not isinstance(cards, list):
        raise SalesError("sales_cards must be a list of product cards.")
    if "mobile_money_from_sales_ugx" in payload:
        raw = payload.get("mobile_money_from_sales_ugx")
        day.mobile_money_from_sales_ugx = (None if raw in (None, "")
                                           else whole_ugx(raw, "mobile_money_from_sales_ugx"))
    if "actual_cash_count_ugx" in payload:
        raw = payload.get("actual_cash_count_ugx")
        day.actual_cash_count_ugx = (None if raw in (None, "")
                                     else whole_ugx(raw, "actual_cash_count_ugx"))
    if "actual_mobile_money_ugx" in payload:
        raw = payload.get("actual_mobile_money_ugx")
        day.actual_mobile_money_ugx = (None if raw in (None, "")
                                       else whole_ugx(raw, "actual_mobile_money_ugx"))
    if "notes" in payload:
        day.notes = (payload.get("notes") or "").strip() or None
    db.session.flush()
    record_audit(actor, "create" if is_new else "save", "sales_day", day.id,
                 after={"business_date": business_date, "source": source})
    # 1. the Vehicle Stock sheet, then every Sales Card priced against exactly
    #    the Sold Today that sheet produces (or, for Save, as it stands)
    save_vehicle_stock(day, lines, actor, elevated=elevated)
    db.session.flush()
    db.session.expire(day, ["closing_lines"])
    save_sales_cards(profile, business_date, cards or [], actor, vehicle_stock=lines,
                     require_balance=require_balance)
    db.session.flush()
    # 2. expenses and money movements - replaced as a whole, so pressing Save
    #    again with the same list writes nothing new
    replace_day_expenses(day, payload.get("expenses") or [], actor)
    replace_day_transfers(day, payload.get("transfers") or [], actor)
    # 3. the unpaid part of today's sales, then money collected on older debts.
    #    A debt may only explain sales already counted, so it is validated
    #    against the invoices written above.
    replace_day_debts(profile, day, payload.get("debts") or [], actor, require_balance=require_balance)
    replace_day_debt_payments(profile, day, payload.get("debt_payments") or [], actor)
    db.session.flush()


def save_working_day(profile, business_date, payload, actor, elevated=False):
    """Persist the current working day even if it is not balanced yet.

    Save never requires the business reconciliation Close Day requires -
    priced quantity against Sold Today, counted cash, stock variance - only
    the basic shape of each row.  The row this creates (or updates) stays
    open, so the salesperson can leave and come back to find exactly what they
    Saved, still editable, and Save it again as many times as they like.
    """
    business_date = valid_date(business_date)
    payload = payload or {}
    day, is_new = _open_or_create_working_day(profile, business_date, actor, action="saved")
    _write_working_day(day, is_new, profile, business_date, payload, actor, elevated,
                       require_balance=False, source="save_day")
    return day


def close_working_day(profile, business_date, payload, actor, elevated=False):
    """Turn the browser's working day into the day's one database transaction.

    Everything the salesperson can see arrives together, exactly as Save
    writes it, and is additionally validated and marked closed in the same
    transaction.  Whether or not this date was Saved first, Close Day writes
    the latest values on screen, never an older Saved snapshot: a refused
    close leaves the day exactly as it was last Saved (or not saved at all),
    and never partially closed.
    """
    business_date = valid_date(business_date)
    payload = payload or {}
    day, is_new = _open_or_create_working_day(profile, business_date, actor, action="closed")
    _write_working_day(day, is_new, profile, business_date, payload, actor, elevated,
                       require_balance=True, source="close_day")
    # validate the finished day and mark it closed - still uncommitted, so a
    # refusal leaves every row above exactly as it was before this call
    close_day(day, actor, payload.get("override_reason"), elevated=elevated)
    db.session.flush()
    return day


def first_divergence(profile, date_from, date_to):
    date_from, date_to = valid_date(date_from, "date_from"), valid_date(date_to, "date_to")
    for day in SalesDay.query.filter(
        SalesDay.sales_profile_id == profile.id, SalesDay.business_date >= date_from, SalesDay.business_date <= date_to,
    ).order_by(SalesDay.business_date):
        summary = daily_summary(profile, day.business_date)
        cash_var = summary["totals"]["cash_variance_ugx"]
        stock_var = any(r["variance"] and r["variance"]["base_unit_qty"] != 0 for r in summary["stock"])
        if (cash_var not in (None, 0)) or stock_var:
            return summary
    return None
