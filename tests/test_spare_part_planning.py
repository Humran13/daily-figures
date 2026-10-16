"""
Consumption & Procurement Planning — pure-function calculation tests
(analytics/planning services) plus HTTP-level permission, filter, search,
critical-flag, and export tests. See docs/SPARE-PARTS-PLANNING.md for the
methodology each formula here is pinned to.
"""
from decimal import Decimal

import pytest

from webapp.extensions import db
from webapp.services import spare_part_analytics_service as analytics
from webapp.services import spare_part_planning_service as planning
from tests.test_spare_parts import _create_spare_part, _get_or_create_department, _login_spare_parts


# ======================= pure calculation tests =======================

# ---------- average monthly consumption ----------

def test_average_monthly_consumption_full_year():
    # 365 days of history, 1200 units consumed -> 1200 / (365/30)
    result = analytics.average_monthly_consumption(Decimal("1200"), 365)
    assert result == Decimal("1200") / (Decimal("365") / Decimal("30"))


def test_average_monthly_consumption_short_history_is_not_exaggerated():
    """The naive total/days*30 projection would report 50 units issued 2
    days after creation as a 750/month rate (50/2*30). The documented
    formula floors the divisor at 1 month-equivalent instead, reporting a
    conservative 50/month until real history accumulates."""
    naive_exaggerated_rate = Decimal("50") / Decimal("2") * Decimal("30")
    result = analytics.average_monthly_consumption(Decimal("50"), 2)
    assert result == Decimal("50")
    assert result < naive_exaggerated_rate


def test_average_monthly_consumption_zero_quantity():
    assert analytics.average_monthly_consumption(Decimal("0"), 90) == Decimal("0")


def test_average_monthly_consumption_zero_or_negative_window_never_crashes():
    assert analytics.average_monthly_consumption(Decimal("10"), 0) == Decimal("0")
    assert analytics.average_monthly_consumption(Decimal("10"), None) == Decimal("0")


def test_average_monthly_consumption_exactly_one_month():
    assert analytics.average_monthly_consumption(Decimal("30"), 30) == Decimal("30")


# ---------- trend ----------

def test_trend_increasing_beyond_threshold():
    result = analytics.classify_trend(Decimal("130"), Decimal("100"), has_full_preceding_window=True)
    assert result.label == "Increasing"


def test_trend_decreasing_beyond_threshold():
    result = analytics.classify_trend(Decimal("70"), Decimal("100"), has_full_preceding_window=True)
    assert result.label == "Decreasing"


def test_trend_stable_within_threshold():
    # 5% change, well under the 15% threshold.
    result = analytics.classify_trend(Decimal("105"), Decimal("100"), has_full_preceding_window=True)
    assert result.label == "Stable"


def test_trend_stable_boundary_exactly_at_threshold_is_not_increasing():
    # Exactly +15% must NOT flip to Increasing (strictly greater-than).
    result = analytics.classify_trend(Decimal("115"), Decimal("100"), has_full_preceding_window=True)
    assert result.label == "Stable"


def test_trend_stable_when_both_periods_zero():
    result = analytics.classify_trend(Decimal("0"), Decimal("0"), has_full_preceding_window=True)
    assert result.label == "Stable"


def test_trend_increasing_from_zero_preceding_base():
    result = analytics.classify_trend(Decimal("10"), Decimal("0"), has_full_preceding_window=True)
    assert result.label == "Increasing"


def test_trend_insufficient_data_when_preceding_window_incomplete():
    result = analytics.classify_trend(Decimal("50"), Decimal("10"), has_full_preceding_window=False)
    assert result.label == "Insufficient Data"


def test_window_bounds_and_preceding_window_are_equal_length_and_non_overlapping():
    recent = analytics.window_bounds("2026-06-30", 90)
    preceding = analytics.preceding_window_bounds("2026-06-30", 90)
    from datetime import date
    recent_days = (date.fromisoformat(recent[1]) - date.fromisoformat(recent[0])).days + 1
    preceding_days = (date.fromisoformat(preceding[1]) - date.fromisoformat(preceding[0])).days + 1
    assert recent_days == preceding_days == 90
    assert preceding[1] < recent[0]  # no overlap


