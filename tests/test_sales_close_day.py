"""Close Day is the only write: the approved business workflow, end to end.

These tests describe the workflow the business approved, not the older
implementation that persisted a draft day:

  * selecting a date writes nothing at all;
  * every separate write endpoint refuses a day that was never closed;
  * one Close Day stores the whole day - stock, cards, expenses, money, debts
    and collections - in a single transaction;
  * a refused Close Day leaves nothing behind;
  * reopening a closed day reproduces exactly what was stored;
  * money collected on a later date reduces the old debt, counts as money
    received then, and never becomes a new product sale.
"""
from datetime import datetime

import pytest

from webapp.extensions import db
from webapp.models.customer import CATEGORY_SALESPERSON, Customer
from webapp.models.packaging_rule import PackagingRule
from webapp.models.product import Product
from webapp.models.sales import (
    SalesBaseline, SalesDay, SalesDebt, SalesDebtPayment, SalesDebtor, SalesExpense,
    SalesInvoice, SalesOpeningStockLine, SalesProfile, SalesVehicleClosingLine,
)
from webapp.models.section import SECTION_SALES
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_VIEWER
from webapp.services import sales_service, section_access_service


def _grant(user):
    section_access_service.ensure_seeded()
    section_access_service.set_sections_for_user(user, [SECTION_SALES], user)
    db.session.commit()


def _login(client, user, password="password123"):
    response = client.post("/api/login",
                           json={"username": user.username, "password": password,
                                 "section": SECTION_SALES})
    assert response.status_code == 200, response.get_json()


@pytest.fixture
def fd(app, make_user):
    """One salesperson, one 10-pack-of-10 product, a starting count, a login."""
    manager = make_user("fd-manager", "password123", ROLE_MANAGER)
    operator = make_user("fd-sales", "password123", ROLE_OPERATOR)
    viewer = make_user("fd-viewer", "password123", ROLE_VIEWER)
    for user in (manager, operator, viewer):
        _grant(user)
    recipient = Customer(name="FD Dispatch", category=CATEGORY_SALESPERSON, active=True)
    product = Product(name="Compact", display_order=1, active=True)
    db.session.add_all([recipient, product])
    db.session.flush()
    rule = PackagingRule(product_id=product.id, cartons_to_packs=10, packs_to_pieces=10,
                         effective_from=datetime(2026, 1, 1), created_by=manager.id)
    db.session.add(rule)
    db.session.flush()
    profile = SalesProfile(user_id=operator.id, display_name="FD Sales",
                           recipient_customer_id=recipient.id)
    db.session.add(profile)
    db.session.flush()
    baseline = SalesBaseline(sales_profile_id=profile.id, effective_date="2026-10-29",
                             opening_cash_ugx=0, opening_mobile_money_ugx=0,
                             created_by=manager.id)
    db.session.add(baseline)
    db.session.flush()
    db.session.add(SalesOpeningStockLine(baseline_id=baseline.id, product_id=product.id,
                                         base_unit_qty=5000, packaging_rule_id=rule.id))
    db.session.commit()
    return dict(manager=manager, operator=operator, viewer=viewer, profile=profile,
                product=product, rule=rule, recipient=recipient)


def _sheet(product_id, previous, issued, total, returned, unit="carton"):
    return {"product_id": product_id, "unit": unit, "previous_qty": previous,
            "issued_qty": issued, "total_qty": total, "return_qty": returned}


def _card(product_id, *rows):
    return {"product_id": product_id,
            "rows": [{"quantity": qty, "unit": "carton", "unit_price_ugx": price}
                     for qty, price in rows]}


def _close(client, profile_id, date="2026-10-30", lines=None, cards=None, **extra):
    body = {"sales_profile_id": profile_id, "vehicle_stock": lines or [],
            "sales_cards": cards or []}
    body.update(extra)
    return client.post(f"/api/sales/days/{date}/close", json=body)


