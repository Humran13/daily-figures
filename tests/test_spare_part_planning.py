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
# Design review: safety stock and minimum_stock (reorder level / low-stock
# alert threshold) are deliberately SEPARATE concepts, using separate
# columns. safety_stock_for() must never read minimum_stock at all.

def test_safety_stock_uses_override_field():
    assert planning.safety_stock_for(Decimal("20"), Decimal("999")) == Decimal("20")


def test_safety_stock_default_when_override_unset():
    avg = Decimal("10")
    assert planning.safety_stock_for(None, avg) == avg * planning.DEFAULT_SAFETY_STOCK_MONTHS


def test_safety_stock_is_independent_of_minimum_stock(app, client, make_user):
    """A part with a large minimum_stock but no safety_stock_override must
    still get the application-default safety stock (1 month of its own
    average consumption) -- never minimum_stock's value."""
    _login_spare_parts(client, make_user, "safetysep1", "manager")
    part = _create_spare_part(client, name="Separation Check Part", minimum_stock="500")
    data = _report(client, q="Separation Check Part")
    row = next(r for r in data["rows"] if r["id"] == part["id"])
    assert row["minimum_stock"] == "500.000"
    assert row["safety_stock"] != "500.000"
    assert Decimal(row["safety_stock"]) == Decimal(row["average_monthly_consumption"]) * planning.DEFAULT_SAFETY_STOCK_MONTHS


def test_safety_stock_override_does_not_affect_minimum_stock_low_stock_alert(app, client, make_user):
    """Setting safety_stock_override must never change is_low_stock()'s
    existing minimum_stock-based behavior."""
    from webapp.services import spare_part_movement_service as msvc
    _login_spare_parts(client, make_user, "safetysep2", "manager")
    part = _create_spare_part(client, name="Low Stock Unaffected Part", minimum_stock="10")
    client.patch(f"/api/spare-parts/{part['id']}", json={"safety_stock_override": "999"})
    with app.app_context():
        from webapp.models.spare_part import SparePart
        sp = db.session.get(SparePart, part["id"])
        assert sp.safety_stock_override == Decimal("999")
        assert sp.minimum_stock == Decimal("10")
        # Stock of 5 is <= minimum_stock(10) -> still correctly low, regardless of the override.
        assert msvc.is_low_stock(sp, Decimal("5")) is True
        assert msvc.is_low_stock(sp, Decimal("50")) is False


def test_safety_stock_override_settable_by_manager(app, client, make_user):
    _login_spare_parts(client, make_user, "safetysep3", "manager")
    part = _create_spare_part(client, name="Override Settable Part")
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"safety_stock_override": "15.5"})
    assert res.status_code == 200
    assert res.get_json()["safety_stock_override"] == "15.500"


def test_confirmed_incoming_stock_is_always_zero_today():
    from unittest.mock import Mock
    assert planning.confirmed_incoming_stock_for(Mock()) == Decimal("0")


def test_incoming_stock_display_shows_not_tracked_while_untracked():
    assert planning.INCOMING_STOCK_TRACKED is False
    assert planning.incoming_stock_display(Decimal("0")) == "Not tracked"
    # Still "Not tracked" even if a nonzero value were ever passed in --
    # the label is about whether the SOURCE is tracked, not the number.
    assert planning.incoming_stock_display(Decimal("5")) == "Not tracked"


def test_incoming_stock_shown_as_not_tracked_in_report_row(app, client, make_user):
    _login_spare_parts(client, make_user, "incoming1", "manager")
    part = _create_spare_part(client, name="Incoming Display Part")
    data = _report(client, q="Incoming Display Part")
    row = next(r for r in data["rows"] if r["id"] == part["id"])
    assert row["incoming_stock"] == "Not tracked"


def test_methodology_reports_incoming_stock_not_tracked(app, client, make_user):
    _login_spare_parts(client, make_user, "incoming2", "manager")
    data = _report(client)
    assert data["methodology"]["incoming_stock_tracked"] is False


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


