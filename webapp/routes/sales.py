"""Section-gated Sales Department JSON API and scoped report exports."""
from flask import Blueprint, Response, jsonify, request

from webapp.auth import current_user, login_required
from webapp.extensions import db
from webapp.models.customer import CATEGORY_SALESPERSON, Customer
from webapp.models.product import Product
from webapp.models.sales import (
    ACTIVE, DAY_CLOSED, DAY_DRAFT, DAY_REOPENED, DEBT_OPENING, MONEY_ACCOUNTS, PAYMENT_METHODS, VOID,
    SalesBaseline, SalesCashAdjustment, SalesDay, SalesDebt, SalesDebtPayment, SalesDebtor,
    SalesExpense, SalesInvoice, SalesMoneyTransfer, SalesOpeningStockLine, SalesPayment,
    SalesProfile, SalesStockAdjustment,
)
from webapp.models.sales_category import SalesCategory
from webapp.models.user import (
    ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, ROLE_VIEWER, User,
)
from webapp.services import branding_service
from webapp.services.audit_service import record_audit
from webapp.services.business_calendar import business_today, utcnow
from webapp.services.export_service import MIME_TYPES, build_export
from webapp.services import sales_service as svc

sales_bp = Blueprint("sales", __name__, url_prefix="/api/sales")
ELEVATED = (ROLE_SUPER_ADMIN, ROLE_MANAGER)
READ_ALL = (ROLE_SUPER_ADMIN, ROLE_MANAGER, ROLE_ACCOUNTANT, ROLE_VIEWER)


def _error(exc, status=400):
    return jsonify({"error": str(exc)}), status


def _profile(profile_id=None, *, write=False):
    user = current_user()
    if user.role == ROLE_OPERATOR:
        profile = SalesProfile.query.filter_by(user_id=user.id, active=True).first()
        if profile is None:
            raise svc.SalesError("Your Sales account has not been set up. Please contact a Manager.")
        if profile_id is not None and int(profile_id) != profile.id:
            raise PermissionError("You cannot access another salesperson's records")
        return profile
    if user.role == ROLE_VIEWER and write:
        raise PermissionError("Viewer access is read-only")
    if profile_id is None:
        raise svc.SalesError("sales_profile_id is required")
    profile = db.session.get(SalesProfile, int(profile_id))
    if profile is None:
        raise svc.SalesError("Sales Profile not found")
    return profile


def _profile_response(profile_id=None, write=False):
    try:
        return _profile(profile_id, write=write), None
    except PermissionError as exc:
        return None, _error(exc, 403)
    except (svc.SalesError, TypeError, ValueError) as exc:
        return None, _error(exc)


def _elevated():
    return current_user().role in ELEVATED


def _operational_writer():
    return current_user().role in (ROLE_OPERATOR, ROLE_MANAGER, ROLE_SUPER_ADMIN)


def _require_editable_day(profile, business_date):
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=svc.valid_date(business_date)).first()
    if day is None:
        raise svc.SalesError("Create the Sales Day first")
    if day.status == DAY_CLOSED:
        raise svc.SalesError("A closed Sales Day must be reopened before it can be edited")
    return day


@sales_bp.route("/metadata")
@login_required
def metadata():
    user = current_user()
    profiles = SalesProfile.query.filter_by(active=True).order_by(SalesProfile.display_name).all()
    if user.role == ROLE_OPERATOR:
        profiles = [p for p in profiles if p.user_id == user.id]
    products = Product.query.filter_by(active=True).order_by(Product.display_order, Product.name).all()
    customers = Customer.query.filter_by(active=True, merged_into_id=None).order_by(Customer.name).all()
    categories = SalesCategory.query.filter_by(active=True).order_by(SalesCategory.display_order, SalesCategory.name).all()
    return jsonify({
        "profiles": [svc.profile_dict(p) for p in profiles],
        "products": [{"id": p.id, "name": p.name} for p in products],
        "customers": [{"id": c.id, "name": c.name, "category": c.category} for c in customers],
        "sales_categories": [c.to_dict() for c in categories],
        "payment_methods": PAYMENT_METHODS, "money_accounts": MONEY_ACCOUNTS,
        "can_manage": _elevated(), "can_record_payment_for_all": user.role in (*ELEVATED, ROLE_ACCOUNTANT),
        "role": user.role, "can_view_reconciliation": user.role != ROLE_OPERATOR,
        "debtor_names": svc.debtor_names([p.id for p in profiles]),
    })


@sales_bp.route("/profiles", methods=["GET", "POST"])
@login_required
def profiles():
    if request.method == "GET":
        rows = SalesProfile.query.order_by(SalesProfile.display_name).all()
        if current_user().role == ROLE_OPERATOR:
            rows = [p for p in rows if p.user_id == current_user().id]
        return jsonify([svc.profile_dict(p) for p in rows])
    if current_user().role != ROLE_SUPER_ADMIN:
        return _error("Only a Super Administrator may manage Sales Profiles", 403)
    data = request.get_json(silent=True) or {}
    try:
        user = db.session.get(User, int(data.get("user_id")))
        if user is None:
            raise svc.SalesError("user not found")
        if user.role != ROLE_OPERATOR:
            raise svc.SalesError("A normal salesperson profile must be linked to an Operator account")
        if SalesProfile.query.filter_by(user_id=user.id).first():
            raise svc.SalesError("This user already has a Sales Profile")
        recipient = db.session.get(Customer, data.get("recipient_customer_id")) if data.get("recipient_customer_id") else None
        if recipient and recipient.category != CATEGORY_SALESPERSON:
            raise svc.SalesError("Dispatch recipient must be a customer record categorized as salesperson")
        category = db.session.get(SalesCategory, data.get("sales_category_id")) if data.get("sales_category_id") else None
        name = (data.get("display_name") or "").strip()
        if not name:
            raise svc.SalesError("display_name is required")
        profile = SalesProfile(user_id=user.id, display_name=name,
                               sales_category_id=category.id if category else None,
                               recipient_customer_id=recipient.id if recipient else None,
                               vehicle_label=(data.get("vehicle_label") or "").strip() or None)
        db.session.add(profile); db.session.flush()
        record_audit(current_user(), "create", "sales_profile", profile.id, after=svc.profile_dict(profile))
        db.session.commit()
        return jsonify(svc.profile_dict(profile)), 201
    except (svc.SalesError, TypeError, ValueError) as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/profiles/<int:profile_id>", methods=["PATCH"])
