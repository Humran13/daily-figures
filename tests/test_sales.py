from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from webapp.extensions import db
from webapp.models.customer import CATEGORY_CUSTOMER, CATEGORY_SALESPERSON, Customer
from webapp.models.audit_log import AuditLog
from webapp.models.dispatch import Dispatch, DispatchLine, STATUS_FINALIZED
from webapp.models.packaging_rule import PackagingRule
from webapp.models.product import Product
from webapp.models.sales import (
    SalesBaseline, SalesDay, SalesDebtor, SalesExpense, SalesInvoice, SalesOpeningStockLine, SalesProfile,
    SalesVehicleClosingLine,
)
from webapp.models.section import SECTION_SALES, Section
from webapp.models.user import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, ROLE_VIEWER
from webapp.services import sales_service, section_access_service


def _grant(user, code=SECTION_SALES):
    section_access_service.ensure_seeded()
    section_access_service.set_sections_for_user(user, [code], user)
    db.session.commit()


def _login(client, user, password="password123", section=SECTION_SALES):
    response = client.post("/api/login", json={"username": user.username, "password": password, "section": section})
    assert response.status_code == 200, response.get_json()


@pytest.fixture
def sales_setup(app, make_user):
    manager = make_user("sales-manager", "password123", ROLE_MANAGER)
    operator = make_user("field-sales", "password123", ROLE_OPERATOR)
    other = make_user("other-sales", "password123", ROLE_OPERATOR)
    _grant(manager); _grant(operator); _grant(other)

    recipient = Customer(name="Field Sales Dispatch", category=CATEGORY_SALESPERSON, active=True)
    customer = Customer(name="Kampala Retailer", category=CATEGORY_CUSTOMER, active=True)
    compact = Product(name="Compact Standard", display_order=1, active=True)
    napkin = Product(name="Napkin Standard", display_order=2, active=True)
    db.session.add_all([recipient, customer, compact, napkin]); db.session.flush()
    compact_rule = PackagingRule(product_id=compact.id, cartons_to_packs=10, packs_to_pieces=10,
                                  effective_from=datetime(2026, 1, 1), created_by=manager.id)
    napkin_rule = PackagingRule(product_id=napkin.id, cartons_to_packs=10, packs_to_pieces=10,
                                 effective_from=datetime(2026, 1, 1), created_by=manager.id)
    db.session.add_all([compact_rule, napkin_rule]); db.session.flush()
    profile = SalesProfile(user_id=operator.id, display_name="Metro Salesperson", recipient_customer_id=recipient.id)
    other_profile = SalesProfile(user_id=other.id, display_name="Other Salesperson")
    db.session.add_all([profile, other_profile]); db.session.flush()
    baseline = SalesBaseline(sales_profile_id=profile.id, effective_date="2026-10-30",
                             opening_cash_ugx=0, opening_mobile_money_ugx=0, created_by=manager.id)
    db.session.add(baseline); db.session.flush()
    db.session.add_all([
        SalesOpeningStockLine(baseline_id=baseline.id, product_id=compact.id, base_unit_qty=50 * 100, packaging_rule_id=compact_rule.id),
        SalesOpeningStockLine(baseline_id=baseline.id, product_id=napkin.id, base_unit_qty=10 * 100, packaging_rule_id=napkin_rule.id),
    ])
    dispatch = Dispatch(dispatch_number="D-1030", date="2026-10-30", shift="Day", customer_id=recipient.id,
                        status=STATUS_FINALIZED, created_by=manager.id)
    db.session.add(dispatch); db.session.flush()
    db.session.add_all([
        DispatchLine(dispatch_id=dispatch.id, product_id=compact.id, cartons=100, packs=0, pieces=0,
                     base_unit_qty=100 * 100, packaging_rule_id=compact_rule.id),
        DispatchLine(dispatch_id=dispatch.id, product_id=napkin.id, cartons=20, packs=0, pieces=0,
                     base_unit_qty=20 * 100, packaging_rule_id=napkin_rule.id),
    ])
    db.session.commit()
    return dict(manager=manager, operator=operator, other=other, profile=profile, other_profile=other_profile,
                customer=customer, compact=compact, napkin=napkin)


def _post_sale(client, profile_id, product_id, quantity, price, customer_id=None, method="cash", paid=None):
    total = quantity * price
    payment = [] if method == "credit" else [{
        "payment_date": "2026-10-30", "amount_ugx": total if paid is None else paid,
        "payment_method": method, "idempotency_key": str(uuid4()),
    }]
    return client.post("/api/sales/invoices", json={
        "sales_profile_id": profile_id, "business_date": "2026-10-30", "customer_id": customer_id,
        "idempotency_key": str(uuid4()),
        "lines": [{"product_id": product_id, "quantity": quantity, "unit": "carton", "unit_price_ugx": price}],
        "payments": payment,
    })


def test_exact_oct_30_stock_sales_expenses_and_cash(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    response = client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["profile"].id})
    assert response.status_code == 201
    for product, closing in ((s["compact"], 40), (s["napkin"], 5)):
        response = client.put("/api/sales/days/2026-10-30/closing-stock", json={
            "sales_profile_id": s["profile"].id, "product_id": product.id, "quantity": closing, "unit": "carton",
        })
        assert response.status_code == 200, response.get_json()

    for product, qty, price in ((s["compact"], 100, 79000), (s["compact"], 10, 80000),
                                (s["napkin"], 20, 103000), (s["napkin"], 5, 104000)):
        response = _post_sale(client, s["profile"].id, product.id, qty, price)
        assert response.status_code == 201, response.get_json()
    for description, amount in (("Lunch", 10000), ("Fahad", 20000), ("Breakfast", 10000)):
        response = client.post("/api/sales/expenses", json={
            "sales_profile_id": s["profile"].id, "business_date": "2026-10-30", "description": description,
            "amount_ugx": amount, "payment_method": "cash", "idempotency_key": str(uuid4()), "notes": None,
        })
        assert response.status_code == 201, response.get_json()

    summary = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['profile'].id}").get_json()
    compact = next(row for row in summary["stock"] if row["product_id"] == s["compact"].id)
    napkin = next(row for row in summary["stock"] if row["product_id"] == s["napkin"].id)
    assert compact["opening"]["cartons"] == 50
    assert compact["dispatch_loaded"]["cartons"] == 100
    assert compact["available"]["cartons"] == 150
    assert compact["derived_sold"]["cartons"] == compact["recorded_sold"]["cartons"] == 110
    assert napkin["derived_sold"]["cartons"] == napkin["recorded_sold"]["cartons"] == 25
    assert summary["totals"]["total_sales_ugx"] == 11_280_000
    assert summary["totals"]["total_expenses_ugx"] == 40_000
    assert summary["totals"]["net_sales_after_expenses_ugx"] == 11_240_000
    assert summary["totals"]["expected_cash_ugx"] == 11_240_000

    # Server-side idempotency: retrying an identical expense request cannot duplicate it.
    payload = {"sales_profile_id": s["profile"].id, "business_date": "2026-10-30", "description": "Parking",
               "amount_ugx": 1000, "payment_method": "cash", "idempotency_key": "retry-expense", "notes": None}
    assert client.post("/api/sales/expenses", json=payload).status_code == 201
    assert client.post("/api/sales/expenses", json=payload).status_code == 200
    counted = client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": s["profile"].id, "actual_cash_count_ugx": 11_240_000})
    assert counted.status_code == 200
    closed = client.post("/api/sales/days/2026-10-30/close", json={"sales_profile_id": s["profile"].id})
    assert closed.status_code == 200, closed.get_json()
    assert client.post("/api/sales/expenses", json={**payload, "idempotency_key": "after-close"}).status_code == 400


def test_historical_closing_correction_propagates_without_editing_sales(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["profile"].id})
    client.put("/api/sales/days/2026-10-30/closing-stock", json={"sales_profile_id": s["profile"].id,
                    "product_id": s["compact"].id, "quantity": 40, "unit": "carton"})
    assert _post_sale(client, s["profile"].id, s["compact"].id, 110, 79000).status_code == 201
    client.put("/api/sales/days/2026-10-30/closing-stock", json={"sales_profile_id": s["profile"].id,
                    "product_id": s["napkin"].id, "quantity": 30, "unit": "carton"})
    assert client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": s["profile"].id, "actual_cash_count_ugx": 8_690_000}).status_code == 200
    assert client.post("/api/sales/days/2026-10-30/close", json={"sales_profile_id": s["profile"].id}).status_code == 200
    invoice = SalesInvoice.query.filter_by(sales_profile_id=s["profile"].id).one()
    original_price = invoice.lines[0].unit_price_ugx

    _login(client, s["manager"])
    assert client.post("/api/sales/days/2026-10-30/reopen", json={"sales_profile_id": s["profile"].id,
                       "reason": "Investigate physical count"}).status_code == 200
    response = client.put("/api/sales/days/2026-10-30/closing-stock", json={"sales_profile_id": s["profile"].id,
                    "product_id": s["compact"].id, "quantity": 45, "unit": "carton", "reason": "Physical count investigation"})
    assert response.status_code == 200, response.get_json()
    row = next(r for r in response.get_json()["stock"] if r["product_id"] == s["compact"].id)
    assert row["derived_sold"]["cartons"] == 105
    assert row["recorded_sold"]["cartons"] == 110
    assert row["variance"]["cartons"] == 5 and row["variance"]["negative"] is True
    db.session.refresh(invoice)
    assert invoice.lines[0].quantity == 110 and invoice.lines[0].unit_price_ugx == original_price
    correction_audit = AuditLog.query.filter_by(entity_type="sales_vehicle_closing", action="correct").order_by(AuditLog.id.desc()).first()
    assert correction_audit is not None
    assert correction_audit.to_dict()["before"]["base_unit_qty"] == 40 * 100
    assert correction_audit.to_dict()["after"]["base_unit_qty"] == 45 * 100
    assert correction_audit.to_dict()["after"]["reason"] == "Physical count investigation"

    client.post("/api/sales/days", json={"business_date": "2026-10-31", "sales_profile_id": s["profile"].id})
    next_day = client.get(f"/api/sales/days/2026-10-31?sales_profile_id={s['profile'].id}").get_json()
    next_compact = next(r for r in next_day["stock"] if r["product_id"] == s["compact"].id)
    assert next_compact["opening"]["cartons"] == 45


def test_credit_late_payment_and_transfer_keep_event_dates(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["profile"].id})
    response = _post_sale(client, s["profile"].id, s["compact"].id, 10, 100000,
                          customer_id=s["customer"].id, method="credit")
    invoice_id = response.get_json()["id"]
    first = client.post(f"/api/sales/invoices/{invoice_id}/payments", json={
        "amount_ugx": 400000, "payment_method": "cash", "payment_date": "2026-10-30", "idempotency_key": str(uuid4())})
    assert first.status_code == 201
    late = client.post(f"/api/sales/invoices/{invoice_id}/payments", json={
        "amount_ugx": 600000, "payment_method": "mobile_money", "payment_date": "2026-11-02", "idempotency_key": str(uuid4())})
    assert late.status_code == 201
    invoice = late.get_json()["invoice"]
    assert invoice["outstanding_ugx"] == 0
    assert sales_service.outstanding(db.session.get(SalesInvoice, invoice_id), "2026-10-30") == 600000
    assert sales_service.money_balance(s["profile"].id, "2026-10-30", "cash") == 400000
    assert sales_service.money_balance(s["profile"].id, "2026-11-02", "mobile_money") == 600000

    client.post("/api/sales/days", json={"business_date": "2026-11-02", "sales_profile_id": s["profile"].id})
    response = client.post("/api/sales/transfers", json={"sales_profile_id": s["profile"].id,
        "business_date": "2026-11-02", "from_account": "cash", "to_account": "mobile_money",
        "amount_ugx": 100000, "idempotency_key": str(uuid4())})
    assert response.status_code == 201
    assert sales_service.money_balance(s["profile"].id, "2026-11-02", "cash") == 300000
    assert sales_service.money_balance(s["profile"].id, "2026-11-02", "mobile_money") == 700000


def test_mixed_cash_mobile_credit_and_cash_to_mobile_acceptance(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["profile"].id})
    cases = [
        (s["compact"], 100, 79000, s["customer"].id,
         [(6_900_000, "cash")]),  # 1,000,000 remains a real customer receivable
        (s["compact"], 10, 80000, None, [(800_000, "cash")]),
        (s["napkin"], 20, 103000, None, [(2_000_000, "mobile_money"), (60_000, "cash")]),
        (s["napkin"], 5, 104000, None, [(520_000, "cash")]),
    ]
    for product, qty, price, customer_id, payments in cases:
        response = client.post("/api/sales/invoices", json={
            "sales_profile_id": s["profile"].id, "business_date": "2026-10-30",
            "customer_id": customer_id, "idempotency_key": str(uuid4()),
            "lines": [{"product_id": product.id, "quantity": qty, "unit": "carton", "unit_price_ugx": price}],
            "payments": [{"payment_date": "2026-10-30", "amount_ugx": amount,
                          "payment_method": method, "idempotency_key": str(uuid4())} for amount, method in payments],
        })
        assert response.status_code == 201, response.get_json()
    for description, amount in (("Lunch", 10000), ("Fahad", 20000), ("Breakfast", 10000)):
        assert client.post("/api/sales/expenses", json={"sales_profile_id": s["profile"].id,
            "business_date": "2026-10-30", "description": description, "amount_ugx": amount,
            "payment_method": "cash", "idempotency_key": str(uuid4()), "notes": None}).status_code == 201

    summary = sales_service.daily_summary(s["profile"], "2026-10-30")
    assert summary["totals"]["total_sales_ugx"] == 11_280_000
    assert summary["totals"]["cash_receipts_ugx"] == 8_280_000
    assert summary["totals"]["mobile_money_receipts_ugx"] == 2_000_000
    assert summary["totals"]["new_customer_receivables_ugx"] == 1_000_000
    assert summary["totals"]["expected_cash_ugx"] == 8_240_000
    assert summary["totals"]["net_sales_after_expenses_ugx"] == 11_240_000
    assert client.post("/api/sales/transfers", json={"sales_profile_id": s["profile"].id,
        "business_date": "2026-10-30", "from_account": "cash", "to_account": "mobile_money",
        "amount_ugx": 1_000_000, "idempotency_key": str(uuid4())}).status_code == 201
    summary = sales_service.daily_summary(s["profile"], "2026-10-30")
    assert summary["totals"]["expected_cash_ugx"] == 7_240_000
    assert summary["totals"]["expected_mobile_money_ugx"] == 3_000_000
    assert summary["totals"]["total_sales_ugx"] == 11_280_000
    assert summary["totals"]["total_expenses_ugx"] == 40_000


