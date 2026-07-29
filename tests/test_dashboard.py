import pytest


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Compact Corporate Test"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Test Sales Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Dalca", "sales_category_id": category["id"]}).get_json()
    return {"product": product, "customer": customer, "category": category}


def test_health_endpoint_ok(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.get_json()["status"] == "ok"


def test_health_endpoint_unauthenticated(client):
    # deliberately no login — health checks must not require a session
    res = client.get("/api/health")
    assert res.status_code == 200


def test_dashboard_requires_login(client):
    res = client.get("/api/dashboard")
    assert res.status_code == 401


def test_dashboard_stock_summary_reflects_todays_activity(client, setup):
    pid = setup["product"]["id"]
    today = "2026-07-28"
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": today, "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    d = client.post("/api/dispatches", json={
        "dispatch_number": "DASH-1", "date": today, "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 0, "packs": 2, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")

    res = client.get(f"/api/dashboard?date={today}")
    assert res.status_code == 200
    data = res.get_json()
    row = next(r for r in data["stock_summary"] if r["product_id"] == pid)
    assert row["opening_base_qty"] == 1000
    assert row["production_base_qty"] == 100
    assert row["issued_base_qty"] == 20
    assert row["closing_base_qty"] == 1080


def test_dashboard_low_stock_warning(client, setup):
    pid = setup["product"]["id"]
    client.patch(f"/api/admin/products/{pid}", json={"low_stock_threshold": 50})
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 0, "packs": 3, "pieces": 0},  # 30 pieces — below threshold
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })

    res = client.get("/api/dashboard?date=2026-07-28")
    data = res.get_json()
    assert any(r["product_id"] == pid for r in data["low_stock"])


def test_dashboard_no_low_stock_warning_without_threshold(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 0, "packs": 1, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    res = client.get("/api/dashboard?date=2026-07-28")
    data = res.get_json()
    assert not any(r["product_id"] == pid for r in data["low_stock"])


def test_dashboard_recent_dispatches_and_draft_count(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/dispatches", json={
        "dispatch_number": "DASH-DRAFT", "date": "2026-07-28", "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    res = client.get("/api/dashboard?date=2026-07-28")
    data = res.get_json()
    assert data["draft_dispatches"]["count"] >= 1
    assert any(d["dispatch_number"] == "DASH-DRAFT" for d in data["recent_dispatches"])


def test_dashboard_top_products_from_finalized_dispatches_only(client, setup):
    pid = setup["product"]["id"]
    finalized = client.post("/api/dispatches", json={
        "dispatch_number": "DASH-TOP", "date": "2026-07-28", "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{finalized['id']}/finalize")

    client.post("/api/dispatches", json={
        "dispatch_number": "DASH-DRAFT2", "date": "2026-07-28", "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })  # left as draft — must not count toward top products

    res = client.get("/api/dashboard?date=2026-07-28")
    top = res.get_json()["top_products"]
    entry = next(t for t in top if t["product_id"] == pid)
    assert entry["base_qty"] == 100  # only the finalized 1-carton line


def test_dashboard_recent_adjustments_and_voids(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "test adjustment",
    })
    d = client.post("/api/dispatches", json={
        "dispatch_number": "DASH-VOID", "date": "2026-07-28", "shift": "Day", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "test void"})

    res = client.get("/api/dashboard?date=2026-07-28")
    data = res.get_json()
    assert any(a["reason"] == "test adjustment" for a in data["recent_adjustments"])
    assert any(v["dispatch_number"] == "DASH-VOID" for v in data["recent_voids"])


def test_low_stock_threshold_rejects_negative(client, setup):
    res = client.patch(f"/api/admin/products/{setup['product']['id']}", json={"low_stock_threshold": -5})
    assert res.status_code == 400


def test_low_stock_threshold_can_be_cleared(client, setup):
    pid = setup["product"]["id"]
    client.patch(f"/api/admin/products/{pid}", json={"low_stock_threshold": 20})
    res = client.patch(f"/api/admin/products/{pid}", json={"low_stock_threshold": None})
    assert res.status_code == 200
    assert res.get_json()["low_stock_threshold"] is None


def test_dashboard_page_loads(client, setup):
    res = client.get("/dashboard.html")
    assert res.status_code == 200


def test_dashboard_page_redirects_unauthenticated_to_login(client):
    # Prior to Stage 1 this served the raw file to anyone; the page is now
    # guarded server-side, not just by its own client-side "gate" div.
    res = client.get("/dashboard.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/"
