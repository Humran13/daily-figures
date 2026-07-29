"""
Stage 2: the consolidated History & Exports page (static/history.html).
No new backend routes were added — this page is a pure frontend
consolidation reusing GET /api/dispatches, GET /api/dispatches/export.<fmt>,
GET /api/daily-figures/history, and GET /api/daily-figures/export.<fmt>
verbatim. As with prior stages, frontend-only behavior (tab structure,
read-only guarantees, nav wiring) is pinned down at the source level since
this project has no JS/browser test runner.
"""
import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
HISTORY_HTML = (STATIC_DIR / "history.html").read_text(encoding="utf-8")
DISPATCH_HTML = (STATIC_DIR / "dispatch.html").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Stage2 Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Stage2 Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Stage2 Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


# ---------- page reachability (equally open to every role, like dispatch.html/index.html) ----------

@pytest.mark.parametrize("role", ["super_admin", "manager", "operator", "viewer"])
def test_history_page_loads_for_every_role(client, login_as, role):
    login_as(f"user_{role}", "password123", role)
    res = client.get("/history.html")
    assert res.status_code == 200


def test_history_page_redirects_unauthenticated_to_login(client):
    # Stage 2 correction: history.html is now guarded server-side (any
    # authenticated role passes, no role restriction) rather than served
    # unconditionally by the generic static handler.
    res = client.get("/history.html")
    assert res.status_code == 302
    assert res.headers["Location"] == "/"


# ---------- reused backend filters (date/shift, previously unexposed in the UI) ----------

def _finalize_dispatch(client, product_id, customer_id, date, shift, number):
    created = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "shift": shift, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{created['id']}/finalize")
    return created


def test_daily_figures_history_shift_filter_works_end_to_end(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Night",
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    res = client.get(f"/api/daily-figures/history?date=2026-07-28&shift=Night&product_id={pid}")
    rows = res.get_json()
    assert len(rows) == 1
    assert rows[0]["shift"] == "Night"


def test_dispatch_history_status_and_category_filters_still_work(client, setup):
    d = _finalize_dispatch(client, setup["product"]["id"], setup["customer"]["id"], "2026-07-28", "Day", "H2-1")
    res = client.get(f"/api/dispatches?status=finalized&sales_category_id={setup['category']['id']}")
    data = res.get_json()
    assert any(r["id"] == d["id"] for r in data["results"])


# ---------- Operator/Viewer read-only ----------

def test_operator_cannot_write_via_underlying_dispatch_or_daily_figure_apis(client, login_as, setup):
    login_as("op1", "password123", "operator")
    # Reading history data is fine...
    assert client.get("/api/dispatches").status_code == 200
    assert client.get("/api/daily-figures/history?date=2026-07-28").status_code == 200
    # ...but Operator's Daily-Figures write access still gated by Stage 1
    # defaults (all False) regardless of being on this new page.
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


def test_viewer_cannot_write_via_underlying_apis(client, login_as, setup):
    login_as("view1", "password123", "viewer")
    assert client.get("/api/dispatches").status_code == 200
    res = client.post("/api/dispatches", json={
        "dispatch_number": "H2-VIEWER", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


# ---------- source-level structure of history.html ----------

def test_history_page_has_two_tabs_in_order():
    match = re.search(r'<div class="tabs">\s*<div class="tab active" data-tab="dispatch">Dispatch History</div>\s*'
                       r'<div class="tab" data-tab="daily-figures">Daily Figures History</div>', HISTORY_HTML)
    assert match, "expected Dispatch History then Daily Figures History tabs in that order"


def test_dispatch_history_tab_has_all_required_filters():
    for field_id in ("fDate", "fDateFrom", "fDateTo", "fSalesCategory", "fCustomer",
                      "fProduct", "fNumber", "fInvoice", "fShift", "fStatus", "fCreatedBy"):
        assert f'id="{field_id}"' in HISTORY_HTML, f"missing filter field {field_id}"
    assert 'data-quick="today"' in HISTORY_HTML
    assert 'data-quick="week"' in HISTORY_HTML


def test_daily_figures_history_tab_has_all_required_filters():
    for field_id in ("hDate", "hDateFrom", "hDateTo", "hShift", "hProduct"):
        assert f'id="{field_id}"' in HISTORY_HTML, f"missing filter field {field_id}"
    assert 'data-hquick="today"' in HISTORY_HTML
    assert 'data-hquick="week"' in HISTORY_HTML


def test_daily_figures_history_groups_by_date_with_collapsible_sections():
    assert "function groupHistoryByDate(results)" in HISTORY_HTML
    assert "data-toggle-hgroup" in HISTORY_HTML
    assert "classList.toggle('collapsed')" in HISTORY_HTML


def test_dispatch_history_groups_by_date_with_collapsible_sections():
    assert "function groupByDate(results)" in HISTORY_HTML
    assert "data-toggle-group" in HISTORY_HTML


def test_exports_reuse_existing_endpoints_and_include_filters():
    assert "'/api/dispatches/export.'+fmt+'?'+params.toString()" in HISTORY_HTML
    assert "'/api/daily-figures/export.'+fmt+'?'+params.toString()" in HISTORY_HTML


def test_dispatch_row_click_navigates_to_dispatch_module_for_corrections():
    """Requirement 6: corrections happen in Dispatch, never inline here."""
    assert "window.location.href = `/dispatch.html?open=${el.dataset.open}`;" in HISTORY_HTML


def test_history_page_contains_no_write_requests():
    """This page must never issue a create/update/delete call — read-only
    by construction for every role, not just Operator/Viewer."""
    for verb in ("'POST'", "'PUT'", "'PATCH'", "'DELETE'", '"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in HISTORY_HTML, f"found a {verb} call in history.html — this page must stay read-only"


def test_history_page_has_operator_nav_and_admin_tier_nav():
    assert 'id="operatorNav"' in HISTORY_HTML
    assert 'id="adminTierNav"' in HISTORY_HTML
    op_nav = re.search(r'<div class="tabs hidden" id="operatorNav">.*?</div>', HISTORY_HTML, re.DOTALL).group(0)
    labels = [l.strip().replace("&amp;", "&") for l in re.findall(r'>([^<]+)</a>', op_nav)]
    assert labels == ["Dispatch", "Daily Figures", "History & Exports"]


def test_dispatch_html_and_index_html_link_to_new_history_page():
    assert 'href="/history.html"' in DISPATCH_HTML
    assert 'href="/history.html"' in INDEX_HTML
    assert "tab=list" not in DISPATCH_HTML.split("operatorNav")[1][:400]
    assert "tab=list" not in INDEX_HTML.split("operatorNav")[1][:400]
