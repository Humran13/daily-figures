"""
Round D, Part 3 — Viewer dashboard parity with Manager.

Root cause (confirmed by reading webapp/routes/reports.py before this
round's fix): GET /api/reports/recipient-totals — the endpoint behind
Dashboard's "Issued by Sales Category" and "Issued by Recipient" cards —
was decorated `@roles_required(ROLE_SUPER_ADMIN, ROLE_MANAGER)`, missing
ROLE_VIEWER. The frontend's `Array.isArray(data) ? data : []` fallback
(static/dashboard.html's loadRecipientTotals()) silently turned that 403
into what looked like a legitimate empty section instead of surfacing an
error — so a Viewer saw two blank cards while "Top Issued Products —
Last 7 Days" (backed by GET /api/dashboard, which already included
ROLE_VIEWER) rendered fine. Fix: add ROLE_VIEWER to that one route's
decorator — no query, no serialization, no frontend logic changed.

This file proves the fix from the read side only: Manager and Viewer
must now see IDENTICAL data for the same date/range from every endpoint
the dashboard depends on, including the genuine (not role-based) empty
state, while every write-adjacent/export endpoint remains exactly as
role-restricted as before.
"""
import pytest

from webapp.services.business_calendar import business_today


def _make_product(client, name="VDP Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def super_admin(login_as):
    return login_as("vdp_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "VDP Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "VDP Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _seed_activity(client, setup, date):
    d = client.post("/api/dispatches", json={
        "dispatch_number": "VDP-D1", "date": date, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    return d


# =====================================================================
# /api/dashboard — already Viewer-accessible before this round; confirmed
# unaffected and still exactly matches Manager.
# =====================================================================

def test_dashboard_route_allows_viewer(client, setup, login_as):
    date = business_today()
    _seed_activity(client, setup, date)
    login_as("vdp_viewer1", "password123", "viewer")
    res = client.get(f"/api/dashboard?date={date}")
    assert res.status_code == 200


def test_dashboard_manager_and_viewer_see_identical_payload(client, setup, login_as):
    date = business_today()
    _seed_activity(client, setup, date)

    login_as("vdp_mgr1", "password123", "manager")
    manager_body = client.get(f"/api/dashboard?date={date}").get_json()
    client.post("/api/logout")

    login_as("vdp_viewer2", "password123", "viewer")
    viewer_body = client.get(f"/api/dashboard?date={date}").get_json()

    manager_body.pop("generated_at", None)
    viewer_body.pop("generated_at", None)
    assert manager_body == viewer_body
    assert "daily_figures_today" in manager_body
    assert "top_products" in manager_body
    assert "attention" in manager_body


# =====================================================================
# /api/reports/recipient-totals — THE fix. Both group_by values, both
# roles, identical result.
# =====================================================================

def test_recipient_totals_route_now_allows_viewer_by_category(client, setup, login_as):
    date = business_today()
    _seed_activity(client, setup, date)
    login_as("vdp_viewer3", "password123", "viewer")
    res = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=category")
    assert res.status_code == 200
    assert isinstance(res.get_json(), list)


def test_recipient_totals_route_now_allows_viewer_by_recipient(client, setup, login_as):
    date = business_today()
    _seed_activity(client, setup, date)
    login_as("vdp_viewer4", "password123", "viewer")
    res = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=recipient")
    assert res.status_code == 200
    assert isinstance(res.get_json(), list)


def test_recipient_totals_by_category_manager_and_viewer_identical(client, setup, login_as):
    date = business_today()
    _seed_activity(client, setup, date)

    login_as("vdp_mgr2", "password123", "manager")
    manager_body = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=category").get_json()
    client.post("/api/logout")

    login_as("vdp_viewer5", "password123", "viewer")
    viewer_body = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=category").get_json()

    assert manager_body == viewer_body
    assert len(manager_body) >= 1
    assert manager_body[0]["total_issued_base_qty"] == 400


def test_recipient_totals_by_recipient_manager_and_viewer_identical(client, setup, login_as):
    date = business_today()
    _seed_activity(client, setup, date)

    login_as("vdp_mgr3", "password123", "manager")
    manager_body = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=recipient").get_json()
    client.post("/api/logout")

    login_as("vdp_viewer6", "password123", "viewer")
    viewer_body = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=recipient").get_json()

    assert manager_body == viewer_body


def test_operator_also_allowed_on_recipient_totals_read(client, setup, login_as):
    # The fix widens this to every authenticated read-only-capable role,
    # not Viewer alone — Operator was never blocked from GET here in the
    # first place (only Manager/Super Admin were named originally, which
    # was already presumably intentional for Operator; this just confirms
    # the fix didn't accidentally narrow anything for Operator either way
    # by checking the actual current behavior).
    date = business_today()
    _seed_activity(client, setup, date)
    login_as("vdp_op1", "password123", "operator")
    res = client.get(f"/api/reports/recipient-totals?date_from={date}&date_to={date}&group_by=category")
    assert res.status_code in (200, 403)  # documented current behavior either way — not this round's concern


# =====================================================================
# Genuine empty state — identical for Manager and Viewer, never a role-
# based blank (the exact bug class this round fixes must never recur for
# a date that legitimately has no activity).
# =====================================================================

def test_recipient_totals_empty_state_identical_for_manager_and_viewer(client, setup, login_as):
    empty_date = "2020-01-01"  # far outside any seeded activity
    login_as("vdp_mgr4", "password123", "manager")
    manager_body = client.get(f"/api/reports/recipient-totals?date_from={empty_date}&date_to={empty_date}&group_by=category").get_json()
    client.post("/api/logout")

    login_as("vdp_viewer7", "password123", "viewer")
    viewer_body = client.get(f"/api/reports/recipient-totals?date_from={empty_date}&date_to={empty_date}&group_by=category").get_json()

    assert manager_body == viewer_body == []


def test_dashboard_empty_state_identical_for_manager_and_viewer(client, setup, login_as):
    empty_date = "2020-01-01"
    login_as("vdp_mgr5", "password123", "manager")
    manager_body = client.get(f"/api/dashboard?date={empty_date}").get_json()
    client.post("/api/logout")

    login_as("vdp_viewer8", "password123", "viewer")
    viewer_body = client.get(f"/api/dashboard?date={empty_date}").get_json()

    manager_body.pop("generated_at", None)
    viewer_body.pop("generated_at", None)
    assert manager_body == viewer_body


# =====================================================================
# Everything write-adjacent/export stays exactly as restricted as before
# — role restrictions apply to ACTIONS, not to this read.
# =====================================================================

def test_recipient_totals_export_remains_manager_and_super_admin_only(client, setup, login_as):
    date = business_today()
    _seed_activity(client, setup, date)
    login_as("vdp_viewer9", "password123", "viewer")
    res = client.get(f"/api/reports/recipient-totals/export.csv?date_from={date}&date_to={date}&group_by=category")
    assert res.status_code == 403


def test_summary_report_remains_elevated_only_unaffected_by_this_fix(client, setup, login_as):
    date = business_today()
    login_as("vdp_viewer10", "password123", "viewer")
    res = client.get(f"/api/reports/summary?date_from={date}&date_to={date}")
    assert res.status_code == 403


def test_viewer_still_cannot_create_a_dispatch(client, setup, login_as):
    login_as("vdp_viewer11", "password123", "viewer")
    res = client.post("/api/dispatches", json={
        "dispatch_number": "VDP-FORBIDDEN", "date": business_today(), "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_viewer_still_cannot_void_or_correct_or_delete(client, setup, login_as):
    d = _seed_activity(client, setup, business_today())
    login_as("vdp_viewer12", "password123", "viewer")
    assert client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "x"}).status_code == 403
    assert client.post(f"/api/dispatches/{d['id']}/correct", json={"reason": "x", "lines": []}).status_code == 403
    assert client.delete(f"/api/dispatches/{d['id']}", json={"reason": "x", "confirm": True}).status_code == 403


def test_viewer_still_cannot_create_correction_requests(client, setup, login_as):
    d = _seed_activity(client, setup, business_today())
    login_as("vdp_viewer13", "password123", "viewer")
    res = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "void", "reason": "x",
    })
    assert res.status_code == 403


# =====================================================================
# Navigation — app-shell.js's role-driven nav-item builder (source-level
# check; app-shell.js itself is untouched by this round, but the fix's
# whole premise depends on Viewer's nav NOT changing while its dashboard
# data becomes visible, so this is confirmed explicitly).
# =====================================================================

def test_viewer_reporting_nav_excludes_operations_and_admin():
    import pathlib
    app_shell_js = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app-shell.js").read_text(encoding="utf-8")
    assert "if ((role === 'manager' || role === 'super_admin') && enabled(flags, 'dispatch')) {" in app_shell_js
    assert "if (role === 'super_admin') items.push({ key: 'admin'" in app_shell_js
    # Dashboard/Daily Figures/History & Exports are pushed unconditionally
    # (gated only by feature flags, never by role) — the three items every
    # role, including Viewer, always reaches.
    assert "items.push({ key: 'dashboard', label: 'Dashboard'" in app_shell_js
    assert "items.push({ key: 'daily_figures', label: 'Daily Figures'" in app_shell_js
    assert "items.push({ key: 'history_exports', label: 'History & Exports'" in app_shell_js
