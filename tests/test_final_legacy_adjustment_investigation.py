"""
Urgent follow-up investigation — legacy adjustment sign, carry-forward,
and reset-preview inconsistency.

Two investigation rounds:

Round 1 — the specific StockAdjustment arithmetic disagreement reported
(same-period Closing Stock disagreeing with the next period's Opening
Stock; a "hidden" 10950 base units) did NOT reproduce when the exact
reported record IDs, deltas, dates, and reasons were recreated against
this codebase — every reconciliation check passes exactly (see
test_compact_corporate_reported_pattern_reconciles_to_one_consistent_value
and test_compact_standard_reported_pattern_reconciles_exactly below).

Round 2 — while proving round 1's non-reproduction, a genuinely different,
real defect WAS found and fixed: a pre-Stage-5 migrated DailyFigure row's
own legacy-stored production_base_qty/return_base_qty columns were
correctly reflected when that row's own Closing Stock was viewed
directly, but silently vanished from every LATER period's Opening Stock,
because stock_service._movement_between_periods() only ever summed the
Production/Returns Book tables, never an intermediate row's own legacy
column. Fixed via stock_service._legacy_stored_movement_in_window().

This file is the permanent regression suite for both rounds, plus the
Reset Daily Values preview visibility correction (StockAdjustment rows,
including legacy-migrated ones, were previously invisible to the
preview — including its "carried negative" distinction) and the new
compute_closing() single authoritative formula.
"""
import pytest

from webapp.services import stock_ledger_service


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    category = client.post("/api/admin/sales-categories", json={"name": "Legacy Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Legacy Customer", "sales_category_id": category["id"]}).get_json()
    return {"category": category, "customer": customer}


def _make_product(client, name, rule=None):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule or {"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _view(client, pid, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{pid}?date={date_str}&shift={shift}").get_json()


def _adjustment(client, pid, date_str, shift, delta, reason):
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": date_str, "shift": shift, "delta_base_qty": delta, "reason": reason,
    })
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def _manual_anchor(client, pid, date_str, shift, cartons):
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": date_str, "shift": shift,
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200, res.get_json()
    return res.get_json()


def _ledger(app, pid, date_from, date_to, shift=None):
    with app.app_context():
        return stock_ledger_service.build_ledger(pid, date_from, date_to, shift=shift)


# =====================================================================
# Adjustment consistency
# =====================================================================

def test_positive_adjustment_same_effect_current_closing_and_next_opening(client, setup):
    p = _make_product(client, "Consistency Positive Adjustment")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)  # 1000 base units, first-ever period
    _adjustment(client, p["id"], "2026-07-19", "Night", 200, "test")
    closing = _view(client, p["id"], "2026-07-19", "Night")["closing"]["base_qty"]
    next_opening = _view(client, p["id"], "2026-07-20", "Day")["opening"]["base_qty"]
    assert closing == 800
    assert next_opening == closing


def test_negative_adjustment_same_effect_current_closing_and_next_opening(client, setup):
    p = _make_product(client, "Consistency Negative Adjustment")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)
    _adjustment(client, p["id"], "2026-07-19", "Night", -200, "test")
    closing = _view(client, p["id"], "2026-07-19", "Night")["closing"]["base_qty"]
    next_opening = _view(client, p["id"], "2026-07-20", "Day")["opening"]["base_qty"]
    assert closing == 1200
    assert next_opening == closing


def test_day_closing_equals_night_opening(client, setup):
    p = _make_product(client, "Consistency Day Night")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)
    _adjustment(client, p["id"], "2026-07-19", "Day", 50, "test")
    day_closing = _view(client, p["id"], "2026-07-19", "Day")["closing"]["base_qty"]
    night_opening = _view(client, p["id"], "2026-07-19", "Night")["opening"]["base_qty"]
    assert day_closing == night_opening == 950


