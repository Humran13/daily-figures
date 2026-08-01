"""
Stage 7 section 4: Super-Administrator-only "Reset Daily Values" control —
narrow scope (one Date + Shift, one product or all), preview before
confirm, transactional execute, and the hard guarantee that it never
touches source-derived Dispatch/Returns/Production totals.
"""
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ADMIN_HTML = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Reset Test Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Reset Test Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Reset Test Customer", "sales_category_id": category["id"],
    }).get_json()
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    return {"product": product, "customer": customer}


# =====================================================================
# Permissions
# =====================================================================

@pytest.mark.parametrize("role", ["manager", "operator", "viewer"])
def test_non_super_admin_cannot_preview_reset(client, login_as, role):
    login_as(f"reset_prev_{role}", "password123", role)
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 403


@pytest.mark.parametrize("role", ["manager", "operator", "viewer"])
def test_non_super_admin_cannot_execute_reset(client, login_as, role):
    login_as(f"reset_exec_{role}", "password123", role)
    res = client.post("/api/daily-reset", json={"date": "2026-08-01", "shift": "Day", "reason": "trying anyway"})
    assert res.status_code == 403


# =====================================================================
# Scoping: one product, or all products for a Date + Shift; never other
# dates or unrelated products
# =====================================================================

def test_super_admin_can_reset_one_product(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid, "reason": "test cleanup",
    })
    assert res.status_code == 200
    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert view["opening"]["base_qty"] == 0