@login_required
def update_profile(profile_id):
    if current_user().role != ROLE_SUPER_ADMIN:
        return _error("Only a Super Administrator may manage Sales Profiles", 403)
    profile = db.session.get(SalesProfile, profile_id)
    if profile is None:
        return _error("not found", 404)
    data = request.get_json(silent=True) or {}
    try:
        before = svc.profile_dict(profile)
        if "display_name" in data:
            profile.display_name = (data["display_name"] or "").strip()
            if not profile.display_name: raise svc.SalesError("display_name is required")
        if "sales_category_id" in data:
            profile.sales_category_id = data["sales_category_id"] or None
        if "recipient_customer_id" in data:
            recipient = db.session.get(Customer, data["recipient_customer_id"]) if data["recipient_customer_id"] else None
            if recipient and recipient.category != CATEGORY_SALESPERSON:
                raise svc.SalesError("Dispatch recipient must be categorized as salesperson")
            profile.recipient_customer_id = recipient.id if recipient else None
        if "vehicle_label" in data: profile.vehicle_label = (data["vehicle_label"] or "").strip() or None
        if "active" in data:
            if not isinstance(data["active"], bool): raise svc.SalesError("active must be true or false")
            profile.active = data["active"]
        db.session.flush(); record_audit(current_user(), "update", "sales_profile", profile.id, before=before, after=svc.profile_dict(profile))
        db.session.commit(); return jsonify(svc.profile_dict(profile))
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/baselines", methods=["POST"])
@login_required
def save_baseline():
    if not _elevated(): return _error("Manager or Super Administrator authority is required", 403)
    data = request.get_json(silent=True) or {}
    profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    try:
        effective = svc.valid_date(data.get("effective_date"), "effective_date")
        cash = svc.whole_ugx(data.get("opening_cash_ugx", 0), "opening_cash_ugx")
        mobile = svc.whole_ugx(data.get("opening_mobile_money_ugx", 0), "opening_mobile_money_ugx")
        baseline = SalesBaseline.query.filter_by(sales_profile_id=profile.id, effective_date=effective).first()
        before = None
        if baseline:
            before = {"opening_cash_ugx": baseline.opening_cash_ugx, "opening_mobile_money_ugx": baseline.opening_mobile_money_ugx,
                      "stock": {line.product_id: line.base_unit_qty for line in baseline.stock_lines}}
            baseline.opening_cash_ugx, baseline.opening_mobile_money_ugx = cash, mobile
            baseline.notes = (data.get("notes") or "").strip() or None
            for line in list(baseline.stock_lines): db.session.delete(line)
        else:
            baseline = SalesBaseline(sales_profile_id=profile.id, effective_date=effective,
                                     opening_cash_ugx=cash, opening_mobile_money_ugx=mobile,
                                     notes=(data.get("notes") or "").strip() or None, created_by=current_user().id)
            db.session.add(baseline); db.session.flush()
        for item in data.get("stock_lines") or []:
            product = db.session.get(Product, item.get("product_id"))
            if product is None: raise svc.SalesError("product not found")
            rule = svc._rule_for(product.id, effective)
            qty = item.get("quantity", 0)
            base = 0 if qty == 0 else svc.unit_to_base(qty, item.get("unit"), rule)
            baseline.stock_lines.append(SalesOpeningStockLine(product_id=product.id, base_unit_qty=base, packaging_rule_id=rule.id))
        db.session.flush()
        after = {"opening_cash_ugx": cash, "opening_mobile_money_ugx": mobile,
                 "stock": {line.product_id: line.base_unit_qty for line in baseline.stock_lines}}
        record_audit(current_user(), "update" if before else "create", "sales_baseline", baseline.id,
                     before=before, after={**after, "effective_date": effective, "reason": data.get("reason")})
        db.session.commit(); return jsonify({"id": baseline.id, "effective_date": effective, **after})
    except (svc.SalesError, TypeError, ValueError) as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/days", methods=["POST"])