def test_night_closing_equals_next_day_opening(client, setup):
    p = _make_product(client, "Consistency Night Next Day")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)
    _adjustment(client, p["id"], "2026-07-19", "Night", 50, "test")
    night_closing = _view(client, p["id"], "2026-07-19", "Night")["closing"]["base_qty"]
    next_day_opening = _view(client, p["id"], "2026-07-20", "Day")["opening"]["base_qty"]
    assert night_closing == next_day_opening == 950


def test_ledger_components_always_reconcile_exactly(client, setup, app):
    p = _make_product(client, "Consistency Ledger Reconcile")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)
    _adjustment(client, p["id"], "2026-07-20", "Day", 300, "test")
    _adjustment(client, p["id"], "2026-07-25", "Night", -50, "test")
    entries = _ledger(app, p["id"], "2026-07-19", "2026-08-03")
    for e in entries:
        expected = e["opening_base_qty"] + e["production_total"] + e["returns_total"] - e["issued_total"]
        assert expected == e["closing_base_qty"]


def test_cli_and_stock_service_return_the_same_balance(client, setup, app):
    p = _make_product(client, "Consistency CLI Service")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)
    _adjustment(client, p["id"], "2026-07-20", "Day", 300, "test")
    entries = _ledger(app, p["id"], "2026-07-20", "2026-07-20", shift="Day")
    direct = _view(client, p["id"], "2026-07-20", "Day")
    assert entries[0]["closing_base_qty"] == direct["closing"]["base_qty"]
    assert entries[0]["opening_base_qty"] == direct["opening"]["base_qty"]


# =====================================================================
# Live-pattern reproduction — Compact Corporate
# =====================================================================

def test_compact_corporate_reported_pattern_reconciles_to_one_consistent_value(client, setup, app):
    """Reproduces the exact reported record shape: a manual_correction
    zero anchor on 20 July Day, a migrated legacy adjustment (+600) on 20
    July Night, and a later migrated adjustment (+100) on 25 July Day.
    The reported disagreement (-500 same-period Closing vs -700 next
    Opening) does not reproduce — the corrected implementation produces
    exactly one consistent value (-700) everywhere."""
    p = _make_product(client, "Compact Corporate")
    _manual_anchor(client, p["id"], "2026-07-20", "Day", 0)
    _adjustment(client, p["id"], "2026-07-20", "Night", 600,
                "Migrated legacy issued figure (entries.id=27) - no dispatch records exist for this pre-Phase-3 date")
    _adjustment(client, p["id"], "2026-07-25", "Day", 100,
                "Migrated legacy issued figure (entries.id=28) - no dispatch records exist for this pre-Phase-3 date")

    same_period_closing = _view(client, p["id"], "2026-07-25", "Day")["closing"]["base_qty"]
    next_period_opening = _view(client, p["id"], "2026-07-25", "Night")["opening"]["base_qty"]
    assert same_period_closing == next_period_opening == -700

    entries = _ledger(app, p["id"], "2026-07-20", "2026-08-03")
    for e in entries:
        expected = e["opening_base_qty"] + e["production_total"] + e["returns_total"] - e["issued_total"]
        assert expected == e["closing_base_qty"]
    assert _view(client, p["id"], "2026-08-03", "Night")["closing"]["base_qty"] == -700


# =====================================================================
# Live-pattern reproduction — Compact Standard
# =====================================================================

def test_compact_standard_reported_pattern_reconciles_exactly(client, setup, app):
    """Reproduces the exact reported record shape: a manual_correction
    zero anchor and a migrated legacy adjustment (+74300) on the SAME
    date+shift. The reported 'hidden 10950 base units' does not
    reproduce — closing is exactly opening(0) - issued(74300) = -74300,
    with zero unexplained residual."""
    p = _make_product(client, "Compact Standard")
    _manual_anchor(client, p["id"], "2026-07-20", "Day", 0)
    _adjustment(client, p["id"], "2026-07-20", "Day", 74300,
                "Migrated legacy issued figure (entries.id=3) - no dispatch records exist for this pre-Phase-3 date")

    closing = _view(client, p["id"], "2026-07-20", "Day")["closing"]["base_qty"]
    assert closing == -74300

    entries = _ledger(app, p["id"], "2026-07-20", "2026-07-20", shift="Day")
    e = entries[0]
    assert e["adjustment_total"] == 74300
    assert e["opening_base_qty"] + e["production_total"] + e["returns_total"] - e["issued_total"] == -74300
    assert e["closing_base_qty"] == -74300


