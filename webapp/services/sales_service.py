"""Authoritative calculations for Sales vehicle stock, receivables and money.

Nothing in this module trusts client totals.  All quantities and balances are
re-derived from immutable/safely-voided source rows on every read, which makes
historical corrections propagate without rewriting unrelated transactions.
"""
from datetime import date, datetime, time
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
from webapp.services.packaging import from_base_units


class SalesError(ValueError):
    pass


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


def quantity_dict(base_qty, rule):
    if base_qty is None:
        return None
    cartons, packs, pieces = from_base_units(abs(base_qty), rule)
    return {
        "base_unit_qty": base_qty,
        "cartons": cartons,
        "packs": packs,
        "pieces": pieces,
        "negative": base_qty < 0,
        "label": _quantity_label(base_qty, rule),
    }


def _quantity_label(base_qty, rule):
    sign = "-" if base_qty < 0 else ""
    cartons, packs, pieces = from_base_units(abs(base_qty), rule)
    parts = []
    if cartons:
        parts.append(f"{cartons} carton{'s' if cartons != 1 else ''}")
    if packs:
        parts.append(f"{packs} pack{'s' if packs != 1 else ''}")
    if pieces or not parts:
        parts.append(f"{pieces} piece{'s' if pieces != 1 else ''}")
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
    return {product_id: int(total or 0) for product_id, total in rows}


def stock_reconciliation(profile, business_date):
    business_date = valid_date(business_date)
    dispatch = dispatch_loads(profile, business_date)
    adjustments = adjustment_loads(profile.id, business_date)
    recorded = recorded_quantities(profile.id, business_date)
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
    closings = {line.product_id: line for line in day.closing_lines} if day else {}
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
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
    lines = {line.product_id: line for line in day.closing_lines} if day else {}
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
            "quantity": line.quantity, "unit": line.sale_unit, "base_unit_qty": line.base_unit_qty,
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


def tender_split(profile_id, business_date):
    """How one day's sales were collected.

    Simplified workflow: everything is treated as cash unless the salesperson
    records a Mobile Money amount for the day or writes an unpaid amount down
    in Debtors.  Legacy rows keep their original per-sale payments and
    receivables semantics, so deployed history reconciles exactly as before
    and nothing is counted twice.
    """
    business_date = valid_date(business_date)
    invoices = sale_invoices(profile_id, business_date)
    total_sales = sum(invoice_total(i) for i in invoices)
    legacy_credit = sum(max(0, outstanding(i, business_date)) for i in invoices if i.customer_id is not None)
    ledger_debt = _sum(db.session.query(func.sum(SalesDebt.amount_ugx)).filter(
        SalesDebt.sales_profile_id == profile_id, SalesDebt.business_date == business_date,
        SalesDebt.kind == DEBT_SALE, SalesDebt.status == ACTIVE,
    ))
    day = SalesDay.query.filter_by(sales_profile_id=profile_id, business_date=business_date).first()
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


def assert_tender_within_sales(profile_id, business_date, extra_debt=0, extra_mobile=None):
    split = tender_split(profile_id, business_date)
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


def money_anchor(profile_id, business_date, account):
    """Where the running money balance starts, and how much it carried.

    A declared starting point always wins.  When the salesperson has not
    declared one yet, the books start at their first Sales Day carrying
    nothing: Cash at Hand is derived from the day's own activity instead of
    waiting for a Manager to unlock it.
    """
    business_date = valid_date(business_date)
    baseline = baseline_for(profile_id, business_date)
    if baseline is not None:
        opening = baseline.opening_cash_ugx if account == "cash" else baseline.opening_mobile_money_ugx
        return baseline.effective_date, int(opening or 0)
    first_date = db.session.query(func.min(SalesDay.business_date)).filter(
        SalesDay.sales_profile_id == profile_id, SalesDay.business_date <= business_date,
    ).scalar()
    if first_date is None:
        return None, 0
    return valid_date(first_date, "business_date"), 0