@login_required
def create_day():
    if not _operational_writer(): return _error("This role cannot create Sales Days", 403)
    data = request.get_json(silent=True) or {}
    profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    try:
        business_date = svc.valid_date(data.get("business_date"))
        day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=business_date).first()
        created = False
        if day is None:
            day = SalesDay(sales_profile_id=profile.id, business_date=business_date, created_by=current_user().id)
            db.session.add(day); db.session.flush(); created = True
            record_audit(current_user(), "create", "sales_day", day.id, after={"business_date": business_date})
            db.session.commit()
        return jsonify(svc.daily_summary(profile, business_date)), 201 if created else 200
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/days/<date_str>", methods=["GET", "PATCH"])
@login_required
def day_detail(date_str):
    if request.method == "PATCH" and not _operational_writer(): return _error("This role cannot edit Sales Days", 403)
    profile_id = request.args.get("sales_profile_id") if request.method == "GET" else (request.get_json(silent=True) or {}).get("sales_profile_id")
    profile, err = _profile_response(profile_id, write=request.method == "PATCH")
    if err: return err
    try:
        date_str = svc.valid_date(date_str)
        if request.method == "PATCH":
            data = request.get_json(silent=True) or {}
            day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=date_str).first()
            if day is None: raise svc.SalesError("Create the Sales Day first")
            if day.status == DAY_CLOSED: raise svc.SalesError("A closed Sales Day must be reopened before it can be edited")
            before = {"actual_cash_count_ugx": day.actual_cash_count_ugx, "actual_mobile_money_ugx": day.actual_mobile_money_ugx,
                      "mobile_money_from_sales_ugx": day.mobile_money_from_sales_ugx, "notes": day.notes}
            if "actual_cash_count_ugx" in data:
                day.actual_cash_count_ugx = None if data["actual_cash_count_ugx"] is None else svc.whole_ugx(data["actual_cash_count_ugx"], "actual_cash_count_ugx")
            if "actual_mobile_money_ugx" in data:
                day.actual_mobile_money_ugx = None if data["actual_mobile_money_ugx"] is None else svc.whole_ugx(data["actual_mobile_money_ugx"], "actual_mobile_money_ugx")
            if "mobile_money_from_sales_ugx" in data:
                raw = data["mobile_money_from_sales_ugx"]
                value = None if raw is None else svc.whole_ugx(raw, "mobile_money_from_sales_ugx")
                if value is not None:
                    # Mobile Money and unpaid amounts can never exceed the
                    # sales already entered for this date.
                    svc.assert_tender_within_sales(profile.id, date_str, extra_mobile=value)
                day.mobile_money_from_sales_ugx = value
            if "notes" in data: day.notes = (data["notes"] or "").strip() or None
            record_audit(current_user(), "update", "sales_day", day.id, before=before,
                         after={"actual_cash_count_ugx": day.actual_cash_count_ugx,
                                "actual_mobile_money_ugx": day.actual_mobile_money_ugx,
                                "mobile_money_from_sales_ugx": day.mobile_money_from_sales_ugx,
                                "notes": day.notes})
            db.session.commit()
        return jsonify(svc.daily_summary(profile, date_str))
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/days/<date_str>/closing-stock", methods=["PUT"])
@login_required
def closing_stock(date_str):
    if not _operational_writer(): return _error("This role cannot edit vehicle stock", 403)
    data = request.get_json(silent=True) or {}
    profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=date_str).first()
    if day is None: return _error("Create the Sales Day first")
    try:
        svc.set_closing_stock(day, data, current_user(), elevated=_elevated())
        db.session.commit(); return jsonify(svc.daily_summary(profile, date_str))
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/days/<date_str>/closing-stock/preview", methods=["POST"])
@login_required
def closing_stock_preview(date_str):
    if not _elevated(): return _error("Manager or Super Administrator authority is required", 403)
    data = request.get_json(silent=True) or {}; profile, err = _profile_response(data.get("sales_profile_id"))
    if err: return err
    try:
        product = db.session.get(Product, data.get("product_id"))
        if product is None: raise svc.SalesError("product not found")
        rule = svc._rule_for(product.id, date_str)
        new_closing = 0 if data.get("quantity") == 0 else svc.unit_to_base(data.get("quantity"), data.get("unit"), rule)
        row = next(r for r in svc.stock_reconciliation(profile, date_str) if r["product_id"] == product.id)
        if row["available"] is None: raise svc.SalesError("A starting point is required for this salesperson before this date. Ask a Manager to set the opening returns.")
        available = row["available"]["base_unit_qty"]
        if new_closing > available: raise svc.SalesError("Return today cannot exceed total stock.")
        old_derived = row["derived_sold"]["base_unit_qty"] if row["derived_sold"] else None
        new_derived = available - new_closing; recorded = row["recorded_sold"]["base_unit_qty"]
        next_day = SalesDay.query.filter(SalesDay.sales_profile_id == profile.id,
            SalesDay.business_date > svc.valid_date(date_str)).order_by(SalesDay.business_date).first()
        return jsonify({"product_name": product.name,
            "old_closing": row["closing"], "new_closing": svc.quantity_dict(new_closing, rule),
            "old_derived_sold": svc.quantity_dict(old_derived, rule), "new_derived_sold": svc.quantity_dict(new_derived, rule),
            "old_variance": row["variance"], "new_variance": svc.quantity_dict(new_derived - recorded, rule),
            "next_sales_day": next_day.business_date if next_day else None,
            "next_opening": svc.quantity_dict(new_closing, rule) if next_day else None})
    except svc.SalesError as exc: return _error(exc)


@sales_bp.route("/days/<date_str>/close", methods=["POST"])
@login_required
def close_day(date_str):
    if not _operational_writer(): return _error("This role cannot close Sales Days", 403)
    data = request.get_json(silent=True) or {}
    profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=date_str).first()
    if day is None: return _error("Create the Sales Day first")
    try:
        svc.close_day(day, current_user(), data.get("override_reason"), elevated=_elevated())
        db.session.commit(); return jsonify(svc.daily_summary(profile, date_str))
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/days/<date_str>/reopen", methods=["POST"])
@login_required
def reopen_day(date_str):
    if not _elevated(): return _error("Manager or Super Administrator authority is required", 403)
    data = request.get_json(silent=True) or {}; profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=date_str).first()
    reason = (data.get("reason") or "").strip()
    if day is None: return _error("not found", 404)
    if not reason: return _error("reason is required")
    before = day.status; day.status = DAY_REOPENED; day.reopened_by = current_user().id; day.reopened_at = utcnow()
    record_audit(current_user(), "reopen", "sales_day", day.id, before={"status": before}, after={"status": day.status, "reason": reason})
    db.session.commit(); return jsonify(svc.daily_summary(profile, date_str))


