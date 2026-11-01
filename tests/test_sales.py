"""Sales Department: the Close-Day workflow and every rule around it.

Rewritten for the approved workflow (Option B): an unfinished working day lives
in the browser and nowhere else, Close Day is the single atomic write, and the
older per-piece endpoints are historical-correction paths that refuse a day that
was never closed.

The previous suite was built on the draft-persistence design (create a day row,
save returns, save cards, add expenses, then close).  Those tests were classified
and handled as follows:

  * still-valid business rules (money allocation, debt ageing, reports, scopes,
    permissions, exports): kept, with their setup moved to Close Day;
  * endpoint/API behaviours (stock adjustments, voids, card saves, returns,
    counted cash): kept as *historical correction* tests - they now close a day,
    reopen it as a Manager, and then exercise the endpoint;
  * obsolete draft-persistence tests ("a card save must not persist the sheet",
    "an open day keeps its figures", "counted money is saved separately"):
    replaced by their approved equivalents, which assert that nothing exists
    before Close Day and that one Close Day stores everything (see also
    tests/test_sales_close_day.py).

Tests whose behaviour did not change at all keep their original bodies.
"""
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
    SalesBaseline, SalesDay, SalesDebt, SalesDebtPayment, SalesDebtor, SalesExpense,
    SalesInvoice, SalesOpeningStockLine, SalesProfile, SalesVehicleClosingLine,
)
from webapp.models.section import SECTION_SALES, Section
from webapp.models.user import (
    ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, ROLE_VIEWER,
)
from webapp.services import sales_service, section_access_service


def _grant(user, code=SECTION_SALES):
    section_access_service.ensure_seeded()
    section_access_service.set_sections_for_user(user, [code], user)
    db.session.commit()


def _login(client, user, password="password123", section=SECTION_SALES):
    response = client.post("/api/login", json={"username": user.username,
                                               "password": password, "section": section})
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
    profile = SalesProfile(user_id=operator.id, display_name="Metro Salesperson",
                           recipient_customer_id=recipient.id)
    other_profile = SalesProfile(user_id=other.id, display_name="Other Salesperson")
    db.session.add_all([profile, other_profile]); db.session.flush()
    baseline = SalesBaseline(sales_profile_id=profile.id, effective_date="2026-10-30",
                             opening_cash_ugx=0, opening_mobile_money_ugx=0, created_by=manager.id)
    db.session.add(baseline); db.session.flush()
    db.session.add_all([
        SalesOpeningStockLine(baseline_id=baseline.id, product_id=compact.id,
                              base_unit_qty=50 * 100, packaging_rule_id=compact_rule.id),
        SalesOpeningStockLine(baseline_id=baseline.id, product_id=napkin.id,
                              base_unit_qty=10 * 100, packaging_rule_id=napkin_rule.id),
    ])
    dispatch = Dispatch(dispatch_number="D-1030", date="2026-10-30", shift="Day",
                        customer_id=recipient.id, status=STATUS_FINALIZED, created_by=manager.id)
    db.session.add(dispatch); db.session.flush()
    db.session.add_all([
        DispatchLine(dispatch_id=dispatch.id, product_id=compact.id, cartons=100, packs=0,
                     pieces=0, base_unit_qty=100 * 100, packaging_rule_id=compact_rule.id),
        DispatchLine(dispatch_id=dispatch.id, product_id=napkin.id, cartons=20, packs=0,
                     pieces=0, base_unit_qty=20 * 100, packaging_rule_id=napkin_rule.id),
    ])
    db.session.commit()
    return dict(manager=manager, operator=operator, other=other, profile=profile,
                other_profile=other_profile, customer=customer, compact=compact, napkin=napkin,
                compact_rule=compact_rule, napkin_rule=napkin_rule)


# ---- The working day: browser state until Close Day -------------------------
def _open_day(client, profile_id, date="2026-10-30"):
    """Selecting a date only asks for its context; it never creates anything."""
    response = client.post("/api/sales/days",
                           json={"business_date": date, "sales_profile_id": profile_id})
    assert response.status_code == 200, response.get_json()
    return response


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


def _acceptance_sheet(s):
    """The paper sheet: Compact 50+100=150 back 40; Napkin 10+20=30 back 5."""
    return [_sheet(s["compact"].id, 50, 100, 150, 40),
            _sheet(s["napkin"].id, 10, 20, 30, 5)]


def _acceptance_cards(s):
    return [_card(s["compact"].id, (100, 79_000), (10, 80_000)),
            _card(s["napkin"].id, (20, 103_000), (5, 104_000))]


def _summary(client, profile_id, date="2026-10-30"):
    return client.get(f"/api/sales/days/{date}?sales_profile_id={profile_id}").get_json()


