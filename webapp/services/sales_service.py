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
    ACTIVE, DAY_CLOSED, DAY_DRAFT, DAY_REOPENED, MONEY_ACCOUNTS, PAYMENT_METHODS, SALE_UNITS, VOID,
    SalesBaseline, SalesCashAdjustment, SalesDay, SalesExpense, SalesInvoice, SalesInvoiceLine,
    SalesMoneyTransfer, SalesOpeningStockLine, SalesPayment, SalesProfile, SalesStockAdjustment,
    SalesVehicleClosingLine,
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
        product_ids.update(line.product_id for line in baseline.stock_lines)
    # Active products are included so the first-day setup clearly shows which
    # ones still need a trusted opening quantity.
    product_ids.update(pid for (pid,) in db.session.query(Product.id).filter_by(active=True).all())
    products = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()} if product_ids else {}
    result = []
    for product_id in sorted(product_ids, key=lambda pid: (products[pid].display_order, products[pid].name)):
        product = products[product_id]
        opening, source, source_date, opening_rule_id = opening_stock(profile.id, product_id, business_date)
        close_line = closings.get(product_id)
        rule_id = close_line.packaging_rule_id if close_line else opening_rule_id
        # db.session.get(Model, None) warns about a NULL primary key; skip the
        # lookup entirely when there is no recorded rule to load.
        rule = db.session.get(PackagingRule, rule_id) if rule_id else None
        rule = rule or _rule_for(product_id, business_date)
        added_dispatch = dispatch.get(product_id, 0)
        added_adjustment = adjustments.get(product_id, 0)
        added = added_dispatch + added_adjustment
        available = None if opening is None else opening + added
        closing = close_line.base_unit_qty if close_line else None
        derived = None if available is None or closing is None else available - closing
        recorded_qty = recorded.get(product_id, 0)
        variance = None if derived is None else derived - recorded_qty
        result.append({
            "product_id": product_id,
            "product_name": product.name,
            "packaging_rule_id": rule.id,
            "supports_packs": rule.has_pack_tier,
            "opening_source": source,
            "opening_source_date": source_date,
            "opening": quantity_dict(opening, rule),
            "dispatch_loaded": quantity_dict(added_dispatch, rule),
            "manual_adjustment": quantity_dict(added_adjustment, rule),
            "added": quantity_dict(added, rule),
            "available": quantity_dict(available, rule),
            "closing": quantity_dict(closing, rule),
            "derived_sold": quantity_dict(derived, rule),
            "recorded_sold": quantity_dict(recorded_qty, rule),
            "variance": quantity_dict(variance, rule),
            "reconciled": variance == 0 if variance is not None else False,
        })
    return result


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


def money_balance(profile_id, business_date, account):
    business_date = valid_date(business_date)
    baseline = baseline_for(profile_id, business_date)
    if baseline is None:
        return None
    opening = baseline.opening_cash_ugx if account == "cash" else baseline.opening_mobile_money_ugx
    payments = _sum(db.session.query(func.sum(SalesPayment.amount_ugx)).filter(
        SalesPayment.sales_profile_id == profile_id, SalesPayment.status == ACTIVE,
        SalesPayment.payment_date >= baseline.effective_date, SalesPayment.payment_date <= business_date,
        SalesPayment.payment_method == account,
    ))
    expenses = _sum(db.session.query(func.sum(SalesExpense.amount_ugx)).filter(
        SalesExpense.sales_profile_id == profile_id, SalesExpense.status == ACTIVE,
        SalesExpense.business_date >= baseline.effective_date, SalesExpense.business_date <= business_date,
        SalesExpense.payment_method == account,
    ))
    incoming = _sum(db.session.query(func.sum(SalesMoneyTransfer.amount_ugx)).filter(
        SalesMoneyTransfer.sales_profile_id == profile_id, SalesMoneyTransfer.status == ACTIVE,
        SalesMoneyTransfer.business_date >= baseline.effective_date, SalesMoneyTransfer.business_date <= business_date,
        SalesMoneyTransfer.to_account == account,
    ))
    outgoing = _sum(db.session.query(func.sum(SalesMoneyTransfer.amount_ugx)).filter(
        SalesMoneyTransfer.sales_profile_id == profile_id, SalesMoneyTransfer.status == ACTIVE,
        SalesMoneyTransfer.business_date >= baseline.effective_date, SalesMoneyTransfer.business_date <= business_date,
        SalesMoneyTransfer.from_account == account,
    ))
    adjustments = _sum(db.session.query(func.sum(SalesCashAdjustment.amount_delta_ugx)).filter(
        SalesCashAdjustment.sales_profile_id == profile_id,
        SalesCashAdjustment.business_date >= baseline.effective_date,
        SalesCashAdjustment.business_date <= business_date,
        SalesCashAdjustment.account == account,
    ))
    return opening + payments + incoming - expenses - outgoing + adjustments