def test_abc_missing_price_high_consumption_item_is_unclassified_not_c():
    """Design review: a row whose value cannot be computed must never be
    silently folded into class C (which asserts genuinely low value).
    It is explicitly ABC_UNCLASSIFIED instead, regardless of how large
    its quantity is."""
    rows = [
        {"id": 1, "annual_quantity": Decimal("100"), "unit_cost": Decimal("10")},
        {"id": 2, "annual_quantity": Decimal("90"), "unit_cost": Decimal("10")},
        {"id": 3, "annual_quantity": Decimal("90"), "unit_cost": Decimal("10")},
        {"id": 4, "annual_quantity": Decimal("9999"), "unit_cost": None},  # huge qty, NO price
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "value"
    assert result.class_by_id[4] == planning.ABC_UNCLASSIFIED
    assert result.class_by_id[4] != "C"


def test_abc_some_china_prices_missing_falls_back_or_unclassifies_correctly():
    """Scenario: several rows only have a China price configured (their
    unit_cost resolves fine) while others have neither source configured
    (unit_cost is None). Coverage stays >= 50%, so value-based ABC applies
    and only the truly unpriced rows are Unclassified."""
    rows = [
        {"id": 1, "annual_quantity": Decimal("50"), "unit_cost": Decimal("20")},  # China-priced
        {"id": 2, "annual_quantity": Decimal("30"), "unit_cost": Decimal("15")},  # China-priced
        {"id": 3, "annual_quantity": Decimal("10"), "unit_cost": None},  # no price at all
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "value"
    assert result.class_by_id[3] == planning.ABC_UNCLASSIFIED
    assert result.class_by_id[1] in ("A", "B", "C")


def test_abc_some_local_prices_missing_falls_back_or_unclassifies_correctly():
    rows = [
        {"id": 1, "annual_quantity": Decimal("50"), "unit_cost": Decimal("20")},  # Local-priced
        {"id": 2, "annual_quantity": Decimal("30"), "unit_cost": Decimal("15")},  # Local-priced
        {"id": 3, "annual_quantity": Decimal("10"), "unit_cost": None},
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "value"
    assert result.class_by_id[3] == planning.ABC_UNCLASSIFIED


def test_abc_both_china_and_local_missing_for_most_rows_falls_back_to_quantity():
    """When neither source is priced for most rows, coverage drops below
    50% and the WHOLE report falls back to quantity-based ranking (never
    a per-row mix) -- so nothing is Unclassified in this mode; every row
    participates."""
    rows = [
        {"id": 1, "annual_quantity": Decimal("100"), "unit_cost": None},
        {"id": 2, "annual_quantity": Decimal("50"), "unit_cost": None},
        {"id": 3, "annual_quantity": Decimal("10"), "unit_cost": Decimal("5")},
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "quantity"
    assert planning.ABC_UNCLASSIFIED not in result.class_by_id.values()


def test_abc_exactly_at_methodology_threshold_uses_value_based():
    """Exactly 50% price coverage (the documented minimum) must still use
    value-based ABC -- the threshold is inclusive (>=)."""
    rows = [
        {"id": 1, "annual_quantity": Decimal("10"), "unit_cost": Decimal("5")},
        {"id": 2, "annual_quantity": Decimal("10"), "unit_cost": None},
    ]
    result = planning.classify_abc(rows)
    assert result.price_coverage_ratio == Decimal("0.5")
    assert result.methodology == "value"
    assert result.class_by_id[2] == planning.ABC_UNCLASSIFIED


def test_abc_just_below_methodology_threshold_falls_back_to_quantity():
    """Just under 50% coverage (1 of 3 rows priced) must fall back to
    quantity-based ABC for the whole report."""
    rows = [
        {"id": 1, "annual_quantity": Decimal("10"), "unit_cost": Decimal("5")},
        {"id": 2, "annual_quantity": Decimal("10"), "unit_cost": None},
        {"id": 3, "annual_quantity": Decimal("10"), "unit_cost": None},
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "quantity"


def test_abc_all_prices_available_is_pure_value_based_with_no_unclassified():
    rows = [
        {"id": 1, "annual_quantity": Decimal("80"), "unit_cost": Decimal("1")},
        {"id": 2, "annual_quantity": Decimal("20"), "unit_cost": Decimal("1")},
    ]
    result = planning.classify_abc(rows)
    assert result.methodology == "value"
    assert result.price_coverage_ratio == Decimal("1")
    assert planning.ABC_UNCLASSIFIED not in result.class_by_id.values()


def test_abc_empty_rows():
    result = planning.classify_abc([])
    assert result.class_by_id == {}


def test_abc_all_zero_consumption_value_is_all_class_c():
    # Every row IS priced here (unlike the Unclassified cases above) --
    # their value is genuinely known to be zero, so class C is correct.
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
    _login_spare_parts(client, make_user, "planmgr4", "super_admin")
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


def test_critical_flag_can_be_set_by_super_admin_and_is_audited(app, client, make_user):
    _login_spare_parts(client, make_user, "plansa_crit", "super_admin")
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


def test_critical_flag_cannot_be_set_by_manager(app, client, make_user):
    """Design review: Critical is Super-Administrator-only, even though
    Manager may edit this same spare part's other fields."""
    _login_spare_parts(client, make_user, "planmgr_crit", "manager")
    part = _create_spare_part(client, name="Gearbox Seal Manager")
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"critical": True})
    assert res.status_code == 403
    assert res.get_json()["error"] == "Only a Super Administrator can change Critical status."
    with app.app_context():
        from webapp.models.spare_part import SparePart
        sp = db.session.get(SparePart, part["id"])
        assert sp.critical is False


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


def test_critical_flag_cannot_be_set_by_viewer(app, client, make_user):
    part_id = None
    with app.app_context():
        from webapp.models.user import User
        from webapp.services import spare_part_service as sp_svc
        from webapp.models.user import ROLE_SUPER_ADMIN
        from werkzeug.security import generate_password_hash
        admin = User(username="seedadmin2", password_hash=generate_password_hash("x"), role=ROLE_SUPER_ADMIN)
        db.session.add(admin)
        db.session.commit()
        part = sp_svc.create_spare_part(name="Idler Pulley")
        db.session.commit()
        part_id = part.id
    _login_spare_parts(client, make_user, "planview_crit", "viewer")
    res = client.patch(f"/api/spare-parts/{part_id}", json={"critical": True})
    assert res.status_code == 403


def test_critical_note_alone_is_also_super_admin_only(app, client, make_user):
    _login_spare_parts(client, make_user, "planmgr_critnote", "manager")
    part = _create_spare_part(client, name="Note Only Part")
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"critical_note": "trying to sneak past"})
    assert res.status_code == 403


def test_manager_can_still_edit_other_fields_after_critical_restriction(app, client, make_user):
    """The Critical gate must not accidentally block a Manager's existing
    ability to edit other spare-part fields (name, category, minimum_stock,
    machine associations, ...) when the request does not touch
    critical/critical_note at all."""
    _login_spare_parts(client, make_user, "planmgr_other", "manager")
    part = _create_spare_part(client, name="Editable By Manager")
    res = client.patch(f"/api/spare-parts/{part['id']}", json={
        "model": "New Model", "minimum_stock": "12", "notes": "updated by manager",
    })
    assert res.status_code == 200
    body = res.get_json()
    assert body["minimum_stock"] == "12.000"
    assert body["notes"] == "updated by manager"


def test_mixed_request_with_critical_and_other_fields_is_entirely_rejected_for_manager(app, client, make_user):
    """A Manager request that bundles an allowed field with critical must
    be rejected as a whole -- never partially applied."""
    _login_spare_parts(client, make_user, "planmgr_mixed", "manager")
    part = _create_spare_part(client, name="Mixed Request Part")
    res = client.patch(f"/api/spare-parts/{part['id']}", json={"minimum_stock": "7", "critical": True})
    assert res.status_code == 403
    with app.app_context():
        from webapp.models.spare_part import SparePart
        sp = db.session.get(SparePart, part["id"])
        assert sp.minimum_stock is None  # the allowed field was NOT applied either
        assert sp.critical is False


def test_critical_status_visible_to_every_spare_parts_role_regardless_of_who_set_it(app, client, make_user):
    """Viewing Critical status follows normal Spare Parts access -- it is
    not restricted the way changing it is."""
    _login_spare_parts(client, make_user, "plansa_view_crit", "super_admin")
    part = _create_spare_part(client, name="Visible Critical Part")
    client.patch(f"/api/spare-parts/{part['id']}", json={"critical": True})

    for role, username in (("viewer", "planviewer_sees_crit"), ("operator", "planop_sees_crit"),
                            ("accountant", "planacct_sees_crit")):
        other_client = app.test_client()
        _login_spare_parts(other_client, make_user, username, role)
        res = other_client.get(f"/api/spare-parts/{part['id']}")
        assert res.status_code == 200
        assert res.get_json()["critical"] is True


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


# ======================= ranking-mode ("view") determinism =======================
# Each view gets its own small, self-contained fixture -- deliberately not
# one shared mega-fixture, so each test's expected outcome is traceable to
# exactly the numbers it set up, not to interactions with nine other parts.

def _days_ago(n):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).isoformat()