def _stock(summary, product_id):
    return next((row for row in summary["stock"] if row["product_id"] == product_id), None)



# ---- One careful day --------------------------------------------------------
def test_a_full_day_is_stored_by_close_day(client, sales_setup):
    """The paper sheet, all the way through Close Day, with exact totals."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    closed = _close(client, s["profile"].id, lines=_acceptance_sheet(s),
                    cards=_acceptance_cards(s),
                    expenses=[{"description": "Lunch", "amount_ugx": 10_000, "payment_method": "cash"},
                              {"description": "Fahad", "amount_ugx": 20_000, "payment_method": "cash"},
                              {"description": "Breakfast", "amount_ugx": 10_000, "payment_method": "cash"}],
                    actual_cash_count_ugx=11_240_000)
    assert closed.status_code == 200, closed.get_json()
    body = closed.get_json()
    compact, napkin = _stock(body, s["compact"].id), _stock(body, s["napkin"].id)
    assert compact["opening"]["cartons"] == 50
    assert compact["dispatch_loaded"]["cartons"] == 100
    assert compact["available"]["cartons"] == 150
    assert compact["derived_sold"]["cartons"] == 110
    assert compact["recorded_sold"]["cartons"] == 110
    assert compact["reconciled"] is True
    assert napkin["derived_sold"]["cartons"] == 25
    totals = body["totals"]
    assert totals["total_sales_ugx"] == 11_280_000
    assert totals["total_expenses_ugx"] == 40_000
    assert totals["net_sales_after_expenses_ugx"] == 11_240_000
    assert totals["expected_cash_ugx"] == 11_240_000
    assert totals["cash_variance_ugx"] == 0
    assert body["day"]["status"] == "closed"
    assert SalesDay.query.count() == 1
    assert SalesInvoice.query.count() == 2

    # Reopening the date reproduces exactly what Close Day stored.
    assert _summary(client, s["profile"].id)["totals"] == totals


def test_nothing_is_stored_until_close_day(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    assert SalesDay.query.count() == 0
    assert client.patch("/api/sales/days/2026-10-30",
                        json={"sales_profile_id": s["profile"].id,
                              "actual_cash_count_ugx": 5_000}).status_code == 409
    assert client.put("/api/sales/days/2026-10-30/vehicle-stock",
                      json={"sales_profile_id": s["profile"].id,
                            "lines": _acceptance_sheet(s)}).status_code == 409
    assert client.post("/api/sales/expenses",
                       json={"sales_profile_id": s["profile"].id, "business_date": "2026-10-30",
                             "description": "Lunch", "amount_ugx": 10_000,
                             "payment_method": "cash",
                             "idempotency_key": str(uuid4())}).status_code == 409
    assert SalesDay.query.count() == 0
    assert SalesVehicleClosingLine.query.count() == 0
    assert SalesInvoice.query.count() == 0
    assert SalesExpense.query.count() == 0
    summary = _summary(client, s["profile"].id)
    assert summary["stock_warnings"] == [] and summary["invoices"] == []



def test_a_cash_shortage_never_rewrites_the_expected_ledger(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    closed = _close(client, s["profile"].id, lines=_acceptance_sheet(s),
                    cards=_acceptance_cards(s),
                    expenses=[{"description": "Field expenses", "amount_ugx": 40_000,
                               "payment_method": "cash"}],
                    actual_cash_count_ugx=10_000_000)
    assert closed.status_code == 200, closed.get_json()
    totals = closed.get_json()["totals"]
    assert totals["expected_cash_ugx"] == 11_240_000
    assert totals["cash_variance_ugx"] == -1_240_000

    # The shortage is an observation: tomorrow starts from the ledger, not from
    # the number that was counted.
    tomorrow = _summary(client, s["profile"].id, "2026-10-31")
    assert tomorrow["totals"]["expected_cash_ugx"] == 11_240_000
    assert tomorrow["totals"]["actual_cash_ugx"] is None
    first = sales_service.first_divergence(s["profile"], "2026-10-30", "2026-10-31")
    assert first["business_date"] == "2026-10-30"


def test_cash_mobile_and_credit_allocate_the_same_sales(client, sales_setup):
    """Cash + Mobile Money + New Debt = Total Sales, with nothing counted twice."""
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    closed = _close(client, s["profile"].id,
                    lines=[_sheet(s["compact"].id, 50, 100, 150, 110),
                           _sheet(s["napkin"].id, 10, 20, 30, 30)],   # 40 cartons
                    cards=[_card(s["compact"].id, (40, 12_500))],          # 500,000
                    mobile_money_from_sales_ugx=150_000,
                    debts=[{"name": "Darius", "amount_ugx": 100_000}],
                    actual_cash_count_ugx=250_000,
                    actual_mobile_money_ugx=150_000)
    assert closed.status_code == 200, closed.get_json()
    totals = closed.get_json()["totals"]
    assert totals["total_sales_ugx"] == 500_000
    assert totals["mobile_money_from_sales_ugx"] == 150_000
    assert totals["new_debt_ugx"] == 100_000
    assert totals["cash_from_sales_ugx"] == 250_000
    assert (totals["cash_from_sales_ugx"] + totals["mobile_money_from_sales_ugx"]
            + totals["new_debt_ugx"]) == totals["total_sales_ugx"]
    assert totals["cash_at_hand_ugx"] == 250_000
    assert totals["expected_mobile_money_ugx"] == 150_000
    assert totals["cash_variance_ugx"] == 0 and totals["mobile_variance_ugx"] == 0
    # The product is still sold: credit never removes anything from Sold Today.
    assert _stock(closed.get_json(), s["compact"].id)["derived_sold"]["cartons"] == 40


def test_mobile_money_and_credit_cannot_exceed_the_sales_they_explain(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    response = _close(client, s["profile"].id, lines=_acceptance_sheet(s),
                      cards=_acceptance_cards(s),
                      mobile_money_from_sales_ugx=11_000_000,
                      debts=[{"name": "Darius", "amount_ugx": 500_000}],
                      actual_cash_count_ugx=0)
    assert response.status_code == 400
    assert "cannot exceed Total Sales" in response.get_json()["error"]
    assert SalesDay.query.count() == 0


def test_a_handover_moves_money_without_being_an_expense_or_a_sale(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    closed = _close(client, s["profile"].id, lines=_acceptance_sheet(s),
                    cards=_acceptance_cards(s),
                    transfers=[{"from_account": "cash", "to_account": "mobile_money",
                                "amount_ugx": 200_000, "reference": "Handover"}],
                    debts=[{"name": "Darius", "amount_ugx": 1_000_000}],
                    mobile_money_from_sales_ugx=300_000,
                    actual_cash_count_ugx=9_780_000)
    assert closed.status_code == 200, closed.get_json()
    totals = closed.get_json()["totals"]
    assert totals["total_sales_ugx"] == 11_280_000
    assert totals["new_debt_ugx"] == 1_000_000
    assert totals["cash_from_sales_ugx"] == 9_980_000
    assert totals["expected_mobile_money_ugx"] == 500_000
    assert totals["cash_at_hand_ugx"] == 9_780_000     # 9,980,000 − 200,000 handed over
    assert totals["total_expenses_ugx"] == 0



# ---- Debtors: unpaid today, collected on the date the money arrives ---------
def test_a_collection_belongs_to_the_date_it_was_received(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    # 30 Oct: 10 cartons on credit, nothing collected.
    first = _close(client, s["profile"].id,
                   lines=[_sheet(s["compact"].id, 50, 100, 150, 140),
                          _sheet(s["napkin"].id, 10, 20, 30, 30)],
                   cards=[_card(s["compact"].id, (10, 79_000))],
                   debts=[{"name": "Darius", "amount_ugx": 790_000}],
                   actual_cash_count_ugx=0)
    assert first.status_code == 200, first.get_json()
    assert first.get_json()["totals"]["cash_from_sales_ugx"] == 0

    # 31 Oct: 400,000 of it arrives by Mobile Money.  No product sold today.
    second = _close(client, s["profile"].id, date="2026-10-31",
                    lines=[_sheet(s["compact"].id, 140, 0, 140, 140),
                           _sheet(s["napkin"].id, 30, 0, 30, 30)],
                    cards=[],
                    debt_payments=[{"name": "Darius", "amount_ugx": 400_000,
                                    "payment_method": "mobile_money"}],
                    actual_mobile_money_ugx=400_000,
                    actual_cash_count_ugx=0)
    assert second.status_code == 200, second.get_json()
    totals = second.get_json()["totals"]
    assert totals["total_sales_ugx"] == 0
    assert totals["old_debt_mobile_money_ugx"] == 400_000
    assert totals["mobile_money_expenses_ugx"] == 0
    assert totals["expected_mobile_money_ugx"] == 400_000
    assert [row["outstanding_ugx"] for row in second.get_json()["debtor_balances"]] == [390_000]

    # The receipt date is 31 October; the debt keeps its own 30 October date.
    payment = SalesDebtPayment.query.one()
    assert payment.payment_date == "2026-10-31"
    assert SalesDebt.query.one().business_date == "2026-10-30"
    assert sales_service.daily_summary(s["profile"], "2026-10-30")["totals"]["total_sales_ugx"] == 790_000


def test_debtor_names_are_case_insensitive_memory_and_never_customers(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    customers_before = Customer.query.count()
    first = _close(client, s["profile"].id,
                   lines=[_sheet(s["compact"].id, 50, 100, 150, 147),
                          _sheet(s["napkin"].id, 10, 20, 30, 30)],
                   cards=[_card(s["compact"].id, (3, 100_000))],
                   debts=[{"name": "Darius", "amount_ugx": 100_000}],
                   actual_cash_count_ugx=200_000)
    assert first.status_code == 200, first.get_json()
    second = _close(client, s["profile"].id, date="2026-10-31",
                    lines=[_sheet(s["compact"].id, 147, 0, 147, 146),
                           _sheet(s["napkin"].id, 30, 0, 30, 30)],
                    cards=[_card(s["compact"].id, (1, 50_000))],
                    debts=[{"name": "  darius  ", "amount_ugx": 50_000}],
                    actual_cash_count_ugx=0)
    assert second.status_code == 200, second.get_json()
    third = _close(client, s["profile"].id, date="2026-11-01",
                   lines=[_sheet(s["compact"].id, 146, 0, 146, 145),
                          _sheet(s["napkin"].id, 30, 0, 30, 30)],
                   cards=[_card(s["compact"].id, (1, 10_000))],
                   debts=[{"name": "DARIUS", "amount_ugx": 10_000}],
                   actual_cash_count_ugx=0)
    assert third.status_code == 200, third.get_json()
    # One debtor identity, three debts, never a Finished Goods customer.
    assert SalesDebtor.query.count() == 1
    assert SalesDebt.query.count() == 3
    assert Customer.query.count() == customers_before
    names = sales_service.debtor_names([s["profile"].id])
    assert [name.casefold() for name in names] == ["darius"]


def test_a_paid_card_keeps_each_price_line_without_a_customer(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    customers_before = Customer.query.count()
    _open_day(client, s["profile"].id)
    closed = _close(client, s["profile"].id, lines=_acceptance_sheet(s),
                    cards=_acceptance_cards(s), actual_cash_count_ugx=11_280_000)
    assert closed.status_code == 200, closed.get_json()
    assert len(closed.get_json()["invoices"]) == 2
    assert {row["lines"][0]["unit_price_ugx"] for row in closed.get_json()["invoices"]} == {79_000, 103_000}
    assert all(row["customer_id"] is None for row in closed.get_json()["invoices"])
    # Ordinary sales never create Finished Goods customers and never need one.
    assert Customer.query.count() == customers_before



# ---- Vehicle stock and the starting count -----------------------------------
def test_the_starting_count_carries_forward_to_the_next_day(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    first = _close(client, s["profile"].id, lines=_acceptance_sheet(s),
                   cards=_acceptance_cards(s), actual_cash_count_ugx=11_280_000)
    assert first.status_code == 200, first.get_json()
    tomorrow = _summary(client, s["profile"].id, "2026-10-31")
    compact = _stock(tomorrow, s["compact"].id)
    # Yesterday's counted return is today's Previous Return; nothing is retyped.
    assert compact["previous_return"]["cartons"] == 40
    assert compact["previous_return_default"]["cartons"] == 40
    assert compact["return_counted"] is False
    assert compact["derived_sold"] is None
    assert compact["opening_source"] == "previous_closing"


def test_a_first_day_needs_a_starting_count_and_it_is_the_salespersons_own(client, sales_setup):
    """No Manager opens the day: whoever holds the stock declares it at Close Day."""
    s = sales_setup; _login(client, s["other"])
    # Nothing has ever been counted for this profile, so zero is never assumed.
    refused = _close(client, s["other_profile"].id,
                     lines=[_sheet(s["compact"].id, None, 0, 0, 0)],
                     cards=[], actual_cash_count_ugx=0)
    assert refused.status_code == 400
    assert "starting stock" in refused.get_json()["error"].lower()
    assert SalesDay.query.count() == 0

    declared = _close(client, s["other_profile"].id,
                      lines=[_sheet(s["compact"].id, 5, 0, 5, 0)],
                      cards=[_card(s["compact"].id, (5, 70_000))],
                      actual_cash_count_ugx=350_000)
    assert declared.status_code == 200, declared.get_json()
    stock = _stock(declared.get_json(), s["compact"].id)
    assert stock["previous_return"]["cartons"] == 5
    assert stock["derived_sold"]["cartons"] == 5
    assert declared.get_json()["day"]["status"] == "closed"


def test_return_today_cannot_exceed_total_stock(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    refused = _close(client, s["profile"].id,
                     lines=[_sheet(s["compact"].id, 50, 100, 150, 151)],
                     cards=[], actual_cash_count_ugx=0)
    assert refused.status_code == 400
    assert "cannot exceed Total Stock" in refused.get_json()["error"]
    assert SalesDay.query.count() == 0


def test_the_vehicle_list_is_the_catalogue_and_only_shows_this_vehicle(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    unrelated = Product(name="Unrelated Widget", display_order=9, active=True)
    db.session.add(unrelated); db.session.commit()
    summary = _summary(client, s["profile"].id)
    ids = {row["product_id"] for row in summary["stock"]}
    assert {s["compact"].id, s["napkin"].id} <= ids
    assert unrelated.id not in ids
    options = client.get(f"/api/sales/days/2026-10-30/vehicle-stock/options"
                         f"?sales_profile_id={s['profile'].id}").get_json()
    names = {product["name"] for product in options["products"]}
    assert {"Compact Standard", "Napkin Standard"} <= names
    # A product without a packaging rule cannot be counted in units, so it is not
    # offered; nothing is hard-coded in JavaScript.
    assert "Unrelated Widget" not in names


# ---- Sales Card quantities are carton-equivalents ---------------------------
def _packing_product(name, packs_per_carton, pieces_per_pack, order=9):
    product = Product(name=name, display_order=order, active=True)
    db.session.add(product); db.session.flush()
    rule = PackagingRule(product_id=product.id, cartons_to_packs=packs_per_carton,
                         packs_to_pieces=pieces_per_pack, effective_from=datetime(2026, 1, 1))
    db.session.add(rule); db.session.commit()
    return product, rule


def _untouched_lines(s):
    """The fixture's own products, nothing sold, so only the new card is tested."""
    return [_sheet(s["compact"].id, 50, 100, 150, 150),
            _sheet(s["napkin"].id, 10, 20, 30, 30)]



