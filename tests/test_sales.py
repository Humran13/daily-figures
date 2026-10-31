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
    SalesBaseline, SalesDay, SalesDebtor, SalesInvoice, SalesOpeningStockLine, SalesProfile,
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


def _save_card(client, profile_id, product_id, rows, date="2026-10-30"):
    return client.put(f"/api/sales/days/{date}/products/{product_id}/sales",
                      json={"sales_profile_id": profile_id, "rows": rows})


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