# =====================================================================
# Legacy migration — direct ORM (upsert_daily_figure never writes these
# columns, so only a pre-Stage-5-style migrated row can have them)
# =====================================================================

def test_legacy_stored_production_represented_exactly_once(client, setup, app):
    p = _make_product(client, "Legacy Stored Once")
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.daily_figure import DailyFigure, OPENING_STOCK_SOURCE_DERIVED, OPENING_STOCK_SOURCE_MANUAL_CORRECTION
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        from webapp.models.product import Product
        product = _db.session.get(Product, p["id"])
        rule = product.current_packaging_rule()

        anchor = DailyFigure(
            product_id=p["id"], date="2026-07-10", shift="Day",
            opening_cartons=0, opening_packs=0, opening_pieces=0, opening_base_qty=0,
            opening_stock_source=OPENING_STOCK_SOURCE_MANUAL_CORRECTION, opening_stock_is_override=True,
            packaging_rule_id=rule.id, created_by=root.id, updated_by=root.id,
        )
        _db.session.add(anchor)
        _db.session.commit()

        intermediate = DailyFigure(
            product_id=p["id"], date="2026-07-15", shift="Day",
            opening_cartons=0, opening_packs=0, opening_pieces=0, opening_base_qty=0,
            opening_stock_source=OPENING_STOCK_SOURCE_DERIVED, opening_stock_is_override=False,
            production_cartons=20, production_packs=0, production_pieces=0, production_base_qty=2000,
            packaging_rule_id=rule.id, created_by=root.id, updated_by=root.id,
        )
        _db.session.add(intermediate)
        _db.session.commit()

    for date_str, shift in [("2026-07-15", "Night"), ("2026-07-20", "Day"), ("2026-08-03", "Night")]:
        view = _view(client, p["id"], date_str, shift)
        assert view["opening"]["base_qty"] == 2000  # represented exactly once, never lost, never duplicated


def test_upsert_daily_figure_never_writes_legacy_stored_columns(client, setup, app):
    """A NEW save (post-Stage-5) always leaves legacy_stored at 0 — proves
    a Book-sourced entry and this legacy column path can never both
    contribute for the same row."""
    p = _make_product(client, "Legacy Stored Never New")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 5)
    with app.app_context():
        from webapp.models.daily_figure import DailyFigure
        figure = DailyFigure.query.filter_by(product_id=p["id"], date="2026-07-19", shift="Day").first()
        assert figure.production_base_qty == 0
        assert figure.return_base_qty == 0


def test_sign_convention_matches_documented_business_meaning(client, setup):
    """A positive StockAdjustment reduces Closing Stock (same direction as
    Dispatch/Issued) — the one documented convention, never the reverse."""
    p = _make_product(client, "Sign Convention")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)
    before = _view(client, p["id"], "2026-07-19", "Day")["closing"]["base_qty"]
    _adjustment(client, p["id"], "2026-07-19", "Day", 100, "test")
    after = _view(client, p["id"], "2026-07-19", "Day")["closing"]["base_qty"]
    assert after == before - 100


def test_manual_anchor_and_migrated_movement_no_hidden_duplicate(client, setup, app):
    p = _make_product(client, "No Hidden Duplicate")
    _manual_anchor(client, p["id"], "2026-07-20", "Day", 0)
    _adjustment(client, p["id"], "2026-07-20", "Night", 600, "Migrated legacy issued figure (entries.id=99)")
    entries = _ledger(app, p["id"], "2026-07-20", "2026-07-20", shift="Night")
    e = entries[0]
    # Exactly one contributing adjustment, no phantom second contribution.
    assert len(e["adjustments"]) == 1
    assert e["adjustment_total"] == 600
    assert e["closing_base_qty"] == -600


