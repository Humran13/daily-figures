"""The Sales Person role: automatic FULL access to the Sales section, and
nothing outside it.

Sales Person (canonical role key: ``salesperson``) is a deliberate exception
to the normal rule that a non-Super-Admin account needs an explicit
per-section grant (webapp/services/section_access_service.py's
user_has_section_access()) — but that exception is scoped to the Sales
section alone. Inside Sales, it carries the same authority as a Manager
(webapp/routes/sales.py's ELEVATED/_operational_writer()), both of which are
local to the Sales blueprint and never consulted anywhere else, so this can
never leak into Finished Goods, Spare Parts or Admin.
"""
from datetime import datetime

import pytest

from webapp.extensions import db
from webapp.models.audit_log import AuditLog
from webapp.models.customer import CATEGORY_SALESPERSON, Customer
from webapp.models.packaging_rule import PackagingRule
from webapp.models.product import Product
from webapp.models.sales import SalesBaseline, SalesDay, SalesOpeningStockLine, SalesProfile
from webapp.models.section import SECTION_FINISHED_GOODS, SECTION_SALES
from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SALESPERSON, ROLE_VIEWER, User
from webapp.services import section_access_service


def _login(client, username, password="password123", section=SECTION_SALES):
    return client.post("/api/login", json={"username": username, "password": password, "section": section})


@pytest.fixture
def sp(app, make_user):
    """One Sales Person account with NO explicit section grant, a Manager,
    an Operator who owns a Sales Profile, one product and a starting count —
    everything a Sales Person needs to exercise the complete Sales workflow
    on an existing vehicle/profile, exactly like a Manager would."""
    manager = make_user("sp-manager", "password123", ROLE_MANAGER)
    operator = make_user("sp-field", "password123", ROLE_OPERATOR)
    salesperson = make_user("sp-person", "password123", ROLE_SALESPERSON)
    # Deliberately no section_access_service grant for `salesperson` — the
    # whole point under test is that the role alone is enough.
    section_access_service.ensure_seeded()
    section_access_service.set_sections_for_user(operator, [SECTION_SALES], manager)
    db.session.commit()

    recipient = Customer(name="SP Dispatch", category=CATEGORY_SALESPERSON, active=True)
    product = Product(name="Compact", display_order=1, active=True)
    db.session.add_all([recipient, product])
    db.session.flush()
    rule = PackagingRule(product_id=product.id, cartons_to_packs=10, packs_to_pieces=10,
                         effective_from=datetime(2026, 1, 1), created_by=manager.id)
    db.session.add(rule)
    db.session.flush()
    profile = SalesProfile(user_id=operator.id, display_name="SP Sales", recipient_customer_id=recipient.id)
    db.session.add(profile)
    db.session.flush()
    baseline = SalesBaseline(sales_profile_id=profile.id, effective_date="2026-10-29",
                             opening_cash_ugx=0, opening_mobile_money_ugx=0, created_by=manager.id)
    db.session.add(baseline)
    db.session.flush()
    db.session.add(SalesOpeningStockLine(baseline_id=baseline.id, product_id=product.id,
                                         base_unit_qty=5000, packaging_rule_id=rule.id))
    db.session.commit()
    return dict(manager=manager, operator=operator, salesperson=salesperson, profile=profile,
               product=product, rule=rule, recipient=recipient)


def _sheet(product_id, previous, issued, total, returned, unit="carton"):
    return {"product_id": product_id, "unit": unit, "previous_qty": previous,
            "issued_qty": issued, "total_qty": total, "return_qty": returned}


def _card(product_id, *rows):
    return {"product_id": product_id,
            "rows": [{"quantity": qty, "unit": "carton", "unit_price_ugx": price} for qty, price in rows]}


def _save(client, profile_id, date, lines=None, cards=None, **extra):
    body = {"sales_profile_id": profile_id, "vehicle_stock": lines or [], "sales_cards": cards or []}
    body.update(extra)
    return client.post(f"/api/sales/days/{date}/save", json=body)


def _close(client, profile_id, date, lines=None, cards=None, **extra):
    body = {"sales_profile_id": profile_id, "vehicle_stock": lines or [], "sales_cards": cards or []}
    body.update(extra)
    return client.post(f"/api/sales/days/{date}/close", json=body)


# ---------------------------------------------------------------------------
# Admin role assignment
# ---------------------------------------------------------------------------