def test_dispatch_void_baseline_required_and_exports(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["profile"].id})
    summary = sales_service.daily_summary(s["profile"], "2026-10-30")
    compact = next(r for r in summary["stock"] if r["product_id"] == s["compact"].id)
    assert compact["dispatch_loaded"]["cartons"] == 100
    dispatch = Dispatch.query.filter_by(dispatch_number="D-1030").one(); dispatch.status = "void"; db.session.commit()
    compact = next(r for r in sales_service.daily_summary(s["profile"], "2026-10-30")["stock"] if r["product_id"] == s["compact"].id)
    assert compact["dispatch_loaded"]["cartons"] == 0  # derived live, never duplicated into Sales

    # The unrelated salesperson has no trusted start and is never silently assumed to be zero.
    other_summary = sales_service.daily_summary(s["other_profile"], "2026-10-30")
    assert other_summary["baseline_required"] is True
    assert all(row["opening_source"] == "required" for row in other_summary["stock"])

    for fmt in ("csv", "xlsx", "pdf"):
        response = client.get(f"/api/sales/reports/daily.{fmt}?business_date=2026-10-30&sales_profile_id={s['profile'].id}")
        assert response.status_code == 200, response.get_json() if response.is_json else response.data[:200]
        assert response.data
        response = client.get(f"/api/sales/reports/reconciliation.{fmt}?date_from=2026-10-30&date_to=2026-10-30&sales_profile_id={s['profile'].id}")
        assert response.status_code == 200
        assert response.data


def test_cash_shortage_does_not_reset_expected_ledger(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["profile"].id})
    response = _post_sale(client, s["profile"].id, s["compact"].id, 1, 11_280_000)
    assert response.status_code == 201
    assert client.post("/api/sales/expenses", json={"sales_profile_id": s["profile"].id,
        "business_date": "2026-10-30", "description": "Field expenses", "amount_ugx": 40_000,
        "payment_method": "cash", "idempotency_key": str(uuid4()), "notes": None}).status_code == 201
    response = client.patch("/api/sales/days/2026-10-30", json={"sales_profile_id": s["profile"].id,
        "actual_cash_count_ugx": 10_000_000})
    totals = response.get_json()["totals"]
    assert totals["expected_cash_ugx"] == 11_240_000
    assert totals["cash_variance_ugx"] == -1_240_000

    client.post("/api/sales/days", json={"business_date": "2026-10-31", "sales_profile_id": s["profile"].id})
    next_day = sales_service.daily_summary(s["profile"], "2026-10-31")
    assert next_day["totals"]["expected_cash_ugx"] == 11_240_000
    assert next_day["totals"]["actual_cash_ugx"] is None
    first = sales_service.first_divergence(s["profile"], "2026-10-30", "2026-10-31")
    assert first["business_date"] == "2026-10-30"


def test_sales_section_and_profile_idor_are_enforced(client, sales_setup):
    s = sales_setup
    # Finished-Goods login cannot call the Sales blueprint.
    manager = s["manager"]
    section_access_service.set_sections_for_user(manager, ["finished_goods", "sales"], manager); db.session.commit()
    _login(client, manager, section="finished_goods")
    assert client.get("/api/sales/metadata").status_code == 403

    _login(client, s["operator"])
    response = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['other_profile'].id}")
    assert response.status_code == 403
    response = client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["other_profile"].id})
    assert response.status_code == 403
    assert client.get("/api/dispatches").status_code == 403


def test_accountant_payment_scope_viewer_read_only_and_overpayment(client, sales_setup, make_user):
    s = sales_setup; _login(client, s["operator"])
    client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["profile"].id})
    invoice_response = _post_sale(client, s["profile"].id, s["compact"].id, 10, 100000,
                                  customer_id=s["customer"].id, method="credit")
    invoice_id = invoice_response.get_json()["id"]

    accountant = make_user("sales-accountant", "password123", ROLE_ACCOUNTANT); _grant(accountant); _login(client, accountant)
    denied = client.post("/api/sales/invoices", json={"sales_profile_id": s["profile"].id})
    assert denied.status_code == 403
    payment = client.post(f"/api/sales/invoices/{invoice_id}/payments", json={
        "amount_ugx": 400000, "payment_method": "cash", "payment_date": "2026-10-31",
        "idempotency_key": str(uuid4())})
    assert payment.status_code == 201
    overpay = client.post(f"/api/sales/invoices/{invoice_id}/payments", json={
        "amount_ugx": 600001, "payment_method": "cash", "payment_date": "2026-10-31",
        "idempotency_key": str(uuid4())})
    assert overpay.status_code == 400
    assert "exceeds" in overpay.get_json()["error"]

    viewer = make_user("sales-viewer", "password123", ROLE_VIEWER); _grant(viewer); _login(client, viewer)
    assert client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['profile'].id}").status_code == 200
    assert client.post("/api/sales/days", json={"business_date": "2026-10-30", "sales_profile_id": s["profile"].id}).status_code == 403


def test_sales_seed_does_not_auto_grant_ordinary_users(app, make_user):
    ordinary = make_user("no-sales-grant", "password123", ROLE_OPERATOR)
    section_access_service.ensure_seeded(); db.session.flush()
    sales = Section.query.filter_by(code=SECTION_SALES).one()
    assert section_access_service.user_has_section_access(ordinary, sales) is False
    super_admin = make_user("global-admin", "password123", ROLE_SUPER_ADMIN)
    assert section_access_service.user_has_section_access(super_admin, sales) is True


def test_debtor_payment_expense_and_stock_reports_are_scoped_and_audited(client, sales_setup):
    s = sales_setup
    _login(client, s["operator"])
    assert client.post("/api/sales/days", json={
        "business_date": "2026-10-30", "sales_profile_id": s["profile"].id}).status_code == 201
    assert _post_sale(client, s["profile"].id, s["compact"].id, 10, 100_000,
                      customer_id=s["customer"].id, method="credit").status_code == 201
    assert _post_sale(client, s["profile"].id, s["compact"].id, 1, 50_000).status_code == 201
    expense = client.post("/api/sales/expenses", json={
        "sales_profile_id": s["profile"].id, "business_date": "2026-10-30",
        "description": "Lunch", "amount_ugx": 10_000, "payment_method": "cash",
        "idempotency_key": str(uuid4()), "notes": None,
    })
    assert expense.status_code == 201, expense.get_json()

    range_query = f"date_from=2026-10-30&date_to=2026-10-30&sales_profile_id={s['profile'].id}"
    for kind, query in (("debtors", f"as_of=2026-10-30&sales_profile_id={s['profile'].id}"),
                        ("payments", range_query), ("expenses", range_query), ("stock", range_query)):
        for fmt in ("csv", "xlsx", "pdf"):
            response = client.get(f"/api/sales/reports/{kind}.{fmt}?{query}")
            assert response.status_code == 200, response.data[:300]
            assert response.data

    debtors_csv = client.get(
        f"/api/sales/reports/debtors.csv?as_of=2026-10-30&sales_profile_id={s['profile'].id}").data.decode()
    assert "Kampala Retailer" in debtors_csv
    assert "1000000" in debtors_csv
    payments_csv = client.get(f"/api/sales/reports/payments.csv?{range_query}").data.decode()
    assert "50000" in payments_csv and "cash" in payments_csv
    stock_csv = client.get(f"/api/sales/reports/stock.csv?{range_query}").data.decode()
    assert "Compact Standard" in stock_csv
    # Exports are an audited action, not a silent read (3 formats + the CSV re-read).
    assert AuditLog.query.filter_by(action="export", entity_type="sales_debtors_report").count() == 4

    # An Operator can never report on somebody else's profile.
    response = client.get(f"/api/sales/reports/debtors.csv?sales_profile_id={s['other_profile'].id}")
    assert response.status_code == 403
    response = client.get(f"/api/sales/reports/expenses.csv?{range_query.replace(str(s['profile'].id), str(s['other_profile'].id))}")
    assert response.status_code == 403

    # A Manager void removes the expense from the report but keeps the audit trail.
    _login(client, s["manager"])
    assert client.post(f"/api/sales/void/expense/{expense.get_json()['id']}",
                       json={"reason": "entered twice"}).status_code == 200
    expenses_csv = client.get(f"/api/sales/reports/expenses.csv?{range_query}").data.decode()
    assert "Lunch" not in expenses_csv

    # Reporting without an explicit profile covers every profile for a Manager.
    response = client.get("/api/sales/reports/debtors.csv")
    assert response.status_code == 200
    assert "Kampala Retailer" in response.data.decode()


def _open_day(client, profile_id, date="2026-10-30"):
    response = client.post("/api/sales/days", json={"business_date": date, "sales_profile_id": profile_id})
    assert response.status_code in (200, 201), response.get_json()
    return response


def _post_plain_sale(client, profile_id, product_id, quantity, price, date="2026-10-30"):
    """An ordinary paid sale: product, quantity, price. No customer anywhere."""
    return client.post("/api/sales/invoices", json={
        "sales_profile_id": profile_id, "business_date": date, "idempotency_key": str(uuid4()),
        "lines": [{"product_id": product_id, "quantity": quantity, "unit": "carton", "unit_price_ugx": price}],
    })


def _cash_expense(client, profile_id, description, amount, date="2026-10-30"):
    return client.post("/api/sales/expenses", json={
        "sales_profile_id": profile_id, "business_date": date, "description": description,
        "amount_ugx": amount, "payment_method": "cash", "idempotency_key": str(uuid4()), "notes": None,
    })