def _table_counts():
    return {
        "days": SalesDay.query.count(),
        "lines": SalesVehicleClosingLine.query.count(),
        "invoices": SalesInvoice.query.count(),
        "expenses": SalesExpense.query.count(),
        "debts": SalesDebt.query.count(),
        "payments": SalesDebtPayment.query.count(),
    }


def _nothing_stored():
    return all(count == 0 for count in _table_counts().values())


def test_selecting_a_date_creates_no_sales_day(client, fd):
    _login(client, fd["operator"])
    opened = client.post("/api/sales/days",
                         json={"sales_profile_id": fd["profile"].id,
                               "business_date": "2026-10-30"})
    assert opened.status_code == 200, opened.get_json()
    body = opened.get_json()
    assert body["day"] is None
    assert body["invoices"] == [] and body["expenses"] == []
    # Nothing was created: no row, and no audit entry for a day that never was.
    assert SalesDay.query.count() == 0

    fetched = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={fd['profile'].id}")
    assert fetched.status_code == 200
    assert fetched.get_json()["day"] is None
    assert SalesDay.query.count() == 0


def test_an_abandoned_working_day_never_reaches_the_database(client, fd):
    """Every separate write refuses an unclosed day, and stores nothing."""
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    attempts = [
        ("PATCH", "/api/sales/days/2026-10-30",
         {"actual_cash_count_ugx": 1000, "mobile_money_from_sales_ugx": 500}),
        ("PUT", "/api/sales/days/2026-10-30/closing-stock",
         {"product_id": product_id, "quantity": 40, "unit": "carton"}),
        ("PUT", "/api/sales/days/2026-10-30/returns",
         {"lines": [{"product_id": product_id, "quantity": 40, "unit": "carton"}]}),
        ("PUT", "/api/sales/days/2026-10-30/vehicle-stock",
         {"lines": [_sheet(product_id, 50, 0, 50, 40)]}),
        ("PUT", f"/api/sales/days/2026-10-30/products/{product_id}/sales",
         {"rows": [{"quantity": 10, "unit": "carton", "unit_price_ugx": 79000}]}),
        ("POST", "/api/sales/expenses",
         {"business_date": "2026-10-30", "description": "Lunch", "amount_ugx": 10000,
          "payment_method": "cash", "idempotency_key": "fd-exp-1"}),
        ("POST", "/api/sales/transfers",
         {"business_date": "2026-10-30", "from_account": "cash",
          "to_account": "mobile_money", "amount_ugx": 1000, "idempotency_key": "fd-tr-1"}),
        ("POST", "/api/sales/debts",
         {"business_date": "2026-10-30", "name": "Darius", "amount_ugx": 5000,
          "idempotency_key": "fd-debt-1"}),
        ("POST", "/api/sales/invoices",
         {"business_date": "2026-10-30", "idempotency_key": "fd-inv-1",
          "lines": [{"product_id": product_id, "quantity": 1, "unit": "carton",
                     "unit_price_ugx": 79000}]}),
    ]
    for method, path, payload in attempts:
        response = client.open(path, method=method,
                               json={**payload, "sales_profile_id": profile_id})
        assert response.status_code == 409, (method, path, response.get_json())
        assert "Close Day" in response.get_json()["error"]
    assert _nothing_stored(), _table_counts()