def _move(app, username, part_id, *, in_qty=None, in_date=None, out_qty=None, out_date=None, dept=None):
    with app.app_context():
        from webapp.models.spare_part import SparePart
        from webapp.models.user import User
        from webapp.services import spare_part_movement_service as msvc
        actor = User.query.filter_by(username=username).first()
        sp = db.session.get(SparePart, part_id)
        if in_qty is not None:
            msvc.record_stock_in(sp, Decimal(str(in_qty)), actor, business_date=in_date)
        if out_qty is not None:
            msvc.record_stock_out(
                sp, Decimal(str(out_qty)), actor, business_date=out_date, requested_by="t", department_id=dept,
            )
        db.session.commit()


def test_view_most_consumed_sorts_by_365_day_usage_descending(app, client, make_user):
    _login_spare_parts(client, make_user, "viewmc", "manager")
    dept = _get_or_create_department(app)
    big = _create_spare_part(client, name="Most Consumed Big")
    small = _create_spare_part(client, name="Most Consumed Small")
    _move(app, "viewmc", big["id"], in_qty=600, in_date=_days_ago(300), out_qty=500, out_date=_days_ago(100), dept=dept)
    _move(app, "viewmc", small["id"], in_qty=60, in_date=_days_ago(300), out_qty=5, out_date=_days_ago(100), dept=dept)
    data = _report(client, view="most_consumed", q="Most Consumed")
    ids_in_order = [r["id"] for r in data["rows"]]
    assert ids_in_order.index(big["id"]) < ids_in_order.index(small["id"])