@sales_bp.route("/invoices", methods=["POST"])
@login_required
def create_invoice():
    if not _operational_writer(): return _error("This role cannot record sales", 403)
    data = request.get_json(silent=True) or {}; profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    try:
        _require_editable_day(profile, data.get("business_date"))
        invoice, created = svc.create_invoice(profile, data, current_user())
        db.session.commit(); return jsonify(svc.invoice_dict(invoice)), 201 if created else 200
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/opening-receivables", methods=["POST"])
@login_required
def opening_receivable():
    if not _elevated(): return _error("Manager or Super Administrator authority is required", 403)
    data = request.get_json(silent=True) or {}; profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    try:
        invoice, created = svc.create_invoice(profile, data, current_user(), opening=True)
        db.session.commit(); return jsonify(svc.invoice_dict(invoice)), 201 if created else 200
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/invoices/<int:invoice_id>/payments", methods=["POST"])
@login_required
def add_payment(invoice_id):
    invoice = db.session.get(SalesInvoice, invoice_id)
    if invoice is None: return _error("not found", 404)
    profile, err = _profile_response(invoice.sales_profile_id, write=True)
    if err: return err
    if current_user().role == ROLE_VIEWER: return _error("Viewer access is read-only", 403)
    try:
        payment, created = svc.create_payment(invoice, request.get_json(silent=True) or {}, current_user())
        db.session.commit(); return jsonify({"payment": svc.payment_dict(payment), "invoice": svc.invoice_dict(invoice)}), 201 if created else 200
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/debts", methods=["POST"])
@login_required
def create_debt():
    """Record an unpaid amount. Names are free text; no Customer is required."""
    if not _operational_writer(): return _error("This role cannot record debts", 403)
    data = request.get_json(silent=True) or {}
    profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    try:
        business_date = svc.valid_date(data.get("business_date") or business_today())
        _require_editable_day(profile, business_date)
        debt, created = svc.create_debt(profile, {**data, "business_date": business_date}, current_user())
        db.session.commit()
        return jsonify({"id": debt.id, "debtor_id": debt.debtor_id,
                        "name": debt.debtor.name if debt.debtor else None,
                        "amount_ugx": debt.amount_ugx, "kind": debt.kind}), 201 if created else 200
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/opening-debts", methods=["POST"])
@login_required
def opening_debt():
    """Historical debt established by a Manager, using a free-text name."""
    if not _elevated(): return _error("Manager or Super Administrator authority is required", 403)
    data = request.get_json(silent=True) or {}
    profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    try:
        debt, created = svc.create_debt(profile, data, current_user(), kind=DEBT_OPENING)
        db.session.commit()
        return jsonify({"id": debt.id, "debtor_id": debt.debtor_id,
                        "name": debt.debtor.name if debt.debtor else None,
                        "amount_ugx": debt.amount_ugx, "kind": debt.kind}), 201 if created else 200
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/debtors/<int:debtor_id>/payments", methods=["POST"])
@login_required
def add_debt_payment(debtor_id):
    debtor = db.session.get(SalesDebtor, debtor_id)
    if debtor is None: return _error("not found", 404)
    profile, err = _profile_response(debtor.sales_profile_id, write=True)
    if err: return err
    data = request.get_json(silent=True) or {}
    try:
        payment_date = svc.valid_date(data.get("payment_date") or business_today(), "payment_date")
        day = SalesDay.query.filter_by(sales_profile_id=profile.id, business_date=payment_date).first()
        if day is not None and day.status == DAY_CLOSED:
            raise svc.SalesError("A closed Sales Day must be reopened before it can be edited")
        payment, created = svc.record_debt_payment(debtor, {**data, "payment_date": payment_date}, current_user())
        db.session.commit()
        return jsonify({
            "payment": {"id": payment.id, "debtor_id": payment.debtor_id, "payment_date": payment.payment_date,
                        "amount_ugx": payment.amount_ugx, "payment_method": payment.payment_method,
                        "status": payment.status},
            "debtor": svc.debtor_dict(debtor), "outstanding_ugx": svc.debtor_balance(debtor),
        }), 201 if created else 200
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/debtor-names")
@login_required
def debtor_names():
    """Existing debtor names for autocomplete (case-insensitive identity)."""
    profile, err = _profile_response(request.args.get("sales_profile_id"))
    if err: return err
    return jsonify(svc.debtor_names([profile.id]))


def _create_simple(model, fields, entity):
    data = request.get_json(silent=True) or {}; profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    key = str(data.get("idempotency_key") or "").strip()
    if not key: return _error("idempotency_key is required")
    existing = model.query.filter_by(idempotency_key=key).first()
    if existing:
        if existing.sales_profile_id != profile.id: return _error("idempotency key is already in use")
        return jsonify({"id": existing.id}), 200
    try:
        business_date = svc.valid_date(data.get("business_date"))
        _require_editable_day(profile, business_date)
        kwargs = {"sales_profile_id": profile.id, "idempotency_key": key, "created_by": current_user().id,
                  "business_date": business_date}
        for name, validator in fields.items(): kwargs[name] = validator(data.get(name))
        row = model(**kwargs); db.session.add(row); db.session.flush()
        record_audit(current_user(), "create", entity, row.id, after={k: v for k, v in kwargs.items() if k != "idempotency_key"})
        db.session.commit(); return jsonify({"id": row.id}), 201
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/expenses", methods=["POST"])
@login_required
def create_expense():
    if not _operational_writer(): return _error("This role cannot record expenses", 403)
    def text(v):
        v = (v or "").strip()
        if not v: raise svc.SalesError("description is required")
        return v
    def method(v):
        if v not in ("cash", "mobile_money", "other"): raise svc.SalesError("invalid expense payment method")
        return v
    return _create_simple(SalesExpense, {"description": text, "amount_ugx": lambda v: svc.whole_ugx(v, "amount_ugx", positive=True),
                                         "payment_method": method, "notes": lambda v: (v or "").strip() or None}, "sales_expense")