def test_paid_sale_needs_no_customer_and_keeps_each_price_line(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    customers_before = Customer.query.count()
    for quantity, price in ((100, 79_000), (10, 80_000)):
        response = _post_plain_sale(client, s["profile"].id, s["compact"].id, quantity, price)
        assert response.status_code == 201, response.get_json()
        assert response.get_json()["customer_id"] is None
    summary = sales_service.daily_summary(s["profile"], "2026-10-30")
    assert summary["totals"]["total_sales_ugx"] == 8_700_000
    assert len(summary["invoices"]) == 2
    assert {row["lines"][0]["unit_price_ugx"] for row in summary["invoices"]} == {79_000, 80_000}
    assert summary["totals"]["cash_from_sales_ugx"] == 8_700_000
    assert summary["totals"]["expected_cash_ugx"] == 8_700_000
    # Ordinary sales never create Finished Goods customers and never need one.
    assert Customer.query.count() == customers_before


def test_mobile_money_and_free_text_debt_reduce_cash_without_extra_sales(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    for product, quantity, price in ((s["compact"], 100, 79_000), (s["compact"], 10, 80_000),
                                     (s["napkin"], 20, 103_000), (s["napkin"], 5, 104_000)):
        assert _post_plain_sale(client, s["profile"].id, product.id, quantity, price).status_code == 201
    for description, amount in (("Lunch", 10_000), ("Fahad", 20_000), ("Breakfast", 10_000)):
        assert _cash_expense(client, s["profile"].id, description, amount).status_code == 201
    assert client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": s["profile"].id, "mobile_money_from_sales_ugx": 2_000_000}).status_code == 200

    debt = client.post("/api/sales/debts", json={
        "sales_profile_id": s["profile"].id, "business_date": "2026-10-30",
        "name": "Darius", "amount_ugx": 1_000_000, "idempotency_key": str(uuid4()),
    })
    assert debt.status_code == 201, debt.get_json()

    summary = sales_service.daily_summary(s["profile"], "2026-10-30")
    assert summary["totals"]["total_sales_ugx"] == 11_280_000  # the debt is not a second sale
    assert summary["totals"]["new_debt_ugx"] == 1_000_000
    assert summary["totals"]["mobile_money_from_sales_ugx"] == 2_000_000
    assert summary["totals"]["cash_from_sales_ugx"] == 8_280_000
    assert summary["totals"]["expected_cash_ugx"] == 8_240_000

    assert client.post("/api/sales/transfers", json={
        "sales_profile_id": s["profile"].id, "business_date": "2026-10-30",
        "from_account": "cash", "to_account": "mobile_money", "amount_ugx": 1_000_000,
        "idempotency_key": str(uuid4())}).status_code == 201
    summary = sales_service.daily_summary(s["profile"], "2026-10-30")
    assert summary["totals"]["expected_cash_ugx"] == 7_240_000
    assert summary["totals"]["expected_mobile_money_ugx"] == 3_000_000
    assert summary["totals"]["total_sales_ugx"] == 11_280_000
    assert summary["totals"]["total_expenses_ugx"] == 40_000

    # Mobile Money + unpaid amounts can never exceed the sales already counted.
    too_much = client.post("/api/sales/debts", json={
        "sales_profile_id": s["profile"].id, "business_date": "2026-10-30",
        "name": "Darius", "amount_ugx": 9_000_000, "idempotency_key": str(uuid4()),
    })
    assert too_much.status_code == 400
    assert "cannot exceed Total Sales" in too_much.get_json()["error"]



def test_debt_payment_reduces_outstanding_on_payment_date_only(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    assert _post_plain_sale(client, s["profile"].id, s["compact"].id, 1, 1_000_000).status_code == 201
    debt = client.post("/api/sales/debts", json={
        "sales_profile_id": s["profile"].id, "business_date": "2026-10-30",
        "name": "  Darius  ", "amount_ugx": 1_000_000, "idempotency_key": str(uuid4()),
    })
    assert debt.status_code == 201, debt.get_json()
    debtor_id = debt.get_json()["debtor_id"]

    _open_day(client, s["profile"].id, "2026-11-02")
    payment = client.post(f"/api/sales/debtors/{debtor_id}/payments", json={
        "amount_ugx": 400_000, "payment_method": "cash", "payment_date": "2026-11-02",
        "idempotency_key": str(uuid4())})
    assert payment.status_code == 201, payment.get_json()
    assert payment.get_json()["outstanding_ugx"] == 600_000

    original = sales_service.daily_summary(s["profile"], "2026-10-30")
    assert original["totals"]["total_sales_ugx"] == 1_000_000  # the old sale date is untouched
    later = sales_service.daily_summary(s["profile"], "2026-11-02")
    assert later["totals"]["total_sales_ugx"] == 0
    assert later["totals"]["old_debt_collections_ugx"] == 400_000
    assert later["totals"]["expected_cash_ugx"] == 400_000

    clearing = client.post(f"/api/sales/debtors/{debtor_id}/payments", json={
        "amount_ugx": 600_000, "payment_method": "cash", "payment_date": "2026-11-02",
        "idempotency_key": str(uuid4())})
    assert clearing.status_code == 201
    assert clearing.get_json()["outstanding_ugx"] == 0
    assert sales_service.daily_summary(s["profile"], "2026-11-02")["totals"]["expected_cash_ugx"] == 1_000_000
    # The name stays available for the salesperson's memory after it is settled.
    assert sales_service.debtor_names([s["profile"].id]) == ["Darius"]
    assert Customer.query.filter_by(name="Darius").count() == 0


def test_debtor_names_are_case_insensitive_memory_and_never_customers(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    assert _post_plain_sale(client, s["profile"].id, s["compact"].id, 10, 100_000).status_code == 201
    for name in ("Darius", "darius", "  DARIUS  ", "Derrick"):
        response = client.post("/api/sales/debts", json={
            "sales_profile_id": s["profile"].id, "business_date": "2026-10-30",
            "name": name, "amount_ugx": 1_000, "idempotency_key": str(uuid4())})
        assert response.status_code == 201, response.get_json()

    names = sales_service.debtor_names([s["profile"].id])
    assert sorted(value.casefold() for value in names) == ["darius", "derrick"]
    assert SalesDebtor.query.count() == 2
    debtors = client.get(f"/api/sales/debtors?sales_profile_id={s['profile'].id}").get_json()
    darius = next(row for row in debtors if (row["name"] or "").casefold() == "darius")
    assert darius["outstanding_ugx"] == 3_000
    # Entering a debtor never creates a row in the Finished Goods customers table.
    assert Customer.query.filter_by(name="Darius").count() == 0
    assert Customer.query.filter_by(name="Derrick").count() == 0



def test_operator_stock_list_only_shows_products_that_belong_to_the_vehicle(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    unrelated = Product(name="Unrelated Widget", display_order=9, active=True)
    db.session.add(unrelated); db.session.commit()
    ids = {row["product_id"] for row in sales_service.daily_summary(s["profile"], "2026-10-30")["stock"]}
    assert {s["compact"].id, s["napkin"].id} <= ids
    assert unrelated.id not in ids


def test_close_requires_cash_in_hand_and_expected_stays_ledger_derived(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    for product, remaining in ((s["compact"], 150), (s["napkin"], 30)):
        assert client.put("/api/sales/days/2026-10-30/closing-stock", json={
            "sales_profile_id": s["profile"].id, "product_id": product.id,
            "quantity": remaining, "unit": "carton"}).status_code == 200
    refused = client.post("/api/sales/days/2026-10-30/close", json={"sales_profile_id": s["profile"].id})
    assert refused.status_code == 400
    assert "Cash Counted" in refused.get_json()["error"]
    assert client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": s["profile"].id, "actual_cash_count_ugx": 5_000}).status_code == 200
    closed = client.post("/api/sales/days/2026-10-30/close", json={"sales_profile_id": s["profile"].id})
    assert closed.status_code == 200, closed.get_json()
    # The shortage is an observation; the expected ledger is not overwritten.
    assert closed.get_json()["totals"]["expected_cash_ugx"] == 0
    assert closed.get_json()["totals"]["cash_variance_ugx"] == 5_000


def test_opening_debt_is_historical_and_collectable_without_a_customer(client, sales_setup):
    s = sales_setup; _login(client, s["manager"])
    response = client.post("/api/sales/opening-debts", json={
        "sales_profile_id": s["profile"].id, "business_date": "2026-10-29",
        "name": "Old Customer", "amount_ugx": 500_000, "idempotency_key": str(uuid4()),
    })
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["kind"] == "opening"
    summary = sales_service.daily_summary(s["profile"], "2026-10-30")
    assert summary["totals"]["total_sales_ugx"] == 0
    assert summary["totals"]["new_debt_ugx"] == 0
    debtors = client.get(f"/api/sales/debtors?sales_profile_id={s['profile'].id}&as_of=2026-10-30").get_json()
    assert any(row["name"] == "Old Customer" and row["outstanding_ugx"] == 500_000 for row in debtors)
    assert Customer.query.filter_by(name="Old Customer").count() == 0

    _login(client, s["operator"])
    debtor_id = response.get_json()["debtor_id"]
    assert client.post(f"/api/sales/debtors/{debtor_id}/payments", json={
        "amount_ugx": 500_000, "payment_method": "mobile_money", "payment_date": "2026-10-31",
        "idempotency_key": str(uuid4())}).status_code == 201
    later = sales_service.daily_summary(s["profile"], "2026-10-31")
    assert later["totals"]["total_sales_ugx"] == 0
    assert later["totals"]["expected_mobile_money_ugx"] == 500_000


def test_operator_without_a_sales_profile_gets_a_human_message(client, make_user):
    user = make_user("no-sales-profile", "password123", ROLE_OPERATOR)
    _grant(user); _login(client, user)
    assert client.get("/api/sales/metadata").status_code == 200
    response = client.get("/api/sales/days/2026-10-30")
    assert response.status_code == 400
    assert "not been set up" in response.get_json()["error"]
    assert "sales_profile_id" not in response.get_json()["error"]


def test_stock_cycle_counts_returned_stock_and_shows_mismatch(client, sales_setup):
    """Acceptance A + C: 50 carried + 100 given = 150 out, 40 back, so 110 sold."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    assert client.put("/api/sales/days/2026-10-30/closing-stock", json={
        "sales_profile_id": s["profile"].id, "product_id": s["compact"].id,
        "quantity": 40, "unit": "carton"}).status_code == 200

    summary = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['profile'].id}").get_json()
    compact = next(r for r in summary["stock"] if r["product_id"] == s["compact"].id)
    assert compact["opening_source"] == "baseline"
    assert (compact["opening"]["cartons"], compact["added"]["cartons"],
            compact["available"]["cartons"]) == (50, 100, 150)
    assert (compact["closing"]["cartons"], compact["derived_sold"]["cartons"]) == (40, 110)

    # Only 105 of the 110 cartons are priced.  The difference is reported on
    # both sides of the comparison; neither figure is silently rewritten.
    assert _post_plain_sale(client, s["profile"].id, s["compact"].id, 105, 79_000).status_code == 201
    summary = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['profile'].id}").get_json()
    compact = next(r for r in summary["stock"] if r["product_id"] == s["compact"].id)
    assert compact["derived_sold"]["cartons"] == 110
    assert compact["recorded_sold"]["cartons"] == 105
    assert compact["variance"]["cartons"] == 5
    assert summary["totals"]["total_sales_ugx"] == 105 * 79_000
    assert any("difference 5 cartons" in warning for warning in summary["stock_warnings"])

    # Tomorrow starts from the counted return; the driver retypes nothing.
    _open_day(client, s["profile"].id, "2026-10-31")
    tomorrow = client.get(f"/api/sales/days/2026-10-31?sales_profile_id={s['profile'].id}").get_json()
    compact = next(r for r in tomorrow["stock"] if r["product_id"] == s["compact"].id)
    assert compact["opening_source"] == "previous_closing"
    assert compact["opening"]["cartons"] == 40


def _declare_opening(client, profile_id, lines, date="2026-10-30"):
    return client.put(f"/api/sales/days/{date}/opening-stock",
                      json={"sales_profile_id": profile_id, "lines": lines})


def _save_returns(client, profile_id, lines, date="2026-10-30"):
    return client.put(f"/api/sales/days/{date}/returns",
                      json={"sales_profile_id": profile_id, "lines": lines})


def _save_card(client, profile_id, product_id, rows, date="2026-10-30", vehicle_stock=None):
    """Save one sales card.

    ``vehicle_stock`` is the sheet the Sales screen is showing: it is sent as the
    context a card is priced against, and is never stored by this request.
    """
    body = {"sales_profile_id": profile_id, "rows": rows}
    if vehicle_stock is not None:
        body["vehicle_stock"] = vehicle_stock
    return client.put(f"/api/sales/days/{date}/products/{product_id}/sales", json=body)


def _day_summary(client, profile_id, date="2026-10-30"):
    return client.get(f"/api/sales/days/{date}?sales_profile_id={profile_id}").get_json()


def _attach_recipient(profile, recipient_id):
    """Give a second salesperson the same vehicle so dispatched stock appears."""
    profile.recipient_customer_id = recipient_id
    db.session.commit()


def test_sales_cards_reconcile_oct_30_exact_figures_with_mobile_money(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    saved = _save_returns(client, s["profile"].id, [
        {"product_id": s["compact"].id, "quantity": 40, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 5, "unit": "carton"},
    ])
    assert saved.status_code == 200, saved.get_json()

    compact_card = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000},
        {"quantity": 10, "unit": "carton", "unit_price_ugx": 80_000},
    ])
    assert compact_card.status_code == 200, compact_card.get_json()
    napkin_card = _save_card(client, s["profile"].id, s["napkin"].id, [
        {"quantity": 20, "unit": "carton", "unit_price_ugx": 103_000},
        {"quantity": 5, "unit": "carton", "unit_price_ugx": 104_000},
    ])
    assert napkin_card.status_code == 200, napkin_card.get_json()

    for description, amount in (("Lunch", 10_000), ("Fahad", 20_000), ("Breakfast", 10_000)):
        assert _cash_expense(client, s["profile"].id, description, amount).status_code == 201
    assert client.post("/api/sales/expenses", json={
        "sales_profile_id": s["profile"].id, "business_date": "2026-10-30", "description": "Data bundle",
        "amount_ugx": 15_000, "payment_method": "mobile_money", "idempotency_key": str(uuid4()),
    }).status_code == 201
    split = client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": s["profile"].id, "mobile_money_from_sales_ugx": 2_000_000})
    assert split.status_code == 200, split.get_json()

    summary = _day_summary(client, s["profile"].id)
    totals = summary["totals"]
    assert totals["total_sales_ugx"] == 11_280_000
    assert totals["mobile_money_from_sales_ugx"] == 2_000_000
    assert totals["cash_from_sales_ugx"] == 9_280_000
    assert totals["new_debt_ugx"] == 0
    assert totals["cash_expenses_ugx"] == 40_000
    assert totals["mobile_money_expenses_ugx"] == 15_000
    assert totals["cash_at_hand_ugx"] == 9_240_000
    assert totals["expected_mobile_money_ugx"] == 1_985_000
    assert totals["total_expenses_ugx"] == 55_000
    assert summary["needs_reconciliation"] is False
    compact = next(r for r in summary["stock"] if r["product_id"] == s["compact"].id)
    napkin = next(r for r in summary["stock"] if r["product_id"] == s["napkin"].id)
    assert compact["derived_sold"]["cartons"] == compact["recorded_sold"]["cartons"] == 110
    assert napkin["derived_sold"]["cartons"] == napkin["recorded_sold"]["cartons"] == 25
    cards = {card["product_id"]: card for card in summary["product_cards"]}
    assert cards[s["compact"].id]["card_total_ugx"] == 8_700_000
    assert cards[s["napkin"].id]["card_total_ugx"] == 2_580_000
    assert len(cards[s["compact"].id]["rows"]) == 2

    counted = client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": s["profile"].id, "actual_cash_count_ugx": 9_240_000,
        "actual_mobile_money_ugx": 1_985_000})
    assert counted.status_code == 200
    closed = client.post("/api/sales/days/2026-10-30/close", json={"sales_profile_id": s["profile"].id})
    assert closed.status_code == 200, closed.get_json()


def test_salesperson_declares_starting_stock_without_a_manager(client, sales_setup):
    s = sales_setup; _login(client, s["other"])
    _attach_recipient(s["other_profile"], s["profile"].recipient_customer_id)
    _open_day(client, s["other_profile"].id)

    start = _day_summary(client, s["other_profile"].id)
    assert start["baseline_required"] is True
    assert start["opening"]["can_declare"] is True
    assert set(start["opening"]["unset_products"]) == {"Compact Standard", "Napkin Standard"}
    assert start["totals"]["cash_at_hand_ugx"] == 0

    response = _declare_opening(client, s["other_profile"].id, [
        {"product_id": s["compact"].id, "quantity": 50, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 10, "unit": "carton"},
    ])
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    compact = next(r for r in body["stock"] if r["product_id"] == s["compact"].id)
    assert compact["opening"]["cartons"] == 50
    assert compact["available"]["cartons"] == 150
    assert compact["can_set_opening"] is True  # same-day correction stays possible
    assert body["opening"]["required"] is False
    assert body["opening"]["started_on"] == "2026-10-30"
    assert SalesBaseline.query.filter_by(sales_profile_id=s["other_profile"].id).count() == 1
    audited = AuditLog.query.filter_by(entity_type="sales_opening_stock").count()
    assert audited == 2

    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    customers_before = Customer.query.count()
    dispatches_before = {dispatch.id: dispatch.status for dispatch in Dispatch.query.all()}
    audit_types_before = {row.entity_type for row in AuditLog.query.all()}

    assert client.put("/api/sales/days/2026-10-30/closing-stock", json={
        "sales_profile_id": s["profile"].id, "product_id": s["compact"].id,
        "quantity": 40, "unit": "carton"}).status_code == 200

    # Unsold stock left in the vehicle is only a vehicle return: no customer
    # return, refund, dispatch change or Finished Goods customer is created.
    assert Customer.query.count() == customers_before
    assert {dispatch.id: dispatch.status for dispatch in Dispatch.query.all()} == dispatches_before
    assert {row.entity_type for row in AuditLog.query.all()} - audit_types_before == {"sales_vehicle_closing"}

    # Stock given for a product that the trusted starting point does not list
    # still reconciles: nothing carried is an explicit zero, not a blocker.
    extra = Product(name="New Vehicle Line", display_order=9, active=True)
    db.session.add(extra); db.session.flush()
    extra_rule = PackagingRule(product_id=extra.id, cartons_to_packs=10, packs_to_pieces=10,
                               effective_from=datetime(2026, 1, 1), created_by=s["manager"].id)
    db.session.add(extra_rule); db.session.flush()
    dispatch = Dispatch(dispatch_number="D-NEW", date="2026-10-30", shift="Day",
                        customer_id=s["profile"].recipient_customer_id,
                        status=STATUS_FINALIZED, created_by=s["manager"].id)
    db.session.add(dispatch); db.session.flush()
    db.session.add(DispatchLine(dispatch_id=dispatch.id, product_id=extra.id, cartons=10, packs=0, pieces=0,
                                base_unit_qty=10 * 100, packaging_rule_id=extra_rule.id))
    db.session.commit()

    row = next(r for r in sales_service.daily_summary(s["profile"], "2026-10-30")["stock"]
               if r["product_id"] == extra.id)
    assert row["opening"]["base_unit_qty"] == 0
    assert row["available"]["cartons"] == 10
    response = client.put("/api/sales/days/2026-10-30/closing-stock", json={
        "sales_profile_id": s["profile"].id, "product_id": extra.id, "quantity": 4, "unit": "carton"})
    assert response.status_code == 200, response.get_json()
    row = next(r for r in response.get_json()["stock"] if r["product_id"] == extra.id)
    assert row["derived_sold"]["cartons"] == 6


def test_operator_balances_the_whole_day_without_a_manager(client, sales_setup):
    s = sales_setup; _login(client, s["other"])
    _attach_recipient(s["other_profile"], s["profile"].recipient_customer_id)
    _open_day(client, s["other_profile"].id)
    pid = s["other_profile"].id

    early_return = _save_returns(client, pid, [{"product_id": s["compact"].id, "quantity": 40, "unit": "carton"}])
    assert early_return.status_code == 400
    assert "starting stock" in early_return.get_json()["error"].lower()
    early_card = _save_card(client, pid, s["compact"].id, [{"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}])
    assert early_card.status_code == 400
    assert "starting stock" in early_card.get_json()["error"].lower()

    declared = _declare_opening(client, pid, [
        {"product_id": s["compact"].id, "quantity": 50, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 10, "unit": "carton"},
    ])
    assert declared.status_code == 200, declared.get_json()
    late_card = _save_card(client, pid, s["compact"].id, [{"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}])
    assert late_card.status_code == 400
    assert "return today" in late_card.get_json()["error"].lower()

    assert _save_returns(client, pid, [
        {"product_id": s["compact"].id, "quantity": 40, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 5, "unit": "carton"},
    ]).status_code == 200
    assert _save_card(client, pid, s["compact"].id, [
        {"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000},
        {"quantity": 10, "unit": "carton", "unit_price_ugx": 80_000},
    ]).status_code == 200
    assert _save_card(client, pid, s["napkin"].id, [
        {"quantity": 20, "unit": "carton", "unit_price_ugx": 103_000},
        {"quantity": 5, "unit": "carton", "unit_price_ugx": 104_000},
    ]).status_code == 200
    for description, amount in (("Lunch", 10_000), ("Fahad", 20_000), ("Breakfast", 10_000)):
        assert _cash_expense(client, pid, description, amount).status_code == 201
    assert client.post("/api/sales/expenses", json={
        "sales_profile_id": pid, "business_date": "2026-10-30", "description": "Data bundle",
        "amount_ugx": 15_000, "payment_method": "mobile_money", "idempotency_key": str(uuid4()),
    }).status_code == 201
    assert client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": pid, "mobile_money_from_sales_ugx": 2_000_000}).status_code == 200

    summary = _day_summary(client, pid)
    totals = summary["totals"]
    assert totals["total_sales_ugx"] == 11_280_000
    assert totals["cash_at_hand_ugx"] == 9_240_000
    assert totals["expected_mobile_money_ugx"] == 1_985_000
    assert summary["needs_reconciliation"] is False
    assert summary["opening"]["required"] is False

    assert client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": pid, "actual_cash_count_ugx": 9_240_000,
        "actual_mobile_money_ugx": 1_985_000}).status_code == 200
    closed = client.post("/api/sales/days/2026-10-30/close", json={"sales_profile_id": pid})
    assert closed.status_code == 200, closed.get_json()
    assert closed.get_json()["day"]["status"] == "closed"
    reopened = client.post("/api/sales/days/2026-10-30/reopen", json={"sales_profile_id": pid})
    assert reopened.status_code == 403
    after = _day_summary(client, pid)
    assert after["opening"]["can_declare"] is False
    assert all(r["can_set_opening"] is False for r in after["stock"])


def test_declared_starting_stock_carries_forward_to_the_next_day(client, sales_setup):
    s = sales_setup; _login(client, s["other"])
    _attach_recipient(s["other_profile"], s["profile"].recipient_customer_id)
    _open_day(client, s["other_profile"].id)
    pid = s["other_profile"].id
    _declare_opening(client, pid, [
        {"product_id": s["compact"].id, "quantity": 50, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 10, "unit": "carton"},
    ])
    _save_returns(client, pid, [
        {"product_id": s["compact"].id, "quantity": 40, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 5, "unit": "carton"},
    ])
    assert _open_day(client, pid, date="2026-10-31").status_code == 201
    nxt = _day_summary(client, pid, date="2026-10-31")
    compact = next(r for r in nxt["stock"] if r["product_id"] == s["compact"].id)
    assert compact["opening"]["cartons"] == 40
    assert compact["opening_source"] == "previous_closing"
    assert compact["can_set_opening"] is False
    assert compact["closing"] is None
    assert nxt["opening"]["started_on"] == "2026-10-30"
    assert nxt["opening"]["unset_products"] == []



def test_starting_stock_is_refused_once_sales_exist_and_allows_a_same_day_correction(client, sales_setup):
    s = sales_setup; _login(client, s["other"])
    _attach_recipient(s["other_profile"], s["profile"].recipient_customer_id)
    _open_day(client, s["other_profile"].id)
    pid = s["other_profile"].id
    _declare_opening(client, pid, [
        {"product_id": s["compact"].id, "quantity": 50, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 10, "unit": "carton"},
    ])
    assert _post_plain_sale(client, pid, s["compact"].id, 1, 7_900_000).status_code == 201
    sold = _declare_opening(client, pid, [{"product_id": s["compact"].id, "quantity": 55, "unit": "carton"}])
    assert sold.status_code == 400
    assert "Sales are already recorded" in sold.get_json()["error"]

    _save_returns(client, pid, [{"product_id": s["napkin"].id, "quantity": 5, "unit": "carton"}])
    corrected = _declare_opening(client, pid, [{"product_id": s["napkin"].id, "quantity": 15, "unit": "carton"}])
    assert corrected.status_code == 200
    row = next(r for r in corrected.get_json()["stock"] if r["product_id"] == s["napkin"].id)
    # A same-day correction is allowed and the sold quantity follows it exactly.
    assert row["opening"]["cartons"] == 15
    assert row["derived_sold"]["base_unit_qty"] == 3500 - 500


def test_starting_stock_is_refused_on_a_day_after_the_declaration_day(client, sales_setup):
    s = sales_setup; _login(client, s["other"])
    _attach_recipient(s["other_profile"], s["profile"].recipient_customer_id)
    _open_day(client, s["other_profile"].id)
    pid = s["other_profile"].id
    _declare_opening(client, pid, [{"product_id": s["compact"].id, "quantity": 50, "unit": "carton"}])
    _open_day(client, pid, date="2026-10-31")
    refused = _declare_opening(client, pid, [{"product_id": s["compact"].id, "quantity": 9, "unit": "carton"}],
                               date="2026-10-31")
    assert refused.status_code == 400
    assert "declared on 2026-10-30" in refused.get_json()["error"]


def test_starting_stock_needs_a_day_and_a_real_product(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    missing_day = _declare_opening(client, s["profile"].id,
                                   [{"product_id": s["compact"].id, "quantity": 1, "unit": "carton"}],
                                   date="2026-11-05")
    assert missing_day.status_code == 400
    assert "Create the Sales Day first" in missing_day.get_json()["error"]
    _open_day(client, s["profile"].id)
    empty = _declare_opening(client, s["profile"].id, [])
    assert empty.status_code == 400
    assert "at least one product" in empty.get_json()["error"]
    bad_product = _declare_opening(client, s["profile"].id,
                                   [{"product_id": 99_999, "quantity": 1, "unit": "carton"}])
    assert bad_product.status_code == 400
    assert "active product" in bad_product.get_json()["error"]
    bad_qty = _declare_opening(client, s["profile"].id,
                               [{"product_id": s["compact"].id, "quantity": "lots", "unit": "carton"}])
    assert bad_qty.status_code == 400
    assert "positive whole number" in bad_qty.get_json()["error"]
    bad_unit = _declare_opening(client, s["profile"].id,
                                [{"product_id": s["compact"].id, "quantity": 1, "unit": "bag"}])
    assert bad_unit.status_code == 400
    assert "unit must be one of" in bad_unit.get_json()["error"]




def test_returns_save_counts_every_product_in_one_action(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    body = _save_returns(client, s["profile"].id, [
        {"product_id": s["compact"].id, "quantity": 40, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 2, "unit": "pack"},
    ])
    assert body.status_code == 200, body.get_json()
    stock = {r["product_id"]: r for r in body.get_json()["stock"]}
    assert stock[s["compact"].id]["closing"]["cartons"] == 40
    assert stock[s["napkin"].id]["closing"]["packs"] == 2
    assert stock[s["compact"].id]["derived_sold"]["cartons"] == 110
    assert stock[s["napkin"].id]["derived_sold"]["base_unit_qty"] == 3000 - 20


def test_returns_save_rejects_bad_and_excessive_lines(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    empty = _save_returns(client, s["profile"].id, [])
    assert empty.status_code == 400
    assert "at least one product" in empty.get_json()["error"]
    foreign = _save_returns(client, s["profile"].id, [{"product_id": 98_765, "quantity": 1, "unit": "carton"}])
    assert foreign.status_code == 400
    assert "product not found" in foreign.get_json()["error"]
    stranger = Product(name="Stranger Product", display_order=9, active=True)
    db.session.add(stranger); db.session.flush()
    db.session.add(PackagingRule(product_id=stranger.id, cartons_to_packs=10, packs_to_pieces=10,
                                 effective_from=datetime(2026, 1, 1), created_by=s["manager"].id))
    db.session.commit()
    outside = _save_returns(client, s["profile"].id, [{"product_id": stranger.id, "quantity": 1, "unit": "carton"}])
    assert outside.status_code == 400
    assert "not part of this vehicle" in outside.get_json()["error"]
    too_much = _save_returns(client, s["profile"].id, [{"product_id": s["compact"].id, "quantity": 151, "unit": "carton"}])
    assert too_much.status_code == 400
    assert "cannot exceed" in too_much.get_json()["error"].lower()


def test_returns_and_starting_stock_are_read_only_on_a_closed_day(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    _save_returns(client, s["profile"].id, [
        {"product_id": s["compact"].id, "quantity": 40, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 5, "unit": "carton"},
    ])
    _save_card(client, s["profile"].id, s["compact"].id, [{"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}])
    _save_card(client, s["profile"].id, s["napkin"].id, [{"quantity": 25, "unit": "carton", "unit_price_ugx": 103_000}])
    client.patch("/api/sales/days/2026-10-30", json={"sales_profile_id": s["profile"].id,
                                                     "actual_cash_count_ugx": 11_280_000})
    assert client.post("/api/sales/days/2026-10-30/close",
                       json={"sales_profile_id": s["profile"].id}).status_code == 200
    blocked_returns = _save_returns(client, s["profile"].id, [
        {"product_id": s["compact"].id, "quantity": 41, "unit": "carton"}])
    assert blocked_returns.status_code == 400
    assert "closed Sales Day" in blocked_returns.get_json()["error"]
    blocked_opening = _declare_opening(client, s["profile"].id, [
        {"product_id": s["compact"].id, "quantity": 51, "unit": "carton"}])
    assert blocked_opening.status_code == 400
    assert "closed Sales Day" in blocked_opening.get_json()["error"]
    blocked_card = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}])
    assert blocked_card.status_code == 400
    assert "closed Sales Day" in blocked_card.get_json()["error"]


def _card_ready(client, s):
    """Open the day with a counted return so a sales card can be saved."""
    _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    _save_returns(client, s["profile"].id, [
        {"product_id": s["compact"].id, "quantity": 40, "unit": "carton"},
        {"product_id": s["napkin"].id, "quantity": 5, "unit": "carton"},
    ])


def test_card_saves_rows_and_reports_what_it_did(client, sales_setup):
    s = sales_setup; _card_ready(client, s)
    first = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000},
        {"quantity": 10, "unit": "carton", "unit_price_ugx": 80_000},
    ])
    assert first.status_code == 200, first.get_json()
    assert first.get_json()["card"]["action"] == "created"
    invoice_id = first.get_json()["card"]["invoice_id"]
    assert invoice_id is not None

    again = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000},
        {"quantity": 10, "unit": "carton", "unit_price_ugx": 80_000},
    ])
    assert again.get_json()["card"]["action"] == "unchanged"
    assert again.get_json()["card"]["invoice_id"] == invoice_id
    assert SalesInvoice.query.filter_by(sales_profile_id=s["profile"].id,
                                        status=sales_service.ACTIVE).count() == 1
    card = next(c for c in again.get_json()["product_cards"] if c["product_id"] == s["compact"].id)
    assert card["card_total_ugx"] == 8_700_000
    assert len(card["rows"]) == 2


def test_card_replaces_its_old_rows_without_double_counting(client, sales_setup):
    s = sales_setup; _card_ready(client, s)
    _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000},
        {"quantity": 10, "unit": "carton", "unit_price_ugx": 80_000},
    ])
    replaced = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000},
    ])
    assert replaced.status_code == 200, replaced.get_json()
    assert replaced.get_json()["card"]["action"] == "created"
    assert replaced.get_json()["totals"]["total_sales_ugx"] == 8_690_000
    statuses = sorted(i.status for i in SalesInvoice.query.filter_by(sales_profile_id=s["profile"].id))
    assert statuses == sorted([sales_service.ACTIVE, sales_service.VOID])


def test_card_refuses_under_and_over_priced_rows(client, sales_setup):
    s = sales_setup; _card_ready(client, s)
    under = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000}])
    assert under.status_code == 400
    assert "Add another row for the remaining" in under.get_json()["error"]
    over = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 120, "unit": "carton", "unit_price_ugx": 79_000}])
    assert over.status_code == 400
    assert "Remove" in over.get_json()["error"]


def test_card_rejects_bad_rows(client, sales_setup):
    s = sales_setup; _card_ready(client, s)
    url = f"/api/sales/days/2026-10-30/products/{s['compact'].id}/sales"
    assert client.put(url, json={"sales_profile_id": s["profile"].id, "rows": "two"}).status_code == 400
    assert "rows must be a list" in client.put(url, json={
        "sales_profile_id": s["profile"].id, "rows": "two"}).get_json()["error"]
    not_dict = client.put(url, json={"sales_profile_id": s["profile"].id, "rows": [1]})
    assert not_dict.status_code == 400
    assert "not a valid price row" in not_dict.get_json()["error"]
    bad_unit = client.put(url, json={"sales_profile_id": s["profile"].id, "rows": [
        {"quantity": 1, "unit": "bag", "unit_price_ugx": 1_000}]})
    assert bad_unit.status_code == 400
    assert "unit must be one of" in bad_unit.get_json()["error"]
    no_price = client.put(url, json={"sales_profile_id": s["profile"].id, "rows": [
        {"quantity": 1, "unit": "carton", "unit_price_ugx": 0}]})
    assert no_price.status_code == 400
    assert "greater than zero" in no_price.get_json()["error"]
    no_qty = client.put(url, json={"sales_profile_id": s["profile"].id, "rows": [
        {"quantity": 0, "unit": "carton", "unit_price_ugx": 1_000}]})
    assert no_qty.status_code == 400
    assert "positive whole number" in no_qty.get_json()["error"]



def test_card_clear_is_refused_while_stock_still_says_sold(client, sales_setup):
    s = sales_setup; _card_ready(client, s)
    _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}])
    refused = _save_card(client, s["profile"].id, s["compact"].id, [])
    assert refused.status_code == 400
    assert "Add another row for the remaining" in refused.get_json()["error"]
    # Returning the whole load leaves nothing sold, so the card can be emptied.
    _save_returns(client, s["profile"].id, [{"product_id": s["compact"].id, "quantity": 150, "unit": "carton"}])
    cleared = _save_card(client, s["profile"].id, s["compact"].id, [])
    assert cleared.status_code == 200, cleared.get_json()
    assert cleared.get_json()["card"]["action"] == "cleared"
    body = cleared.get_json()
    assert body["totals"]["total_sales_ugx"] == 0
    row = next(r for r in body["stock"] if r["product_id"] == s["compact"].id)
    assert row["derived_sold"]["base_unit_qty"] == 0
    assert row["variance"]["base_unit_qty"] == 0
    assert SalesInvoice.query.filter_by(sales_profile_id=s["profile"].id,
                                        status=sales_service.ACTIVE).count() == 0


def test_card_refuses_an_invoice_that_also_prices_another_product(client, sales_setup):
    s = sales_setup; _card_ready(client, s)
    mixed = client.post("/api/sales/invoices", json={
        "sales_profile_id": s["profile"].id, "business_date": "2026-10-30", "idempotency_key": str(uuid4()),
        "lines": [
            {"product_id": s["compact"].id, "quantity": 10, "unit": "carton", "unit_price_ugx": 79_000},
            {"product_id": s["napkin"].id, "quantity": 5, "unit": "carton", "unit_price_ugx": 103_000},
        ],
    })
    assert mixed.status_code == 201, mixed.get_json()
    refused = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}])
    assert refused.status_code == 400
    assert "also prices another product" in refused.get_json()["error"]
    assert "Void that sale first" in refused.get_json()["error"]


def test_card_cannot_be_saved_on_another_salespersons_profile(client, sales_setup):
    s = sales_setup
    _login(client, s["other"])
    _open_day(client, s["other_profile"].id)
    response = _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 1, "unit": "carton", "unit_price_ugx": 1_000}])
    assert response.status_code in (403, 404)
    day = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['profile'].id}")
    assert day.status_code in (403, 404)


def test_viewer_cannot_write_stock_money_or_cards(client, sales_setup, make_user):
    s = sales_setup
    viewer = make_user("sales-viewer", "password123", ROLE_VIEWER)
    _grant(viewer)
    _login(client, viewer)
    assert _save_returns(client, s["profile"].id, [
        {"product_id": s["compact"].id, "quantity": 1, "unit": "carton"}]).status_code == 403
    assert _declare_opening(client, s["profile"].id, [
        {"product_id": s["compact"].id, "quantity": 1, "unit": "carton"}]).status_code == 403
    assert _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 1, "unit": "carton", "unit_price_ugx": 1_000}]).status_code == 403


def test_operator_voids_own_expense_but_not_another_profiles(client, sales_setup):
    s = sales_setup
    _card_ready(client, s)
    own = _cash_expense(client, s["profile"].id, "Lunch", 10_000)
    assert own.status_code == 201
    expense_id = own.get_json()["id"]
    voided = client.post(f"/api/sales/void/expense/{expense_id}", json={"reason": "Wrong amount"})
    assert voided.status_code == 200, voided.get_json()
    assert _day_summary(client, s["profile"].id)["totals"]["cash_expenses_ugx"] == 0

    foreign = _cash_expense(client, s["profile"].id, "Fuel", 20_000)
    foreign_id = foreign.get_json()["id"]
    _login(client, s["other"])
    _open_day(client, s["other_profile"].id)
    blocked = client.post(f"/api/sales/void/expense/{foreign_id}", json={"reason": "Not mine"})
    assert blocked.status_code in (403, 404)
    _login(client, s["operator"])
    assert _day_summary(client, s["profile"].id)["totals"]["cash_expenses_ugx"] == 20_000


def test_mobile_money_and_debt_split_cannot_exceed_sales(client, sales_setup):
    s = sales_setup; _card_ready(client, s)
    _save_card(client, s["profile"].id, s["compact"].id, [
        {"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}])
    too_much = client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": s["profile"].id, "mobile_money_from_sales_ugx": 8_690_001})
    assert too_much.status_code == 400
    assert "cannot exceed Total Sales" in too_much.get_json()["error"]
    exactly = client.patch("/api/sales/days/2026-10-30", json={
        "sales_profile_id": s["profile"].id, "mobile_money_from_sales_ugx": 8_690_000})
    assert exactly.status_code == 200
    assert exactly.get_json()["totals"]["cash_from_sales_ugx"] == 0
    assert exactly.get_json()["totals"]["cash_at_hand_ugx"] == 0


# ---------------------------------------------------------------------------
# Step 1 — Vehicle Stock: the salesperson's own editable sheet
# ---------------------------------------------------------------------------

def _save_vehicle_stock(client, profile_id, lines, date="2026-10-30"):
    return client.put(f"/api/sales/days/{date}/vehicle-stock",
                      json={"sales_profile_id": profile_id, "lines": lines})


def _vehicle_options(client, profile_id, date="2026-10-30"):
    return client.get(f"/api/sales/days/{date}/vehicle-stock/options?sales_profile_id={profile_id}")


def _stock_row(summary, product_id):
    return next(row for row in summary["stock"] if row["product_id"] == product_id)


def _add_straws(s, name="Straws"):
    """A product that nothing has carried yet: it can only be chosen by hand."""
    product = Product(name=name, display_order=3, active=True)
    db.session.add(product); db.session.flush()
    db.session.add(PackagingRule(product_id=product.id, cartons_to_packs=10, packs_to_pieces=10,
                                 effective_from=datetime(2026, 1, 1), created_by=s["manager"].id))
    db.session.commit()
    return product


def test_vehicle_stock_product_picker_is_the_product_catalogue(client, sales_setup):
    """The Product dropdown is the product table, not a list written in JavaScript."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    straws = _add_straws(s)
    retired = Product(name="Retired Widget", display_order=8, active=False)
    no_rule = Product(name="No Packaging Rule", display_order=9, active=True)
    db.session.add_all([retired, no_rule]); db.session.flush()
    db.session.add(PackagingRule(product_id=retired.id, cartons_to_packs=10, packs_to_pieces=10,
                                 effective_from=datetime(2026, 1, 1), created_by=s["manager"].id))
    db.session.commit()

    body = _vehicle_options(client, s["profile"].id).get_json()
    assert [row["name"] for row in body["products"]] == ["Compact Standard", "Napkin Standard", "Straws"]
    straw = next(row for row in body["products"] if row["id"] == straws.id)
    assert straw["on_vehicle"] is False
    # A trusted opening exists, so nothing carried is an explicit zero, not a blank.
    assert straw["previous_return"]["cartons"] == 0
    assert straw["issued_today"]["cartons"] == 0
    compact = next(row for row in body["products"] if row["id"] == s["compact"].id)
    assert compact["on_vehicle"] is True
    assert compact["previous_return"]["cartons"] == 50
    assert compact["issued_today"]["cartons"] == 100


def test_vehicle_stock_adds_a_product_from_the_catalogue(client, sales_setup):
    """A product with no history at all can still be put on the vehicle."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    straws = _add_straws(s)

    saved = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": straws.id, "unit": "carton", "previous_qty": 0,
         "issued_qty": 20, "return_qty": 5},
    ])
    assert saved.status_code == 200, saved.get_json()
    row = _stock_row(saved.get_json(), straws.id)
    assert (row["previous_return"]["cartons"], row["issued_today"]["cartons"],
            row["total_stock"]["cartons"], row["closing"]["cartons"],
            row["derived_sold"]["cartons"]) == (0, 20, 20, 5, 15)

    # Saved means saved: the next read shows the same card.
    reloaded = _stock_row(_day_summary(client, s["profile"].id), straws.id)
    assert (reloaded["total_stock"]["cartons"], reloaded["closing"]["cartons"],
            reloaded["return_counted"]) == (20, 5, True)
    # And the picker no longer offers a second card for it.
    assert next(item for item in _vehicle_options(client, s["profile"].id).get_json()["products"]
                if item["id"] == straws.id)["on_vehicle"] is True



def test_vehicle_stock_figures_are_all_editable_without_a_manager(client, sales_setup):
    """Previous Return, Given Stock, Total Stock and Return Today are all the driver's."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    # 52 carried + 100 given = 152, corrected by hand to 150, less 40 back = 110 sold.
    saved = _save_vehicle_stock(client, s["profile"].id, [{
        "product_id": s["compact"].id, "unit": "carton",
        "previous_qty": 52, "issued_qty": 100, "total_qty": 150, "return_qty": 40}])
    assert saved.status_code == 200, saved.get_json()
    row = _stock_row(saved.get_json(), s["compact"].id)
    assert (row["previous_return"]["cartons"], row["issued_today"]["cartons"],
            row["total_stock"]["cartons"], row["closing"]["cartons"],
            row["derived_sold"]["cartons"]) == (52, 100, 150, 40, 110)
    assert row["previous_overridden"] is True and row["total_overridden"] is True
    # Given Stock still equals the dispatch, so it is not copied into Sales.
    assert row["issued_overridden"] is False
    assert DispatchLine.query.filter_by(product_id=s["compact"].id).one().base_unit_qty == 100 * 100
    assert SalesOpeningStockLine.query.filter_by(product_id=s["compact"].id).one().base_unit_qty == 50 * 100
    assert AuditLog.query.filter_by(entity_type="sales_vehicle_closing").count() == 1

    # Total Stock is not locked to Previous Return + Given Stock: correcting
    # Given Stock does not fight a Total Stock the salesperson typed earlier.
    again = _save_vehicle_stock(client, s["profile"].id, [{
        "product_id": s["compact"].id, "unit": "carton",
        "previous_qty": 52, "issued_qty": 90, "total_qty": 150, "return_qty": 40}])
    row = _stock_row(again.get_json(), s["compact"].id)
    assert (row["issued_today"]["cartons"], row["total_stock"]["cartons"],
            row["derived_sold"]["cartons"]) == (90, 150, 110)


def test_vehicle_stock_refuses_impossible_and_duplicate_cards(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    too_much = _save_vehicle_stock(client, s["profile"].id, [{
        "product_id": s["compact"].id, "unit": "carton",
        "previous_qty": 50, "issued_qty": 100, "return_qty": 151}])
    assert too_much.status_code == 400
    assert "cannot exceed" in too_much.get_json()["error"].lower()
    negative = _save_vehicle_stock(client, s["profile"].id, [{
        "product_id": s["compact"].id, "unit": "carton", "previous_qty": -5}])
    assert negative.status_code == 400
    assert "cannot be negative" in negative.get_json()["error"].lower()
    duplicate = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 50},
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 50}])
    assert duplicate.status_code == 400
    assert "already on the vehicle stock" in duplicate.get_json()["error"]
    # A refused save writes nothing at all.
    assert SalesVehicleClosingLine.query.count() == 0


def test_vehicle_stock_carry_forward_is_editable_and_never_rewrites_yesterday(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    first = _save_vehicle_stock(client, s["profile"].id, [{
        "product_id": s["compact"].id, "unit": "carton",
        "previous_qty": 50, "issued_qty": 100, "return_qty": 40}])
    assert first.status_code == 200, first.get_json()

    _open_day(client, s["profile"].id, date="2026-10-31")
    tomorrow = _stock_row(_day_summary(client, s["profile"].id, date="2026-10-31"), s["compact"].id)
    assert tomorrow["previous_return"]["cartons"] == 40      # carried forward
    assert tomorrow["closing"] is None and tomorrow["return_counted"] is False

    # The salesperson physically counts 42 today and corrects the carry-forward.
    corrected = _save_vehicle_stock(client, s["profile"].id, [{
        "product_id": s["compact"].id, "unit": "carton", "previous_qty": 42}], date="2026-10-31")
    assert corrected.status_code == 200, corrected.get_json()
    row = _stock_row(corrected.get_json(), s["compact"].id)
    assert row["previous_return"]["cartons"] == 42
    assert row["previous_overridden"] is True

    # Yesterday is exactly as it was: previous 50, return 40, untouched row.
    yesterday = _stock_row(_day_summary(client, s["profile"].id), s["compact"].id)
    assert (yesterday["previous_return"]["cartons"], yesterday["closing"]["cartons"]) == (50, 40)
    stored = (SalesVehicleClosingLine.query.join(SalesDay)
              .filter(SalesDay.business_date == "2026-10-30",
                      SalesVehicleClosingLine.product_id == s["compact"].id).one())
    assert stored.base_unit_qty == 40 * 100
    assert stored.previous_return_base_qty is None



def test_vehicle_stock_cards_are_independent_and_feed_the_sales_cards(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    straws = _add_straws(s)
    saved = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
         "issued_qty": 100, "total_qty": 150, "return_qty": 40},
        {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
         "issued_qty": 20, "return_qty": 5},
        {"product_id": straws.id, "unit": "carton", "previous_qty": 0,
         "issued_qty": 10, "return_qty": 10},
    ])
    assert saved.status_code == 200, saved.get_json()
    body = saved.get_json()
    # Each card computes its own Sold Today; nothing leaks between products.
    assert [_stock_row(body, product.id)["derived_sold"]["cartons"]
            for product in (s["compact"], s["napkin"], straws)] == [110, 25, 0]
    assert _stock_row(body, s["compact"].id)["total_stock"]["cartons"] == 150
    assert _stock_row(body, s["napkin"].id)["total_stock"]["cartons"] == 30
    assert _stock_row(body, straws.id)["total_stock"]["cartons"] == 10

    # Step 3 still prices exactly the Sold Today each card produced.
    assert _save_card(client, s["profile"].id, s["compact"].id,
                      [{"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}]).status_code == 200
    assert _save_card(client, s["profile"].id, s["napkin"].id,
                      [{"quantity": 25, "unit": "carton", "unit_price_ugx": 103_000}]).status_code == 200
    totals = _day_summary(client, s["profile"].id)["totals"]
    assert totals["total_sales_ugx"] == 110 * 79_000 + 25 * 103_000


def test_vehicle_stock_is_read_only_on_a_closed_day(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    # Nothing sold, so the day can be closed on the counted cash alone.
    assert _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 50,
         "issued_qty": 100, "return_qty": 150},
        {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
         "issued_qty": 20, "return_qty": 30},
    ]).status_code == 200
    base = {"sales_profile_id": s["profile"].id}
    assert client.patch("/api/sales/days/2026-10-30",
                        json={**base, "actual_cash_count_ugx": 0}).status_code == 200
    assert client.post("/api/sales/days/2026-10-30/close", json=base).status_code == 200
    blocked = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 51}])
    assert blocked.status_code == 400
    assert "closed Sales Day" in blocked.get_json()["error"]