def test_close_day_persists_the_whole_day_in_one_transaction(client, fd):
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    closed = _close(
        client, profile_id,
        lines=[_sheet(product_id, 50, 0, 50, 40)],          # 10 cartons sold
        cards=[_card(product_id, (10, 79000))],
        expenses=[{"description": "Lunch", "amount_ugx": 10000, "payment_method": "cash"}],
        transfers=[{"from_account": "cash", "to_account": "mobile_money",
                    "amount_ugx": 5000, "reference": "Handover"}],
        mobile_money_from_sales_ugx=300000,
        actual_cash_count_ugx=375000,
        actual_mobile_money_ugx=305000,
        debts=[{"name": "Darius", "amount_ugx": 100000}],
        notes="Saturday route",
    )
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    totals = body["totals"]
    assert body["day"]["status"] == "closed"
    assert body["day"]["notes"] == "Saturday route"
    # The approved allocation: Cash + Mobile Money + New Debt = Total Sales.
    assert totals["total_sales_ugx"] == 790000
    assert totals["mobile_money_from_sales_ugx"] == 300000
    assert totals["new_debt_ugx"] == 100000
    assert totals["cash_from_sales_ugx"] == 390000
    assert (totals["cash_from_sales_ugx"] + totals["mobile_money_from_sales_ugx"]
            + totals["new_debt_ugx"]) == totals["total_sales_ugx"]
    assert totals["total_expenses_ugx"] == 10000
    assert totals["cash_expenses_ugx"] == 10000
    # Cash at Hand = cash received − cash expenses − the 5,000 handed over to
    # Mobile Money, where it is still expected.
    assert totals["cash_at_hand_ugx"] == 375000
    assert totals["expected_mobile_money_ugx"] == 305000
    assert totals["cash_variance_ugx"] == 0
    assert totals["mobile_variance_ugx"] == 0
    stock = next(row for row in body["stock"] if row["product_id"] == product_id)
    assert stock["derived_sold"]["cartons"] == 10
    assert stock["recorded_sold"]["cartons"] == 10
    assert stock["reconciled"] is True
    assert body["expenses"][0]["description"] == "Lunch"
    assert [d["amount_ugx"] for d in body["debts"]] == [100000]
    assert [row["quantity"] for row in body["product_cards"][0]["rows"]] == [10]

    assert _table_counts() == {"days": 1, "lines": 1, "invoices": 1,
                               "expenses": 1, "debts": 1, "payments": 0}

    # Reopening the date reproduces exactly what was stored.
    again = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={profile_id}").get_json()
    assert again["day"]["status"] == "closed"
    assert again["totals"] == totals
    assert again["stock"][0]["closing"]["cartons"] == 40


def test_a_refused_close_writes_absolutely_nothing(client, fd):
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    # The card prices 5 cartons but the sheet says 10 were sold, so the close is
    # refused after the sheet, card, expense and debt were staged in the session.
    refused = _close(
        client, profile_id,
        lines=[_sheet(product_id, 50, 0, 50, 40)],
        cards=[_card(product_id, (5, 79000))],
        expenses=[{"description": "Lunch", "amount_ugx": 10000, "payment_method": "cash"}],
        mobile_money_from_sales_ugx=100000,
        actual_cash_count_ugx=100000,
        debts=[{"name": "Darius", "amount_ugx": 5000}],
    )
    assert refused.status_code == 400
    assert "were sold" in refused.get_json()["error"]
    assert _nothing_stored(), _table_counts()

    # ...and the same date can then be closed correctly.
    fixed = _close(client, profile_id, lines=[_sheet(product_id, 50, 0, 50, 40)],
                   cards=[_card(product_id, (10, 79000))], actual_cash_count_ugx=790000)
    assert fixed.status_code == 200, fixed.get_json()
    assert _table_counts()["days"] == 1


# ---- One product, more than one price on the same day -----------------------
def test_multiple_price_rows_can_represent_one_sold_quantity(client, fd):
    """One product sold at two prices: both rows together must equal Sold Today."""
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    closed = _close(
        client, profile_id,
        lines=[_sheet(product_id, 50, 0, 50, 40)],           # 10 cartons sold
        cards=[_card(product_id, (8, 79000), (2, 80000))],
        actual_cash_count_ugx=792000,
    )
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    assert body["totals"]["total_sales_ugx"] == 8 * 79000 + 2 * 80000
    rows = body["product_cards"][0]["rows"]
    assert sorted(row["quantity"] for row in rows) == [2, 8]
    stock = next(row for row in body["stock"] if row["product_id"] == product_id)
    assert stock["reconciled"] is True