@sales_bp.route("/transfers", methods=["POST"])
@login_required
def create_transfer():
    if not _operational_writer(): return _error("This role cannot record transfers", 403)
    def account(v):
        if v not in MONEY_ACCOUNTS: raise svc.SalesError("invalid money account")
        return v
    data = request.get_json(silent=True) or {}
    if data.get("from_account") == data.get("to_account"): return _error("from_account and to_account must differ")
    return _create_simple(SalesMoneyTransfer, {"from_account": account, "to_account": account,
        "amount_ugx": lambda v: svc.whole_ugx(v, "amount_ugx", positive=True),
        "reference": lambda v: (v or "").strip() or None, "notes": lambda v: (v or "").strip() or None}, "sales_money_transfer")


@sales_bp.route("/stock-adjustments", methods=["POST"])
@login_required
def stock_adjustment():
    if not _elevated(): return _error("Manager or Super Administrator authority is required", 403)
    data = request.get_json(silent=True) or {}; profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    try:
        product = db.session.get(Product, data.get("product_id"))
        if product is None: raise svc.SalesError("product not found")
        business_date = svc.valid_date(data.get("business_date")); rule = svc._rule_for(product.id, business_date)
        base = svc.unit_to_base(data.get("quantity"), data.get("unit"), rule)
        if data.get("direction") == "remove": base = -base
        elif data.get("direction") != "add": raise svc.SalesError("direction must be add or remove")
        reason = (data.get("reason") or "").strip()
        if not reason: raise svc.SalesError("reason is required")
        row = SalesStockAdjustment(sales_profile_id=profile.id, business_date=business_date, product_id=product.id,
             base_unit_delta=base, packaging_rule_id=rule.id, reason=reason,
             notes=(data.get("notes") or "").strip() or None, created_by=current_user().id)
        db.session.add(row); db.session.flush(); record_audit(current_user(), "create", "sales_stock_adjustment", row.id, after={"base_unit_delta": base, "reason": reason})
        db.session.commit(); return jsonify({"id": row.id}), 201
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/cash-adjustments", methods=["POST"])
@login_required
def cash_adjustment():
    if not _elevated(): return _error("Manager or Super Administrator authority is required", 403)
    data = request.get_json(silent=True) or {}; profile, err = _profile_response(data.get("sales_profile_id"), write=True)
    if err: return err
    try:
        account = data.get("account", "cash")
        if account not in ("cash", "mobile_money"): raise svc.SalesError("account must be cash or mobile_money")
        delta = svc.whole_ugx(data.get("amount_delta_ugx"), "amount_delta_ugx", nonnegative=False)
        if delta == 0: raise svc.SalesError("amount_delta_ugx cannot be zero")
        reason = (data.get("reason") or "").strip()
        if not reason: raise svc.SalesError("reason is required")
        row = SalesCashAdjustment(sales_profile_id=profile.id, business_date=svc.valid_date(data.get("business_date")),
                                  account=account, amount_delta_ugx=delta, reason=reason, created_by=current_user().id)
        db.session.add(row); db.session.flush(); record_audit(current_user(), "create", "sales_cash_adjustment", row.id, after={"account": account, "amount_delta_ugx": delta, "reason": reason})
        db.session.commit(); return jsonify({"id": row.id}), 201
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/void/<entity>/<int:record_id>", methods=["POST"])
@login_required
def void_entity(entity, record_id):
    if not _elevated(): return _error("Manager or Super Administrator authority is required", 403)
    mapping = {"invoice": SalesInvoice, "payment": SalesPayment, "expense": SalesExpense,
               "transfer": SalesMoneyTransfer, "stock-adjustment": SalesStockAdjustment,
               "debt": SalesDebt, "debt-payment": SalesDebtPayment}
    model = mapping.get(entity)
    if model is None: return _error("unknown entity", 404)
    row = db.session.get(model, record_id)
    if row is None: return _error("not found", 404)
    profile_id = row.sales_profile_id if hasattr(row, "sales_profile_id") else row.invoice.sales_profile_id
    _, err = _profile_response(profile_id, write=True)
    if err: return err
    try:
        svc.void_record(row, current_user(), (request.get_json(silent=True) or {}).get("reason"))
        db.session.commit(); return jsonify({"ok": True})
    except svc.SalesError as exc:
        db.session.rollback(); return _error(exc)


@sales_bp.route("/debtors")
@login_required
def debtors():
    if current_user().role == ROLE_OPERATOR:
        profile, err = _profile_response(request.args.get("sales_profile_id"))
        if err: return err
        ids = [profile.id]
    else:
        ids = [int(request.args["sales_profile_id"])] if request.args.get("sales_profile_id") else None
    try: return jsonify(svc.debtors(ids, request.args.get("as_of")))
    except svc.SalesError as exc: return _error(exc)


