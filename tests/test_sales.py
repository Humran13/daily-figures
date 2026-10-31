from datetime import datetime
from uuid import uuid4

import pytest

from webapp.extensions import db
from webapp.models.customer import CATEGORY_CUSTOMER, CATEGORY_SALESPERSON, Customer
from webapp.models.audit_log import AuditLog
from webapp.models.dispatch import Dispatch, DispatchLine, STATUS_FINALIZED
from webapp.models.packaging_rule import PackagingRule
from webapp.models.product import Product
from webapp.models.sales import (
    SalesBaseline, SalesDay, SalesInvoice, SalesOpeningStockLine, SalesProfile,
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