def test_underpriced_multi_row_card_is_refused_at_close(client, fd):
    """Rows that sum to less than Sold Today are refused, and nothing persists."""
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    refused = _close(
        client, profile_id,
        lines=[_sheet(product_id, 50, 0, 50, 40)],           # 10 cartons sold
        cards=[_card(product_id, (8, 79000), (1, 80000))],   # only 9 priced
        actual_cash_count_ugx=712000,
    )
    assert refused.status_code == 400
    assert "were sold" in refused.get_json()["error"]
    assert _nothing_stored(), _table_counts()


def test_overpriced_multi_row_card_is_refused_at_close(client, fd):
    """Rows that sum to more than Sold Today are refused, and nothing persists."""
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    refused = _close(
        client, profile_id,
        lines=[_sheet(product_id, 50, 0, 50, 40)],           # 10 cartons sold
        cards=[_card(product_id, (8, 79000), (4, 80000))],   # 12 priced, 2 too many
        actual_cash_count_ugx=952000,
    )
    assert refused.status_code == 400
    assert "only" in refused.get_json()["error"]
    assert _nothing_stored(), _table_counts()


def test_decimal_multi_row_card_sums_exactly(client, fd):
    """Fractional carton rows (1 + 0.5) must sum to exactly 1.5 with no float drift."""
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    # 15 packs issued and none returned: 15 packs x 10 pieces = 150 pieces,
    # and this product's 100 pieces per carton makes that 1.5 cartons sold.
    closed = _close(
        client, profile_id,
        lines=[_sheet(product_id, 0, 15, 15, 0, unit="pack")],
        cards=[_card(product_id, (1, 79000), (0.5, 80000))],
        actual_cash_count_ugx=119000,
    )
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    assert body["totals"]["total_sales_ugx"] == 79000 + 40000
    stock = next(row for row in body["stock"] if row["product_id"] == product_id)
    # 1.5 cartons = 1 whole carton + 5 packs (half of the 10-pack carton).
    assert stock["derived_sold"]["base_unit_qty"] == 150
    assert stock["derived_sold"]["cartons"] == 1
    assert stock["derived_sold"]["packs"] == 5
    assert stock["reconciled"] is True


def test_close_requires_the_cash_count_unless_a_manager_overrides(client, fd):
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    without_cash = _close(client, profile_id, lines=[_sheet(product_id, 50, 0, 50, 40)],
                          cards=[_card(product_id, (10, 79000))])
    assert without_cash.status_code == 400
    assert "cash you counted" in without_cash.get_json()["error"]
    assert _nothing_stored()

    _login(client, fd["manager"])
    overridden = _close(client, profile_id, lines=[_sheet(product_id, 50, 0, 50, 40)],
                        cards=[_card(product_id, (10, 79000))],
                        override_reason="Cash box was taken to the bank")
    assert overridden.status_code == 200, overridden.get_json()
    assert overridden.get_json()["day"]["close_override_reason"] == "Cash box was taken to the bank"


