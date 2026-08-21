"""
Stage 5: the two new feature flags, `returns` and `production`. Covers
seeding, route enforcement, page guards, the chosen dependency graph
(returns -> customer_management; production has none — see
webapp/services/feature_flag_service.py's module docstring for why),
data-preservation-through-disable, and the same lockout protections already
proven for the original six modules in Stage 4.
"""
import pytest


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = client.post("/api/admin/products", json={"name": "Flag Test Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return {"product": product}


def _flags(client):
    return {f["module_key"]: f["enabled"] for f in client.get("/api/feature-flags").get_json()}


# ---------- seeding ----------

def test_returns_and_production_seeded_enabled(client, setup):
    flags = _flags(client)
    assert flags["returns"] is True
    assert flags["production"] is True


# ---------- route enforcement ----------

def test_returns_routes_blocked_when_disabled(client, setup):
    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    res = client.post("/api/returns", json={
        "date": "2026-07-28",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403
    assert "returns" in res.get_json()["error"]


def test_production_routes_blocked_when_disabled(client, setup):
    client.patch("/api/feature-flags/production", json={"enabled": False})
    res = client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403
    assert "production" in res.get_json()["error"]


def test_returns_read_routes_also_blocked_when_disabled(client, setup):
    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    assert client.get("/api/returns").status_code == 403
    assert client.get("/api/returns/export.csv").status_code == 403


# ---------- dependency graph ----------

def test_returns_requires_customer_management(client, setup):
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    assert _flags(client)["returns"] is False  # cascaded off automatically

    res = client.patch("/api/feature-flags/returns", json={"enabled": True})
    assert res.status_code == 400
    assert "Customer Management" in res.get_json()["error"]


def test_disabling_customer_management_without_cascade_blocked_by_returns_too(client, setup):
    res = client.patch("/api/feature-flags/customer_management", json={"enabled": False})
    assert res.status_code == 400
    assert "Returns" in res.get_json()["error"]


def test_production_has_no_dependency(client, setup):
    """Production can be freely disabled/enabled regardless of every other
    module's state — it has no recipient field and no cross-module lookup."""
    client.patch("/api/feature-flags/customer_management", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/dispatch", json={"enabled": False})  # already off via cascade, harmless
    res = client.patch("/api/feature-flags/production", json={"enabled": False})
    assert res.status_code == 200
    assert client.patch("/api/feature-flags/production", json={"enabled": True}).status_code == 200


def test_daily_figures_does_not_require_returns_or_production(client, setup):
    """Daily Figures must not need either source-book flag on — it reads
    their tables directly and degrades to zero contributions when a source
    is off, rather than crashing or being blocked itself."""
    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/production", json={"enabled": False})
    res = client.get(f"/api/daily-figures/{setup['product']['id']}?date=2026-07-28&shift=Day")
    assert res.status_code == 200


def test_disabling_returns_or_production_does_not_cascade_to_daily_figures(client, setup):
    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/production", json={"enabled": False})
    flags = _flags(client)
    assert flags["daily_figures"] is True
    assert flags["dispatch"] is True
    assert flags["reporting"] is True


# ---------- daily figures gracefully reflects disabled source modules ----------

def test_daily_figures_reflects_finalized_data_even_after_its_source_module_is_disabled(client, setup):
    """Disabling a module never deletes its data — Daily Figures must keep
    showing a Return/Production total that was finalized before the
    Returns/Production module was switched off."""
    pid = setup["product"]["id"]
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    created = client.post("/api/returns", json={
        "date": "2026-07-28", "customer_id": customer_id,
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})

    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-28&shift=Day").get_json()
    assert view["return_"]["base_qty"] == 100  # data preserved, still counted


def test_reenabling_returns_restores_full_access_without_data_loss(client, setup):
    pid = setup["product"]["id"]
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    created = client.post("/api/returns", json={
        "date": "2026-07-28", "customer_id": customer_id,
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/customer_management", json={"enabled": True})
    client.patch("/api/feature-flags/returns", json={"enabled": True})

    res = client.get(f"/api/returns/{created['id']}")
    assert res.status_code == 200
    assert res.get_json()["status"] == "finalized"
    assert res.get_json()["lines"][0]["base_unit_qty"] == 100


# ---------- page guards: no lockout ----------

def test_returns_page_redirects_not_errors_when_disabled(client, setup):
    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    res = client.get("/returns.html")
    assert res.status_code in (302, 503)  # fallback redirect, or 503 only if every fallback is also off


def test_production_page_redirects_not_errors_when_disabled(client, setup):
    client.patch("/api/feature-flags/production", json={"enabled": False})
    res = client.get("/production.html")
    assert res.status_code in (302, 503)


def test_admin_page_never_gated_by_returns_or_production_flags(client, setup):
    """admin.html must always be reachable to re-enable a disabled module —
    the exact lockout bug already fixed in the Stage 4 correction."""
    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/production", json={"enabled": False})
    res = client.get("/admin.html")
    assert res.status_code == 200


def test_login_page_never_gated_by_returns_or_production_flags(client, setup):
    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    client.patch("/api/feature-flags/production", json={"enabled": False})
    client.post("/api/logout")
    res = client.get("/")
    assert res.status_code == 200


# ---------- super-admin-only writes ----------

@pytest.mark.parametrize("role", ["manager", "operator", "viewer"])
def test_non_super_admin_cannot_toggle_returns_or_production(client, login_as, role, setup):
    login_as(f"changer_{role}", "password123", role)
    assert client.patch("/api/feature-flags/returns", json={"enabled": False}).status_code == 403
    assert client.patch("/api/feature-flags/production", json={"enabled": False}).status_code == 403


# ---------- audit ----------

def test_toggling_returns_flag_is_audited(client, setup, app):
    client.patch("/api/feature-flags/returns", json={"enabled": False, "cascade": True})
    from webapp.models.audit_log import AuditLog
    with app.app_context():
        assert AuditLog.query.filter_by(action="update", entity_type="feature_flag", entity_id="returns").first() is not None
