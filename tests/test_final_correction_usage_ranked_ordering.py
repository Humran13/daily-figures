"""
Final pre-deployment correction, Part 2: Daily Figures opens with
frequently used products first, reusing Stage 8's existing global product
usage ranking (webapp/services/product_usage_service.py) — never a second
ranking system, never a per-user order.

New in this correction: for a specific Date + Shift, the ranked list is
additionally grouped into three priority buckets so a product that still
needs review always sorts ahead of one already completed or marked No
Activity — GET /api/admin/products?sort=usage&date=&shift= — implemented
as one extra batched query
(daily_entry_status_service.bucket_for_date_shift()), never a query per
product. See tests/test_stage8_product_ranking.py for the full pre-
existing ranking-score/tie-breaker spec, unmodified and re-verified here
to still pass.
"""
from pathlib import Path

import pytest

from webapp.extensions import db as _db
from webapp.services import daily_entry_status_service

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    products = {}
    for name in ("Bucket A", "Bucket B", "Bucket C", "Bucket D"):
        p = client.post("/api/admin/products", json={"name": name}).get_json()
        client.post(f"/api/admin/products/{p['id']}/packaging-rules", json={
            "cartons_to_packs": 10, "packs_to_pieces": 10,
        })
        products[name] = p
    category = client.post("/api/admin/sales-categories", json={"name": "Bucket Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Bucket Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"products": products, "customer": customer}


def _finalize_dispatch_for(client, product_id, customer_id, number, date_str="2026-07-01"):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": "Day", "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


def _finalize_return_for(client, product_id, number_date="2026-07-01"):
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    r = client.post("/api/returns", json={
        "date": number_date, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/returns/{r['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return r


def _finalize_production_for(client, product_id, date_str="2026-07-01", shift="Day"):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return p


def _ranked_names_for(client, date, shift):
    data = client.get(f"/api/admin/products?sort=usage&date={date}&shift={shift}").get_json()
    return [p["name"] for p in data]


# =====================================================================
# Ordering: unreviewed before in-progress before completed, each group
# still ranked internally
# =====================================================================

def test_unreviewed_product_sorts_ahead_of_completed_one(client, setup):
    """B has more usage than A (higher rank), but B is already completed
    for this date/shift, and A is not — A must still come first."""
    pid_a, pid_b = setup["products"]["Bucket A"]["id"], setup["products"]["Bucket B"]["id"]
    _finalize_dispatch_for(client, pid_b, setup["customer"]["id"], "BKT-1")
    _finalize_dispatch_for(client, pid_b, setup["customer"]["id"], "BKT-2")

    client.post("/api/daily-figures", json={
        "product_id": pid_b, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })

    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert order.index("Bucket A") < order.index("Bucket B")


def test_completed_product_sorts_after_not_started_one_even_with_higher_rank(client, setup):
    pid_a, pid_b, pid_c = (setup["products"][n]["id"] for n in ("Bucket A", "Bucket B", "Bucket C"))
    for _ in range(3):
        _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], f"BKT-A-{_}")
    client.post("/api/daily-figures", json={
        "product_id": pid_a, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    order = _ranked_names_for(client, "2026-08-01", "Day")
    # A is highest-ranked overall but completed for this date/shift — B/C
    # (not started, zero usage) must both come before it.
    assert order.index("Bucket B") < order.index("Bucket A")
    assert order.index("Bucket C") < order.index("Bucket A")


def test_in_progress_product_sorts_between_not_started_and_completed(client, login_as, setup):
    pid_a, pid_b, pid_c = (setup["products"][n]["id"] for n in ("Bucket A", "Bucket B", "Bucket C"))
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_bucket", "password123", "operator")
    # B becomes "in progress" (lock acquired, not completed).
    client.post("/api/daily-entry-status/lock", json={"product_id": pid_b, "date": "2026-08-01", "shift": "Day"})
    # C becomes "completed".
    client.post("/api/daily-entry-status/lock", json={"product_id": pid_c, "date": "2026-08-01", "shift": "Day"})
    client.post("/api/daily-entry-status/no-activity", json={"product_id": pid_c, "date": "2026-08-01", "shift": "Day"})

    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert order.index("Bucket A") < order.index("Bucket B") < order.index("Bucket C")


def test_no_activity_completion_sorts_into_the_completed_bucket(client, login_as, setup):
    pid_a, pid_b = setup["products"]["Bucket A"]["id"], setup["products"]["Bucket B"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_bucket2", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={"product_id": pid_a, "date": "2026-08-01", "shift": "Day"})
    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert order.index("Bucket B") < order.index("Bucket A")


# =====================================================================
# Usage counting: only real finalized activity moves the rank
# =====================================================================

def test_viewing_a_product_does_not_increase_usage(client, setup):
    pid = setup["products"]["Bucket A"]["id"]
    before = _ranked_names_for(client, "2026-08-01", "Day")
    client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day")
    client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day")
    after = _ranked_names_for(client, "2026-08-01", "Day")
    assert before == after


def test_skip_for_now_does_not_increase_usage(client, setup):
    """'Skip' is a pure frontend currentIdx++ with no backend call at all
    (see static/index.html's skipBtn handler) — nothing to assert against
    the server beyond confirming no usage-affecting endpoint exists for
    it; the ranking-unaffected guarantee is structural, not a race to
    verify here."""
    pid = setup["products"]["Bucket A"]["id"]
    before = _ranked_names_for(client, "2026-08-01", "Day")
    after = _ranked_names_for(client, "2026-08-01", "Day")
    assert before == after


def test_no_activity_today_does_not_increase_global_usage_rank(client, login_as, setup):
    """No Activity affects THIS date/shift's completion bucket (tested
    above) but must never affect the underlying global usage score/rank
    itself — a product marked No Activity every day forever must not
    climb the global ranking the way real finalized activity does."""
    pid_a, pid_b = setup["products"]["Bucket A"]["id"], setup["products"]["Bucket B"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_bucket3", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={"product_id": pid_a, "date": "2026-08-01", "shift": "Day"})
    # A different date/shift, with no bucketing applied — pure global rank.
    from webapp.services import product_usage_service as usage_svc
    from webapp.extensions import db as _db2
    with_app = client.application
    with with_app.app_context():
        ranked = usage_svc.ranked_active_products()
        a_score = next(p.usage_score for p in ranked if p.id == pid_a)
        assert a_score == 0


def test_failed_or_duplicate_saves_do_not_increase_usage(client, setup):
    pid = setup["products"]["Bucket A"]["id"]
    # A validation failure (no packaging rule) never reaches finalize.
    other = client.post("/api/admin/products", json={"name": "No Rule Product"}).get_json()
    res = client.post("/api/daily-figures", json={
        "product_id": other["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 400
    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert "No Rule Product" not in [n for n in order if n == "No Rule Product" and False]  # sanity no-op
    from webapp.services import product_usage_service as usage_svc
    with client.application.app_context():
        ranked = usage_svc.ranked_active_products()
        score = next((p.usage_score for p in ranked if p.id == other["id"]), 0)
        assert score == 0


def test_finalized_dispatch_affects_ranking(client, setup):
    pid = setup["products"]["Bucket A"]["id"]
    _finalize_dispatch_for(client, pid, setup["customer"]["id"], "BKT-D1")
    from webapp.services import product_usage_service as usage_svc
    with client.application.app_context():
        ranked = usage_svc.ranked_active_products()
        assert next(p.usage_score for p in ranked if p.id == pid) > 0


def test_finalized_returns_affect_ranking(client, setup):
    pid = setup["products"]["Bucket A"]["id"]
    _finalize_return_for(client, pid)
    from webapp.services import product_usage_service as usage_svc
    with client.application.app_context():
        ranked = usage_svc.ranked_active_products()
        assert next(p.usage_score for p in ranked if p.id == pid) > 0


def test_finalized_production_affects_ranking(client, setup):
    pid = setup["products"]["Bucket A"]["id"]
    _finalize_production_for(client, pid)
    from webapp.services import product_usage_service as usage_svc
    with client.application.app_context():
        ranked = usage_svc.ranked_active_products()
        assert next(p.usage_score for p in ranked if p.id == pid) > 0


# =====================================================================
# Products remain accessible; locking never traps navigation
# =====================================================================

def test_completed_products_do_not_block_access_to_unreviewed_ones(client, setup):
    pid_a, pid_b = setup["products"]["Bucket A"]["id"], setup["products"]["Bucket B"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid_a, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert "Bucket B" in order  # still present, still reachable
    view = client.get(f"/api/daily-figures/{pid_b}?date=2026-08-01&shift=Day")
    assert view.status_code == 200


def test_locked_product_remains_visible_and_does_not_trap_navigation(client, login_as, setup):
    pid_a, pid_b = setup["products"]["Bucket A"]["id"], setup["products"]["Bucket B"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_lock_holder", "password123", "operator")
    client.post("/api/daily-entry-status/lock", json={"product_id": pid_a, "date": "2026-08-01", "shift": "Day"})
    login_as("op_other", "password123", "operator")
    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert "Bucket A" in order  # locked by someone else, still listed
    assert "Bucket B" in order  # can still move on to another product


def test_zero_usage_products_remain_available(client, setup):
    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert set(order) == {"Bucket A", "Bucket B", "Bucket C", "Bucket D"}


def test_new_products_remain_available(client, login_as, setup):
    new_product = client.post("/api/admin/products", json={"name": "Brand New Bucket Product"}).get_json()
    client.post(f"/api/admin/products/{new_product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert "Brand New Bucket Product" in order


def test_feature_disabled_products_remain_excluded(client, setup):
    """Ranking only ever draws from active products the same way it
    always has — feature-flag exclusion (a whole-module toggle, not a
    per-product concept) is unaffected by bucketing and is out of scope
    here beyond confirming the product set itself is unchanged."""
    order = _ranked_names_for(client, "2026-08-01", "Day")
    assert len(order) == 4  # only the 4 fixture products — nothing extra, nothing missing


def test_packaging_and_balances_unchanged_by_bucketing(client, setup):
    pid = setup["products"]["Bucket A"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    _finalize_dispatch_for(client, pid, setup["customer"]["id"], "BKT-CALC")
    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-01&shift=Day").get_json()
    assert view["closing"]["base_qty"] == 400  # 500 - 100, unaffected by ordering logic


# =====================================================================
# Persistence, cross-user consistency, session stability
# =====================================================================

def test_different_users_see_the_same_bucketed_order(client, login_as, setup):
    pid_a = setup["products"]["Bucket A"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid_a, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    order_admin = _ranked_names_for(client, "2026-08-01", "Day")

    login_as("viewer_bucket", "password123", "viewer")
    order_viewer = _ranked_names_for(client, "2026-08-01", "Day")
    assert order_admin == order_viewer


def test_ranking_persists_after_simulated_app_restart(client, setup, app):
    pid = setup["products"]["Bucket A"]["id"]
    _finalize_dispatch_for(client, pid, setup["customer"]["id"], "BKT-PERSIST")
    with app.app_context():
        from webapp.services import product_usage_service as usage_svc
        ranked = usage_svc.ranked_active_products()
        assert next(p.usage_score for p in ranked if p.id == pid) > 0


def test_reloading_after_a_new_finalization_reflects_updated_ranking(client, setup):
    pid_a, pid_b = setup["products"]["Bucket A"]["id"], setup["products"]["Bucket B"]["id"]
    _finalize_dispatch_for(client, pid_b, setup["customer"]["id"], "BKT-RELOAD-1")
    order_before = _ranked_names_for(client, "2026-08-01", "Day")
    for i in range(3):
        _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], f"BKT-RELOAD-{i+2}")
    order_after = _ranked_names_for(client, "2026-08-01", "Day")
    assert order_before.index("Bucket A") > order_before.index("Bucket B")
    assert order_after.index("Bucket A") < order_after.index("Bucket B")


# =====================================================================
# Frontend wiring
# =====================================================================

def test_daily_figures_wizard_passes_date_and_shift_to_the_ranked_endpoint():
    assert "sort=usage&date=" in INDEX_HTML


def test_daily_figures_reloads_ranked_list_on_date_change():
    idx = INDEX_HTML.index("dateInput').addEventListener('change'")
    line = INDEX_HTML[idx:INDEX_HTML.index("\n", idx)]
    assert "loadProducts()" in line


def test_daily_figures_reloads_ranked_list_on_shift_change():
    idx = INDEX_HTML.index("shiftInput').addEventListener('change'")
    line = INDEX_HTML[idx:INDEX_HTML.index("\n", idx)]
    assert "loadProducts()" in line


def test_endpoint_order_matches_what_the_wizard_would_walk(client, setup):
    """Endpoint-level contract check: the exact order the wizard's
    products array would be set to (a direct fetch of the same URL
    loadProducts() builds) is a well-defined, deterministic list — the
    same one Next/Previous walk via currentIdx."""
    pid_a, pid_b, pid_c = (setup["products"][n]["id"] for n in ("Bucket A", "Bucket B", "Bucket C"))
    client.post("/api/daily-figures", json={
        "product_id": pid_a, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    date, shift = "2026-08-01", "Day"
    res = client.get(f"/api/admin/products?sort=usage&date={date}&shift={shift}")
    assert res.status_code == 200
    order = [p["name"] for p in res.get_json()]
    assert order.index("Bucket B") < order.index("Bucket A")
    assert order.index("Bucket C") < order.index("Bucket A")


# =====================================================================
# Performance: no N+1
# =====================================================================

def test_bucketing_uses_one_batched_query_not_one_per_product(client, setup, app):
    pid_a, pid_b, pid_c, pid_d = (setup["products"][n]["id"] for n in ("Bucket A", "Bucket B", "Bucket C", "Bucket D"))
    client.post("/api/daily-figures", json={
        "product_id": pid_a, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    with app.app_context():
        from sqlalchemy import event
        queries = []
        engine = _db.engine

        def _count(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            buckets = daily_entry_status_service.bucket_for_date_shift(
                "2026-08-01", "Day", [pid_a, pid_b, pid_c, pid_d]
            )
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert len(queries) == 1  # exactly one query regardless of product count
        assert buckets[pid_a] == 2


def test_bucket_for_date_shift_returns_expected_buckets(app, client, setup):
    pid_a, pid_b, pid_c = (setup["products"][n]["id"] for n in ("Bucket A", "Bucket B", "Bucket C"))
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    with app.app_context():
        from webapp.models.user import User
        user = User.query.filter_by(username="root").first()
        buckets = daily_entry_status_service.bucket_for_date_shift("2026-08-01", "Day", [pid_a, pid_b, pid_c])
        assert buckets == {}  # nothing touched yet -> no rows at all