def test_a_later_debtor_payment_is_money_today_not_a_new_sale(client, fd):
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    # 30 Oct: ten cartons on credit, nothing collected in cash.
    first = _close(client, profile_id, date="2026-10-30",
                   lines=[_sheet(product_id, 50, 0, 50, 40)],
                   cards=[_card(product_id, (10, 79000))],
                   debts=[{"name": "Darius", "amount_ugx": 790000}],
                   actual_cash_count_ugx=0)
    assert first.status_code == 200, first.get_json()
    assert first.get_json()["totals"]["cash_from_sales_ugx"] == 0
    assert first.get_json()["totals"]["new_debt_ugx"] == 790000
    assert [row["outstanding_ugx"] for row in first.get_json()["debtor_balances"]] == [790000]

    # 2 Nov: five more cartons sold for cash, and 40,000 collected on the old debt.
    second = _close(client, profile_id, date="2026-11-02",
                    lines=[_sheet(product_id, 40, 0, 40, 35)],
                    cards=[_card(product_id, (5, 80000))],
                    debt_payments=[{"name": "darius", "amount_ugx": 40000,
                                    "payment_method": "cash"}],
                    actual_cash_count_ugx=440000)
    assert second.status_code == 200, second.get_json()
    totals = second.get_json()["totals"]
    # The collection is money received on 2 November, never a second sale.
    assert totals["total_sales_ugx"] == 400000
    assert totals["cash_from_sales_ugx"] == 400000
    assert totals["old_debt_collections_ugx"] == 40000
    assert totals["cash_at_hand_ugx"] == 440000
    assert totals["cash_variance_ugx"] == 0
    assert [row["outstanding_ugx"] for row in second.get_json()["debtor_balances"]] == [750000]

    # 30 October itself is untouched.
    october = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={profile_id}").get_json()
    assert october["totals"]["total_sales_ugx"] == 790000
    assert october["totals"]["old_debt_collections_ugx"] == 0
    stock = next(row for row in october["stock"] if row["product_id"] == product_id)
    assert stock["derived_sold"]["cartons"] == 10

    # 3 Nov: the balance arrives on Mobile Money; the debt is Paid.
    third = _close(client, profile_id, date="2026-11-03",
                   lines=[_sheet(product_id, 35, 0, 35, 35)],
                   cards=[],
                   debt_payments=[{"name": "Darius", "amount_ugx": 750000,
                                   "payment_method": "mobile_money"}],
                   actual_mobile_money_ugx=750000,
                   actual_cash_count_ugx=440000)
    assert third.status_code == 200, third.get_json()
    totals = third.get_json()["totals"]
    assert totals["total_sales_ugx"] == 0
    assert totals["old_debt_mobile_money_ugx"] == 750000
    assert totals["mobile_money_from_sales_ugx"] == 0
    assert totals["expected_mobile_money_ugx"] == 750000
    assert third.get_json()["debtor_balances"] == []
    debt = SalesDebt.query.one()
    assert debt.business_date == "2026-10-30"
    assert SalesDebtPayment.query.count() == 2


def test_mobile_money_and_debt_cannot_exceed_the_sales_they_explain(client, fd):
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    too_much = _close(client, profile_id,
                      lines=[_sheet(product_id, 50, 0, 50, 40)],
                      cards=[_card(product_id, (10, 79000))],
                      mobile_money_from_sales_ugx=500000,
                      debts=[{"name": "Darius", "amount_ugx": 400000}],
                      actual_cash_count_ugx=0)
    assert too_much.status_code == 400
    assert "cannot exceed Total Sales" in too_much.get_json()["error"]
    assert _nothing_stored()


def test_removing_a_product_from_a_closed_day_is_audited_and_keeps_the_row(client, fd):
    from webapp.models.audit_log import AuditLog

    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    closed = _close(client, profile_id, lines=[_sheet(product_id, 50, 0, 50, 40)],
                    cards=[_card(product_id, (10, 79000))], actual_cash_count_ugx=790000)
    assert closed.status_code == 200
    # A salesperson cannot rewrite a closed day's vehicle at all.
    refused = client.post("/api/sales/days/2026-10-30/vehicle-stock/%d/remove" % product_id,
                          json={"sales_profile_id": profile_id, "reason": "Typo"})
    assert refused.status_code == 403

    _login(client, fd["manager"])
    removed = client.post("/api/sales/days/2026-10-30/vehicle-stock/%d/remove" % product_id,
                          json={"sales_profile_id": profile_id,
                                "reason": "This van never carried Compact"})
    assert removed.status_code == 200, removed.get_json()
    line = SalesVehicleClosingLine.query.one()
    assert line.removed_at is not None                        # kept, not deleted
    assert line.removal_reason == "This van never carried Compact"
    assert line.base_unit_qty == 4000                         # the count is intact
    audit = AuditLog.query.filter_by(entity_type="sales_vehicle_closing", action="remove").first()
    assert audit is not None and str(audit.entity_id) == str(line.id)
    summary = removed.get_json()
    stock = next((row for row in summary["stock"] if row["product_id"] == product_id), None)
    # The product no longer counts as a counted return for that day.
    assert stock is None or stock["closing"] is None
    assert summary["stock_warnings"] == []