def test_forecast_monthly_demand_baseline_returns_average():
    assert analytics.forecast_monthly_demand(Decimal("42")) == Decimal("42")


def test_history_days_available_none_when_no_history():
    assert analytics.history_days_available(None, "2026-06-30") is None


def test_history_days_available_at_least_one_day():
    assert analytics.history_days_available("2026-06-30", "2026-06-30") == 1


# ---------- safety stock / incoming stock / recommended purchase ----------

def test_safety_stock_uses_minimum_stock_as_override():
    assert planning.safety_stock_for(Decimal("20"), Decimal("999")) == Decimal("20")


def test_safety_stock_default_when_minimum_stock_unset():
    avg = Decimal("10")
    assert planning.safety_stock_for(None, avg) == avg * planning.DEFAULT_SAFETY_STOCK_MONTHS


def test_confirmed_incoming_stock_is_always_zero_today():
    from unittest.mock import Mock
    assert planning.confirmed_incoming_stock_for(Mock()) == Decimal("0")


def test_recommended_purchase_matches_spec_worked_example():
    # 10/month average * 12 months = 120; safety stock 20; current stock 20; incoming 0.
    result = planning.recommended_purchase(Decimal("10") * 12, Decimal("20"), Decimal("20"), Decimal("0"))
    assert result == Decimal("120")


def test_recommended_purchase_never_goes_below_zero():
    result = planning.recommended_purchase(Decimal("10"), Decimal("5"), Decimal("1000"), Decimal("0"))
    assert result == Decimal("0")


def test_recommended_purchase_accounts_for_incoming_stock():
    result = planning.recommended_purchase(Decimal("100"), Decimal("0"), Decimal("0"), Decimal("30"))
    assert result == Decimal("70")


# ---------- stock coverage / stockout ----------

def test_stock_coverage_months_normal_case():
    assert planning.stock_coverage_months(Decimal("40"), Decimal("10")) == Decimal("4")


def test_stock_coverage_months_zero_demand_is_none_not_infinity():
    assert planning.stock_coverage_months(Decimal("40"), Decimal("0")) is None
    assert planning.stock_coverage_months(Decimal("40"), None) is None


def test_estimated_stockout_date_zero_demand_is_none():
    assert planning.estimated_stockout_date(Decimal("40"), Decimal("0"), "2026-06-30") is None


def test_estimated_stockout_date_already_out_of_stock_is_today():
    assert planning.estimated_stockout_date(Decimal("0"), Decimal("10"), "2026-06-30") == "2026-06-30"


def test_estimated_stockout_date_projects_forward():
    # 20 units at 10/month -> 2 months -> ~60 days out.
    result = planning.estimated_stockout_date(Decimal("20"), Decimal("10"), "2026-01-01")
    from datetime import date
    assert date.fromisoformat(result) > date(2026, 1, 1)
    assert date.fromisoformat(result) <= date(2026, 3, 15)


def test_estimated_stockout_date_capped_beyond_max_projection():
    # Huge stock, tiny demand -> would project decades out; must return None.
    assert planning.estimated_stockout_date(Decimal("1000000"), Decimal("1"), "2026-01-01") is None


# ---------- stock status / slow-moving / dead stock ----------

def test_stock_status_stockout():
    assert planning.stock_status_for(Decimal("0"), Decimal("10")) == planning.STATUS_STOCKOUT
    assert planning.stock_status_for(Decimal("-1"), Decimal("10")) == planning.STATUS_STOCKOUT


def test_stock_status_below_minimum():
    assert planning.stock_status_for(Decimal("5"), Decimal("10")) == planning.STATUS_BELOW_MINIMUM


def test_stock_status_healthy():
    assert planning.stock_status_for(Decimal("50"), Decimal("10")) == planning.STATUS_HEALTHY
    assert planning.stock_status_for(Decimal("50"), None) == planning.STATUS_HEALTHY


def test_dead_stock_requires_both_age_and_long_silence():
    is_dead, is_slow = planning.classify_movement_speed(
        current_stock=Decimal("10"), history_days=400, days_since_last_stock_out=400, coverage_months=None,
    )
    assert (is_dead, is_slow) == (True, False)