@sales_bp.route("/history")
@login_required
def history():
    try:
        date_from = svc.valid_date(request.args.get("date_from"), "date_from"); date_to = svc.valid_date(request.args.get("date_to"), "date_to")
        if current_user().role == ROLE_OPERATOR:
            profile, err = _profile_response(request.args.get("sales_profile_id"));
            if err: return err
            profiles = [profile]
        elif request.args.get("sales_profile_id"):
            profile, err = _profile_response(request.args.get("sales_profile_id"));
            if err: return err
            profiles = [profile]
        else: profiles = SalesProfile.query.order_by(SalesProfile.display_name).all()
        rows = []
        for profile in profiles:
            dates = [d for (d,) in db.session.query(SalesDay.business_date).filter(SalesDay.sales_profile_id == profile.id,
                    SalesDay.business_date >= date_from, SalesDay.business_date <= date_to).order_by(SalesDay.business_date)]
            rows.extend(svc.daily_summary(profile, d) for d in dates)
        return jsonify(rows)
    except svc.SalesError as exc: return _error(exc)


@sales_bp.route("/first-divergence")
@login_required
def first_divergence():
    profile, err = _profile_response(request.args.get("sales_profile_id"))
    if err: return err
    try: return jsonify(svc.first_divergence(profile, request.args.get("date_from"), request.args.get("date_to")))
    except svc.SalesError as exc: return _error(exc)


@sales_bp.route("/reports/daily.<fmt>")
@login_required
def daily_export(fmt):
    profile, err = _profile_response(request.args.get("sales_profile_id"))
    if err: return err
    try:
        business_date = svc.valid_date(request.args.get("business_date")); summary = svc.daily_summary(profile, business_date)
        rows = [{"section": "Stock", "item": r["product_name"], "detail": f"Previous Return {r['opening']['label'] if r['opening'] else 'required'}; Stock Added {r['added']['label']}; Return Today {r['closing']['label'] if r['closing'] else 'not counted'}; Sold {r['derived_sold']['label'] if r['derived_sold'] else 'pending'}", "amount": ""} for r in summary["stock"]]
        rows += [{"section": "Sale", "item": inv["reference"], "detail": inv["customer_name"] or "Paid sale", "amount": inv["total_ugx"]} for inv in summary["invoices"] if not inv["is_opening_receivable"]]
        rows += [{"section": "Unpaid debt", "item": d["name"] or "", "detail": d["kind"], "amount": d["amount_ugx"]} for d in summary["debts"]]
        rows += [{"section": "Expense", "item": e["description"], "detail": e["payment_method"], "amount": e["amount_ugx"]} for e in summary["expenses"]]
        for label, key in (("Sales", "total_sales_ugx"), ("Less unpaid debts", "new_debt_ugx"),
                           ("Less Mobile Money", "mobile_money_from_sales_ugx"),
                           ("Cash from today's sales", "cash_from_sales_ugx"),
                           ("Expenses", "total_expenses_ugx"), ("Expected Cash", "expected_cash_ugx"),
                           ("Cash in Hand", "actual_cash_ugx"), ("Difference", "cash_variance_ugx")):
            rows.append({"section": "Money", "item": label, "detail": "", "amount": summary["totals"][key]})
        content = build_export(fmt, title="Daily Sales & Cash Summary",
            filters={"date": business_date, "salesperson": profile.display_name}, generated_by=current_user().username,
            columns=[("section", "Section"), ("item", "Item"), ("detail", "Detail"), ("amount", "UGX Amount")], rows=rows,
            totals={"section": "TOTAL", "item": "Expected Cash", "amount": summary["totals"]["expected_cash_ugx"]},
            **branding_service.export_kwargs())
        record_audit(current_user(), "export", "sales_daily_report", after={"profile_id": profile.id, "date": business_date, "format": fmt})
        db.session.commit()
        return Response(content, mimetype=MIME_TYPES[fmt], headers={"Content-Disposition": f"attachment; filename=sales-{business_date}.{fmt}"})
    except (svc.SalesError, ValueError, KeyError) as exc: return _error(exc)


@sales_bp.route("/reports/reconciliation.<fmt>")
@login_required
def reconciliation_export(fmt):
    try:
        date_from = svc.valid_date(request.args.get("date_from"), "date_from")
        date_to = svc.valid_date(request.args.get("date_to"), "date_to")
        if current_user().role == ROLE_OPERATOR:
            profile, err = _profile_response(request.args.get("sales_profile_id"))
            if err: return err
            profiles = [profile]
        elif request.args.get("sales_profile_id"):
            profile, err = _profile_response(request.args.get("sales_profile_id"))
            if err: return err
            profiles = [profile]
        else:
            profiles = SalesProfile.query.order_by(SalesProfile.display_name).all()
        rows = []
        for profile in profiles:
            dates = [d for (d,) in db.session.query(SalesDay.business_date).filter(
                SalesDay.sales_profile_id == profile.id, SalesDay.business_date >= date_from,
                SalesDay.business_date <= date_to).order_by(SalesDay.business_date)]
            for day_date in dates:
                summary = svc.daily_summary(profile, day_date); totals = summary["totals"]
                rows.append({
                    "date": day_date, "salesperson": profile.display_name,
                    "sales_category": profile.sales_category.name if profile.sales_category else "",
                    "total_sales": totals["total_sales_ugx"], "cash_from_sales": totals["cash_from_sales_ugx"],
                    "mobile_from_sales": totals["mobile_money_from_sales_ugx"],
                    "old_debt": totals["old_debt_collections_ugx"], "new_debt": totals["new_debt_ugx"],
                    "expenses": totals["total_expenses_ugx"], "net_sales": totals["net_sales_after_expenses_ugx"],
                    "expected_cash": totals["expected_cash_ugx"], "actual_cash": totals["actual_cash_ugx"],
                    "cash_variance": totals["cash_variance_ugx"],
                    "stock_status": "Needs reconciliation" if summary["needs_reconciliation"] else "Balanced",
                    "day_status": summary["day"]["status"] if summary["day"] else "",
                })
        columns = [("date", "Date"), ("salesperson", "Salesperson"), ("sales_category", "Sales Channel"),
                   ("total_sales", "Sales UGX"), ("cash_from_sales", "Cash From Sales UGX"),
                   ("mobile_from_sales", "Mobile Money UGX"), ("old_debt", "Old Debts Collected UGX"),
                   ("new_debt", "New Debts UGX"), ("expenses", "Expenses UGX"),
                   ("net_sales", "Sales Less Expenses UGX"), ("expected_cash", "Expected Cash UGX"),
                   ("actual_cash", "Cash in Hand UGX"), ("cash_variance", "Difference UGX"),
                   ("stock_status", "Stock Reconciliation"), ("day_status", "Day Status")]
        content = build_export(fmt, title="Sales Reconciliation History",
            filters={"date_from": date_from, "date_to": date_to, "sales_profile_id": request.args.get("sales_profile_id")},
            generated_by=current_user().username, columns=columns, rows=rows,
            **branding_service.export_kwargs())
        record_audit(current_user(), "export", "sales_reconciliation_report",
                     after={"date_from": date_from, "date_to": date_to, "format": fmt, "row_count": len(rows)})
        db.session.commit()
        return Response(content, mimetype=MIME_TYPES[fmt], headers={"Content-Disposition": f"attachment; filename=sales-reconciliation-{date_from}-to-{date_to}.{fmt}"})
    except (svc.SalesError, ValueError, KeyError) as exc:
        return _error(exc)