def test_a_voided_historical_expense_stops_counting_but_stays_for_audit(client, fd):
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    closed = _close(client, profile_id, lines=[_sheet(product_id, 50, 0, 50, 40)],
                    cards=[_card(product_id, (10, 79000))],
                    expenses=[{"description": "Lunch", "amount_ugx": 10000,
                               "payment_method": "cash"}],
                    actual_cash_count_ugx=780000)
    assert closed.status_code == 200, closed.get_json()
    expense_id = closed.get_json()["expenses"][0]["id"]
    assert closed.get_json()["totals"]["cash_expenses_ugx"] == 10000

    # A Manager reopens the closed day and voids the expense with a reason.
    _login(client, fd["manager"])
    reopened = client.post("/api/sales/days/2026-10-30/reopen",
                           json={"sales_profile_id": profile_id, "reason": "Expense was personal"})
    assert reopened.status_code == 200
    voided = client.post(f"/api/sales/void/expense/{expense_id}",
                         json={"reason": "Personal purchase, not a business expense"})
    assert voided.status_code == 200, voided.get_json()

    after = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={profile_id}").get_json()
    assert after["expenses"] == []
    assert after["totals"]["total_expenses_ugx"] == 0
    assert after["totals"]["cash_expenses_ugx"] == 0
    assert after["totals"]["cash_at_hand_ugx"] == 790000
    assert SalesExpense.query.one().status == "void"           # still there, for the audit


def test_a_closed_day_cannot_be_saved_again(client, fd):
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    first = _close(client, profile_id, lines=[_sheet(product_id, 50, 0, 50, 40)],
                   cards=[_card(product_id, (10, 79000))], actual_cash_count_ugx=790000)
    assert first.status_code == 200
    second = _close(client, profile_id, lines=[_sheet(product_id, 50, 0, 50, 30)],
                    cards=[_card(product_id, (20, 79000))], actual_cash_count_ugx=790000)
    assert second.status_code == 400
    assert "already closed" in second.get_json()["error"]
    assert _table_counts()["days"] == 1 and SalesInvoice.query.count() == 1


# ---- A collection may never exceed what is still owed ------------------------
def _credit_day(client, profile_id, product_id, date="2026-10-30", cartons=10, price=79_000):
    """A day whose whole sale is credit, so the debtor owes exactly the sale."""
    closed = _close(client, profile_id, date=date,
                    lines=[_sheet(product_id, 50, 0, 50, 50 - cartons)],
                    cards=[_card(product_id, (cartons, price))],
                    debts=[{"name": "Darius", "amount_ugx": cartons * price}],
                    actual_cash_count_ugx=0)
    assert closed.status_code == 200, closed.get_json()
    return closed


def _balance(client, profile_id, date):
    summary = client.get(f"/api/sales/days/{date}?sales_profile_id={profile_id}").get_json()
    return [row["outstanding_ugx"] for row in summary["debtor_balances"]], summary