def daily_summary(profile, business_date):
    business_date = valid_date(business_date)
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
    invoices = SalesInvoice.query.filter_by(sales_profile_id=profile.id, business_date=business_date, status=ACTIVE).all()
    sale_invoices = [i for i in invoices if not i.is_opening_receivable]
    total_sales = sum(invoice_total(i) for i in sale_invoices)
    expenses = SalesExpense.query.filter_by(sales_profile_id=profile.id, business_date=business_date, status=ACTIVE).all()
    total_expenses = sum(e.amount_ugx for e in expenses)
    payments = SalesPayment.query.filter_by(sales_profile_id=profile.id, payment_date=business_date, status=ACTIVE).all()
    cash_receipts = sum(p.amount_ugx for p in payments if p.payment_method == "cash")
    mobile_receipts = sum(p.amount_ugx for p in payments if p.payment_method == "mobile_money")
    old_debt = sum(p.amount_ugx for p in payments if p.invoice.business_date < business_date)
    new_credit = sum(max(0, outstanding(i, business_date)) for i in sale_invoices)
    expected_cash = money_balance(profile.id, business_date, "cash")
    expected_mobile = money_balance(profile.id, business_date, "mobile_money")
    actual_cash = day.actual_cash_count_ugx if day else None
    actual_mobile = day.actual_mobile_money_ugx if day else None
    stock = stock_reconciliation(profile, business_date)
    stock_variance = any(r["variance"] is not None and r["variance"]["base_unit_qty"] != 0 for r in stock)
    baseline_required = baseline_for(profile.id, business_date) is None or any(r["opening_source"] == "required" for r in stock)
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
            "actual_cash_count_ugx": actual_cash, "actual_mobile_money_ugx": actual_mobile,
            "close_override_reason": day.close_override_reason,
        },
        "stock": stock,
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
        } for p in payments],
        "totals": {
            "total_sales_ugx": total_sales, "total_expenses_ugx": total_expenses,
            "net_sales_after_expenses_ugx": total_sales - total_expenses,
            "cash_receipts_ugx": cash_receipts, "mobile_money_receipts_ugx": mobile_receipts,
            "old_debt_collections_ugx": old_debt, "new_customer_receivables_ugx": new_credit,
            "expected_cash_ugx": expected_cash, "actual_cash_ugx": actual_cash,
            "cash_variance_ugx": None if expected_cash is None or actual_cash is None else actual_cash - expected_cash,
            "expected_mobile_money_ugx": expected_mobile, "actual_mobile_money_ugx": actual_mobile,
            "mobile_variance_ugx": None if expected_mobile is None or actual_mobile is None else actual_mobile - expected_mobile,
        },
        "baseline_required": baseline_required,
        "needs_reconciliation": baseline_required or stock_variance,
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
    if not customer and sum(p.get("amount_ugx", 0) for p in supplied_payments) != invoice_total(invoice):
        raise SalesError("A customer is required for a credit or partially paid sale")
    record_audit(actor, "create", "sales_invoice", invoice.id, after=invoice_dict(invoice, include_payments=False))
    for p in supplied_payments:
        create_payment(invoice, p, actor, default_date=business_date, allow_batch_partial=customer is None)
    if outstanding(invoice) > 0 and invoice.customer_id is None:
        raise SalesError("Any sale that creates customer credit must have a real customer")
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


