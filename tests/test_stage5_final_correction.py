"""
Final pre-live correction: book-style quantity display (see
tests/test_stage5_book_notation.py for the formatter itself),
Dispatch Day-only enforcement, navigation cleanup, and history filter
clarity. Backend behavior is tested through the Flask test client; frontend
conventions are source-level regression guards, same rationale as every
other frontend-only piece of this project (no JS/browser test runner
exists here).
"""
import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
HISTORY_HTML = (STATIC_DIR / "history.html").read_text(encoding="utf-8")
DISPATCH_HTML = (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8")
RETURNS_HTML = (STATIC_DIR / "returns.html").read_text(encoding="utf-8")
PRODUCTION_HTML = (STATIC_DIR / "production.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = client.post("/api/admin/products", json={"name": "Correction Test Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Correction Test Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Dalca", "sales_category_id": category["id"]}).get_json()
    return {"product": product, "customer": customer, "category": category}


# ================= Dispatch Day-only enforcement =================

def test_create_dispatch_ignores_a_submitted_night_shift(client, setup):
    """Not merely 'defaults to Day' — an explicit Night in the request body
    must not be honored at all."""
    res = client.post("/api/dispatches", json={
        "dispatch_number": "CORR-1", "date": "2026-07-29", "shift": "Night",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201
    assert res.get_json()["shift"] == "Day"


def test_create_dispatch_without_a_shift_field_at_all_still_works(client, setup):
    res = client.post("/api/dispatches", json={
        "dispatch_number": "CORR-2", "date": "2026-07-29",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 201
    assert res.get_json()["shift"] == "Day"


def test_update_dispatch_ignores_an_attempt_to_change_shift_to_night(client, setup):
    created = client.post("/api/dispatches", json={
        "dispatch_number": "CORR-3", "date": "2026-07-29",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.patch(f"/api/dispatches/{created['id']}", json={"shift": "Night"})
    assert res.status_code == 200
    assert res.get_json()["shift"] == "Day"


def test_duplicate_dispatch_forces_day_even_when_source_is_a_legacy_night_dispatch(client, setup, app):
    """Existing historical Night dispatch records are untouched, but a NEW
    dispatch (even one duplicated from a Night source) is always Day."""
    from webapp.extensions import db
    from webapp.models.dispatch import Dispatch

    created = client.post("/api/dispatches", json={
        "dispatch_number": "CORR-4", "date": "2026-07-29",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()

    # Simulate a pre-existing legacy Night dispatch directly, bypassing the
    # (now Day-only) API — exactly like a record that predates this
    # correction would look on disk.
    with app.app_context():
        dispatch = db.session.get(Dispatch, created["id"])
        dispatch.shift = "Night"
        db.session.commit()

    res = client.post(f"/api/dispatches/{created['id']}/duplicate", json={"dispatch_number": "CORR-4-COPY"})
    assert res.status_code == 201
    assert res.get_json()["shift"] == "Day"

    # The original legacy Night record itself remains untouched.
    with app.app_context():
        original = db.session.get(Dispatch, created["id"])
        assert original.shift == "Night"


def test_dispatch_history_export_still_works_without_a_shift_param(client, setup):
    client.post("/api/dispatches", json={
        "dispatch_number": "CORR-5", "date": "2026-07-29",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    res = client.get("/api/dispatches/export.csv")
    assert res.status_code == 200


# ================= Dispatch frontend: no shift selector/filter =================

def test_dispatch_entry_form_has_no_shift_selector():
    assert 'id="dShift"' not in DISPATCH_HTML


def test_dispatch_list_filters_have_no_shift_filter():
    assert 'id="fShift"' not in DISPATCH_HTML


def test_dispatch_save_payload_does_not_send_shift():
    idx = DISPATCH_HTML.index("const body = {")
    body_literal = DISPATCH_HTML[idx:DISPATCH_HTML.index("};", idx)]
    assert "shift:" not in body_literal


def test_history_page_dispatch_tab_has_no_shift_filter():
    assert 'id="fShift"' not in HISTORY_HTML


def test_production_and_daily_figures_history_keep_their_shift_filters():
    """Only Dispatch History loses its shift filter — Production is
    genuinely Day/Night, and Daily Figures already shows both shifts."""
    assert 'id="pShift"' in HISTORY_HTML
    assert 'id="hShift"' in HISTORY_HTML


def test_production_entry_form_still_has_shift_selector():
    assert 'id="pShift"' in PRODUCTION_HTML


def test_returns_has_no_shift_concept_anywhere():
    assert 'id="rShift"' not in RETURNS_HTML
    assert "Shift" not in RETURNS_HTML.split("<script>")[0]  # no shift label in the markup at all


# ================= navigation cleanup =================

def test_dispatch_returns_production_no_longer_hardcode_a_fake_back_link():
    # Stage 6 explicitly removed the old "<- Daily Figures" links that were
    # being used as a fake universal Back button (they always went to Daily
    # Figures regardless of where the user actually came from) — real Back
    # navigation is now a shared, history-aware control in the header
    # rendered by static/app-shell.js. See tests/test_stage6_app_shell.py.
    for name, source in [("dispatch.html", DISPATCH_HTML), ("returns.html", RETURNS_HTML), ("production.html", PRODUCTION_HTML)]:
        assert 'id="backToDailyFiguresLink"' not in source, f"{name} still has the old fake Back link"
        assert 'id="appIdentityBar"' in source, f"{name} is missing the shared header (with real Back) placeholder"


def test_returns_and_production_history_tab_labels_are_disambiguated():
    assert 'data-tab="list">Returns History</div>' in RETURNS_HTML
    assert 'data-tab="list">Production History</div>' in PRODUCTION_HTML


# ================= history filter clarity =================

def test_all_four_history_tabs_have_labeled_date_fields():
    for prefix in ("f", "r", "p", "h"):
        assert f'<label>Exact Date</label><input id="{prefix}Date"' in HISTORY_HTML
        assert f'<label>From Date</label><input id="{prefix}DateFrom"' in HISTORY_HTML
        assert f'<label>To Date</label><input id="{prefix}DateTo"' in HISTORY_HTML


def test_sales_category_filter_has_a_responsive_width_fix():
    assert "#fSalesCategory{" in HISTORY_HTML
    assert "#fSalesCategory{" in DISPATCH_HTML


def test_quick_filters_preserved_on_every_history_tab():
    for marker in ("data-quick=", "data-rquick=", "data-pquick=", "data-hquick="):
        assert f'{marker}"today"' in HISTORY_HTML
        assert f'{marker}"week"' in HISTORY_HTML


# ================= Daily Figures: earlier (restored) layout =================
# A later correction restored the original, more compact entry-card layout
# (single-row stock-readouts with inline hints, Opening Stock rendered via
# the same disabled-qtyInputsHtml gate every other role-gated field uses)
# in place of the "large read-only card" redesign these tests used to pin.
# See tests/test_stage5_ui_restoration.py for the current layout's coverage.


# ================= permissions/calculations unchanged =================

def test_manager_can_still_edit_opening_stock_unconditionally(client, setup, login_as):
    client.post("/api/logout")
    login_as("mgr1", "password123", "manager")
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-29", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200


def test_viewer_still_cannot_write_daily_figures(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-29", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


def test_closing_stock_formula_value_is_unchanged_by_the_display_correction(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-29", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-29&shift=Day").get_json()
    assert view["closing"]["base_qty"] == 1000
    assert view["closing"]["cartons"] == 10 and view["closing"]["packs"] == 0 and view["closing"]["pieces"] == 0