def test_close_day_persists_the_vehicle_stock_sheet_in_the_same_action(client, sales_setup):
    """The Sales screen has no Vehicle stock save: Close day writes the sheet.

    The sheet on screen is part of the close request, so a figure typed there
    is never lost and never persisted twice - and a product added from the
    catalogue reaches the database through the same day-level action.
    """
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    straws = _add_straws(s)
    lines = [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
         "issued_qty": 100, "total_qty": 150, "return_qty": 150},
        {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
         "issued_qty": 20, "return_qty": 30},
        {"product_id": straws.id, "unit": "carton", "previous_qty": 0,
         "issued_qty": 26, "return_qty": 26},
    ]
    # Nothing has been saved for the sheet yet: the day still holds the system
    # figures and does not know the newly added product at all.
    before = _day_summary(client, s["profile"].id)
    assert _stock_row(before, s["compact"].id)["closing"] is None
    assert all(row["product_id"] != straws.id for row in before["stock"])

    base = {"sales_profile_id": s["profile"].id}
    assert client.patch("/api/sales/days/2026-10-30",
                        json={**base, "actual_cash_count_ugx": 0}).status_code == 200
    closed = client.post("/api/sales/days/2026-10-30/close",
                         json={**base, "vehicle_stock": lines})
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    assert body["day"]["status"] == "closed"
    assert _stock_row(body, s["compact"].id)["previous_return"]["cartons"] == 52
    assert _stock_row(body, s["compact"].id)["total_stock"]["cartons"] == 150
    assert _stock_row(body, s["compact"].id)["closing"]["cartons"] == 150
    assert _stock_row(body, straws.id)["total_stock"]["cartons"] == 26

    # Reloading reproduces exactly the figures that were closed.
    reloaded = _day_summary(client, s["profile"].id)
    assert _stock_row(reloaded, s["napkin"].id)["closing"]["cartons"] == 30
    assert _stock_row(reloaded, straws.id)["closing"]["cartons"] == 26
    assert _stock_row(reloaded, straws.id)["derived_sold"]["cartons"] == 0