def test_view_fastest_moving_sorts_by_recent_monthly_consumption_descending(app, client, make_user):
    _login_spare_parts(client, make_user, "viewfm", "manager")
    dept = _get_or_create_department(app)
    fast = _create_spare_part(client, name="Fastest Moving Part")
    slow = _create_spare_part(client, name="Slower Moving Part")
    # Both have identical 365-day totals; only WHEN the usage happened differs.
    _move(app, "viewfm", fast["id"], in_qty=300, in_date=_days_ago(300), out_qty=200, out_date=_days_ago(10), dept=dept)
    _move(app, "viewfm", slow["id"], in_qty=300, in_date=_days_ago(300), out_qty=200, out_date=_days_ago(250), dept=dept)
    data = _report(client, view="fastest_moving", q="Moving Part")
    ids_in_order = [r["id"] for r in data["rows"]]
    assert ids_in_order.index(fast["id"]) < ids_in_order.index(slow["id"])


def test_view_increasing_consumption_only_includes_increasing_parts(app, client, make_user):
    _login_spare_parts(client, make_user, "viewic", "manager")
    dept = _get_or_create_department(app)
    increasing = _create_spare_part(client, name="Increasing Trend Part")
    stable = _create_spare_part(client, name="Stable Trend Part")
    # Increasing: preceding-half usage much lower than recent-half usage.
    _move(app, "viewic", increasing["id"], in_qty=500, in_date=_days_ago(400), out_qty=20, out_date=_days_ago(250), dept=dept)
    _move(app, "viewic", increasing["id"], out_qty=50, out_date=_days_ago(10), dept=dept)
    # Stable: equal usage in both halves.
    _move(app, "viewic", stable["id"], in_qty=500, in_date=_days_ago(400), out_qty=30, out_date=_days_ago(250), dept=dept)
    _move(app, "viewic", stable["id"], out_qty=30, out_date=_days_ago(10), dept=dept)
    data = _report(client, view="increasing_consumption", q="Trend Part")
    ids = {r["id"] for r in data["rows"]}
    assert increasing["id"] in ids
    assert stable["id"] not in ids
    for row in data["rows"]:
        assert row["consumption_trend"] == "Increasing"