def _report_profiles():
    """
    The profiles a report may cover. An Operator is always narrowed to their
    own profile, exactly like every other Sales read; everyone else may pass
    one profile id or omit it to report across all profiles.
    """
    if current_user().role == ROLE_OPERATOR:
        profile, err = _profile_response(request.args.get("sales_profile_id"))
        if err:
            return None, err
        return [profile], None
    requested = request.args.get("sales_profile_id")
    if requested:
        profile, err = _profile_response(requested)
        if err:
            return None, err
        return [profile], None
    return SalesProfile.query.order_by(SalesProfile.display_name).all(), None


def _export_response(fmt, *, title, filters, columns, rows, totals, filename, audit_entity, audit_detail):
    content = build_export(fmt, title=title, filters=filters, generated_by=current_user().username,
                           columns=columns, rows=rows, totals=totals, **branding_service.export_kwargs())
    record_audit(current_user(), "export", audit_entity,
                 after={**audit_detail, "format": fmt, "row_count": len(rows)})
    db.session.commit()
    return Response(content, mimetype=MIME_TYPES[fmt],
                    headers={"Content-Disposition": f"attachment; filename={filename}.{fmt}"})


@sales_bp.route("/reports/debtors.<fmt>")
@login_required
def debtors_export(fmt):
    profiles, err = _report_profiles()
    if err: return err
    try:
        as_of = request.args.get("as_of") or None
        rows = svc.debtors([p.id for p in profiles], as_of)
        columns = [("salesperson", "Salesperson"), ("customer", "Debtor"), ("reference", "Reference"),
                   ("business_date", "Owing Since"), ("days_outstanding", "Days Outstanding"),
                   ("aging_bucket", "Aging"), ("total_ugx", "Owed UGX"),
                   ("paid_ugx", "Paid UGX"), ("outstanding_ugx", "Outstanding UGX")]
        report_rows = [{**r, "salesperson": r.get("salesperson") or "", "customer": r.get("customer_name") or ""}
                       for r in rows]
        return _export_response(
            fmt, title="Sales Debtors (Outstanding Debts)",
            filters={"as_of": as_of or "current", "salespeople": ", ".join(p.display_name for p in profiles) or "All"},
            columns=columns, rows=report_rows,
            totals={"customer": "TOTAL", "outstanding_ugx": sum(r["outstanding_ugx"] for r in report_rows)},
            filename=f"sales-debtors-{as_of or 'current'}", audit_entity="sales_debtors_report",
            audit_detail={"profile_ids": [p.id for p in profiles], "as_of": as_of})
    except (svc.SalesError, ValueError, KeyError) as exc:
        return _error(exc)