def test_a_refused_close_does_not_persist_the_vehicle_stock_sheet(client, sales_setup):
    """A close that is refused writes nothing at all - not even the sheet."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    base = {"sales_profile_id": s["profile"].id}
    lines = [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 42,
         "issued_qty": 100, "total_qty": 142, "return_qty": 40},
        {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
         "issued_qty": 20, "return_qty": 30},
    ]
    refused = client.post("/api/sales/days/2026-10-30/close",
                          json={**base, "vehicle_stock": lines})
    assert refused.status_code == 400
    assert "cash you counted" in refused.get_json()["error"]
    after = _day_summary(client, s["profile"].id)
    compact = _stock_row(after, s["compact"].id)
    assert compact["previous_return"]["cartons"] == 50   # the sheet was not written
    assert compact["closing"] is None
    assert after["day"]["status"] == "draft"


def test_open_day_vehicle_stock_edits_need_no_manager_correction_reason(client, sales_setup):
    """Correcting Vehicle stock is ordinary work, not an approval."""
    s = sales_setup; _login(client, s["manager"])
    _open_day(client, s["profile"].id)
    first = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
         "issued_qty": 100, "total_qty": 150, "return_qty": 40},
    ])
    assert first.status_code == 200, first.get_json()
    # An ordinary correction to a figure that is already on the sheet: no
    # Manager/Super Administrator correction reason is asked for or required.
    corrected = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
         "issued_qty": 100, "total_qty": 150, "return_qty": 20},
    ])
    assert corrected.status_code == 200, corrected.get_json()
    assert _stock_row(corrected.get_json(), s["compact"].id)["derived_sold"]["cartons"] == 130

    # A day the Manager has just reopened is open again, not an exception.
    assert _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
         "issued_qty": 100, "total_qty": 150, "return_qty": 150},
        {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
         "issued_qty": 20, "return_qty": 30},
    ]).status_code == 200
    base = {"sales_profile_id": s["profile"].id}
    assert client.patch("/api/sales/days/2026-10-30",
                        json={**base, "actual_cash_count_ugx": 0}).status_code == 200
    assert client.post("/api/sales/days/2026-10-30/close", json=base).status_code == 200
    assert client.post("/api/sales/days/2026-10-30/reopen",
                       json={**base, "reason": "Physical count"}).status_code == 200
    reopened = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
         "issued_qty": 100, "total_qty": 150, "return_qty": 10},
    ])
    assert reopened.status_code == 200, reopened.get_json()
    # A reason supplied by hand is still recorded in the audit trail.
    with_reason = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
         "issued_qty": 100, "total_qty": 150, "return_qty": 12,
         "reason": "Recounted the vehicle"},
    ])
    assert with_reason.status_code == 200, with_reason.get_json()
    stored = (SalesVehicleClosingLine.query.join(SalesDay)
              .filter(SalesDay.business_date == "2026-10-30",
                      SalesVehicleClosingLine.product_id == s["compact"].id).one())
    assert stored.correction_reason == "Recounted the vehicle"


def test_third_catalogue_product_reaches_its_own_sales_card(client, sales_setup):
    """A product added from the catalogue gets a Sales Card with no extra save."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    straws = _add_straws(s)
    response = _save_vehicle_stock(client, s["profile"].id, [
        {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
         "issued_qty": 100, "total_qty": 150, "return_qty": 17},
        {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
         "issued_qty": 20, "return_qty": 5},
        {"product_id": straws.id, "unit": "carton", "previous_qty": 0,
         "issued_qty": 26, "return_qty": 2},
    ])
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert [_stock_row(body, product.id)["derived_sold"]["cartons"]
            for product in (s["compact"], s["napkin"], straws)] == [133, 25, 24]
    # Sold Today is what each product's sales card prices - nothing is retyped.
    assert _save_card(client, s["profile"].id, straws.id,
                      [{"quantity": 24, "unit": "carton", "unit_price_ugx": 5_000}]).status_code == 200
    assert _save_card(client, s["profile"].id, s["compact"].id,
                      [{"quantity": 133, "unit": "carton", "unit_price_ugx": 79_000}]).status_code == 200
    totals = _day_summary(client, s["profile"].id)["totals"]
    assert totals["total_sales_ugx"] == 24 * 5_000 + 133 * 79_000