def test_super_admin_can_reset_all_products_for_a_date_shift(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-02", "shift": "Day",
        "opening": {"cartons": 3, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-reset", json={"date": "2026-08-02", "shift": "Day", "reason": "bulk cleanup"})
    assert res.status_code == 200
    assert setup["product"]["name"] in res.get_json()["products"]


def test_other_dates_remain_unchanged(client, setup):
    """08-04's Opening Stock is deliberately set to a value that differs
    from what pure carry-forward from 08-03 would derive (7 cartons, not
    4), so it becomes a genuine independent anchor in its own right — not
    one that merely happens to match 08-03's carried-forward balance. That
    way, resetting 08-03 (which un-anchors it — see daily_reset_service.py)
    is a real test of "reset doesn't affect an unrelated date", rather than
    coincidentally relying on 08-04 having never been independently
    anchored at all."""
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-03", "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-04", "shift": "Day",
        "opening": {"cartons": 7, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-reset", json={"date": "2026-08-03", "shift": "Day", "reason": "scoped reset"})

    untouched = client.get(f"/api/daily-figures/{pid}?date=2026-08-04&shift=Day").get_json()
    assert untouched["opening"]["base_qty"] == 700


def test_other_products_remain_unchanged_when_resetting_one(client, setup, login_as):
    pid = setup["product"]["id"]
    product2 = client.post("/api/admin/products", json={"name": "Reset Test Product 2"}).get_json()
    client.post(f"/api/admin/products/{product2['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-05", "shift": "Day",
        "opening": {"cartons": 2, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": product2["id"], "date": "2026-08-05", "shift": "Day",
        "opening": {"cartons": 2, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-reset", json={"date": "2026-08-05", "shift": "Day", "product_id": pid, "reason": "one product only"})

    other = client.get(f"/api/daily-figures/{product2['id']}?date=2026-08-05&shift=Day").get_json()
    assert other["opening"]["base_qty"] == 200


# =====================================================================
# Never touches source-derived Dispatch/Returns/Production totals
# =====================================================================

def test_reset_never_zeroes_finalized_dispatch_issued_total(client, setup):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "RESET-1", "date": "2026-08-06", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")

    client.post("/api/daily-reset", json={"date": "2026-08-06", "shift": "Day", "reason": "test"})

    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-06&shift=Day").get_json()
    assert view["issued"]["base_qty"] == 100  # unchanged — the dispatch itself is untouched


def test_reset_never_touches_finalized_production_totals(client, setup):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-07", "shift": "Day",
        "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    client.post("/api/daily-reset", json={"date": "2026-08-07", "shift": "Day", "reason": "test"})

    view = client.get(f"/api/daily-figures/{pid}?date=2026-08-07&shift=Day").get_json()
    assert view["production"]["base_qty"] == 200


def test_reset_response_warns_source_totals_are_not_deleted():
    assert "Dispatch, Returns, and Production totals are calculated from their source books" in ADMIN_HTML


# =====================================================================
# Confirmation + reason required; preview before execute
# =====================================================================

def test_preview_shows_affected_products_without_changing_anything(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-08", "shift": "Day",
        "opening": {"cartons": 6, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-08", "shift": "Day", "product_id": pid})
    assert res.status_code == 200
    data = res.get_json()
    assert data["products"][0]["has_manual_opening_entry"] is True

    unchanged = client.get(f"/api/daily-figures/{pid}?date=2026-08-08&shift=Day").get_json()
    assert unchanged["opening"]["base_qty"] == 600


def test_execute_requires_a_reason(client, setup):
    res = client.post("/api/daily-reset", json={"date": "2026-08-09", "shift": "Day"})
    assert res.status_code == 400


def test_execute_requires_a_date(client, setup):
    res = client.post("/api/daily-reset", json={"shift": "Day", "reason": "no date given"})
    assert res.status_code == 400


def test_execute_requires_a_valid_shift(client, setup):
    res = client.post("/api/daily-reset", json={"date": "2026-08-10", "shift": "Afternoon", "reason": "bad shift"})
    assert res.status_code == 400


# =====================================================================
# Transactional: failed reset rolls back completely
# =====================================================================

def test_reset_with_nonexistent_product_id_fails_and_changes_nothing(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-11", "shift": "Day",
        "opening": {"cartons": 8, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-reset", json={
        "date": "2026-08-11", "shift": "Day", "product_id": 999999, "reason": "bad product",
    })
    assert res.status_code == 400
    unchanged = client.get(f"/api/daily-figures/{pid}?date=2026-08-11&shift=Day").get_json()
    assert unchanged["opening"]["base_qty"] == 800


# =====================================================================
# Audit
# =====================================================================

def test_reset_creates_an_audit_entry_with_no_sensitive_data(app, client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-12", "shift": "Day",
        "opening": {"cartons": 9, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-reset", json={
        "date": "2026-08-12", "shift": "Day", "product_id": pid, "reason": "audited reset",
    })

    from webapp.models.audit_log import AuditLog
    entry = AuditLog.query.filter_by(action="reset_daily_values").order_by(AuditLog.id.desc()).first()
    assert entry is not None
    assert entry.username == "root"
    assert "audited reset" in (entry.after_json or "")
    assert "password" not in (entry.after_json or "").lower()


# =====================================================================
# Ownership/completion status also cleared by reset (section 4)
# =====================================================================

def test_reset_clears_a_completed_no_activity_review(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-entry-status/no-activity", json={"product_id": pid, "date": "2026-08-13", "shift": "Day"})
    status_before = client.get(f"/api/daily-entry-status?date=2026-08-13&shift=Day&product_id={pid}").get_json()
    assert status_before["status"] == "completed"

    client.post("/api/daily-reset", json={"date": "2026-08-13", "shift": "Day", "product_id": pid, "reason": "clear review"})

    status_after = client.get(f"/api/daily-entry-status?date=2026-08-13&shift=Day&product_id={pid}").get_json()
    assert status_after["status"] == "not_started"


# =====================================================================
# Admin UI
# =====================================================================

def test_admin_html_has_reset_daily_values_tab_and_panel():
    assert '<div class="tab" data-panel="reset">Reset Daily Values</div>' in ADMIN_HTML
    assert 'id="panel-reset"' in ADMIN_HTML


def test_admin_html_reset_form_has_date_shift_product_controls():
    assert 'id="resetDate"' in ADMIN_HTML
    assert 'id="resetShift"' in ADMIN_HTML
    assert 'id="resetProduct"' in ADMIN_HTML
    assert 'id="resetPreviewBtn"' in ADMIN_HTML


def test_admin_html_reset_requires_confirmation_modal_and_reason():
    assert 'id="resetConfirmModal"' in ADMIN_HTML
    assert 'id="resetConfirmReason"' in ADMIN_HTML
    idx = ADMIN_HTML.index("resetConfirmSubmitBtn').addEventListener")
    snippet = ADMIN_HTML[idx:idx + 500]
    assert "if(!reason)" in snippet