def test_half_a_carton_is_five_packs_on_a_ten_pack_carton(client, sales_setup):
    """10 packs of 6: 0.5 on a card is 5 packs, never half a pack."""
    s = sales_setup; _login(client, s["operator"])
    product, rule = _packing_product("Ten Pack", 10, 6)          # 60 pieces a carton
    assert rule.pieces_per_carton == 60
    # The van counted this product in packs and sold 5 of them.
    closed = _close(client, s["profile"].id,
                    lines=_untouched_lines(s) + [_sheet(product.id, 100, 0, 100, 95, unit="pack")],
                    cards=[_card(product.id, (0.5, 90_000))],
                    actual_cash_count_ugx=45_000)
    assert closed.status_code == 200, closed.get_json()
    rows = next(card for card in closed.get_json()["product_cards"]
                if card["product_id"] == product.id)["rows"]
    assert [(row["quantity"], row["unit"]) for row in rows] == [(0.5, "carton")]
    assert sales_service.quantity_dict(Decimal("30"), rule)["packs"] == 5
    assert sales_service.sales_row_base(Decimal("0.5"), "pack", rule) == Decimal("3")
    assert closed.get_json()["totals"]["total_sales_ugx"] == 45_000


def test_half_a_carton_is_six_packs_on_a_twelve_pack_carton(client, sales_setup):
    """12 packs of 5: the same 0.5 is 6 packs, still exactly half a carton."""
    s = sales_setup; _login(client, s["operator"])
    product, rule = _packing_product("Twelve Pack", 12, 5)       # 60 pieces a carton
    assert rule.pieces_per_carton == 60
    closed = _close(client, s["profile"].id,
                    lines=_untouched_lines(s) + [_sheet(product.id, 100, 0, 100, 94, unit="pack")],
                    cards=[_card(product.id, (0.5, 90_000))],
                    actual_cash_count_ugx=45_000)
    assert closed.status_code == 200, closed.get_json()
    assert sales_service.quantity_dict(Decimal("30"), rule)["packs"] == 6
    assert sales_service.sales_row_base(Decimal("0.5"), "pack", rule) == Decimal("2.5")