def debtors(profile_ids=None, as_of=None):
    q = SalesInvoice.query.filter_by(status=ACTIVE)
    if profile_ids is not None:
        q = q.filter(SalesInvoice.sales_profile_id.in_(profile_ids))
    invoices = []
    for invoice in q.order_by(SalesInvoice.business_date, SalesInvoice.id):
        balance = outstanding(invoice, as_of)
        if balance > 0:
            row = invoice_dict(invoice, as_of=as_of)
            age_date = date.fromisoformat(valid_date(as_of, "as_of")) if as_of else date.fromisoformat(business_today())
            age = (age_date - date.fromisoformat(invoice.business_date)).days
            row["days_outstanding"] = max(0, age)
            row["aging_bucket"] = "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "90+"
            invoices.append(row)
    return invoices


def set_closing_stock(day, payload, actor, elevated=False):
    if day.status == DAY_CLOSED:
        raise SalesError("A closed Sales Day must be reopened before it can be edited")
    product = db.session.get(Product, payload.get("product_id"))
    if product is None:
        raise SalesError("product not found")
    rule = _rule_for(product.id, day.business_date)
    base_qty = unit_to_base(payload.get("quantity"), payload.get("unit"), rule) if payload.get("quantity") else 0
    row = SalesVehicleClosingLine.query.filter_by(sales_day_id=day.id, product_id=product.id).first()
    before = None if row is None else {"base_unit_qty": row.base_unit_qty}
    rec = next(r for r in stock_reconciliation(day.profile, day.business_date) if r["product_id"] == product.id)
    available = rec["available"]["base_unit_qty"] if rec["available"] else None
    if available is None:
        raise SalesError("A reconciliation opening balance is required before this date.")
    if base_qty > available:
        raise SalesError("Closing vehicle stock cannot exceed available stock.")
    if row is None:
        row = SalesVehicleClosingLine(sales_day_id=day.id, product_id=product.id, base_unit_qty=base_qty, packaging_rule_id=rule.id)
        db.session.add(row)
    else:
        if row.base_unit_qty != base_qty:
            reason = (payload.get("reason") or "").strip()
            if day.status != DAY_DRAFT and not elevated:
                raise SalesError("A closed Sales Day must be reopened by a Manager or Super Administrator")
            if elevated and not reason:
                raise SalesError("A Manager/Super Administrator correction reason is required")
            if reason:
                row.corrected_by = actor.id
                row.correction_reason = reason
        row.base_unit_qty = base_qty
        row.packaging_rule_id = rule.id
    db.session.flush()
    record_audit(actor, "correct" if before else "create", "sales_vehicle_closing", row.id,
                 before=before, after={"base_unit_qty": base_qty, "reason": payload.get("reason")})
    return row


def close_day(day, actor, override_reason=None, elevated=False):
    summary = daily_summary(day.profile, day.business_date)
    if summary["baseline_required"]:
        raise SalesError("A reconciliation opening balance is required before this date.")
    uncounted = [r for r in summary["stock"] if r["closing"] is None]
    if uncounted:
        raise SalesError(f"Closing vehicle stock is required for {uncounted[0]['product_name']}.")
    mismatches = [r for r in summary["stock"] if r["variance"] and r["variance"]["base_unit_qty"] != 0]
    if mismatches and not (elevated and (override_reason or "").strip()):
        first = mismatches[0]
        difference = abs(first["variance"]["base_unit_qty"])
        relation = "under" if first["variance"]["base_unit_qty"] > 0 else "exceed"
        raise SalesError(f"Recorded {first['product_name']} sales {relation} stock-derived sold quantity by {difference} base units.")
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