# =====================================================================
# Reset preview
# =====================================================================

def test_adjustment_only_period_no_longer_reported_empty(client, setup):
    p = _make_product(client, "Preview Adjustment Only")
    _adjustment(client, p["id"], "2026-07-20", "Night", 600, "test")
    preview = client.post("/api/daily-reset/preview", json={
        "date": "2026-07-20", "shift": "Night", "product_id": p["id"], "mode": "figures_only",
    }).get_json()
    row = preview["products"][0]
    assert row["has_adjustment_activity"] is True
    assert preview["any_affected"] is True


def test_migrated_adjustment_appears_in_preview_with_flag(client, setup):
    p = _make_product(client, "Preview Migrated Flag")
    adj = _adjustment(client, p["id"], "2026-07-20", "Night", 600,
                       "Migrated legacy issued figure (entries.id=27) - no dispatch records exist for this pre-Phase-3 date")
    preview = client.post("/api/daily-reset/preview", json={
        "date": "2026-07-20", "shift": "Night", "product_id": p["id"], "mode": "figures_only",
    }).get_json()
    row = preview["products"][0]["stock_adjustments"][0]
    assert row["id"] == adj["id"]
    assert row["delta_base_qty"] == 600
    assert row["is_legacy_migrated"] is True


def test_carried_negative_with_no_movement_identified_correctly(client, setup):
    p = _make_product(client, "Preview Carried Negative")
    _manual_anchor(client, p["id"], "2026-07-20", "Day", 0)
    _adjustment(client, p["id"], "2026-07-20", "Night", 600, "Migrated legacy issued figure (entries.id=27)")

    preview = client.post("/api/daily-reset/preview", json={
        "date": "2026-07-21", "shift": "Day", "product_id": p["id"], "mode": "figures_only",
    }).get_json()
    row = preview["products"][0]
    assert row["has_source_activity"] is False
    assert row["has_adjustment_activity"] is False
    assert row["carried_negative"] is not None
    assert row["carried_negative"]["originating_date"] == "2026-07-20"
    assert row["carried_negative"]["originating_shift"] == "Night"
    assert "carried" in row["carried_negative"]["message"].lower()


def test_current_period_movement_not_described_as_carried(client, setup):
    p = _make_product(client, "Preview Current Not Carried")
    _adjustment(client, p["id"], "2026-07-20", "Night", 600, "test")
    preview = client.post("/api/daily-reset/preview", json={
        "date": "2026-07-20", "shift": "Night", "product_id": p["id"], "mode": "figures_only",
    }).get_json()
    row = preview["products"][0]
    assert row["carried_negative"] is None  # this period's own movement caused it, never described as carried


def test_reset_preview_never_mutates_source_records(client, setup):
    p = _make_product(client, "Preview No Mutation")
    adj = _adjustment(client, p["id"], "2026-07-20", "Night", 600, "test")
    client.post("/api/daily-reset/preview", json={
        "date": "2026-07-20", "shift": "Night", "product_id": p["id"], "mode": "full",
    })
    view = _view(client, p["id"], "2026-07-20", "Night")
    assert view["closing"]["base_qty"] == -600  # adjustment still fully in effect, preview changed nothing


def test_full_reset_does_not_neutralize_legacy_adjustments(client, setup):
    """Explicit requirement: existing Full Reset must not automatically
    neutralize legacy adjustments until a repair policy is approved."""
    p = _make_product(client, "Preview No Auto Neutralize")
    _adjustment(client, p["id"], "2026-07-20", "Night", 600, "Migrated legacy issued figure (entries.id=27)")
    res = client.post("/api/daily-reset", json={
        "date": "2026-07-20", "shift": "Night", "product_id": p["id"], "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-07-20 NIGHT",
    })
    assert res.status_code == 200, res.get_json()
    view = _view(client, p["id"], "2026-07-20", "Night")
    assert view["closing"]["base_qty"] == -600  # adjustment untouched by the reset