def test_salesperson_is_a_valid_role_choice(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/users", json={
        "username": "newsp", "password": "password123", "role": "salesperson",
    })
    assert res.status_code == 201, res.get_json()
    assert res.get_json()["role"] == "salesperson"


def test_role_persists_as_canonical_key_after_reload(client, login_as):
    login_as("root", "password123", "super_admin")
    created = client.post("/api/admin/users", json={
        "username": "reloadsp", "password": "password123", "role": "salesperson",
    }).get_json()
    reloaded = next(u for u in client.get("/api/admin/users").get_json() if u["id"] == created["id"])
    assert reloaded["role"] == "salesperson"


def test_admin_can_edit_an_existing_user_to_salesperson(client, login_as):
    login_as("root", "password123", "super_admin")
    created = client.post("/api/admin/users", json={
        "username": "promote", "password": "password123", "role": "viewer",
    }).get_json()
    res = client.patch(f"/api/admin/users/{created['id']}", json={"role": "salesperson"})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["role"] == "salesperson"
    reloaded = next(u for u in client.get("/api/admin/users").get_json() if u["id"] == created["id"])
    assert reloaded["role"] == "salesperson"


def test_invalid_role_is_still_rejected(client, login_as):
    login_as("root", "password123", "super_admin")
    res = client.post("/api/admin/users", json={
        "username": "badrole", "password": "password123", "role": "sales_manager_super",
    })
    assert res.status_code == 400


def test_non_super_admin_cannot_assign_salesperson_role(client, login_as):
    login_as("mgr", "password123", "manager")
    res = client.post("/api/admin/users", json={
        "username": "x", "password": "password123", "role": "salesperson",
    })
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Automatic Sales-section access, with NO explicit section grant
# ---------------------------------------------------------------------------

def test_salesperson_logs_into_sales_with_no_explicit_grant(client, sp):
    res = _login(client, "sp-person")
    assert res.status_code == 200, res.get_json()
    assert client.get("/api/session").get_json()["active_section"] == "sales"


def test_salesperson_has_no_section_access_rows_at_all(app, sp):
    with app.app_context():
        from webapp.models.section import UserSectionAccess
        grants = UserSectionAccess.query.filter_by(user_id=sp["salesperson"].id).count()
        assert grants == 0


def test_sales_page_returns_200(client, sp):
    _login(client, "sp-person")
    assert client.get("/sales.html").status_code == 200


def test_sales_metadata_returns_200_and_full_capability_flags(client, sp):
    _login(client, "sp-person")
    res = client.get("/api/sales/metadata")
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["role"] == "salesperson"
    assert body["can_manage"] is True
    assert body["can_view_reconciliation"] is True


def test_daily_date_endpoint_returns_200(client, sp):
    _login(client, "sp-person")
    res = client.get(f"/api/sales/days/2026-10-30?sales_profile_id={sp['profile'].id}")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["day"] is None


def test_save_day_authorization_passes_and_persists(client, sp):
    _login(client, "sp-person")
    saved = _save(client, sp["profile"].id, "2026-10-30",
                  lines=[_sheet(sp["product"].id, 50, 0, 50, 45)],
                  cards=[_card(sp["product"].id, (5, 10_000))])
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["day"]["status"] == "draft"


def test_close_day_authorization_passes_and_closes(client, sp):
    _login(client, "sp-person")
    closed = _close(client, sp["profile"].id, "2026-10-30",
                    lines=[_sheet(sp["product"].id, 50, 0, 50, 45)],
                    cards=[_card(sp["product"].id, (5, 10_000))],
                    actual_cash_count_ugx=50_000)
    assert closed.status_code == 200, closed.get_json()
    assert closed.get_json()["day"]["status"] == "closed"


def test_debtors_access_passes(client, sp):
    _login(client, "sp-person")
    res = client.get(f"/api/sales/debtors?sales_profile_id={sp['profile'].id}")
    assert res.status_code == 200, res.get_json()