@sales_bp.route("/reports/payments.<fmt>")
@login_required
def payments_export(fmt):
    profiles, err = _report_profiles()
    if err: return err
    try:
        date_from = svc.valid_date(request.args.get("date_from"), "date_from")
        date_to = svc.valid_date(request.args.get("date_to"), "date_to")
        names = {p.id: p.display_name for p in profiles}
        payments = SalesPayment.query.filter(
            SalesPayment.sales_profile_id.in_([p.id for p in profiles]),
            SalesPayment.status == ACTIVE,
            SalesPayment.payment_date >= date_from, SalesPayment.payment_date <= date_to,
        ).order_by(SalesPayment.payment_date, SalesPayment.id).all()
        rows = [{
            "date": p.payment_date, "salesperson": names.get(p.sales_profile_id, ""),
            "invoice": p.invoice.reference if p.invoice else "",
            "customer": p.invoice.customer.name if p.invoice and p.invoice.customer else "Paid sale",
            "method": p.payment_method, "amount_ugx": p.amount_ugx,
            "invoice_date": p.invoice.business_date if p.invoice else "",
            "reference": p.reference or "", "notes": p.notes or "",
        } for p in payments]
        # Debt collections recorded in the Debtors workspace are receipts too.
        debt_payments = SalesDebtPayment.query.filter(
            SalesDebtPayment.sales_profile_id.in_([p.id for p in profiles]),
            SalesDebtPayment.status == ACTIVE,
            SalesDebtPayment.payment_date >= date_from, SalesDebtPayment.payment_date <= date_to,
        ).order_by(SalesDebtPayment.payment_date, SalesDebtPayment.id).all()
        rows += [{
            "date": p.payment_date, "salesperson": names.get(p.sales_profile_id, ""),
            "invoice": "Unpaid amount", "customer": p.debtor.name if p.debtor else "",
            "method": p.payment_method, "amount_ugx": p.amount_ugx,
            "invoice_date": "", "reference": p.reference or "", "notes": p.notes or "",
        } for p in debt_payments]
        rows.sort(key=lambda item: (item["date"], item["invoice"]))
        columns = [("date", "Receipt Date"), ("salesperson", "Salesperson"), ("invoice", "Sale"),
                   ("customer", "Debtor"), ("method", "Received As"), ("amount_ugx", "Amount UGX"),
                   ("invoice_date", "Sale Date"), ("reference", "Reference"), ("notes", "Notes")]
        return _export_response(
            fmt, title="Sales Money Received",
            filters={"date_from": date_from, "date_to": date_to,
                     "salespeople": ", ".join(p.display_name for p in profiles) or "All"},
            columns=columns, rows=rows,
            totals={"date": "TOTAL", "amount_ugx": sum(r["amount_ugx"] for r in rows)},
            filename=f"sales-payments-{date_from}-to-{date_to}", audit_entity="sales_payments_report",
            audit_detail={"profile_ids": [p.id for p in profiles], "date_from": date_from, "date_to": date_to})
    except (svc.SalesError, ValueError, KeyError) as exc:
        return _error(exc)


@sales_bp.route("/reports/expenses.<fmt>")
@login_required
def expenses_export(fmt):
    profiles, err = _report_profiles()
    if err: return err
    try:
        date_from = svc.valid_date(request.args.get("date_from"), "date_from")
        date_to = svc.valid_date(request.args.get("date_to"), "date_to")
        names = {p.id: p.display_name for p in profiles}
        expenses = SalesExpense.query.filter(
            SalesExpense.sales_profile_id.in_([p.id for p in profiles]),
            SalesExpense.status == ACTIVE,
            SalesExpense.business_date >= date_from, SalesExpense.business_date <= date_to,
        ).order_by(SalesExpense.business_date, SalesExpense.id).all()
        rows = [{
            "date": e.business_date, "salesperson": names.get(e.sales_profile_id, ""),
            "description": e.description, "method": e.payment_method,
            "amount_ugx": e.amount_ugx, "notes": e.notes or "",
        } for e in expenses]
        columns = [("date", "Date"), ("salesperson", "Salesperson"), ("description", "Description"),
                   ("method", "Paid By"), ("amount_ugx", "Amount UGX"), ("notes", "Notes")]
        return _export_response(
            fmt, title="Sales Expenses",
            filters={"date_from": date_from, "date_to": date_to,
                     "salespeople": ", ".join(p.display_name for p in profiles) or "All"},
            columns=columns, rows=rows,
            totals={"date": "TOTAL", "amount_ugx": sum(r["amount_ugx"] for r in rows)},
            filename=f"sales-expenses-{date_from}-to-{date_to}", audit_entity="sales_expenses_report",
            audit_detail={"profile_ids": [p.id for p in profiles], "date_from": date_from, "date_to": date_to})
    except (svc.SalesError, ValueError, KeyError) as exc:
        return _error(exc)


@sales_bp.route("/reports/stock.<fmt>")
@login_required
def stock_export(fmt):
    profiles, err = _report_profiles()
    if err: return err
    try:
        date_from = svc.valid_date(request.args.get("date_from"), "date_from")
        date_to = svc.valid_date(request.args.get("date_to"), "date_to")
        rows = []
        for profile in profiles:
            dates = [d for (d,) in db.session.query(SalesDay.business_date).filter(
                SalesDay.sales_profile_id == profile.id, SalesDay.business_date >= date_from,
                SalesDay.business_date <= date_to).order_by(SalesDay.business_date)]
            for day_date in dates:
                for line in svc.stock_reconciliation(profile, day_date):
                    rows.append({
                        "date": day_date, "salesperson": profile.display_name,
                        "product": line["product_name"],
                        "opening": line["opening"]["label"] if line["opening"] else "Required",
                        "loaded": line["added"]["label"], "adjustment": line["manual_adjustment"]["label"],
                        "available": line["available"]["label"] if line["available"] else "",
                        "closing": line["closing"]["label"] if line["closing"] else "Not counted",
                        "stock_sold": line["derived_sold"]["label"] if line["derived_sold"] else "Pending",
                        "priced_sold": line["recorded_sold"]["label"],
                        "variance": line["variance"]["label"] if line["variance"] else "",
                    })
        columns = [("date", "Date"), ("salesperson", "Salesperson"), ("product", "Product"),
                   ("opening", "Previous Return"), ("loaded", "Stock Added"), ("adjustment", "Adjustment"),
                   ("available", "Total Stock"), ("closing", "Return Today"), ("stock_sold", "Sold"),
                   ("priced_sold", "Priced"), ("variance", "Difference")]
        return _export_response(
            fmt, title="Sales Stock-to-Sales Reconciliation",
            filters={"date_from": date_from, "date_to": date_to,
                     "salespeople": ", ".join(p.display_name for p in profiles) or "All"},
            columns=columns, rows=rows, totals=None,
            filename=f"sales-stock-reconciliation-{date_from}-to-{date_to}",
            audit_entity="sales_stock_report",
            audit_detail={"profile_ids": [p.id for p in profiles], "date_from": date_from, "date_to": date_to})
    except (svc.SalesError, ValueError, KeyError) as exc:
        return _error(exc)