# ---- Save everything at once ------------------------------------------------
# The Vehicle stock sheet is the salesperson's working paper on screen.  It is
# validated as often as needed, and stored exactly once: by Close day.

def _closed_lines(date="2026-10-30"):
    return (SalesVehicleClosingLine.query.join(SalesDay)
            .filter(SalesDay.business_date == date).all())


def _close_day(client, profile_id, lines=None, date="2026-10-30", **extra):
    body = {"sales_profile_id": profile_id, **extra}
    if lines is not None:
        body["vehicle_stock"] = lines
    return client.post(f"/api/sales/days/{date}/close", json=body)


def test_saving_a_sales_card_never_persists_the_vehicle_stock_sheet(client, sales_setup):
    """A card is priced against the sheet on screen; the sheet is not written.

    Total Stock 150 less Return Today 17 is 133 sold, so the card can price 133
    cartons with nothing saved first.  Correcting Return Today to 20 makes it
    130 and the card follows in the same way.  The database keeps the day's
    untouched figures throughout: Close day is the only write.
    """
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    sheet = {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
             "issued_qty": 100, "total_qty": 150, "return_qty": 17}
    saved = _save_card(client, s["profile"].id, s["compact"].id,
                       [{"quantity": 133, "unit": "carton", "unit_price_ugx": 79_000}],
                       vehicle_stock=[sheet])
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["card"]["action"] == "created"
    assert saved.get_json()["totals"]["total_sales_ugx"] == 133 * 79_000

    # Nothing about Vehicle stock reached the database.
    after = _day_summary(client, s["profile"].id)
    row = _stock_row(after, s["compact"].id)
    assert row["closing"] is None and row["return_counted"] is False
    assert row["previous_return"]["cartons"] == 50      # the system figure, not 52
    assert row["total_stock"]["cartons"] == 150
    assert row["derived_sold"] is None
    assert _closed_lines() == []

    # 150 less 20 is 130 sold: the live sheet prices the card again, and again
    # nothing is stored.
    repriced = _save_card(client, s["profile"].id, s["compact"].id,
                          [{"quantity": 130, "unit": "carton", "unit_price_ugx": 79_000}],
                          vehicle_stock=[{**sheet, "return_qty": 20}])
    assert repriced.status_code == 200, repriced.get_json()
    assert repriced.get_json()["card"]["action"] == "created"
    assert repriced.get_json()["totals"]["total_sales_ugx"] == 130 * 79_000
    still = _day_summary(client, s["profile"].id)
    assert _stock_row(still, s["compact"].id)["closing"] is None
    assert _stock_row(still, s["compact"].id)["previous_return"]["cartons"] == 50
    assert _closed_lines() == []



