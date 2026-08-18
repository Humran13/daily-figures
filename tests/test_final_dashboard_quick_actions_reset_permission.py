"""
Targeted fix: Dashboard Quick Actions still showed "Reset Daily Values"
to Manager, even though the top nav (app-shell.js), the /api/daily-reset*
routes, and the /reset-daily-values.html page guard were all already
tightened to Super Administrator only in an earlier round. This file
proves the Quick Actions link now matches those three, and that nothing
else on the Dashboard changed.
"""
import re
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DASHBOARD_HTML = (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


def _quick_actions_body():
    return re.search(r"function renderQuickActions\(role\)\{(.*?)\n\}", DASHBOARD_HTML, re.DOTALL).group(1)


@pytest.fixture
def super_admin(login_as):
    return login_as("qar_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = client.post("/api/admin/products", json={"name": "QAR Product"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return {"product": product}


# =====================================================================
# 1-4: source-level role gate (no JS/browser runner in this project —
# every other Dashboard/nav test in this suite uses the same pattern)
# =====================================================================

def test_manager_quick_actions_does_not_offer_reset_daily_values():
    body = _quick_actions_body()
    assert not re.search(
        r"if\(role === 'manager' \|\| role === 'super_admin'\)\{\s*actions\.push\(\{href:'/reset-daily-values\.html'",
        body,
    ), "Manager must not be gated into Reset Daily Values on the Dashboard Quick Actions"


def test_super_admin_quick_actions_offers_reset_daily_values():
    body = _quick_actions_body()
    assert re.search(
        r"if\(role === 'super_admin'\)\{\s*actions\.push\(\{href:'/reset-daily-values\.html', label:'Reset Daily Values'\}\);",
        body,
    ), "Super Administrator must still be offered Reset Daily Values on the Dashboard Quick Actions"


def test_operator_role_branch_never_reaches_reset_daily_values():
    # Operator can't reach dashboard.html at all (see the live page-guard
    # test below) — this proves it at the source level too: nothing in
    # renderQuickActions() would push the Reset Daily Values action for
    # role === 'operator' even if it somehow ran.
    body = _quick_actions_body()
    for line in body.splitlines():
        if "reset-daily-values.html" in line or "role === 'operator'" in line:
            assert "role === 'operator'" not in line


def test_viewer_quick_actions_card_is_entirely_hidden():
    body = _quick_actions_body()
    idx = body.index("role === 'viewer'")
    viewer_branch = body[idx:body.index("return", idx)]
    assert "classList.add('hidden')" in viewer_branch


# =====================================================================
# 5-6: backend permission
# =====================================================================

def test_manager_forged_reset_preview_api_call_returns_403(client, setup, login_as):
    login_as("qar_mgr", "password123", "manager")
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 403


def test_manager_forged_reset_execute_api_call_returns_403(client, setup, login_as):
    login_as("qar_mgr2", "password123", "manager")
    res = client.post("/api/daily-reset", json={"date": "2026-08-01", "shift": "Day", "reason": "trying anyway"})
    assert res.status_code == 403


def test_super_admin_reset_api_access_remains_allowed(client, setup, super_admin):
    res = client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 200


def test_operator_and_viewer_reset_api_access_remains_forbidden(client, setup, login_as):
    login_as("qar_op", "password123", "operator")
    assert client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day"}).status_code == 403
    client.post("/api/logout")
    login_as("qar_viewer", "password123", "viewer")
    assert client.post("/api/daily-reset/preview", json={"date": "2026-08-01", "shift": "Day"}).status_code == 403


def test_dashboard_page_guard_still_excludes_operator(client, setup, login_as):
    login_as("qar_op2", "password123", "operator")
    res = client.get("/dashboard.html")
    assert res.status_code in (302, 303)
    assert res.headers["Location"] != "/dashboard.html"


def test_manager_page_guard_still_excludes_reset_page(client, setup, login_as):
    login_as("qar_mgr3", "password123", "manager")
    res = client.get("/reset-daily-values.html")
    assert res.status_code in (302, 303)
    assert res.headers["Location"] != "/reset-daily-values.html"


def test_super_admin_page_guard_still_allows_reset_page(client, setup, super_admin):
    res = client.get("/reset-daily-values.html")
    assert res.status_code == 200


# =====================================================================
# 7-8: nothing else on the Dashboard changed
# =====================================================================

def test_other_quick_actions_unchanged():
    body = _quick_actions_body()
    assert "if(role === 'manager' || role === 'super_admin'){\n    actions.push({href:'/dispatch.html', label:'Open Operations'});" in body
    assert "actions.push({href:'/', label:'View Daily Figures'});" in body
    assert "actions.push({href:'/history.html', label:'Open History & Exports'});" in body
    assert "if(role === 'super_admin'){\n    actions.push({href:'/admin.html', label:'Open Admin'});" in body


def test_manager_still_sees_open_operations_and_other_actions():
    body = _quick_actions_body()
    assert re.search(
        r"if\(role === 'manager' \|\| role === 'super_admin'\)\{\s*actions\.push\(\{href:'/dispatch\.html', label:'Open Operations'\}\);",
        body,
    )


def test_reset_calculations_and_reset_service_behavior_untouched(client, setup, super_admin):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    res = client.post("/api/daily-reset", json={
        "date": "2026-08-01", "shift": "Day", "product_id": pid,
        "reason": "confirming reset behavior is unchanged", "mode": "full",
        "confirmation_text": "FULL RESET 2026-08-01 DAY",
    })
    assert res.status_code == 200
    after = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert after["opening"]["base_qty"] == 0


def test_dashboard_no_other_role_gate_lines_changed():
    # Snapshot the exact set of role-gate `if(...)` lines inside
    # renderQuickActions() — proves this fix touched only the one line
    # (Reset Daily Values), not Open Operations/Admin/Viewer.
    body = _quick_actions_body()
    gate_lines = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("if(role ===")]
    assert gate_lines == [
        "if(role === 'viewer'){",
        "if(role === 'manager' || role === 'super_admin'){",
        "if(role === 'super_admin'){",
        "if(role === 'super_admin'){",
    ]