def test_half_a_carton_does_not_follow_the_vehicle_stock_unit(client, sales_setup):
    """Vehicle stock counted in pieces: 0.5 is still half a carton, not half a piece."""
    s = sales_setup; _login(client, s["operator"])
    product, rule = _packing_product("Twelve Pack", 12, 5)
    closed = _close(client, s["profile"].id,
                    # Counted in pieces: 600 − 570 = 30 pieces sold.
                    lines=_untouched_lines(s) + [_sheet(product.id, 600, 0, 600, 570, unit="piece")],
                    cards=[_card(product.id, (0.5, 90_000))],
                    actual_cash_count_ugx=45_000)
    assert closed.status_code == 200, closed.get_json()
    stock = _stock(closed.get_json(), product.id)
    assert stock["derived_sold"]["base_unit_qty"] == 30
    assert stock["variance"]["base_unit_qty"] == 0
    # Had the card followed "Counted in: pieces" it would have priced 0.5.
    assert sales_service.sales_row_base(Decimal("0.5"), "piece", rule) == Decimal("0.5")


def test_quarter_and_one_and_a_half_cartons_convert_exactly(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    ten, ten_rule = _packing_product("Ten Pack", 10, 6, order=9)
    twelve, twelve_rule = _packing_product("Twelve Pack", 12, 5, order=10)
    for rule in (ten_rule, twelve_rule):
        assert sales_service.sales_row_base(Decimal("0.25"), "carton", rule) == Decimal("15")
        assert sales_service.sales_row_base(Decimal("1.5"), "carton", rule) == Decimal("90")
    closed = _close(client, s["profile"].id,
                    lines=_untouched_lines(s) + [_sheet(ten.id, 600, 0, 600, 495, unit="piece"),
                                                 _sheet(twelve.id, 600, 0, 600, 495, unit="piece")],
                    cards=[_card(ten.id, (0.25, 100_000), (1.5, 100_000)),
                           _card(twelve.id, (0.25, 100_000), (1.5, 100_000))],
                    actual_cash_count_ugx=350_000)
    assert closed.status_code == 200, closed.get_json()
    for product in (ten, twelve):
        rows = next(card for card in closed.get_json()["product_cards"]
                    if card["product_id"] == product.id)["rows"]
        assert [row["quantity"] for row in rows] == [0.25, 1.5]
    assert sales_service.quantity_dict(Decimal("90"), ten_rule)["label"] == "1 carton 5 packs"
    assert sales_service.quantity_dict(Decimal("90"), twelve_rule)["label"] == "1 carton 6 packs"
    assert closed.get_json()["totals"]["total_sales_ugx"] == 350_000



# ---- Historical corrections stay deliberate and audited --------------------
def _closed_standard_day(client, s, cash=11_280_000, **extra):
    response = _close(client, s["profile"].id, lines=_acceptance_sheet(s),
                      cards=_acceptance_cards(s), actual_cash_count_ugx=cash, **extra)
    assert response.status_code == 200, response.get_json()
    return response


def test_a_closed_day_refuses_every_separate_write_until_a_manager_reopens_it(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _closed_standard_day(client, s)
    returns = client.put("/api/sales/days/2026-10-30/returns",
                         json={"sales_profile_id": s["profile"].id,
                               "lines": [{"product_id": s["compact"].id, "quantity": 39,
                                          "unit": "carton"}]})
    assert returns.status_code == 400 and "reopened" in returns.get_json()["error"]
    card = client.put(f"/api/sales/days/2026-10-30/products/{s['compact'].id}/sales",
                      json={"sales_profile_id": s["profile"].id,
                            "rows": [{"quantity": 111, "unit": "carton", "unit_price_ugx": 79_000}]})
    assert card.status_code == 400 and "reopened" in card.get_json()["error"]
    expense = client.post("/api/sales/expenses",
                          json={"sales_profile_id": s["profile"].id,
                                "business_date": "2026-10-30", "description": "Lunch",
                                "amount_ugx": 1_000, "payment_method": "cash",
                                "idempotency_key": str(uuid4())})
    assert expense.status_code == 400
    # Reopening is a Manager action with a recorded reason.
    assert client.post("/api/sales/days/2026-10-30/reopen",
                       json={"sales_profile_id": s["profile"].id,
                             "reason": "Count was wrong"}).status_code == 403
    _login(client, s["manager"])
    reopened = client.post("/api/sales/days/2026-10-30/reopen",
                           json={"sales_profile_id": s["profile"].id,
                                 "reason": "Count was wrong"})
    assert reopened.status_code == 200
    assert reopened.get_json()["day"]["status"] == "reopened"
    corrected = client.put("/api/sales/days/2026-10-30/returns",
                           json={"sales_profile_id": s["profile"].id,
                                 "lines": [{"product_id": s["compact"].id, "quantity": 39,
                                            "unit": "carton"}]})
    assert corrected.status_code == 200, corrected.get_json()
    assert _stock(corrected.get_json(), s["compact"].id)["closing"]["cartons"] == 39
    # The correction is audited with who, what and why.
    audit = AuditLog.query.filter_by(entity_type="sales_day", action="reopen").one()
    assert audit is not None


def test_an_operator_cannot_void_another_profiles_expense(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    closed = _closed_standard_day(
        client, s, expenses=[{"description": "Lunch", "amount_ugx": 1_000, "payment_method": "cash"}])
    expense_id = closed.get_json()["expenses"][0]["id"]

    _login(client, s["other"])
    denied = client.post(f"/api/sales/void/expense/{expense_id}", json={"reason": "mine now"})
    assert denied.status_code in (403, 404, 400)

    _login(client, s["manager"])
    client.post("/api/sales/days/2026-10-30/reopen",
                json={"sales_profile_id": s["profile"].id, "reason": "Expense was personal"})
    voided = client.post(f"/api/sales/void/expense/{expense_id}", json={"reason": "personal"})
    assert voided.status_code == 200, voided.get_json()
    assert SalesExpense.query.one().status == "void"


def test_a_reopened_day_can_take_a_card_correction_without_rewriting_history(client, sales_setup):
    s = sales_setup; _login(client, s["manager"])
    _closed_standard_day(client, s)
    client.post("/api/sales/days/2026-10-30/reopen",
                json={"sales_profile_id": s["profile"].id, "reason": "Price was mistyped"})
    corrected = client.put(f"/api/sales/days/2026-10-30/products/{s['compact'].id}/sales",
                           json={"sales_profile_id": s["profile"].id,
                                 "rows": [{"quantity": 110, "unit": "carton",
                                           "unit_price_ugx": 79_000}]})
    assert corrected.status_code == 200, corrected.get_json()
    rows = next(card for card in corrected.get_json()["product_cards"]
                if card["product_id"] == s["compact"].id)["rows"]
    assert [row["quantity"] for row in rows] == [110]
    # The old card is voided, not erased, and the totals follow the new one.
    assert SalesInvoice.query.filter_by(status="void").count() == 1
    assert corrected.get_json()["totals"]["total_sales_ugx"] == (110 * 79_000) + (20 * 103_000) + (5 * 104_000)



# ---- Access, scope and reports (behaviour that did not change) --------------
def test_sales_section_and_profile_idor_are_enforced(client, sales_setup):
    s = sales_setup
    # A Finished-Goods login cannot call the Sales blueprint.
    manager = s["manager"]
    section_access_service.set_sections_for_user(manager, ["finished_goods", "sales"], manager)
    db.session.commit()
    _login(client, manager, section="finished_goods")
    assert client.get("/api/sales/metadata").status_code == 403

    _login(client, s["operator"])
    assert client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['other_profile'].id}").status_code == 403
    assert client.post("/api/sales/days", json={"business_date": "2026-10-30",
                                                "sales_profile_id": s["other_profile"].id}).status_code == 403
    assert client.get("/api/dispatches").status_code == 403


def test_sales_seed_does_not_auto_grant_ordinary_users(app, make_user):
    ordinary = make_user("no-sales-grant", "password123", ROLE_OPERATOR)
    section_access_service.ensure_seeded(); db.session.flush()
    sales = Section.query.filter_by(code=SECTION_SALES).one()
    assert section_access_service.user_has_section_access(ordinary, sales) is False
    super_admin = make_user("global-admin", "password123", ROLE_SUPER_ADMIN)
    assert section_access_service.user_has_section_access(super_admin, sales) is True


def test_operator_without_a_sales_profile_gets_a_human_message(client, make_user):
    user = make_user("no-sales-profile", "password123", ROLE_OPERATOR)
    _grant(user); _login(client, user)
    assert client.get("/api/sales/metadata").status_code == 200
    response = client.get("/api/sales/days/2026-10-30")
    assert response.status_code == 400
    assert "not been set up" in response.get_json()["error"]
    assert "sales_profile_id" not in response.get_json()["error"]


def test_viewer_cannot_write_stock_money_or_cards(client, sales_setup, make_user):
    s = sales_setup
    viewer = make_user("sales-viewer", "password123", ROLE_VIEWER)
    _grant(viewer)
    _login(client, viewer)
    assert client.put("/api/sales/days/2026-10-30/returns",
                      json={"sales_profile_id": s["profile"].id,
                            "lines": [{"product_id": s["compact"].id, "quantity": 1,
                                       "unit": "carton"}]}).status_code == 403
    assert client.put("/api/sales/days/2026-10-30/opening-stock",
                      json={"sales_profile_id": s["profile"].id,
                            "lines": [{"product_id": s["compact"].id, "quantity": 1,
                                       "unit": "carton"}]}).status_code == 403
    assert client.put(f"/api/sales/days/2026-10-30/products/{s['compact'].id}/sales",
                      json={"sales_profile_id": s["profile"].id,
                            "rows": [{"quantity": 1, "unit": "carton",
                                      "unit_price_ugx": 1_000}]}).status_code == 403
    assert _close(client, s["profile"].id, lines=_acceptance_sheet(s)).status_code == 403
    assert client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['profile'].id}").status_code == 200



def test_card_cannot_be_saved_on_another_salespersons_profile(client, sales_setup):
    s = sales_setup
    _login(client, s["other"])
    _open_day(client, s["other_profile"].id)
    response = client.put(f"/api/sales/days/2026-10-30/products/{s['compact'].id}/sales",
                          json={"sales_profile_id": s["profile"].id,
                                "rows": [{"quantity": 1, "unit": "carton",
                                          "unit_price_ugx": 1_000}]})
    assert response.status_code in (403, 404)
    assert client.get(f"/api/sales/days/2026-10-30?sales_profile_id={s['profile'].id}").status_code in (403, 404)


def _add_straws(s, name="Straws"):
    """A product nothing has carried yet: it can only be chosen by hand."""
    product = Product(name=name, display_order=3, active=True)
    db.session.add(product); db.session.flush()
    db.session.add(PackagingRule(product_id=product.id, cartons_to_packs=10, packs_to_pieces=10,
                                 effective_from=datetime(2026, 1, 1), created_by=s["manager"].id))
    db.session.commit()
    return product


def test_the_product_picker_is_the_catalogue(client, sales_setup):
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

    body = client.get(f"/api/sales/days/2026-10-30/vehicle-stock/options"
                      f"?sales_profile_id={s['profile'].id}").get_json()
    assert [row["name"] for row in body["products"]] == ["Compact Standard", "Napkin Standard", "Straws"]
    straw = next(row for row in body["products"] if row["id"] == straws.id)
    assert straw["on_vehicle"] is False
    assert straw["previous_return"]["cartons"] == 0
    assert straw["issued_today"]["cartons"] == 0
    compact = next(row for row in body["products"] if row["id"] == s["compact"].id)
    assert compact["on_vehicle"] is True
    assert compact["previous_return"]["cartons"] == 50
    assert compact["issued_today"]["cartons"] == 100


def test_dispatch_void_baseline_required_and_exports(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _open_day(client, s["profile"].id)
    compact = _stock(sales_service.daily_summary(s["profile"], "2026-10-30"), s["compact"].id)
    assert compact["dispatch_loaded"]["cartons"] == 100
    dispatch = Dispatch.query.filter_by(dispatch_number="D-1030").one()
    dispatch.status = "void"; db.session.commit()
    compact = _stock(sales_service.daily_summary(s["profile"], "2026-10-30"), s["compact"].id)
    assert compact["dispatch_loaded"]["cartons"] == 0   # derived live, never copied into Sales

    # The unrelated salesperson has no trusted start and is never assumed to be zero.
    other_summary = sales_service.daily_summary(s["other_profile"], "2026-10-30")
    assert other_summary["baseline_required"] is True
    assert all(row["opening_source"] == "required" for row in other_summary["stock"])

    for fmt in ("csv", "xlsx", "pdf"):
        response = client.get(f"/api/sales/reports/daily.{fmt}?business_date=2026-10-30"
                              f"&sales_profile_id={s['profile'].id}")
        assert response.status_code == 200
        assert response.data
        response = client.get(f"/api/sales/reports/reconciliation.{fmt}?date_from=2026-10-30"
                              f"&date_to=2026-10-30&sales_profile_id={s['profile'].id}")
        assert response.status_code == 200
        assert response.data


def test_reports_are_scoped_and_audited(client, sales_setup):
    s = sales_setup; _login(client, s["operator"])
    _closed_standard_day(
        client, s,
        expenses=[{"description": "Lunch", "amount_ugx": 1_000, "payment_method": "cash"}],
        debts=[{"name": "Darius", "amount_ugx": 500_000}],
        cash=10_780_000)
    query = f"date_from=2026-10-30&date_to=2026-10-30&sales_profile_id={s['profile'].id}"
    expenses_csv = client.get(f"/api/sales/reports/expenses.csv?{query}")
    assert expenses_csv.status_code == 200
    assert "Lunch" in expenses_csv.data.decode()
    debtors_csv = client.get(f"/api/sales/reports/debtors.csv?sales_profile_id={s['profile'].id}")
    assert debtors_csv.status_code == 200
    assert "Darius" in debtors_csv.data.decode()
    payments_csv = client.get(f"/api/sales/reports/payments.csv?{query}")
    assert payments_csv.status_code == 200
    assert AuditLog.query.filter_by(action="export").count() >= 1

    # Another salesperson's exports never contain this profile's rows.
    _login(client, s["other"])
    foreign = client.get(f"/api/sales/reports/expenses.csv?{query}")
    assert foreign.status_code in (200, 403)
    if foreign.status_code == 200:
        assert "Lunch" not in foreign.data.decode()