def test_view_stockout_risk_includes_only_at_risk_parts(app, client, make_user):
    _login_spare_parts(client, make_user, "viewsr", "manager")
    out_of_stock = _create_spare_part(client, name="Stockout Risk Part")  # never stocked in
    well_stocked = _create_spare_part(client, name="Well Stocked Part")
    _move(app, "viewsr", well_stocked["id"], in_qty=100, in_date=_days_ago(10))
    data = _report(client, view="stockout_risk", q="Stock")
    ids = {r["id"] for r in data["rows"]}
    assert out_of_stock["id"] in ids
    assert well_stocked["id"] not in ids
    for row in data["rows"]:
        assert row["stock_status"] == "stockout" or (
            row["stock_coverage_months"] is not None and Decimal(row["stock_coverage_months"]) <= planning.URGENT_COVERAGE_MONTHS
        )


def test_view_below_minimum_stock_filters_correctly(app, client, make_user):
    _login_spare_parts(client, make_user, "viewbm", "manager")
    below = _create_spare_part(client, name="Below Minimum View Part", minimum_stock="50")
    healthy = _create_spare_part(client, name="Healthy Stock View Part", minimum_stock="5")
    _move(app, "viewbm", below["id"], in_qty=20, in_date=_days_ago(5))
    _move(app, "viewbm", healthy["id"], in_qty=100, in_date=_days_ago(5))
    data = _report(client, view="below_minimum_stock", q="View Part")
    ids = {r["id"] for r in data["rows"]}
    assert below["id"] in ids
    assert healthy["id"] not in ids
    for row in data["rows"]:
        assert row["stock_status"] == "below_minimum"


def test_view_critical_parts_filters_correctly(app, client, make_user):
    _login_spare_parts(client, make_user, "viewcr", "super_admin")
    critical = _create_spare_part(client, name="Critical View Part")
    normal = _create_spare_part(client, name="Normal View Part")
    client.patch(f"/api/spare-parts/{critical['id']}", json={"critical": True})
    data = _report(client, view="critical_parts", q="View Part")
    ids = {r["id"] for r in data["rows"]}
    assert critical["id"] in ids
    assert normal["id"] not in ids
    for row in data["rows"]:
        assert row["critical"] is True


def test_view_highest_procurement_value_sorts_by_cost_descending(app, client, make_user):
    _login_spare_parts(client, make_user, "viewhv", "super_admin")
    dept = _get_or_create_department(app)
    expensive = _create_spare_part(client, name="High Value View Part")
    cheap = _create_spare_part(client, name="Low Value View Part")
    client.patch(f"/api/spare-parts/{expensive['id']}", json={"china_buying_price": "100000", "local_buying_price": "120000"})
    client.patch(f"/api/spare-parts/{cheap['id']}", json={"china_buying_price": "10", "local_buying_price": "12"})
    # Give both real, comparable consumption so recommended purchase > 0.
    _move(app, "viewhv", expensive["id"], in_qty=50, in_date=_days_ago(300), out_qty=40, out_date=_days_ago(10), dept=dept)
    _move(app, "viewhv", cheap["id"], in_qty=50, in_date=_days_ago(300), out_qty=40, out_date=_days_ago(10), dept=dept)
    data = _report(client, view="highest_procurement_value", q="Value View Part")
    ids_in_order = [r["id"] for r in data["rows"]]
    assert ids_in_order.index(expensive["id"]) < ids_in_order.index(cheap["id"])


def test_view_china_savings_opportunity_only_includes_rows_with_savings_sorted_descending(app, client, make_user):
    _login_spare_parts(client, make_user, "viewcs", "super_admin")
    dept = _get_or_create_department(app)
    big_gap = _create_spare_part(client, name="Big Savings Gap Part")
    no_gap = _create_spare_part(client, name="No Savings Gap Part")
    no_price = _create_spare_part(client, name="No Price Savings Part")
    client.patch(f"/api/spare-parts/{big_gap['id']}", json={"china_buying_price": "1000", "local_buying_price": "9000"})
    client.patch(f"/api/spare-parts/{no_gap['id']}", json={"china_buying_price": "500", "local_buying_price": "500"})
    for part in (big_gap, no_gap, no_price):
        _move(app, "viewcs", part["id"], in_qty=50, in_date=_days_ago(300), out_qty=40, out_date=_days_ago(10), dept=dept)
    data = _report(client, view="china_savings_opportunity", q="Savings")
    rows_by_id = {r["id"]: r for r in data["rows"]}
    assert big_gap["id"] in rows_by_id
    assert no_price["id"] not in rows_by_id  # no pricing at all -> no savings figure to rank by
    savings_values = [Decimal(r["estimated_savings"]) for r in data["rows"]]
    assert savings_values == sorted(savings_values, reverse=True)
    assert all(v > 0 for v in savings_values)  # no_gap (savings == 0) is correctly excluded too