def test_add_unpaid_sale_and_collection_authorization_passes(client, sp):
    # An unpaid sale and a later collection travel inside the atomic
    # Save/Close payload (webapp/services/sales_service.py's
    # replace_day_debts()/replace_day_debt_payments()) — the standalone
    # /debts and /debtors/<id>/payments endpoints are correction-only
    # (_require_editable_day() only accepts a REOPENED day), exactly like
    # the rest of this role's workflow coverage below.
    _login(client, "sp-person")
    day1 = _save(client, sp["profile"].id, "2026-11-01",
                lines=[_sheet(sp["product"].id, 50, 0, 50, 45)],
                cards=[_card(sp["product"].id, (5, 10_000))],
                debts=[{"name": "Kato", "amount_ugx": 5_000}])
    assert day1.status_code == 200, day1.get_json()
    assert day1.get_json()["debts"][0]["name"] == "Kato"

    day2 = _save(client, sp["profile"].id, "2026-11-02",
                lines=[_sheet(sp["product"].id, 50, 0, 50, 48)],
                debt_payments=[{"name": "Kato", "amount_ugx": 5_000, "payment_method": "cash"}])
    assert day2.status_code == 200, day2.get_json()
    assert day2.get_json()["debt_payments"][0]["amount_ugx"] == 5_000


def test_reconciliation_history_access_passes(client, sp):
    _login(client, "sp-person")
    res = client.get("/api/sales/history?date_from=2026-10-01&date_to=2026-10-31")
    assert res.status_code == 200, res.get_json()


def test_sales_settings_baseline_authorization_passes(client, sp):
    _login(client, "sp-person")
    res = client.post("/api/sales/baselines", json={
        "sales_profile_id": sp["profile"].id, "effective_date": "2026-11-15",
        "opening_cash_ugx": 0, "opening_mobile_money_ugx": 0,
    })
    assert res.status_code == 200, res.get_json()


def test_sales_reports_export_access_passes(client, sp):
    _login(client, "sp-person")
    _close(client, sp["profile"].id, "2026-10-30",
          lines=[_sheet(sp["product"].id, 50, 0, 50, 45)],
          cards=[_card(sp["product"].id, (5, 10_000))], actual_cash_count_ugx=50_000)
    res = client.get(f"/api/sales/reports/daily.csv?sales_profile_id={sp['profile'].id}&business_date=2026-10-30")
    assert res.status_code == 200
    res2 = client.get("/api/sales/reports/reconciliation.csv?date_from=2026-10-01&date_to=2026-10-31")
    assert res2.status_code == 200


def test_reopen_closed_day_authorization_passes_with_audit(client, sp):
    _login(client, "sp-person")
    _close(client, sp["profile"].id, "2026-10-30",
          lines=[_sheet(sp["product"].id, 50, 0, 50, 45)],
          cards=[_card(sp["product"].id, (5, 10_000))], actual_cash_count_ugx=50_000)
    # Audit is still required: an empty reason must still be rejected.
    missing_reason = client.post("/api/sales/days/2026-10-30/reopen",
                                 json={"sales_profile_id": sp["profile"].id})
    assert missing_reason.status_code == 400

    reopened = client.post("/api/sales/days/2026-10-30/reopen",
                           json={"sales_profile_id": sp["profile"].id, "reason": "Price correction needed"})
    assert reopened.status_code == 200, reopened.get_json()

    day = SalesDay.query.filter_by(sales_profile_id=sp["profile"].id, business_date="2026-10-30").first()
    audited = AuditLog.query.filter_by(action="reopen", entity_type="sales_day", entity_id=day.id).first()
    assert audited is not None
    assert "Price correction needed" in (audited.after_json or "")


def test_stock_and_cash_adjustment_authorization_passes_with_required_reason(client, sp):
    # Stock/cash adjustments are historical corrections: _require_editable_day()
    # only accepts a day that has been closed and then reopened, so this
    # closes 2026-11-02 first and reopens it before adjusting it.
    _login(client, "sp-person")
    closed = _close(client, sp["profile"].id, "2026-11-02",
                    lines=[_sheet(sp["product"].id, 50, 0, 50, 45)],
                    cards=[_card(sp["product"].id, (5, 10_000))], actual_cash_count_ugx=50_000)
    assert closed.status_code == 200, closed.get_json()
    reopened = client.post("/api/sales/days/2026-11-02/reopen",
                           json={"sales_profile_id": sp["profile"].id, "reason": "Recount needed"})
    assert reopened.status_code == 200, reopened.get_json()

    missing_reason = client.post("/api/sales/stock-adjustments", json={
        "sales_profile_id": sp["profile"].id, "business_date": "2026-11-02",
        "product_id": sp["product"].id, "quantity": 1, "unit": "carton", "direction": "add",
    })
    assert missing_reason.status_code == 400
    assert "reason" in missing_reason.get_json()["error"].lower()

    adjusted = client.post("/api/sales/stock-adjustments", json={
        "sales_profile_id": sp["profile"].id, "business_date": "2026-11-02",
        "product_id": sp["product"].id, "quantity": 1, "unit": "carton", "direction": "add",
        "reason": "Recount found one extra carton",
    })
    assert adjusted.status_code == 201, adjusted.get_json()

    cash = client.post("/api/sales/cash-adjustments", json={
        "sales_profile_id": sp["profile"].id, "business_date": "2026-11-02",
        "account": "cash", "amount_delta_ugx": 1_000, "reason": "Till float correction",
    })
    assert cash.status_code == 201, cash.get_json()