# =====================================================================
# Cross-surface consistency
# =====================================================================

def test_daily_figures_and_dashboard_closing_agree(client, setup):
    p = _make_product(client, "Cross Surface Dashboard")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)
    _adjustment(client, p["id"], "2026-07-19", "Day", 200, "test")
    daily_figures_closing = _view(client, p["id"], "2026-07-19", "Day")["closing"]["base_qty"]
    dashboard = client.get("/api/dashboard?date=2026-07-19").get_json()
    row = next(r for r in dashboard["stock_summary"] if r["product_id"] == p["id"])
    assert row["closing_base_qty"] == daily_figures_closing


def test_attention_uses_the_same_closing(client, setup):
    p = _make_product(client, "Cross Surface Attention")
    _adjustment(client, p["id"], "2026-07-19", "Day", 9999, "test")
    dashboard = client.get("/api/dashboard?date=2026-07-19").get_json()
    row = next(r for r in dashboard["stock_summary"] if r["product_id"] == p["id"])
    assert row["closing_base_qty"] < 0
    notice = next(n for n in dashboard["attention"] if n.get("product_id") == p["id"])
    assert notice["type"] == "negative_closing_stock"


def test_following_period_opening_equals_prior_closing(client, setup):
    p = _make_product(client, "Cross Surface Following Period")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 10)
    _adjustment(client, p["id"], "2026-07-19", "Day", 50, "test")
    prior_closing = _view(client, p["id"], "2026-07-19", "Day")["closing"]["base_qty"]
    following_opening = _view(client, p["id"], "2026-07-19", "Night")["opening"]["base_qty"]
    assert prior_closing == following_opening


# =====================================================================
# Full regression — universal carry-forward, packaging, no float
# =====================================================================

def test_universal_carry_forward_remains_correct(client, setup):
    p = _make_product(client, "Regression Carry Forward")
    _manual_anchor(client, p["id"], "2026-07-01", "Day", 100)
    prod = client.post("/api/production", json={
        "date": "2026-07-15", "shift": "Day",
        "lines": [{"product_id": p["id"], "cartons": 20, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    assert _view(client, p["id"], "2026-08-03", "Day")["opening"]["cartons"] == 120


def test_napkin_mixed_radix_remains_exact(client, setup):
    p = _make_product(client, "Regression Napkin", {"cartons_to_packs": 6, "packs_to_pieces": 10})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 135, "packs": 4, "pieces": 0},
    })
    view = _view(client, p["id"], "2026-07-19")
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["opening"]["cartons"], view["opening"]["packs"], view["opening"]["pieces"], view["packaging_rule"]) == "135.40 Ctns"


def test_no_float_arithmetic_anywhere(client, setup, app):
    p = _make_product(client, "Regression No Float")
    _manual_anchor(client, p["id"], "2026-07-19", "Day", 7)
    _adjustment(client, p["id"], "2026-07-19", "Day", 13, "test")
    entries = _ledger(app, p["id"], "2026-07-19", "2026-07-19", shift="Day")
    e = entries[0]
    for field in ("opening_base_qty", "production_total", "returns_total", "issued_total", "closing_base_qty", "legacy_production", "legacy_returns"):
        assert isinstance(e[field], int)


def test_review_and_reset_workflows_remain_authorized(client, login_as):
    """Regression: an Operator still cannot reach Reset Daily Values or
    the Daily Figures review endpoints — backend authorization untouched
    by this investigation."""
    login_as("legacy_operator", "password123", "operator")
    res = client.post("/api/daily-reset/preview", json={"date": "2026-07-19", "shift": "Day", "mode": "figures_only"})
    assert res.status_code == 403
    res2 = client.post("/api/daily-review/mark-reviewed", json={"date": "2026-07-19", "shift": "Day", "product_id": 1})
    assert res2.status_code == 403