def test_a_card_is_refused_when_it_does_not_price_the_live_sold_quantity(client, sales_setup):
    """The rule is unchanged: rows must price exactly the Sold Today on screen."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    sheet = {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
             "issued_qty": 100, "total_qty": 150, "return_qty": 17}
    over = _save_card(client, s["profile"].id, s["compact"].id,
                      [{"quantity": 150, "unit": "carton", "unit_price_ugx": 79_000}],
                      vehicle_stock=[sheet])
    assert over.status_code == 400
    assert "but only 133 cartons were sold" in over.get_json()["error"]
    under = _save_card(client, s["profile"].id, s["compact"].id,
                       [{"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000}],
                       vehicle_stock=[sheet])
    assert under.status_code == 400
    assert "remaining 33 cartons" in under.get_json()["error"]
    # A refusal writes neither a sale nor any part of the sheet.
    assert SalesInvoice.query.filter_by(sales_profile_id=s["profile"].id).count() == 0
    assert _closed_lines() == []
    # A sheet that cannot describe a sold quantity is refused before pricing too.
    invalid = _save_card(client, s["profile"].id, s["compact"].id,
                         [{"quantity": 133, "unit": "carton", "unit_price_ugx": 79_000}],
                         vehicle_stock=[{**sheet, "return_qty": 151}])
    assert invalid.status_code == 400
    assert "Return Today cannot exceed Total Stock" in invalid.get_json()["error"]
    assert SalesInvoice.query.filter_by(sales_profile_id=s["profile"].id).count() == 0


def test_close_day_saves_the_sheet_and_the_cards_in_one_action(client, sales_setup):
    """Close day persists the sheet, derives Sold Today from it and closes.

    Both cards were priced while the sheet was still on screen and nothing was
    stored; the close request carries the complete sheet and commits it with the
    day, so a reload reproduces every figure exactly.
    """
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    compact = {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
               "issued_qty": 100, "total_qty": 150, "return_qty": 17}
    napkin = {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
              "issued_qty": 20, "return_qty": 5}
    assert _save_card(client, s["profile"].id, s["compact"].id,
                      [{"quantity": 133, "unit": "carton", "unit_price_ugx": 79_000}],
                      vehicle_stock=[compact, napkin]).status_code == 200
    assert _save_card(client, s["profile"].id, s["napkin"].id,
                      [{"quantity": 25, "unit": "carton", "unit_price_ugx": 103_000}],
                      vehicle_stock=[compact, napkin]).status_code == 200
    assert _closed_lines() == []
    total = 133 * 79_000 + 25 * 103_000
    assert client.patch("/api/sales/days/2026-10-30",
                        json={"sales_profile_id": s["profile"].id,
                              "actual_cash_count_ugx": total}).status_code == 200

    closed = _close_day(client, s["profile"].id, [compact, napkin])
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    assert body["day"]["status"] == "closed"
    assert _stock_row(body, s["compact"].id)["previous_return"]["cartons"] == 52
    assert _stock_row(body, s["compact"].id)["total_stock"]["cartons"] == 150
    assert _stock_row(body, s["compact"].id)["closing"]["cartons"] == 17
    assert _stock_row(body, s["compact"].id)["derived_sold"]["cartons"] == 133
    assert _stock_row(body, s["napkin"].id)["derived_sold"]["cartons"] == 25
    assert body["totals"]["total_sales_ugx"] == total

    # Reloading reproduces exactly what was closed, from the stored sheet.
    reloaded = _day_summary(client, s["profile"].id)
    assert reloaded["day"]["status"] == "closed"
    assert _stock_row(reloaded, s["compact"].id)["previous_return"]["cartons"] == 52
    assert _stock_row(reloaded, s["compact"].id)["closing"]["cartons"] == 17
    assert _stock_row(reloaded, s["napkin"].id)["total_stock"]["cartons"] == 30
    assert _stock_row(reloaded, s["napkin"].id)["derived_sold"]["cartons"] == 25


def test_a_close_refused_for_a_card_mismatch_writes_nothing(client, sales_setup):
    """A genuine validation failure keeps the day open and the sheet unwritten."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    # Sold Today 25 on screen, and the card is priced for those 25 cartons.
    compact = {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
               "issued_qty": 100, "total_qty": 150, "return_qty": 125}
    napkin = {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
              "issued_qty": 20, "return_qty": 30}
    assert _save_card(client, s["profile"].id, s["compact"].id,
                      [{"quantity": 25, "unit": "carton", "unit_price_ugx": 79_000}],
                      vehicle_stock=[compact, napkin]).status_code == 200
    assert client.patch("/api/sales/days/2026-10-30",
                        json={"sales_profile_id": s["profile"].id,
                              "actual_cash_count_ugx": 25 * 79_000}).status_code == 200

    # The sheet submitted at Close day no longer agrees with the priced card:
    # 150 less 120 is 30 sold, but only 25 were priced.
    refused = _close_day(client, s["profile"].id, [{**compact, "return_qty": 120}, napkin])
    assert refused.status_code == 400
    error = refused.get_json()["error"]
    assert "Compact Standard" in error and "difference" in error
    assert "Correct the count or the sales before closing." in error
    # The day is still open and nothing from the attempt was written.
    after = _day_summary(client, s["profile"].id)
    assert after["day"]["status"] == "draft"
    row = _stock_row(after, s["compact"].id)
    assert row["closing"] is None and row["return_counted"] is False
    assert row["previous_return"]["cartons"] == 50      # not the 52 that was typed
    assert row["total_stock"]["cartons"] == 150
    assert _closed_lines() == []

    # The corrected sheet closes, and then it is stored.
    fixed = _close_day(client, s["profile"].id, [compact, napkin])
    assert fixed.status_code == 200, fixed.get_json()
    assert fixed.get_json()["day"]["status"] == "closed"
    assert _stock_row(_day_summary(client, s["profile"].id),
                      s["compact"].id)["closing"]["cartons"] == 125
    assert len(_closed_lines()) == 2