# ---------------------------------------------------------------------------
# No access outside Sales
# ---------------------------------------------------------------------------

def test_salesperson_denied_finished_goods_section_without_explicit_grant(client, sp):
    res = _login(client, "sp-person", section=SECTION_FINISHED_GOODS)
    assert res.status_code == 403
    assert client.get("/api/session").get_json()["authed"] is False


def test_salesperson_denied_finished_goods_api_while_in_sales_section(client, sp):
    _login(client, "sp-person")  # section=sales
    res = client.get("/api/dashboard")
    assert res.status_code == 403
    assert res.get_json()["error"] == "forbidden_section"


def test_salesperson_denied_admin_page(client, sp):
    _login(client, "sp-person")
    res = client.get("/admin.html")
    # Denied the actual admin content either way; redirected to its own
    # section's landing page rather than Dashboard (which it also can't
    # see), so this never becomes a redirect loop.
    assert res.status_code == 302
    assert res.headers["Location"] == "/sales.html"
    # Following that redirect lands on Sales for real, never bounces again.
    assert client.get("/sales.html").status_code == 200


def test_salesperson_denied_admin_user_management_api(client, sp):
    _login(client, "sp-person")
    res = client.get("/api/admin/users")
    assert res.status_code == 403


def test_salesperson_gains_finished_goods_with_an_explicit_grant(app, client, sp):
    with app.app_context():
        section_access_service.set_sections_for_user(sp["salesperson"], ["finished_goods"], sp["manager"])
        db.session.commit()
    res = _login(client, "sp-person", section=SECTION_FINISHED_GOODS)
    assert res.status_code == 200, res.get_json()


# ---------------------------------------------------------------------------
# Super Admin regression — still global by role, no username hardcoding
# ---------------------------------------------------------------------------

def test_super_admin_still_enters_sales_without_explicit_grant(client, make_user):
    make_user("freshsuper", "password123", "super_admin")
    res = _login(client, "freshsuper")
    assert res.status_code == 200
    assert client.get("/api/sales/metadata").status_code == 200


def test_super_admin_still_enters_finished_goods_without_explicit_grant(client, make_user):
    make_user("freshsuper2", "password123", "super_admin")
    res = _login(client, "freshsuper2", section=SECTION_FINISHED_GOODS)
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# Ordinary-role regression — the role does not leak to anyone else
# ---------------------------------------------------------------------------

def test_plain_operator_still_denied_sales_without_salesperson_role_or_grant(client, make_user):
    make_user("plainop", "password123", "operator")
    res = _login(client, "plainop")
    assert res.status_code == 403


def test_viewer_still_denied_sales_without_explicit_grant(client, make_user):
    make_user("plainviewer", "password123", "viewer")
    res = _login(client, "plainviewer")
    assert res.status_code == 403


def test_manager_still_requires_explicit_sales_grant(client, make_user):
    # Manager is already elevated INSIDE Sales once logged in, but entry
    # into the section itself still needs the normal explicit grant —
    # unchanged by the Sales Person exception.
    make_user("plainmgr", "password123", "manager")
    res = _login(client, "plainmgr")
    assert res.status_code == 403


def test_explicit_sales_grant_for_an_ordinary_role_still_works_unchanged(app, client, make_user):
    user = make_user("grantedviewer", "password123", "viewer")
    with app.app_context():
        section_access_service.ensure_seeded()
        section_access_service.set_sections_for_user(user, ["sales"], user)
        db.session.commit()
    res = _login(client, "grantedviewer")
    assert res.status_code == 200