def money_balance(profile_id, business_date, account):
    business_date = valid_date(business_date)
    start_date, opening = money_anchor(profile_id, business_date, account)
    if start_date is None:
        return None
    cash_tender, mobile_tender = _range_tender(profile_id, start_date, business_date)
    tender = cash_tender if account == "cash" else mobile_tender
    collections = _money_collections_between(profile_id, start_date, business_date, account)
    expenses = _sum(db.session.query(func.sum(SalesExpense.amount_ugx)).filter(
        SalesExpense.sales_profile_id == profile_id, SalesExpense.status == ACTIVE,
        SalesExpense.business_date >= start_date, SalesExpense.business_date <= business_date,
        SalesExpense.payment_method == account,
    ))
    incoming = _sum(db.session.query(func.sum(SalesMoneyTransfer.amount_ugx)).filter(
        SalesMoneyTransfer.sales_profile_id == profile_id, SalesMoneyTransfer.status == ACTIVE,
        SalesMoneyTransfer.business_date >= start_date, SalesMoneyTransfer.business_date <= business_date,
        SalesMoneyTransfer.to_account == account,
    ))
    outgoing = _sum(db.session.query(func.sum(SalesMoneyTransfer.amount_ugx)).filter(
        SalesMoneyTransfer.sales_profile_id == profile_id, SalesMoneyTransfer.status == ACTIVE,
        SalesMoneyTransfer.business_date >= start_date, SalesMoneyTransfer.business_date <= business_date,
        SalesMoneyTransfer.from_account == account,
    ))
    adjustments = _sum(db.session.query(func.sum(SalesCashAdjustment.amount_delta_ugx)).filter(
        SalesCashAdjustment.sales_profile_id == profile_id,
        SalesCashAdjustment.business_date >= start_date,
        SalesCashAdjustment.business_date <= business_date,
        SalesCashAdjustment.account == account,
    ))
    return opening + tender + collections + incoming - expenses - outgoing + adjustments


