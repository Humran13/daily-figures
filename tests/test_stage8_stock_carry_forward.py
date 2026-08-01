"""
Stage 8 Part 1: chronological stock carry-forward. Stock is a running
balance — Opening Stock for any later period must equal the latest prior
period's Closing Stock, even across weeks/months of no Daily Figures rows,
and even when the intervening activity (Production/Returns/Dispatch) was
entered after later dates had already been viewed.

Root cause (see webapp/services/stock_service.py's module docstring and
get_prior_closing_base_qty()): the previous implementation found the
correct "anchor" DailyFigure row but only ever used that anchor's OWN
single-day closing, silently discarding any finalized activity recorded
on dates between the anchor and the date being viewed — since a
DailyFigure row is only ever created at a product's first-ever period or
an explicit correction (never one per day), this meant Opening Stock was
almost always wrong (frozen at the anchor's own value) beyond the very
first day after that anchor.
"""
import pytest

from webapp.services import stock_service as svc


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Carry Forward Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Carry Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Carry Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "customer": customer}


def _finalize_dispatch(client, product_id, customer_id, date_str, shift, cartons, number):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": shift, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


def _finalize_production(client, product_id, date_str, shift, cartons):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return p


def _finalize_return(client, product_id, date_str, cartons):
    r = client.post("/api/returns", json={
        "date": date_str,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/returns/{r['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return r


def _opening_cartons(client, product_id, date_str, shift="Day"):
    view = client.get(f"/api/daily-figures/{product_id}?date={date_str}&shift={shift}").get_json()
    return view["opening"]["cartons"]


def _closing_cartons(client, product_id, date_str, shift="Day"):
    view = client.get(f"/api/daily-figures/{product_id}?date={date_str}&shift={shift}").get_json()
    return view["closing"]["cartons"]


# =====================================================================
# The spec's own worked regression, verbatim
# =====================================================================

def test_worked_regression_19_july_to_1_august(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-19", "Day", 20)
    _finalize_return(client, pid, "2026-07-19", 5)
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-19", "Day", 30, "CF-1")

    closing_19 = _closing_cartons(client, pid, "2026-07-19")
    assert closing_19 == 95

    # 20-31 July: no activity at all.
    opening_aug1 = _opening_cartons(client, pid, "2026-08-01")
    assert opening_aug1 == 95
    assert opening_aug1 == closing_19


# =====================================================================
# Individual source books each affect a later Opening Stock
# =====================================================================

def test_historical_production_affects_later_opening(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-05", "Day", 7)
    assert _opening_cartons(client, pid, "2026-07-20") == 17


def test_historical_returns_affect_later_opening(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_return(client, pid, "2026-07-05", 3)
    assert _opening_cartons(client, pid, "2026-07-20") == 13


def test_historical_dispatch_affects_later_opening(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-05", "Day", 4, "CF-2")
    assert _opening_cartons(client, pid, "2026-07-20") == 6


def test_combined_historical_movements_calculate_correctly(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 50, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-05", "Day", 10)
    _finalize_return(client, pid, "2026-07-10", 2)
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-15", "Day", 8, "CF-3")
    # 50 + 10 + 2 - 8 = 54
    assert _opening_cartons(client, pid, "2026-08-01") == 54


# =====================================================================
# No intervening rows, multiple no-activity days, never resets to zero
# =====================================================================

def test_later_date_with_no_intervening_rows_still_receives_balance(client, setup, app):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-06-01", "shift": "Day",
        "opening": {"cartons": 30, "packs": 0, "pieces": 0},
    })
    from webapp.models.daily_figure import DailyFigure
    rows = DailyFigure.query.filter_by(product_id=pid).all()
    assert len(rows) == 1  # only the genesis row — nothing created for the gap
    assert _opening_cartons(client, pid, "2026-09-15") == 30


def test_multiple_no_activity_days_preserve_the_same_balance(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 20, "packs": 0, "pieces": 0},
    })
    for d in ("2026-07-05", "2026-07-10", "2026-07-15", "2026-07-20", "2026-08-01"):
        assert _opening_cartons(client, pid, d) == 20


def test_later_date_does_not_reset_to_zero(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    assert _opening_cartons(client, pid, "2027-01-01") != 0
    assert _opening_cartons(client, pid, "2027-01-01") == 100


# =====================================================================
# Shift ordering — Day/Night on the same date, and across dates
# =====================================================================

def test_day_closing_carries_to_night_opening_same_date(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-10", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-10", "Day", 2, "CF-4")
    closing_day = _closing_cartons(client, pid, "2026-07-10", "Day")
    assert closing_day == 8
    assert _opening_cartons(client, pid, "2026-07-10", "Night") == 8


def test_night_production_included_before_carrying_to_next_day(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-10", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-10", "Night", 5)
    assert _opening_cartons(client, pid, "2026-07-11", "Day") == 15


def test_night_closing_carries_to_next_days_day_opening(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-10", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-10", "Night", 3)
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-10", "Day", 1, "CF-5")
    # Day: 10 - 1 = 9. Night: 9 + 3 = 12. Next Day opening = 12.
    assert _opening_cartons(client, pid, "2026-07-11", "Day") == 12


def test_day_only_source_books_remain_day_only(client, setup):
    """Returns and Dispatch never contribute to a Night period directly —
    only via the Day period's closing carrying into Night's opening."""
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-10", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_return(client, pid, "2026-07-10", 4)
    view_night = svc.daily_figure_view(_product(client, pid), "2026-07-10", "Night")
    assert view_night["return_"]["from_returns_book"] == 0  # Returns only ever attributed to Day
    assert view_night["opening"]["cartons"] == 14  # but the Day return still carried into Night's opening


def _product(client, product_id):
    from webapp.models.product import Product
    from webapp.extensions import db as _db
    return _db.session.get(Product, product_id)


def test_production_across_a_multi_day_gap_with_both_shifts(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-01", "Night", 2)
    _finalize_production(client, pid, "2026-07-02", "Day", 3)
    _finalize_production(client, pid, "2026-07-02", "Night", 4)
    _finalize_production(client, pid, "2026-07-03", "Day", 1)
    # opening for 2026-07-03 Night = 0 + 2 + 3 + 4 + 1 = 10
    assert _opening_cartons(client, pid, "2026-07-03", "Night") == 10


# =====================================================================
# Ripple-forward on historical corrections
# =====================================================================

def test_historical_correction_ripples_through_all_later_periods(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 100, "packs": 0, "pieces": 0},
    })
    assert _opening_cartons(client, pid, "2026-08-01") == 100

    # A later authorized user adds 10 cartons of historical Production.
    _finalize_production(client, pid, "2026-07-19", "Day", 10)

    assert _opening_cartons(client, pid, "2026-08-01") == 110


def test_reopening_and_refinalizing_updates_later_balances(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 50, "packs": 0, "pieces": 0},
    })
    d = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-05", "Day", 5, "CF-6")
    assert _opening_cartons(client, pid, "2026-08-01") == 45

    reopen_res = client.post(f"/api/dispatches/{d['id']}/reopen", json={"reason": "correcting quantity"})
    assert reopen_res.status_code == 200
    line_id = client.get(f"/api/dispatches/{d['id']}").get_json()["lines"][0]["id"]
    client.patch(f"/api/dispatches/{d['id']}/lines/{line_id}", json={"cartons": 2, "packs": 0, "pieces": 0})
    refinalize_res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert refinalize_res.status_code == 200

    assert _opening_cartons(client, pid, "2026-08-01") == 48  # 50 - 2, not 50 - 5


def test_voiding_a_historical_dispatch_updates_later_balances(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 50, "packs": 0, "pieces": 0},
    })
    d = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-05", "Day", 5, "CF-7")
    assert _opening_cartons(client, pid, "2026-08-01") == 45
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "cancelled"})
    assert _opening_cartons(client, pid, "2026-08-01") == 50