def test_a_debtor_payment_cannot_exceed_the_remaining_balance(client, fd):
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id

    # A. 790,000 owed, 400,000 paid on 31 October, so 390,000 remains.
    _credit_day(client, profile_id, product_id)                       # 30 Oct
    first = _close(client, profile_id, date="2026-10-31",
                   lines=[_sheet(product_id, 40, 0, 40, 40)], cards=[],
                   debt_payments=[{"name": "Darius", "amount_ugx": 400_000,
                                   "payment_method": "cash"}],
                   actual_cash_count_ugx=400_000)
    assert first.status_code == 200, first.get_json()
    assert [row["outstanding_ugx"] for row in first.get_json()["debtor_balances"]] == [390_000]

    # B/E. 390,001 in cash and 750,000 by Mobile Money are both refused, and
    # neither leaves a day, a payment, a receipt or any sales behind.
    for method, amount, excess in (("cash", 390_001, "UGX 1"),
                                   ("mobile_money", 750_000, "UGX 360,000")):
        refused = _close(client, profile_id, date="2026-11-02",
                         lines=[_sheet(product_id, 40, 0, 40, 40)], cards=[],
                         debt_payments=[{"name": "Darius", "amount_ugx": amount,
                                         "payment_method": method}],
                         actual_cash_count_ugx=0)
        assert refused.status_code == 400, refused.get_json()
        assert "exceeds the outstanding balance by " + excess in refused.get_json()["error"]
        assert SalesDay.query.filter_by(business_date="2026-11-02").count() == 0
        assert SalesDebtPayment.query.count() == 1            # only the 400,000
        outstanding, summary = _balance(client, profile_id, "2026-11-02")
        assert outstanding == [390_000]                       # nothing was taken off
        assert summary["day"] is None
        assert summary["totals"]["total_sales_ugx"] == 0
        assert summary["totals"]["old_debt_collections_ugx"] == 0
        assert summary["totals"]["new_debt_ugx"] == 0

    # C. Exactly the remaining 390,000 is accepted, and the debt is Paid.
    paid = _close(client, profile_id, date="2026-11-02",
                  lines=[_sheet(product_id, 40, 0, 40, 40)], cards=[],
                  debt_payments=[{"name": "Darius", "amount_ugx": 390_000,
                                  "payment_method": "mobile_money"}],
                  actual_mobile_money_ugx=390_000, actual_cash_count_ugx=400_000)
    assert paid.status_code == 200, paid.get_json()
    assert paid.get_json()["debtor_balances"] == []
    assert paid.get_json()["totals"]["old_debt_mobile_money_ugx"] == 390_000
    assert sales_service.debtor_balance(SalesDebtor.query.one()) == 0
    assert SalesDebtPayment.query.count() == 2

    # D. Nothing is owed any more, so even one shilling is refused.
    again = _close(client, profile_id, date="2026-11-03",
                   lines=[_sheet(product_id, 40, 0, 40, 40)], cards=[],
                   debt_payments=[{"name": "Darius", "amount_ugx": 1,
                                   "payment_method": "cash"}],
                   actual_cash_count_ugx=0)
    assert again.status_code == 400
    assert "exceeds the outstanding balance" in again.get_json()["error"]
    assert SalesDay.query.filter_by(business_date="2026-11-03").count() == 0
    assert SalesDebtPayment.query.count() == 2


def test_a_close_day_with_an_excess_collection_rolls_back_everything(client, fd):
    """F: the whole close is refused and no part of the attempted day survives."""
    _login(client, fd["operator"])
    profile_id, product_id = fd["profile"].id, fd["product"].id
    _credit_day(client, profile_id, product_id)                       # owes 790,000
    before = _table_counts()

    refused = _close(client, profile_id, date="2026-11-02",
                     lines=[_sheet(product_id, 40, 0, 40, 39)],        # 1 carton sold
                     cards=[_card(product_id, (1, 100_000))],
                     expenses=[{"description": "Lunch", "amount_ugx": 10_000,
                                "payment_method": "cash"}],
                     debts=[{"name": "Musa", "amount_ugx": 20_000}],
                     debt_payments=[{"name": "Darius", "amount_ugx": 800_000,
                                     "payment_method": "cash"}],
                     actual_cash_count_ugx=0)
    assert refused.status_code == 400
    assert "exceeds the outstanding balance" in refused.get_json()["error"]

    after = _table_counts()
    assert after == before, (before, after)                # day, line, card, expense, debt
    assert SalesDebtPayment.query.count() == 0
    assert SalesDebtor.query.filter_by(name="Musa").count() == 0
    outstanding, summary = _balance(client, profile_id, "2026-11-02")
    assert outstanding == [790_000]
    assert summary["day"] is None
    assert summary["totals"]["total_sales_ugx"] == 0
    assert summary["expenses"] == [] and summary["debts"] == []