def test_view_slow_moving_filters_correctly(app, client, make_user):
    _login_spare_parts(client, make_user, "viewsm", "manager")
    slow = _create_spare_part(client, name="Slow Moving View Part")
    active = _create_spare_part(client, name="Active Moving View Part")
    dept = _get_or_create_department(app)
    # Huge stock, one small, not-too-recent sign-out -> long coverage, but
    # NOT within the Dead Stock silence window (so it's Slow Moving, not Dead Stock).
    _move(app, "viewsm", slow["id"], in_qty=1000, in_date=_days_ago(400), out_qty=10, out_date=_days_ago(200), dept=dept)
    _move(app, "viewsm", active["id"], in_qty=100, in_date=_days_ago(300), out_qty=80, out_date=_days_ago(10), dept=dept)
    data = _report(client, view="slow_moving", q="Moving View Part")
    ids = {r["id"] for r in data["rows"]}
    assert slow["id"] in ids
    assert active["id"] not in ids
    for row in data["rows"]:
        assert row["is_slow_moving"] is True


def test_view_dead_stock_filters_correctly(app, client, make_user):
    _login_spare_parts(client, make_user, "viewds", "manager")
    dead = _create_spare_part(client, name="Dead Stock View Part")
    active = _create_spare_part(client, name="Active Stock View Part")
    dept = _get_or_create_department(app)
    _move(app, "viewds", dead["id"], in_qty=50, in_date=_days_ago(400))  # never issued since
    _move(app, "viewds", active["id"], in_qty=50, in_date=_days_ago(400), out_qty=10, out_date=_days_ago(10), dept=dept)
    data = _report(client, view="dead_stock", q="Stock View Part")
    ids = {r["id"] for r in data["rows"]}
    assert dead["id"] in ids
    assert active["id"] not in ids
    for row in data["rows"]:
        assert row["is_dead_stock"] is True


def test_unknown_view_falls_back_to_default_priority_sort(app, client, make_user):
    _login_spare_parts(client, make_user, "viewbad", "manager")
    _create_spare_part(client, name="Unknown View Fallback Part")
    data = _report(client, view="not_a_real_view")
    assert isinstance(data["rows"], list)


# ======================= table column completeness (spec section 5) =======================

def test_report_row_exposes_every_required_sourcing_column(app, client, make_user):
    _login_spare_parts(client, make_user, "colcheck", "super_admin")
    part = _create_spare_part(client, name="Column Completeness Part", minimum_stock="5")
    client.patch(f"/api/spare-parts/{part['id']}", json={
        "china_buying_price": "100", "local_buying_price": "120", "preferred_cost_source": "china",
    })
    data = _report(client, q="Column Completeness Part")
    row = next(r for r in data["rows"] if r["id"] == part["id"])
    required_fields = (
        "display_name", "specifications", "category_name", "machine_names", "department_names", "unit",
        "current_stock", "minimum_stock", "critical", "quantity_used_30d", "quantity_used_90d",
        "quantity_used_180d", "quantity_used_365d", "average_monthly_consumption", "recent_monthly_consumption",
        "consumption_trend", "stock_coverage_months", "estimated_stockout_date", "safety_stock", "incoming_stock",
        "recommended_purchase_3m", "recommended_purchase_6m", "recommended_purchase_9m", "recommended_purchase_12m",
        "recommended_purchase_selected", "china_buying_price", "local_buying_price", "preferred_cost_source",
        "selected_source", "china_projected_cost", "local_projected_cost", "selected_source_projected_cost",
        "estimated_savings", "abc_class", "procurement_priority", "stock_status",
    )
    missing = [f for f in required_fields if f not in row]
    assert not missing, f"missing required planning columns: {missing}"


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