# =====================================================================
# Opening Stock anchors — elevated corrections
# =====================================================================

def test_elevated_opening_correction_becomes_a_new_anchor(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    # Manager corrects Opening Stock for a LATER, normally-derived period.
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["cartons"] == 500  # honored, not silently forced


def test_later_anchor_supersedes_prior_carry_forward_for_subsequent_periods(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
    })
    # A period after the new anchor derives from IT, not the original.
    assert _opening_cartons(client, pid, "2026-08-01") == 500


def test_operator_cannot_create_a_new_anchor_on_a_derived_period(client, setup, login_as):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_carry", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 999, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["cartons"] == 10  # forced to the derived value, not honored


def test_unrelated_products_remain_unchanged_by_a_correction(client, setup):
    # `setup` already logs `client` in as root — no second login_as needed.
    other = client.post("/api/admin/products", json={"name": "Unrelated Product"}).get_json()
    client.post(f"/api/admin/products/{other['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": other["id"], "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 20, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-05", "Day", 100)
    assert _opening_cartons(client, other["id"], "2026-08-01") == 20  # untouched


def test_unrelated_dates_before_a_correction_remain_unchanged(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    before_closing = _closing_cartons(client, pid, "2026-07-05")
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
    })
    after_closing = _closing_cartons(client, pid, "2026-07-05")
    assert before_closing == after_closing == 10