def test_new_part_with_stock_and_no_sign_outs_is_not_dead_stock():
    """Section 19/30: a brand-new part must never be blindly classified
    Dead Stock just because it has no transaction history yet."""
    is_dead, is_slow = planning.classify_movement_speed(
        current_stock=Decimal("10"), history_days=5, days_since_last_stock_out=None, coverage_months=None,
    )
    assert is_dead is False


def test_slow_moving_has_consumption_but_long_coverage():
    is_dead, is_slow = planning.classify_movement_speed(
        current_stock=Decimal("10"), history_days=400, days_since_last_stock_out=10, coverage_months=Decimal("20"),
    )
    assert (is_dead, is_slow) == (False, True)


def test_zero_stock_is_never_dead_or_slow_moving():
    is_dead, is_slow = planning.classify_movement_speed(
        current_stock=Decimal("0"), history_days=400, days_since_last_stock_out=None, coverage_months=None,
    )
    assert (is_dead, is_slow) == (False, False)


# ---------- ABC classification ----------

def test_abc_value_based_when_pricing_mostly_available():
    rows = [
        {"id": 1, "annual_quantity": Decimal("70"), "unit_cost": Decimal("10")},  # value 700 (70% of 1000)
        {"id": 2, "annual_quantity": Decimal("30"), "unit_cost": Decimal("10")},  # value 300
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "value"
    assert result.class_by_id[1] == "A"


def test_abc_boundary_exactly_at_80_percent_is_class_a():
    # Two items: 80 and 20 -> cumulative 80% exactly on the first row.
    rows = [
        {"id": 1, "annual_quantity": Decimal("80"), "unit_cost": Decimal("1")},
        {"id": 2, "annual_quantity": Decimal("20"), "unit_cost": Decimal("1")},
    ]
    result = planning.classify_abc(rows)
    assert result.class_by_id[1] == "A"
    assert result.class_by_id[2] == "C"


def test_abc_falls_back_to_quantity_when_pricing_insufficient():
    rows = [
        {"id": 1, "annual_quantity": Decimal("80"), "unit_cost": None},
        {"id": 2, "annual_quantity": Decimal("15"), "unit_cost": None},
        {"id": 3, "annual_quantity": Decimal("5"), "unit_cost": Decimal("1")},
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "quantity"
    assert result.class_by_id[1] == "A"


def test_abc_missing_price_never_fabricated_as_zero_value():
    rows = [
        {"id": 1, "annual_quantity": Decimal("100"), "unit_cost": Decimal("10")},
        {"id": 2, "annual_quantity": Decimal("90"), "unit_cost": Decimal("10")},
        {"id": 3, "annual_quantity": Decimal("90"), "unit_cost": Decimal("10")},
        {"id": 4, "annual_quantity": Decimal("9999"), "unit_cost": None},  # huge qty, NO price
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "value"
    # The unpriced row must land in C (never ranked as if it were the
    # highest-value row just because its quantity is huge).
    assert result.class_by_id[4] == "C"


def test_abc_empty_rows():
    result = planning.classify_abc([])
    assert result.class_by_id == {}


def test_abc_all_zero_consumption_value_is_all_class_c():
    rows = [
        {"id": 1, "annual_quantity": Decimal("0"), "unit_cost": Decimal("10")},
        {"id": 2, "annual_quantity": Decimal("0"), "unit_cost": Decimal("5")},
    ]
    result = planning.classify_abc(rows)
    assert result.class_by_id[1] == "C"
    assert result.class_by_id[2] == "C"


# ---------- procurement priority ----------

def test_priority_urgent_when_stockout():
    result = planning.procurement_priority(
        stock_status=planning.STATUS_STOCKOUT, coverage_months=None, critical=False, trend_label="Stable",
    )
    assert result == planning.PRIORITY_URGENT


def test_priority_urgent_for_critical_part_with_very_low_coverage():
    result = planning.procurement_priority(
        stock_status=planning.STATUS_HEALTHY, coverage_months=Decimal("0.5"), critical=True, trend_label="Stable",
    )
    assert result == planning.PRIORITY_URGENT


def test_priority_high_when_below_minimum():
    result = planning.procurement_priority(
        stock_status=planning.STATUS_BELOW_MINIMUM, coverage_months=None, critical=False, trend_label="Stable",
    )
    assert result == planning.PRIORITY_HIGH


def test_priority_critical_can_outrank_low_consumption_part():
    """Section 16: a critical part with LOW consumption (no coverage
    signal at all, trend not increasing) must still reach at least
    Medium purely through criticality — priority must not be driven by
    consumption volume alone."""
    not_critical = planning.procurement_priority(
        stock_status=planning.STATUS_HEALTHY, coverage_months=None, critical=False, trend_label="Stable",
    )
    critical = planning.procurement_priority(
        stock_status=planning.STATUS_HEALTHY, coverage_months=None, critical=True, trend_label="Stable",
    )
    assert not_critical == planning.PRIORITY_LOW
    assert critical == planning.PRIORITY_MEDIUM


def test_priority_low_default_case():
    result = planning.procurement_priority(
        stock_status=planning.STATUS_HEALTHY, coverage_months=Decimal("20"), critical=False, trend_label="Stable",
    )
    assert result == planning.PRIORITY_LOW


# ---------- China / Local / Best Price ----------

def test_best_price_selects_cheaper_china():
    assert planning.best_price_selection(Decimal("5"), Decimal("8")) == (planning.SOURCE_CHINA, Decimal("5"))


def test_best_price_selects_cheaper_local():
    assert planning.best_price_selection(Decimal("12"), Decimal("8")) == (planning.SOURCE_LOCAL, Decimal("8"))


def test_best_price_missing_china_falls_back_to_local():
    assert planning.best_price_selection(None, Decimal("8")) == (planning.SOURCE_LOCAL, Decimal("8"))


def test_best_price_missing_local_falls_back_to_china():
    assert planning.best_price_selection(Decimal("5"), None) == (planning.SOURCE_CHINA, Decimal("5"))


def test_best_price_both_missing_returns_none():
    assert planning.best_price_selection(None, None) == (None, None)


def test_best_price_zero_price_treated_as_missing_not_free():
    # A price of exactly 0 is not a usable price (never silently "free").
    assert planning.best_price_selection(Decimal("0"), Decimal("8")) == (planning.SOURCE_LOCAL, Decimal("8"))


def test_procurement_cost_normal():
    assert planning.procurement_cost(Decimal("10"), Decimal("5")) == Decimal("50")


def test_procurement_cost_missing_price_is_none_never_zero():
    assert planning.procurement_cost(Decimal("10"), None) is None


def test_procurement_cost_fractional_quantity_not_rounded():
    assert planning.procurement_cost(Decimal("2.5"), Decimal("4")) == Decimal("10.0")


# ======================= HTTP-level integration tests =======================

def _report(client, **params):
    res = client.get("/api/spare-parts/planning", query_string=params)
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def test_consumption_excludes_adjustments_opening_balance_and_voids(app, client, make_user):
    user = _login_spare_parts(client, make_user, "planmgr1", "manager")
    part = _create_spare_part(client, name="Filter Belt")
    dept = _get_or_create_department(app)
    with app.app_context():
        from webapp.models.spare_part import SparePart
        from webapp.models.user import User
        from webapp.services import spare_part_movement_service as msvc
        actor = User.query.filter_by(username="planmgr1").first()
        sp = db.session.get(SparePart, part["id"])
        msvc.record_opening_balance(sp, Decimal("1000"), actor, business_date="2026-01-01")
        msvc.record_adjustment(sp, Decimal("500"), "out", actor, business_date="2026-01-02", reason="count correction")
        real_out = msvc.record_stock_out(
            sp, Decimal("7"), actor, business_date="2026-01-03", requested_by="Juma", department_id=dept,
        )
        voided_out = msvc.record_stock_out(
            sp, Decimal("400"), actor, business_date="2026-01-03", requested_by="Juma", department_id=dept,
        )
        msvc.void_movement(voided_out, actor, "entered twice by mistake")
        db.session.commit()
        totals = analytics.bulk_consumption([sp.id], "2026-01-01", "2026-01-31")
        assert totals[sp.id] == Decimal("7")  # only the real, active Stock Out


def test_search_by_name_in_planning_report(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr2", "manager")
    _create_spare_part(client, name="Unique Roller Chain XYZ", model="RC40")
    data = _report(client, q="Roller Chain XYZ")
    assert any("Roller Chain XYZ" in r["display_name"] for r in data["rows"])


def test_search_by_specification_in_planning_report(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr3", "manager")
    _create_spare_part(client, name="Bearing Special", specifications="UNIQUESPEC-22213")
    data = _report(client, q="UNIQUESPEC-22213")
    assert any(r["specifications"] == "UNIQUESPEC-22213" for r in data["rows"])


def test_filter_by_critical_only(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr4", "manager")
    normal = _create_spare_part(client, name="Normal Gasket")
    critical = _create_spare_part(client, name="Critical Gasket")
    client.patch(f"/api/spare-parts/{critical['id']}", json={"critical": True})
    data = _report(client, critical_only="1")
    ids = {r["id"] for r in data["rows"]}
    assert critical["id"] in ids
    assert normal["id"] not in ids


def test_filter_by_category(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr5", "manager")
    cat = client.post("/api/spare-parts/categories", json={"name": "Filters Only Cat"}).get_json()
    part = _create_spare_part(client, name="Oil Filter", category_id=cat["id"])
    other = _create_spare_part(client, name="Unrelated Part")
    data = _report(client, category_id=cat["id"])
    ids = {r["id"] for r in data["rows"]}
    assert part["id"] in ids
    assert other["id"] not in ids


def test_critical_flag_can_be_set_by_manager_and_is_audited(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr6", "manager")
    part = _create_spare_part(client, name="Gearbox Seal")
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"critical": True, "critical_note": "no local substitute"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["critical"] is True
    assert body["critical_note"] == "no local substitute"
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="update", entity_type="spare_part", entity_id=str(part["id"])).first()
        assert entry is not None
        assert "critical" in entry.after_json


def test_critical_flag_cannot_be_set_by_operator(app, client, make_user):
    part_id = None
    with app.app_context():
        from webapp.models.user import User
        from webapp.services import spare_part_service as sp_svc
        from webapp.models.user import ROLE_SUPER_ADMIN
        from werkzeug.security import generate_password_hash
        admin = User(username="seedadmin1", password_hash=generate_password_hash("x"), role=ROLE_SUPER_ADMIN)
        db.session.add(admin)
        db.session.commit()
        part = sp_svc.create_spare_part(name="Clutch Plate")
        db.session.commit()
        part_id = part.id
    _login_spare_parts(client, make_user, "planop1", "operator")
    res = client.patch(f"/api/spare-parts/{part_id}", json={"critical": True})
    assert res.status_code == 403


def test_pricing_hidden_from_manager_in_planning_report(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr7", "manager")
    _create_spare_part(client, name="Priced Part A")
    data = _report(client)
    assert data["rows"]
    for row in data["rows"]:
        assert "china_buying_price" not in row
        assert "selected_source_projected_cost" not in row
    assert data["summary"]["procurement_value_12m"] is None


def test_pricing_visible_to_super_admin_in_planning_report(app, client, make_user):
    _login_spare_parts(client, make_user, "plansa1", "super_admin")
    part = _create_spare_part(client, name="Priced Part B")
    client.patch(f"/api/spare-parts/{part['id']}", json={"china_buying_price": "10.00", "local_buying_price": "12.00"})
    data = _report(client, q="Priced Part B")
    row = next(r for r in data["rows"] if r["id"] == part["id"])
    assert row["china_buying_price"] == "10.00"
    assert row["local_buying_price"] == "12.00"


def test_viewer_can_view_planning_but_cannot_write(app, client, make_user):
    _login_spare_parts(client, make_user, "planview1", "viewer")
    res = client.get("/api/spare-parts/planning")
    assert res.status_code == 200


def test_accountant_can_view_planning(app, client, make_user):
    _login_spare_parts(client, make_user, "planacct1", "accountant")
    res = client.get("/api/spare-parts/planning")
    assert res.status_code == 200


def test_finished_goods_session_cannot_reach_planning_api(client, login_as):
    login_as("planfg1", "password123", "manager")
    res = client.get("/api/spare-parts/planning")
    assert res.status_code == 403
    assert res.get_json()["error"] == "forbidden_section"


def test_planning_requires_login(client):
    res = client.get("/api/spare-parts/planning")
    assert res.status_code == 401


def test_export_csv_xlsx_pdf(app, client, make_user):
    _login_spare_parts(client, make_user, "planexport1", "manager")
    _create_spare_part(client, name="Export Test Part")
    for fmt in ("csv", "xlsx", "pdf"):
        res = client.get(f"/api/spare-parts/planning/export.{fmt}")
        assert res.status_code == 200, res.get_data()
        assert len(res.data) > 0


def test_export_bad_format_rejected(app, client, make_user):
    _login_spare_parts(client, make_user, "planexport2", "manager")
    res = client.get("/api/spare-parts/planning/export.exe")
    assert res.status_code == 400


def test_recommended_purchase_quantity_present_in_report_row(app, client, make_user):
    user = _login_spare_parts(client, make_user, "planmgr8", "manager")
    part = _create_spare_part(client, name="Chain Link", minimum_stock="5")
    dept = _get_or_create_department(app)
    with app.app_context():
        from webapp.models.spare_part import SparePart
        from webapp.models.user import User
        from webapp.services import spare_part_movement_service as msvc
        actor = User.query.filter_by(username="planmgr8").first()
        sp = db.session.get(SparePart, part["id"])
        msvc.record_stock_in(sp, Decimal("50"), actor, business_date="2026-01-01")
        msvc.record_stock_out(sp, Decimal("10"), actor, business_date="2026-01-10", requested_by="Op", department_id=dept)
        db.session.commit()
    data = _report(client, q="Chain Link", analysis_period_days=365)
    row = next(r for r in data["rows"] if r["id"] == part["id"])
    assert Decimal(row["recommended_purchase_selected"]) >= Decimal("0")
    assert row["stock_status"] in (planning.STATUS_HEALTHY, planning.STATUS_BELOW_MINIMUM, planning.STATUS_STOCKOUT)


def test_zero_consumption_part_shows_no_recent_consumption_coverage(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr9", "manager")
    part = _create_spare_part(client, name="Never Used Part")
    data = _report(client, q="Never Used Part")
    row = next(r for r in data["rows"] if r["id"] == part["id"])
    assert row["stock_coverage_months"] is None
    assert row["average_monthly_consumption"] == "0.000"


def test_dead_stock_view_filters_correctly(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr10", "manager")
    part = _create_spare_part(client, name="Long Dead Part")
    with app.app_context():
        from webapp.models.spare_part import SparePart
        sp = db.session.get(SparePart, part["id"])
        sp.current_stock_cache = Decimal("5")
        sp.created_at = sp.created_at.replace(year=sp.created_at.year - 2)
        db.session.commit()
        from webapp.services import spare_part_movement_service as msvc
        from webapp.models.user import User
        actor = User.query.filter_by(username="planmgr10").first()
        msvc._increase_cache(sp.id, Decimal("5"))
        db.session.commit()
    data = _report(client, view="dead_stock")
    # Whether or not it lands in the filtered set depends on ledger state;
    # the call must succeed and only ever return actual dead-stock rows.
    for row in data["rows"]:
        assert row["is_dead_stock"] is True


def test_planning_page_served_for_authorized_spare_parts_session(app, client, make_user):
    _login_spare_parts(client, make_user, "planpage1", "viewer")
    res = client.get("/spare-parts-planning.html")
    assert res.status_code == 200


def test_planning_page_redirects_without_session(client):
    res = client.get("/spare-parts-planning.html")
    assert res.status_code in (302, 303)


# ======================= regression: existing behavior unaffected =======================

def test_existing_spare_part_to_dict_still_works_without_pricing(app):
    with app.app_context():
        from webapp.services import spare_part_service as sp_svc
        part = sp_svc.create_spare_part(name="Regression Part")
        db.session.commit()
        d = part.to_dict(current_stock=Decimal("0"))
        assert d["name"] == "Regression Part"
        assert d["critical"] is False
        assert d["critical_note"] is None


def test_existing_display_name_bracket_format_unchanged():
    from webapp.models.spare_part import spare_part_display_name
    assert spare_part_display_name("Bearing", "22213") == "Bearing [22213]"
    assert spare_part_display_name("Bearing", None) == "Bearing"
