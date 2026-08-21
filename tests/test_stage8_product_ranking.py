"""
Stage 8 Part 2: global product quick-selection ranking. Usage is recorded
only from finalized Dispatch/Returns/Production activity, is shared across
every user (never per-Operator), persists in the database (survives
logout, session change, and an application restart), and naturally lets
inactive products drift down over time since the active-usage score is
recomputed fresh from `now` on every read rather than stored.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from webapp.services import product_usage_service as usage_svc

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    products = {}
    for name in ("Ranked A", "Ranked B", "Ranked C"):
        p = client.post("/api/admin/products", json={"name": name}).get_json()
        client.post(f"/api/admin/products/{p['id']}/packaging-rules", json={
            "cartons_to_packs": 10, "packs_to_pieces": 10,
        })
        products[name] = p
    category = client.post("/api/admin/sales-categories", json={"name": "Ranked Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Ranked Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"products": products, "customer": customer}


def _ranked_names(client):
    data = client.get("/api/admin/products?sort=usage").get_json()
    return [p["name"] for p in data]


def _frequently_used_names(client):
    data = client.get("/api/admin/products?sort=usage").get_json()
    return {p["name"] for p in data if p["frequently_used"]}


def _finalize_dispatch_for(client, product_id, customer_id, number, date_str="2026-07-01"):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": "Day", "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


def _backdate_events(app, days_ago):
    """One shared timestamp for every row — computed once, not per-row —
    so tests relying on two products being genuinely TIED on recency (and
    needing total_uses to be the deciding tiebreaker) aren't flaky from
    microsecond drift between rows touched in the same loop."""
    from webapp.extensions import db as _db
    from webapp.models.product_usage_event import ProductUsageEvent
    when = _utcnow() - timedelta(days=days_ago)
    for row in ProductUsageEvent.query.all():
        row.used_at = when
    _db.session.commit()


# =====================================================================
# What counts as usage
# =====================================================================

def test_finalized_dispatch_counts_as_usage(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid, setup["customer"]["id"], "RANK-1")
    assert "Ranked A" in _frequently_used_names(client)


def test_finalized_return_counts_as_usage(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    r = client.post("/api/returns", json={
        "date": "2026-07-01", "customer_id": customer_id,
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    assert "Ranked A" in _frequently_used_names(client)


def test_finalized_production_counts_as_usage(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    p = client.post("/api/production", json={
        "date": "2026-07-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{p['id']}/finalize")
    assert "Ranked A" in _frequently_used_names(client)


def test_opening_the_selector_does_not_count_as_usage(client, setup):
    client.get("/api/admin/products?sort=usage")
    client.get("/api/admin/products?sort=usage")
    client.get("/api/admin/products?sort=usage")
    from webapp.models.product_usage_event import ProductUsageEvent
    assert ProductUsageEvent.query.count() == 0


def test_viewing_a_product_does_not_count_as_usage(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    client.get(f"/api/daily-figures/{pid}?date=2026-07-01&shift=Day")
    from webapp.models.product_usage_event import ProductUsageEvent
    assert ProductUsageEvent.query.count() == 0


def test_a_failed_save_does_not_count(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    # No packaging rule product -> build_line raises, dispatch creation fails.
    bad_product = client.post("/api/admin/products", json={"name": "No Rule Product"}).get_json()
    res = client.post("/api/dispatches", json={
        "dispatch_number": "RANK-BAD", "date": "2026-07-01", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": bad_product["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400
    from webapp.models.product_usage_event import ProductUsageEvent
    assert ProductUsageEvent.query.count() == 0


def test_a_duplicate_finalization_does_not_count_twice(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    d = _finalize_dispatch_for(client, pid, setup["customer"]["id"], "RANK-DUP")
    res = client.post(f"/api/dispatches/{d['id']}/finalize")  # already finalized
    assert res.status_code == 400
    from webapp.models.product_usage_event import ProductUsageEvent
    assert ProductUsageEvent.query.filter_by(source="dispatch", source_id=d["id"]).count() == 1


def test_editing_before_finalizing_does_not_create_repeated_events(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "RANK-EDIT", "date": "2026-07-01", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    line_id = d["lines"][0]["id"]
    client.patch(f"/api/dispatches/{d['id']}/lines/{line_id}", json={"cartons": 2, "packs": 0, "pieces": 0})
    client.patch(f"/api/dispatches/{d['id']}/lines/{line_id}", json={"cartons": 3, "packs": 0, "pieces": 0})
    client.post(f"/api/dispatches/{d['id']}/finalize")
    from webapp.models.product_usage_event import ProductUsageEvent
    assert ProductUsageEvent.query.filter_by(source="dispatch", source_id=d["id"]).count() == 1


def test_reopening_and_refinalizing_refreshes_not_duplicates_the_event(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    d = _finalize_dispatch_for(client, pid, setup["customer"]["id"], "RANK-REOPEN")
    client.post(f"/api/dispatches/{d['id']}/reopen", json={"reason": "fixing"})
    client.post(f"/api/dispatches/{d['id']}/finalize")
    from webapp.models.product_usage_event import ProductUsageEvent
    assert ProductUsageEvent.query.filter_by(source="dispatch", source_id=d["id"]).count() == 1


def test_voiding_removes_the_usage_event(client, setup):
    pid = setup["products"]["Ranked A"]["id"]
    d = _finalize_dispatch_for(client, pid, setup["customer"]["id"], "RANK-VOID")
    assert "Ranked A" in _frequently_used_names(client)
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "cancelled"})
    assert "Ranked A" not in _frequently_used_names(client)


def test_removing_a_product_from_a_line_before_refinalizing_drops_its_event(client, setup, login_as):
    pid_a = setup["products"]["Ranked A"]["id"]
    pid_b = setup["products"]["Ranked B"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "RANK-SWAP", "date": "2026-07-01", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid_a, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post(f"/api/dispatches/{d['id']}/reopen", json={"reason": "swap product"})
    line_id = d["lines"][0]["id"]
    # A dispatch must always have at least one line — add the replacement
    # before removing the original.
    client.post(f"/api/dispatches/{d['id']}/lines", json={"product_id": pid_b, "cartons": 1, "packs": 0, "pieces": 0})
    client.delete(f"/api/dispatches/{d['id']}/lines/{line_id}")
    client.post(f"/api/dispatches/{d['id']}/finalize")

    from webapp.models.product_usage_event import ProductUsageEvent
    remaining = ProductUsageEvent.query.filter_by(source="dispatch", source_id=d["id"]).all()
    assert {r.product_id for r in remaining} == {pid_b}


def test_skip_for_now_never_calls_any_usage_or_write_endpoint():
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    idx = index_html.index("if(skipBtn) skipBtn.addEventListener")
    body = index_html[idx:idx + 200]
    assert "usage" not in body.lower()
    assert "apiPost" not in body


def test_no_activity_today_route_never_records_product_usage():
    import inspect
    from webapp.routes import daily_entry_status as route_module
    source = inspect.getsource(route_module)
    assert "product_usage_service" not in source
    assert "record_usage" not in source


# =====================================================================
# Ranking order — frequency, recency, drift
# =====================================================================

def test_frequently_used_products_rank_above_unused(client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-ORD1")
    names = _ranked_names(client)
    assert names.index("Ranked A") < names.index("Ranked B")
    assert names.index("Ranked A") < names.index("Ranked C")


def test_more_frequently_used_product_ranks_above_less_frequently_used(client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    pid_b = setup["products"]["Ranked B"]["id"]
    for i in range(3):
        _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], f"RANK-FREQ-A{i}")
    _finalize_dispatch_for(client, pid_b, setup["customer"]["id"], "RANK-FREQ-B1")
    names = _ranked_names(client)
    assert names.index("Ranked A") < names.index("Ranked B")


def test_recently_used_products_receive_the_intended_recency_advantage(app, client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    pid_b = setup["products"]["Ranked B"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-REC-A")
    _finalize_dispatch_for(client, pid_b, setup["customer"]["id"], "RANK-REC-B")

    # Backdate only B's event into the low-weight 91-180 day bucket.
    from webapp.extensions import db as _db
    from webapp.models.product_usage_event import ProductUsageEvent
    row_b = ProductUsageEvent.query.filter_by(product_id=pid_b).first()
    row_b.used_at = _utcnow() - timedelta(days=100)
    _db.session.commit()

    names = _ranked_names(client)
    assert names.index("Ranked A") < names.index("Ranked B")


def test_old_inactive_products_drift_lower_but_remain_present(app, client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    pid_b = setup["products"]["Ranked B"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-DRIFT-A")
    _finalize_dispatch_for(client, pid_b, setup["customer"]["id"], "RANK-DRIFT-B")

    from webapp.extensions import db as _db
    from webapp.models.product_usage_event import ProductUsageEvent
    row_a = ProductUsageEvent.query.filter_by(product_id=pid_a).first()
    row_a.used_at = _utcnow() - timedelta(days=400)  # ancient — 0 active score
    _db.session.commit()

    names = _ranked_names(client)
    assert names.index("Ranked B") < names.index("Ranked A")
    assert "Ranked A" in names  # still present and selectable


def test_all_time_count_is_the_tie_breaker_for_equally_inactive_products(app, client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    pid_b = setup["products"]["Ranked B"]["id"]
    for i in range(3):
        _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], f"RANK-TIE-A{i}")
    _finalize_dispatch_for(client, pid_b, setup["customer"]["id"], "RANK-TIE-B")
    _backdate_events(app, 400)  # both now score 0 (equally "inactive")

    names = _ranked_names(client)
    assert names.index("Ranked A") < names.index("Ranked B")  # 3 all-time uses beats 1


def test_score_formula_matches_spec_windows(app, setup, client):
    pid = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid, setup["customer"]["id"], "RANK-SCORE")
    from webapp.extensions import db as _db
    from webapp.models.product_usage_event import ProductUsageEvent
    row = ProductUsageEvent.query.filter_by(product_id=pid).first()

    row.used_at = _utcnow() - timedelta(days=10)
    _db.session.commit()
    ranked = usage_svc.ranked_active_products()
    assert next(p for p in ranked if p.id == pid).usage_score == 5

    row.used_at = _utcnow() - timedelta(days=60)
    _db.session.commit()
    ranked = usage_svc.ranked_active_products()
    assert next(p for p in ranked if p.id == pid).usage_score == 2

    row.used_at = _utcnow() - timedelta(days=150)
    _db.session.commit()
    ranked = usage_svc.ranked_active_products()
    assert next(p for p in ranked if p.id == pid).usage_score == 1

    row.used_at = _utcnow() - timedelta(days=200)
    _db.session.commit()
    ranked = usage_svc.ranked_active_products()
    assert next(p for p in ranked if p.id == pid).usage_score == 0


# =====================================================================
# Global — shared across users, persistent
# =====================================================================

def test_ranking_is_the_same_for_different_users(app, client, setup, login_as):
    pid_a = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-GLOBAL")
    ranking_as_admin = _ranked_names(client)

    viewer_client = app.test_client()
    from werkzeug.security import generate_password_hash
    from webapp.models.user import User
    from webapp.extensions import db as _db
    v = User(username="rank_viewer", password_hash=generate_password_hash("password123"), role="viewer", active=True)
    _db.session.add(v)
    _db.session.commit()
    viewer_client.post("/api/login", json={"username": "rank_viewer", "password": "password123"})
    ranking_as_viewer = [p["name"] for p in viewer_client.get("/api/admin/products?sort=usage").get_json()]

    assert ranking_as_admin == ranking_as_viewer


def test_ranking_persists_after_logout(client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-LOGOUT")
    before = _ranked_names(client)
    client.post("/api/logout")
    client.post("/api/login", json={"username": "root", "password": "password123"})
    after = _ranked_names(client)
    assert before == after


def test_ranking_persists_after_a_simulated_app_restart(app, client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-RESTART")
    with app.app_context():
        ranked = usage_svc.ranked_active_products()
        assert ranked[0].name == "Ranked A"


def test_no_separate_per_user_ranking_storage_exists():
    from webapp.models.product_usage_event import ProductUsageEvent
    columns = {c.name for c in ProductUsageEvent.__table__.columns}
    assert "user_id" not in columns
    assert "created_by" not in columns


# =====================================================================
# Selector behavior — everything remains selectable, safe, unobstructive
# =====================================================================

def test_every_active_product_remains_present_in_ranked_results(client, setup):
    names = _ranked_names(client)
    assert set(names) == {"Ranked A", "Ranked B", "Ranked C"}


def test_inactive_products_excluded_by_default_same_as_before(client, setup, login_as):
    pid_c = setup["products"]["Ranked C"]["id"]
    client.patch(f"/api/admin/products/{pid_c}", json={"active": False})
    names = _ranked_names(client)
    assert "Ranked C" not in names


def test_include_inactive_still_works_with_usage_sort(client, setup):
    pid_c = setup["products"]["Ranked C"]["id"]
    client.patch(f"/api/admin/products/{pid_c}", json={"active": False})
    data = client.get("/api/admin/products?sort=usage&include_inactive=1").get_json()
    assert any(p["name"] == "Ranked C" for p in data)


def test_default_order_unchanged_for_admin_config_screens(client, setup):
    """No ?sort=usage param -> exactly the pre-existing display_order,name
    behavior, unaffected by any usage activity."""
    pid_a = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-DEFAULT")
    default_order = [p["name"] for p in client.get("/api/admin/products").get_json()]
    assert default_order == sorted(default_order)  # alphabetical, since all display_order are equal/default


def test_ranked_response_never_exposes_which_user_performed_usage(client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-PRIVACY")
    data = client.get("/api/admin/products?sort=usage").get_json()
    for product in data:
        assert "used_by" not in product
        assert "user" not in product
        assert "root" not in str(product)


def test_product_identity_and_packaging_unchanged_by_ranking(client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-IDENTITY")
    data = client.get("/api/admin/products?sort=usage").get_json()
    product = next(p for p in data if p["id"] == pid_a)
    assert product["name"] == "Ranked A"
    assert product["packaging_rule"]["cartons_to_packs"] == 10
    assert product["packaging_rule"]["packs_to_pieces"] == 10


def test_calculations_unaffected_by_ranking(client, setup):
    pid_a = setup["products"]["Ranked A"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid_a, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    _finalize_dispatch_for(client, pid_a, setup["customer"]["id"], "RANK-CALC")
    view = client.get(f"/api/daily-figures/{pid_a}?date=2026-07-01&shift=Day").get_json()
    assert view["closing"]["base_qty"] == 400  # 500 opening - 100 issued


# =====================================================================
# Frontend wiring — Frequently used label, no reordering underneath
# =====================================================================

def test_dispatch_returns_production_pages_request_usage_sort():
    for page in ("dispatch.html", "returns.html", "production.html"):
        source = (STATIC_DIR / page).read_text(encoding="utf-8")
        assert "/api/admin/products?sort=usage" in source


def test_index_html_wizard_requests_usage_sort():
    source = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "/api/admin/products?sort=usage" in source


def test_frequently_used_optgroup_label_present():
    for page in ("dispatch.html", "returns.html", "production.html"):
        source = (STATIC_DIR / page).read_text(encoding="utf-8")
        assert "Frequently used" in source


def test_products_loaded_only_once_at_boot_never_reordered_mid_session():
    for page in ("dispatch.html", "returns.html", "production.html"):
        source = (STATIC_DIR / page).read_text(encoding="utf-8")
        # "loadProducts()" appears twice by construction (the function
        # definition itself, and its one call site) — the call site is the
        # only occurrence followed by a statement-ending ";", so counting
        # that distinguishes "called once" from "declared once".
        assert source.count("loadProducts();") == 1  # called once from init(), never on a timer/interval
        assert "setInterval" not in source


def test_admin_products_management_screen_does_not_request_usage_sort():
    admin_html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    assert "sort=usage" not in admin_html


# =====================================================================
# Migration
# =====================================================================

def test_product_usage_event_migration_module_defines_upgrade_and_downgrade():
    import importlib
    module = importlib.import_module(
        "migrations.versions.c7a4e29f1b83_add_product_usage_events"
    )
    assert hasattr(module, "upgrade")
    assert hasattr(module, "downgrade")
    assert module.down_revision == "b6f2a913c7d4"