# =====================================================================
# No duplicate rows, no fake movement rows
# =====================================================================

def test_no_duplicate_daily_figure_rows_created_by_repeated_views(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    for _ in range(5):
        client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day")

    from webapp.models.daily_figure import DailyFigure
    rows = DailyFigure.query.filter_by(product_id=pid).all()
    assert len(rows) == 1


def test_no_activity_today_creates_no_fake_movement_row(client, setup, login_as):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_na", "password123", "operator")
    res = client.post("/api/daily-entry-status/no-activity", json={
        "product_id": pid, "date": "2026-07-10", "shift": "Day",
    })
    assert res.status_code == 200

    from webapp.models.daily_figure import DailyFigure
    from webapp.models.dispatch import Dispatch
    from webapp.models.return_record import ReturnRecord
    from webapp.models.production_record import ProductionRecord
    assert DailyFigure.query.filter_by(product_id=pid, date="2026-07-10").count() == 0
    assert Dispatch.query.filter_by(date="2026-07-10").count() == 0
    assert ReturnRecord.query.filter_by(date="2026-07-10").count() == 0
    assert ProductionRecord.query.filter_by(date="2026-07-10").count() == 0

    assert _opening_cartons(client, pid, "2026-08-01") == 10  # preserved through the no-activity day


# =====================================================================
# Super-Admin reset — scoped, never touches source-derived values
# =====================================================================

def test_reset_affects_only_the_selected_scope(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-reset", json={
        "date": "2026-07-15", "shift": "Day", "product_id": pid, "reason": "correction mistake",
    })
    # The 07-15 anchor is now 0 — later periods derive from IT (07-01's
    # anchor is untouched but no longer the nearest one).
    assert _opening_cartons(client, pid, "2026-08-01") == 0
    assert _closing_cartons(client, pid, "2026-07-05") == 10  # before the reset date, untouched


def test_reset_never_touches_finalized_source_totals(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-01", "Day", 7)
    client.post("/api/daily-reset", json={"date": "2026-07-01", "shift": "Day", "product_id": pid, "reason": "test"})
    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-01&shift=Day").get_json()
    assert view["production"]["cartons"] == 7  # source-derived, survives the reset intact


# =====================================================================
# Integer arithmetic, packaging notation, Napkin formatting
# =====================================================================

def test_carried_balance_is_exact_integer_base_units(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 1, "packs": 3, "pieces": 7},
    })
    _finalize_production(client, pid, "2026-07-05", "Day", 2)
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert isinstance(view["opening"]["base_qty"], int)
    assert view["opening"]["base_qty"] == 337  # (1*100 + 3*10 + 7) + 2*100 production


def test_napkin_carry_forward_uses_exact_mixed_radix_notation(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Carry Napkin"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 6, "packs_to_pieces": 10,
    })
    pid = product["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 1, "packs": 2, "pieces": 4},
    })
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view["opening"]["cartons"] == 1
    assert view["opening"]["packs"] == 2
    assert view["opening"]["pieces"] == 4
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["opening"]["cartons"], view["opening"]["packs"], view["opening"]["pieces"], view["packaging_rule"]) == "1.24 Ctns"


# =====================================================================
# Negative-stock policy — preserved, never clamped
# =====================================================================

def test_negative_carried_balance_is_not_clamped_to_zero(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-05", "Day", 5, "CF-8")
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view["opening"]["base_qty"] == -400  # 100 - 500, never clamped to 0
    assert view["closing"].get("warning") == "negative — check entries"


# =====================================================================
# Persistence across an application restart
# =====================================================================

def test_balance_survives_a_simulated_app_restart(app, setup, client):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 40, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-05", "Day", 6)

    # A brand-new app_context (simulating a fresh process attaching to the
    # same DB_PATH) must derive the identical balance purely from what's
    # persisted — nothing is held in memory between requests already, but
    # this confirms there's no in-process cache masking that.
    with app.app_context():
        from webapp.models.product import Product
        from webapp.extensions import db as _db
        product = _db.session.get(Product, pid)
        opening = svc.get_prior_closing_base_qty(pid, "2026-08-01", "Day")
        assert opening == 4600  # (40+6)*100