def daily_summary(profile, business_date):
    business_date = valid_date(business_date)
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
    invoices = SalesInvoice.query.filter_by(sales_profile_id=profile.id, business_date=business_date, status=ACTIVE).all()
    split = tender_split(profile.id, business_date)
    total_sales = split["total_sales_ugx"]
    expenses = SalesExpense.query.filter_by(sales_profile_id=profile.id, business_date=business_date, status=ACTIVE).all()
    total_expenses = sum(e.amount_ugx for e in expenses)
    # Only these two accounts exist, and an expense paid from one never touches
    # the other.  Keeping them apart is what makes Cash at Hand correct.
    cash_expenses = sum(e.amount_ugx for e in expenses if e.payment_method == "cash")
    mobile_expenses = sum(e.amount_ugx for e in expenses if e.payment_method == "mobile_money")
    # Everything collected on this date that belongs to an earlier debt is
    # counted here, on the date it was actually received.
    cash_receipts = split["cash_from_sales_ugx"]
    mobile_receipts = split["mobile_money_from_sales_ugx"]
    old_debt = debt_collections(profile.id, business_date)
    old_debt_cash = debt_collections(profile.id, business_date, "cash")
    old_debt_mobile = debt_collections(profile.id, business_date, "mobile_money")
    new_credit = split["new_debt_ugx"]
    expected_cash = money_balance(profile.id, business_date, "cash")
    expected_mobile = money_balance(profile.id, business_date, "mobile_money")
    actual_cash = day.actual_cash_count_ugx if day else None
    actual_mobile = day.actual_mobile_money_ugx if day else None
    stock = stock_reconciliation(profile, business_date)
    stock_variance = any(r["variance"] is not None and r["variance"]["base_unit_qty"] != 0 for r in stock)
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
                "quantity": line.quantity, "unit": line.sale_unit,
                "base_unit_qty": line.base_unit_qty, "unit_price_ugx": line.unit_price_ugx,
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
    debts = SalesDebt.query.filter_by(
        sales_profile_id=profile.id, business_date=business_date, status=ACTIVE,
    ).order_by(SalesDebt.id).all()
    debt_payments = SalesDebtPayment.query.filter_by(
        sales_profile_id=profile.id, payment_date=business_date, status=ACTIVE,
    ).order_by(SalesDebtPayment.id).all()

    # Previous descriptions for this salesperson make the expense form's
    # suggestions useful without inventing any values: newest first, unique,
    # and only ever descriptions this profile itself already recorded.
    seen_descriptions = set()
    expense_suggestions = []
    for (description,) in db.session.query(SalesExpense.description).filter(
        SalesExpense.sales_profile_id == profile.id,
        SalesExpense.status == ACTIVE,
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


def create_debt(profile, payload, actor, *, kind=DEBT_SALE):
    """Record an unpaid amount against a free-text debtor name."""
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
    if kind == DEBT_SALE:
        # The sale is already counted; the debt may only explain money that was
        # not collected in cash today.  It can never create extra sales.
        assert_tender_within_sales(profile.id, business_date, extra_debt=amount)
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
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
    stored = {line.product_id: line for line in day.closing_lines} if day else {}
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


def save_product_sales(profile, business_date, product_id, rows, actor, vehicle_stock=None):
    """Save one product's sales card as a set of price rows.

    The card is authoritative for one product on one day.  It is replaced as a
    whole, so re-saving the same numbers changes nothing and an edited card can
    never be counted twice.  Quantities must add up to the stock sold exactly:
    an under- or over-priced card is refused with the difference spelled out
    rather than silently balanced.

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
    if vehicle_stock is not None:
        # The Sales screen sends the sheet it is showing, so the card is priced
        # against the Sold Today on screen.  Nothing is written here: Close Day
        # stores the sheet and re-checks this card against those figures, so the
        # rule that a card must price exactly what was sold is never weakened.
        live = live_vehicle_stock(profile, business_date, vehicle_stock).get(product.id)
        if live is None:
            raise SalesError(f"{product.name} is not part of this vehicle's stock for {business_date}.")
        sold, rule = live["sold"], live["rule"]
        if sold is None:
            raise SalesError(f"Enter the return today for {product.name} first, "
                             "then its sold quantity is known.")
    else:
        rec = next((r for r in stock_reconciliation(profile, business_date)
                    if r["product_id"] == product.id), None)
        if rec is None:
            raise SalesError(f"{product.name} is not part of this vehicle's stock for {business_date}.")
        if rec["available"] is None:
            raise SalesError(f"Save the starting stock for {product.name} first.")
        if rec["derived_sold"] is None:
            raise SalesError(f"Save the return today for {product.name} first, then its sold quantity is known.")
        sold = rec["derived_sold"]["base_unit_qty"]
        rule = (db.session.get(PackagingRule, rec["packaging_rule_id"])
                if rec["packaging_rule_id"] else None) or _rule_for(product.id, business_date)
    if not isinstance(rows, list):
        raise SalesError("rows must be a list of price rows")
    parsed = []
    priced = 0
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            raise SalesError(f"Row {index} is not a valid price row.")
        unit = item.get("unit")
        if unit not in SALE_UNITS:
            raise SalesError(f"Row {index}: unit must be one of {SALE_UNITS}.")
        base_qty = unit_to_base(item.get("quantity"), unit, rule)
        price = whole_ugx(item.get("unit_price_ugx"), f"Row {index} selling price")
        if price <= 0:
            raise SalesError(f"Row {index}: the selling price must be greater than zero.")
        priced += base_qty
        parsed.append({"product_id": product.id, "quantity": item.get("quantity"),
                       "unit": unit, "unit_price_ugx": price})
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
        base_qty = unit_to_base(p["quantity"], p["unit"], rule)
        invoice.lines.append(SalesInvoiceLine(
            product_id=product.id, quantity=p["quantity"], sale_unit=p["unit"],
            base_unit_qty=base_qty, packaging_rule_id=rule.id,
            unit_price_ugx=p["unit_price_ugx"], line_total_ugx=p["quantity"] * p["unit_price_ugx"],
        ))
    db.session.flush()
    record_audit(actor, "create", "sales_card", invoice.id,
                 after={"product_id": product.id, "business_date": business_date, "rows": parsed,
                        "card_total_ugx": invoice_total(invoice)})
    return invoice, "created"


def close_day(day, actor, override_reason=None, elevated=False):
    summary = daily_summary(day.profile, day.business_date)
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