def test_a_catalogue_product_reaches_its_card_and_the_close_with_no_stock_save(client, sales_setup):
    """Straws is in no code and in no saved sheet - only on the screen."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    straws = _add_straws(s)
    compact = {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
               "issued_qty": 100, "total_qty": 150, "return_qty": 150}
    napkin = {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
              "issued_qty": 20, "return_qty": 30}
    straw = {"product_id": straws.id, "unit": "carton", "previous_qty": 0,
             "issued_qty": 26, "return_qty": 2}
    priced = _save_card(client, s["profile"].id, straws.id,
                        [{"quantity": 24, "unit": "carton", "unit_price_ugx": 5_000}],
                        vehicle_stock=[compact, napkin, straw])
    assert priced.status_code == 200, priced.get_json()
    assert priced.get_json()["totals"]["total_sales_ugx"] == 24 * 5_000
    # Straws has no stored row anywhere yet: only the sale it just priced is in
    # the database, so the day still shows its own system figures for it.
    assert _closed_lines() == []
    untyped = _stock_row(_day_summary(client, s["profile"].id), straws.id)
    assert untyped["total_stock"]["cartons"] == 0        # not the 26 on screen
    assert untyped["derived_sold"] is None

    assert client.patch("/api/sales/days/2026-10-30",
                        json={"sales_profile_id": s["profile"].id,
                              "actual_cash_count_ugx": 24 * 5_000}).status_code == 200
    closed = _close_day(client, s["profile"].id, [compact, napkin, straw])
    assert closed.status_code == 200, closed.get_json()
    stored = _stock_row(_day_summary(client, s["profile"].id), straws.id)
    assert stored["total_stock"]["cartons"] == 26
    assert stored["closing"]["cartons"] == 2
    assert stored["derived_sold"]["cartons"] == 24
    assert len(_closed_lines()) == 3


# ---------------------------------------------------------------------------
# The Sales screen the salesperson actually works on: Close day is the only
# save (the sheet and every Sales Card together), a card that was added by
# mistake can be cancelled, an expense is cancelled with a compact X that
# leaves the rest of the day alone, descriptions are remembered, and a card
# prices fractional quantities with no unit selector of its own.
# ---------------------------------------------------------------------------

def _close_with_cards(client, profile_id, lines, cards, date="2026-10-30", **extra):
    body = {"sales_profile_id": profile_id, "vehicle_stock": lines, "sales_cards": cards, **extra}
    return client.post(f"/api/sales/days/{date}/close", json=body)


def test_close_day_stores_the_sales_cards_and_the_sheet_together(client, sales_setup):
    """One request closes the day: both cards and the whole sheet are stored."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    compact = {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
               "issued_qty": 100, "total_qty": 150, "return_qty": 40}
    napkin = {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
              "issued_qty": 20, "return_qty": 5}
    cards = [
        {"product_id": s["compact"].id, "rows": [
            {"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000},
            {"quantity": 10, "unit": "carton", "unit_price_ugx": 80_000},
        ]},
        {"product_id": s["napkin"].id, "rows": [
            {"quantity": 25, "unit": "carton", "unit_price_ugx": 103_000},
        ]},
    ]
    # While the day is open nothing from either card is stored.
    assert SalesInvoice.query.count() == 0
    assert _closed_lines() == []
    assert client.patch("/api/sales/days/2026-10-30",
                        json={"sales_profile_id": s["profile"].id,
                              "actual_cash_count_ugx": 10_580_000}).status_code == 200

    closed = _close_with_cards(client, s["profile"].id, [compact, napkin], cards)
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    assert body["day"]["status"] == "closed"
    assert body["totals"]["total_sales_ugx"] == 100 * 79_000 + 10 * 80_000 + 25 * 103_000
    # One card per product, with the price rows exactly as submitted.
    assert SalesInvoice.query.count() == 2
    stored_card = next(c for c in body["product_cards"] if c["product_id"] == s["compact"].id)
    assert [(r["quantity"], r["unit"], r["unit_price_ugx"]) for r in stored_card["rows"]] == [
        (100, "carton", 79_000), (10, "carton", 80_000)]
    # The sheet that was typed is the sheet that is stored, and reloading
    # reproduces the close exactly.
    reloaded = _day_summary(client, s["profile"].id)
    compact_row = _stock_row(reloaded, s["compact"].id)
    assert (compact_row["previous_return"]["cartons"], compact_row["total_stock"]["cartons"],
            compact_row["closing"]["cartons"], compact_row["derived_sold"]["cartons"]) == (52, 150, 40, 110)
    assert reloaded["totals"]["total_sales_ugx"] == body["totals"]["total_sales_ugx"]



def test_a_close_refused_for_a_card_writes_no_card_and_no_sheet(client, sales_setup):
    """A refused close is all or nothing: no partial card, no partial sheet."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    compact = {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
               "issued_qty": 100, "total_qty": 150, "return_qty": 40}
    napkin = {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
              "issued_qty": 20, "return_qty": 5}
    good_napkin = {"product_id": s["napkin"].id,
                   "rows": [{"quantity": 25, "unit": "carton", "unit_price_ugx": 103_000}]}
    assert client.patch("/api/sales/days/2026-10-30",
                        json={"sales_profile_id": s["profile"].id,
                              "actual_cash_count_ugx": 0}).status_code == 200

    # Napkins prices exactly what was sold; Compact prices 100 of the 110 sold,
    # so the second card is refused after the first was already priced.
    refused = _close_with_cards(client, s["profile"].id, [compact, napkin], [
        good_napkin,
        {"product_id": s["compact"].id,
         "rows": [{"quantity": 100, "unit": "carton", "unit_price_ugx": 79_000}]},
    ])
    assert refused.status_code == 400
    error = refused.get_json()["error"]
    assert "Compact Standard" in error and "were sold" in error
    # Nothing at all was written: no card, no sheet row, still open.
    assert SalesInvoice.query.count() == 0
    assert _closed_lines() == []
    after = _day_summary(client, s["profile"].id)
    assert after["day"]["status"] == "draft"
    assert after["totals"]["total_sales_ugx"] == 0

    # The corrected card closes the day and both are stored.
    fixed = _close_with_cards(client, s["profile"].id, [compact, napkin], [
        good_napkin,
        {"product_id": s["compact"].id,
         "rows": [{"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}]},
    ])
    assert fixed.status_code == 200, fixed.get_json()
    assert SalesInvoice.query.count() == 2
    assert len(_closed_lines()) == 2


def test_decimal_sales_card_quantities_price_sold_today_exactly(client, sales_setup):
    """0.5, 0.25 and 1.5 price what the vehicle says was sold - exactly."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    # Compact: 152 less 149 is 3 cartons sold, priced as 1.5 + 1.5.
    compact = {"product_id": s["compact"].id, "unit": "carton", "previous_qty": 52,
               "issued_qty": 100, "total_qty": 152, "return_qty": 149}
    # Napkins: 30 less 29 is 1 carton sold, priced as 0.25 + 0.25 + 0.5.
    napkin = {"product_id": s["napkin"].id, "unit": "carton", "previous_qty": 10,
              "issued_qty": 20, "return_qty": 29}
    assert client.patch("/api/sales/days/2026-10-30",
                        json={"sales_profile_id": s["profile"].id,
                              "actual_cash_count_ugx": 0}).status_code == 200
    cards = [
        {"product_id": s["compact"].id, "rows": [
            {"quantity": 1.5, "unit": "carton", "unit_price_ugx": 79_000},
            {"quantity": 1.5, "unit": "carton", "unit_price_ugx": 80_000},
        ]},
        {"product_id": s["napkin"].id, "rows": [
            {"quantity": 0.25, "unit": "carton", "unit_price_ugx": 103_000},
            {"quantity": 0.25, "unit": "carton", "unit_price_ugx": 104_000},
            {"quantity": 0.5, "unit": "carton", "unit_price_ugx": 105_000},
        ]},
    ]
    closed = _close_with_cards(client, s["profile"].id, [compact, napkin], cards)
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    compact_total = 1.5 * 79_000 + 1.5 * 80_000
    napkin_total = 0.25 * 103_000 + 0.25 * 104_000 + 0.5 * 105_000
    assert body["totals"]["total_sales_ugx"] == compact_total + napkin_total
    compact_card = next(c for c in body["product_cards"] if c["product_id"] == s["compact"].id)
    assert [r["quantity"] for r in compact_card["rows"]] == [1.5, 1.5]
    assert compact_card["card_total_ugx"] == compact_total
    # The stored base quantities are exact pieces: 1.5 cartons is 150 pieces.
    assert [r["base_unit_qty"] for r in compact_card["rows"]] == [150, 150]
    # Sold Today and the priced rows agree, so the close found no variance.
    assert body["stock_warnings"] == []



def test_decimal_rows_that_do_not_price_sold_today_are_refused(client, sales_setup):
    """A fractional card is still checked against Sold Today, not rounded."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    compact = {"product_id": s["compact"].id, "unit": "piece", "previous_qty": 15000,
               "issued_qty": 0, "total_qty": 15000, "return_qty": 14950}
    # 50 pieces sold; 0.25 + 0.25 cartons is 50 pieces exactly, 0.2 is not.
    short = _save_card(client, s["profile"].id, s["compact"].id,
                       [{"quantity": 0.25, "unit": "carton", "unit_price_ugx": 79_000},
                        {"quantity": 0.2, "unit": "carton", "unit_price_ugx": 79_000}],
                       vehicle_stock=[compact])
    assert short.status_code == 400
    assert "were sold" in short.get_json()["error"]
    exact = _save_card(client, s["profile"].id, s["compact"].id,
                       [{"quantity": 0.25, "unit": "carton", "unit_price_ugx": 79_000},
                        {"quantity": 0.25, "unit": "carton", "unit_price_ugx": 80_000}],
                       vehicle_stock=[compact])
    assert exact.status_code == 200, exact.get_json()
    assert exact.get_json()["totals"]["total_sales_ugx"] == 0.25 * 79_000 + 0.25 * 80_000
    # More than three decimal places is a typo, not silently rounded away.
    too_precise = _save_card(client, s["profile"].id, s["compact"].id,
                             [{"quantity": 0.1234, "unit": "carton", "unit_price_ugx": 79_000}],
                             vehicle_stock=[compact])
    assert too_precise.status_code == 400
    assert "three decimal places" in too_precise.get_json()["error"]


# ---- A sales card quantity is always a carton-equivalent --------------------
# The number typed on a Sales Card means cartons and nothing else: 1 is one
# carton, 0.5 half a carton, 0.25 a quarter and 1.5 one and a half.  Pieces and
# packs are always derived from the product's PackagingRule, so the meaning of
# the number never changes with the Vehicle stock "Counted in" unit that the
# same product happens to be counted in on the sheet.

def _packing_product(name, packs_per_carton, pieces_per_pack, order=9):
    product = Product(name=name, display_order=order, active=True)
    db.session.add(product); db.session.flush()
    rule = PackagingRule(product_id=product.id, cartons_to_packs=packs_per_carton,
                         packs_to_pieces=pieces_per_pack,
                         effective_from=datetime(2026, 1, 1))
    db.session.add(rule); db.session.commit()
    return product, rule


def _sheet_line(product_id, unit, previous, issued, total, returned):
    return {"product_id": product_id, "unit": unit, "previous_qty": previous,
            "issued_qty": issued, "total_qty": total, "return_qty": returned}


def _untouched_lines(s):
    """The fixture's own two products, with nothing sold, so only the new
    product's card is under test and the close has every return it needs."""
    return [_sheet_line(s["compact"].id, "carton", 50, 100, 150, 150),
            _sheet_line(s["napkin"].id, "carton", 10, 20, 30, 30)]


def _count_cash(client, profile_id, amount, date="2026-10-30"):
    response = client.patch(f"/api/sales/days/{date}",
                           json={"sales_profile_id": profile_id, "actual_cash_count_ugx": amount})
    assert response.status_code == 200, response.get_json()


def _card_rows(summary, product_id):
    card = next((c for c in summary["product_cards"] if c["product_id"] == product_id), None)
    return [(r["quantity"], r["unit"], r["base_unit_qty"]) for r in card["rows"]] if card else []


def test_half_a_carton_is_five_packs_on_a_ten_pack_carton(client, sales_setup):
    """10 packs per carton: 0.5 on a card is 5 packs, never half a pack."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    product, rule = _packing_product("Ten Pack", 10, 6)          # 10 packs of 6 = 60 pieces
    assert rule.pieces_per_carton == 60
    # The vehicle counted this product in packs and sold 5 of them.
    sheet = _untouched_lines(s) + [_sheet_line(product.id, "pack", 100, 0, 100, 95)]
    cards = [{"product_id": product.id,
              "rows": [{"quantity": 0.5, "unit": "carton", "unit_price_ugx": 90_000}]}]
    _count_cash(client, s["profile"].id, 45_000)
    closed = _close_with_cards(client, s["profile"].id, sheet, cards)
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    # 0.5 cartons is stored as half of a 60-piece carton: 30 pieces.
    assert _card_rows(body, product.id) == [(0.5, "carton", 30)]
    # 30 pieces is exactly 5 packs of 6, and half a pack would have been 3.
    assert sales_service.quantity_dict(Decimal("30"), rule)["packs"] == 5
    assert sales_service.sales_row_base(Decimal("0.5"), "pack", rule) == Decimal("3")
    assert body["totals"]["total_sales_ugx"] == 45_000


def test_half_a_carton_is_six_packs_on_a_twelve_pack_carton(client, sales_setup):
    """12 packs per carton: the same 0.5 is 6 packs, still exactly half a carton."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    product, rule = _packing_product("Twelve Pack", 12, 5)       # 12 packs of 5 = 60 pieces
    assert rule.pieces_per_carton == 60
    sheet = _untouched_lines(s) + [_sheet_line(product.id, "pack", 100, 0, 100, 94)]   # 6 packs sold
    cards = [{"product_id": product.id,
              "rows": [{"quantity": 0.5, "unit": "carton", "unit_price_ugx": 90_000}]}]
    _count_cash(client, s["profile"].id, 45_000)
    closed = _close_with_cards(client, s["profile"].id, sheet, cards)
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    assert _card_rows(body, product.id) == [(0.5, "carton", 30)]
    assert sales_service.quantity_dict(Decimal("30"), rule)["packs"] == 6
    # Half a pack would have been 2.5 pieces - a different quantity entirely.
    assert sales_service.sales_row_base(Decimal("0.5"), "pack", rule) == Decimal("2.5")
    # The pack count differs from the ten-pack product, the carton does not.
    assert body["stock_warnings"] == []


def test_half_a_carton_does_not_follow_the_vehicle_stock_unit(client, sales_setup):
    """Vehicle stock counted in pieces: 0.5 is still half a carton, not 0.5 piece."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    product, rule = _packing_product("Twelve Pack", 12, 5)
    # Counted in pieces this time: 600 less 570 is 30 pieces sold.
    sheet = _untouched_lines(s) + [_sheet_line(product.id, "piece", 600, 0, 600, 570)]
    cards = [{"product_id": product.id,
              "rows": [{"quantity": 0.5, "unit": "carton", "unit_price_ugx": 90_000}]}]
    _count_cash(client, s["profile"].id, 45_000)
    closed = _close_with_cards(client, s["profile"].id, sheet, cards)
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    assert _card_rows(body, product.id) == [(0.5, "carton", 30)]
    # Had the card number followed "Counted in: pieces" it would have priced 0.5.
    assert sales_service.sales_row_base(Decimal("0.5"), "piece", rule) == Decimal("0.5")
    stock = _stock_row(body, product.id)
    assert stock["derived_sold"]["base_unit_qty"] == 30
    assert stock["variance"]["base_unit_qty"] == 0


def test_quarter_and_one_and_a_half_cartons_convert_exactly(client, sales_setup):
    """0.25 and 1.5 go through the rule exactly, on both packing shapes."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    ten, ten_rule = _packing_product("Ten Pack", 10, 6, order=9)
    twelve, twelve_rule = _packing_product("Twelve Pack", 12, 5, order=10)
    for rule in (ten_rule, twelve_rule):
        assert sales_service.sales_row_base(Decimal("0.25"), "carton", rule) == Decimal("15")
        assert sales_service.sales_row_base(Decimal("1.5"), "carton", rule) == Decimal("90")
    # 0.25 + 1.5 = 1.75 cartons = 105 pieces sold on each product.
    sheet = _untouched_lines(s) + [_sheet_line(ten.id, "piece", 600, 0, 600, 495),
                                   _sheet_line(twelve.id, "piece", 600, 0, 600, 495)]
    rows = [{"quantity": 0.25, "unit": "carton", "unit_price_ugx": 100_000},
            {"quantity": 1.5, "unit": "carton", "unit_price_ugx": 100_000}]
    cards = [{"product_id": ten.id, "rows": rows}, {"product_id": twelve.id, "rows": rows}]
    _count_cash(client, s["profile"].id, 350_000)
    closed = _close_with_cards(client, s["profile"].id, sheet, cards)
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    for product in (ten, twelve):
        assert _card_rows(body, product.id) == [(0.25, "carton", 15), (1.5, "carton", 90)]
    # The same 90 pieces reads as 1 carton 5 packs or 1 carton 6 packs: the
    # carton is the only unit the card number itself ever means.
    assert sales_service.quantity_dict(Decimal("90"), ten_rule)["label"] == "1 carton 5 packs"
    assert sales_service.quantity_dict(Decimal("90"), twelve_rule)["label"] == "1 carton 6 packs"
    assert body["totals"]["total_sales_ugx"] == 2 * (0.25 * 100_000 + 1.5 * 100_000)
    assert body["stock_warnings"] == []


def test_cancelling_an_expense_leaves_stock_sales_and_history_alone(client, sales_setup):
    """The X on an expense only cancels that expense; nothing else moves."""
    s = sales_setup; _login(client, s["operator"])
    _card_ready(client, s)
    assert _save_card(client, s["profile"].id, s["compact"].id,
                      [{"quantity": 110, "unit": "carton", "unit_price_ugx": 79_000}]).status_code == 200
    lunch = _cash_expense(client, s["profile"].id, "Lunch", 10_000)
    fahad = _cash_expense(client, s["profile"].id, "Fahad", 20_000)
    assert lunch.status_code == 201 and fahad.status_code == 201

    before = _day_summary(client, s["profile"].id)
    cancelled = client.post(f"/api/sales/void/expense/{lunch.get_json()['id']}",
                            json={"reason": "Removed from the Sales screen"})
    assert cancelled.status_code == 200
    after = _day_summary(client, s["profile"].id)

    # Only the cancelled expense left the active list, and the money follows.
    assert [e["description"] for e in after["expenses"]] == ["Fahad"]
    assert after["totals"]["cash_expenses_ugx"] == 20_000
    assert after["totals"]["cash_at_hand_ugx"] == before["totals"]["cash_at_hand_ugx"] + 10_000
    # Sales, stock and yesterday's closed figures are untouched.
    assert after["totals"]["total_sales_ugx"] == before["totals"]["total_sales_ugx"] == 110 * 79_000
    compact_row = _stock_row(after, s["compact"].id)
    assert compact_row["closing"]["cartons"] == 40
    assert compact_row["previous_return"]["cartons"] == 50
    assert compact_row["derived_sold"]["cartons"] == 110
    # It is a soft void with a reason, never a physical delete.
    row = db.session.get(SalesExpense, lunch.get_json()["id"])
    assert row is not None and row.status == "void" and row.void_reason


def test_expense_descriptions_are_remembered_for_later_days(client, sales_setup):
    """Descriptions survive days and sessions, and capitals are one suggestion."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    assert _cash_expense(client, s["profile"].id, "Lunch", 10_000).status_code == 201
    assert _cash_expense(client, s["profile"].id, "lunch", 5_000).status_code == 201
    assert _cash_expense(client, s["profile"].id, "Fahad", 20_000).status_code == 201

    today = _day_summary(client, s["profile"].id)["expense_suggestions"]
    # Newest first, one suggestion per spelling, and free text is preserved.
    assert today == ["Fahad", "lunch"]
    # A later day still offers them, and a cancelled expense keeps its words.
    tomorrow = _day_summary(client, s["profile"].id, date="2026-10-31")["expense_suggestions"]
    assert tomorrow == today
    expenses = _day_summary(client, s["profile"].id)["expenses"]
    assert client.post(f"/api/sales/void/expense/{expenses[0]['id']}",
                       json={"reason": "Removed from the Sales screen"}).status_code == 200
    assert "Fahad" in _day_summary(client, s["profile"].id)["expense_suggestions"]
