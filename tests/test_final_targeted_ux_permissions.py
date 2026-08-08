"""
Targeted UX + permission improvements (final consolidated round):
  - Super Admin customer rename (in-place, ID preserved, snapshot backfill,
    dedicated audit entry).
  - Returns "Name & Sign" defaults to the authenticated account, server-
    enforced against Operator impersonation.
  - Dispatch/Returns/Production History standardized to Reopen/Edit/
    Delete/Print (Void/Duplicate removed from the UI only — the backend
    endpoints are untouched).
  - Returns/Production gain the same permanent hard delete Dispatch
    already had.
  - Viewer Dashboard no longer shows Quick Actions.
  - Manager/Super Admin can re-review an already-submitted Daily Figures
    period (frontend now routes them straight to the review screen,
    which already had a working Reopen action).
  - Dashboard Per-Product Daily Figures is now a horizontally-scrollable
    table instead of a card grid that could wrap Closing Stock onto its
    own line.

No stock formula, ledger/cutover logic, packaging rule, or Operator Daily
Figures behavior is touched by any of this.
"""
import json
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
ADMIN_HTML = (STATIC / "admin.html").read_text(encoding="utf-8")
DISPATCH_HTML = (STATIC / "dispatch.html").read_text(encoding="utf-8")
RETURNS_HTML = (STATIC / "returns.html").read_text(encoding="utf-8")
PRODUCTION_HTML = (STATIC / "production.html").read_text(encoding="utf-8")
DASHBOARD_HTML = (STATIC / "dashboard.html").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, cartons_to_packs=10, packs_to_pieces=10):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces,
    })
    return product


def _make_customer(client, category_id, name):
    return client.post("/api/admin/customers", json={
        "name": name, "sales_category_id": category_id, "confirm_not_duplicate": True,
    }).get_json()


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client, "TUX Product")
    category = client.post("/api/admin/sales-categories", json={"name": "TUX Category"}).get_json()
    customer = _make_customer(client, category["id"], "TUX Recipient")
    return {"product": product, "category": category, "customer": customer}


def _issued_base_qty(client, product_id, date, shift="Day"):
    row = client.get(f"/api/daily-figures/{product_id}?date={date}&shift={shift}").get_json()
    return row["issued"]["base_qty"]


# =====================================================================
# SECTION 14 — CUSTOMER NAME EDIT
# =====================================================================

def test_super_admin_can_rename_customer(client, setup):
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"name": "TUX Recipient Renamed"})
    assert res.status_code == 200
    assert res.get_json()["name"] == "TUX Recipient Renamed"


def test_manager_cannot_rename_customer(client, setup, login_as):
    # Final correction — customer rename is Super Administrator only.
    # Manager keeps every OTHER field this same endpoint already granted
    # (category/active/contact_info/notes/sales_category_id) — only "name"
    # is blocked.
    login_as("tux_mgr1", "password123", "manager")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"name": "Manager Renamed"})
    assert res.status_code == 403
    unchanged = client.get("/api/admin/customers").get_json()
    assert next(c for c in unchanged if c["id"] == setup["customer"]["id"])["name"] == "TUX Recipient"


def test_manager_can_still_edit_other_customer_fields(client, setup, login_as):
    # The rename restriction is scoped to "name" only — every other field
    # this endpoint already let Manager edit remains editable.
    login_as("tux_mgr1b", "password123", "manager")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"contact_info": "0700-000-000"})
    assert res.status_code == 200
    assert res.get_json()["contact_info"] == "0700-000-000"


def test_operator_cannot_rename_customer(client, setup, login_as):
    login_as("tux_op1", "password123", "operator")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"name": "Operator Attempt"})
    assert res.status_code == 403


def test_viewer_cannot_rename_customer(client, setup, login_as):
    login_as("tux_viewer1", "password123", "viewer")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"name": "Viewer Attempt"})
    assert res.status_code == 403


def test_customer_id_does_not_change_after_rename(client, setup):
    cust_id = setup["customer"]["id"]
    res = client.patch(f"/api/admin/customers/{cust_id}", json={"name": "Same ID New Name"})
    assert res.get_json()["id"] == cust_id


def test_existing_dispatches_remain_linked_to_same_customer_id(client, setup):
    cust_id = setup["customer"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-1", "date": "2026-08-01", "customer_id": cust_id,
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.patch(f"/api/admin/customers/{cust_id}", json={"name": "Linked Rename"})
    still = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert still["customer_id"] == cust_id


def test_updated_name_appears_in_dispatch_history(client, setup):
    cust_id = setup["customer"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-2", "date": "2026-08-01", "customer_id": cust_id,
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.patch(f"/api/admin/customers/{cust_id}", json={"name": "History Shows This"})
    updated = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert updated["customer_name"] == "History Shows This"
    listed = client.get("/api/dispatches?limit=200").get_json()["results"]
    row = next(r for r in listed if r["id"] == d["id"])
    assert row["customer_name"] == "History Shows This"


def test_audit_entry_captures_old_and_new_name(client, setup, app):
    cust_id = setup["customer"]["id"]
    client.patch(f"/api/admin/customers/{cust_id}", json={"name": "Audited New Name"})
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="rename", entity_type="customer", entity_id=str(cust_id)).first()
        assert entry is not None
        before = json.loads(entry.before_json)
        after = json.loads(entry.after_json)
        assert before["previous_name"] == "TUX Recipient"
        assert after["new_name"] == "Audited New Name"
        assert before["customer_id"] == cust_id


def test_no_stock_figures_change_after_rename(client, setup):
    cust_id = setup["customer"]["id"]
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-3", "date": "2026-08-01", "customer_id": cust_id,
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    before_issued = _issued_base_qty(client, pid, "2026-08-01")

    client.patch(f"/api/admin/customers/{cust_id}", json={"name": "Stock Unaffected Rename"})

    after_issued = _issued_base_qty(client, pid, "2026-08-01")
    assert before_issued == after_issued == 300


def test_rename_prevented_on_duplicate_name_without_confirmation(client, setup):
    _make_customer(client, setup["category"]["id"], "Existing Similar Name")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"name": "Existing Similar Name"})
    assert res.status_code == 409
    assert res.get_json()["warning"] == "similar_customers_exist"


def test_rename_duplicate_can_proceed_with_explicit_confirmation(client, setup):
    _make_customer(client, setup["category"]["id"], "Confirmed Duplicate Target")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={
        "name": "Confirmed Duplicate Target", "confirm_not_duplicate": True,
    })
    assert res.status_code == 200


def test_admin_html_has_rename_action():
    assert "data-rename-customer" in ADMIN_HTML


def test_admin_html_rename_handler_checks_role_explicitly_not_just_page_access():
    idx = ADMIN_HTML.index("document.querySelectorAll('[data-rename-customer]')")
    end = ADMIN_HTML.index("document.querySelectorAll('[data-toggle-customer]')")
    body = ADMIN_HTML[idx:end]
    assert "currentUserRole !== 'super_admin'" in body


# =====================================================================
# SECTION 15 — RETURNS DEFAULT SIGNER
# =====================================================================

def _create_return(client, product_id, cartons=1, **kwargs):
    body = {"date": "2026-08-01", "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}]}
    body.update(kwargs)
    return client.post("/api/returns", json=body)


def test_operator_is_default_signer(client, setup, login_as):
    login_as("tux_signer_op", "password123", "operator")
    res = _create_return(client, setup["product"]["id"])
    assert res.status_code == 201
    assert res.get_json()["signed_by_name"] == "tux_signer_op"


def test_operator_cannot_forge_another_signer(client, setup, login_as):
    login_as("tux_signer_op2", "password123", "operator")
    res = _create_return(client, setup["product"]["id"], signed_by_name="Someone Else Entirely")
    assert res.status_code == 201
    assert res.get_json()["signed_by_name"] == "tux_signer_op2"  # forged value silently overridden


def test_operator_cannot_forge_signer_via_update_either(client, setup, login_as):
    login_as("tux_signer_op3", "password123", "operator")
    created = _create_return(client, setup["product"]["id"]).get_json()
    res = client.patch(f"/api/returns/{created['id']}", json={"signed_by_name": "Forged Via Patch"})
    assert res.status_code == 200
    assert res.get_json()["signed_by_name"] == "tux_signer_op3"


def test_manager_can_override_signer(client, setup, login_as):
    login_as("tux_signer_mgr", "password123", "manager")
    res = _create_return(client, setup["product"]["id"], signed_by_name="Authorized Warehouse Lead")
    assert res.status_code == 201
    assert res.get_json()["signed_by_name"] == "Authorized Warehouse Lead"


def test_manager_defaults_to_self_when_not_overridden(client, setup, login_as):
    login_as("tux_signer_mgr2", "password123", "manager")
    res = _create_return(client, setup["product"]["id"])
    assert res.get_json()["signed_by_name"] == "tux_signer_mgr2"


def test_super_admin_can_override_signer(client, setup, super_admin):
    res = _create_return(client, setup["product"]["id"], signed_by_name="Authorized Site Manager")
    assert res.status_code == 201
    assert res.get_json()["signed_by_name"] == "Authorized Site Manager"


def test_returns_stock_calculation_unchanged_by_signer_logic(client, setup, login_as):
    login_as("tux_signer_op4", "password123", "operator")
    pid = setup["product"]["id"]
    created = _create_return(client, pid, cartons=4).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    row = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert row["return_"]["base_qty"] == 400


def test_returns_html_signer_field_defaults_and_locks_for_operator():
    assert "applySignerDefault" in RETURNS_HTML
    assert "input.readOnly = currentUser.role === 'operator'" in RETURNS_HTML


# =====================================================================
# SECTION 16 — STANDARD HISTORY ACTION BUTTONS
# =====================================================================

def _detail_actions_body(html, marker="const actions = document.getElementById('detailActions');"):
    idx = html.index(marker)
    end = html.index("actions.innerHTML = buttons.join('');", idx)
    return html[idx:end]


def test_dispatch_elevated_actions_are_reopen_edit_delete_print():
    body = _detail_actions_body(DISPATCH_HTML)
    assert 'data-action="reopen">Reopen<' in body
    assert 'data-action="correct">Edit<' in body
    assert 'data-action="delete">Delete<' in body
    assert 'data-action="print">Print<' in DISPATCH_HTML[DISPATCH_HTML.index(body):DISPATCH_HTML.index(body) + len(body) + 200]


def test_dispatch_no_void_or_duplicate_button():
    body = _detail_actions_body(DISPATCH_HTML)
    assert 'data-action="void"' not in body
    assert 'data-action="duplicate"' not in body
    assert ">Void<" not in body
    assert ">Duplicate<" not in body


def test_returns_elevated_actions_standardized():
    body = _detail_actions_body(RETURNS_HTML)
    assert 'data-action="reopen">Reopen<' in body
    assert 'data-action="correct">Edit<' in body
    assert 'data-action="delete">Delete<' in body
    assert 'data-action="void"' not in body


def test_production_elevated_actions_standardized():
    body = _detail_actions_body(PRODUCTION_HTML)
    assert 'data-action="reopen">Reopen<' in body
    assert 'data-action="correct">Edit<' in body
    assert 'data-action="delete">Delete<' in body
    assert 'data-action="void"' not in body


def test_operator_and_viewer_do_not_gain_elevated_actions(client, setup, login_as):
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-PERM-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")

    login_as("tux_hist_op", "password123", "operator")
    assert client.post(f"/api/dispatches/{d['id']}/reopen", json={"reason": "x"}).status_code == 403
    assert client.post(f"/api/dispatches/{d['id']}/correct", json={"reason": "x", "lines": []}).status_code == 403
    assert client.delete(f"/api/dispatches/{d['id']}", json={"reason": "x", "confirm": True}).status_code == 403

    client.post("/api/logout")
    login_as("tux_hist_viewer", "password123", "viewer")
    assert client.post(f"/api/dispatches/{d['id']}/reopen", json={"reason": "x"}).status_code == 403
    assert client.delete(f"/api/dispatches/{d['id']}", json={"reason": "x", "confirm": True}).status_code == 403


def test_reopen_does_not_duplicate_stock_dispatch(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-REOPEN-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _issued_base_qty(client, pid, "2026-08-01") == 500

    client.post(f"/api/dispatches/{d['id']}/reopen", json={"reason": "fixing quantity"})
    assert _issued_base_qty(client, pid, "2026-08-01") == 0  # draft again — no longer contributes

    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _issued_base_qty(client, pid, "2026-08-01") == 500  # counted exactly once again, never doubled


def test_edit_action_uses_existing_correction_service(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-EDIT-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "correcting via Edit action", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert _issued_base_qty(client, pid, "2026-08-01") == 300


def test_delete_retains_permanent_delete_semantics_for_returns(client, setup, super_admin, app):
    pid = setup["product"]["id"]
    created = _create_return(client, pid, cartons=2).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["return_"]["base_qty"] == 200

    res = client.delete(f"/api/returns/{created['id']}", json={"reason": "entered in error", "confirm": True})
    assert res.status_code == 200
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["return_"]["base_qty"] == 0

    with app.app_context():
        from webapp.models.return_record import ReturnRecord
        assert _db_get(app, ReturnRecord, created["id"]) is None


def _db_get(app, model, pk):
    from webapp.extensions import db as _db
    return _db.session.get(model, pk)


def test_delete_retains_permanent_delete_semantics_for_production(client, setup, super_admin, app):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 6, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["production"]["base_qty"] == 600

    res = client.delete(f"/api/production/{prod['id']}", json={"reason": "entered in error", "confirm": True})
    assert res.status_code == 200
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["production"]["base_qty"] == 0

    with app.app_context():
        from webapp.models.production_record import ProductionRecord
        assert _db_get(app, ProductionRecord, prod["id"]) is None


def test_returns_delete_requires_manager_or_super_admin(client, setup, login_as):
    pid = setup["product"]["id"]
    created = _create_return(client, pid).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    login_as("tux_ret_del_op", "password123", "operator")
    assert client.delete(f"/api/returns/{created['id']}", json={"reason": "x", "confirm": True}).status_code == 403


def test_production_delete_requires_manager_or_super_admin(client, setup, login_as):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    login_as("tux_prod_del_op", "password123", "operator")
    assert client.delete(f"/api/production/{prod['id']}", json={"reason": "x", "confirm": True}).status_code == 403


def test_returns_delete_audit_snapshot_survives(client, setup, super_admin, app):
    pid = setup["product"]["id"]
    created = _create_return(client, pid).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    client.delete(f"/api/returns/{created['id']}", json={"reason": "audit check", "confirm": True})
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="permanent_delete_return", entity_id=str(created["id"])).first()
        assert entry is not None
        before = json.loads(entry.before_json)
        assert before["operation"] == "permanent_delete_return"
        assert before["deletion_reason"] == "audit check"


def test_production_delete_audit_snapshot_survives(client, setup, super_admin, app):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    client.delete(f"/api/production/{prod['id']}", json={"reason": "audit check", "confirm": True})
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="permanent_delete_production", entity_id=str(prod["id"])).first()
        assert entry is not None
        before = json.loads(entry.before_json)
        assert before["operation"] == "permanent_delete_production"
        assert before["deletion_reason"] == "audit check"


def test_print_action_present_on_all_three_history_pages():
    for html in (DISPATCH_HTML, RETURNS_HTML, PRODUCTION_HTML):
        assert 'data-action="print">Print<' in html
        assert "window.print()" in html


# =====================================================================
# SECTION 17 — DAILY FIGURES RE-REVIEW
# =====================================================================

@pytest.fixture
def review_setup(client, super_admin):
    product = _make_product(client, "TUX Review Product")
    return {"product": product}


def _mark_reviewed(client, date, shift, product_id):
    return client.post("/api/daily-review/mark-reviewed", json={
        "date": date, "shift": shift, "product_id": product_id, "edited": False,
    })


def _submit(client, date, shift):
    return client.post("/api/daily-review/submit", json={"date": date, "shift": shift})


def test_manager_can_reopen_previously_submitted_review(client, review_setup, login_as):
    login_as("tux_review_mgr", "password123", "manager")
    pid = review_setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    assert _submit(client, "2026-08-01", "Day").status_code == 200

    res = client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "needs correction"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "reopened"


def test_super_admin_can_reopen_previously_submitted_review(client, review_setup, super_admin):
    pid = review_setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    _submit(client, "2026-08-01", "Day")
    res = client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "needs correction"})
    assert res.status_code == 200


def test_operator_cannot_reopen_review(client, review_setup, login_as):
    login_as("tux_review_op", "password123", "operator")
    res = client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "x"})
    assert res.status_code == 403


def test_viewer_cannot_reopen_review(client, review_setup, login_as):
    login_as("tux_review_viewer", "password123", "viewer")
    res = client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "x"})
    assert res.status_code == 403


def test_reopen_and_resubmit_does_not_create_duplicate_period(client, review_setup, super_admin, app):
    pid = review_setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    _submit(client, "2026-08-01", "Day")
    client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "fix"})
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    res = _submit(client, "2026-08-01", "Day")
    assert res.status_code == 200
    with app.app_context():
        from webapp.models.daily_review_session import DailyReviewSession
        assert DailyReviewSession.query.filter_by(date="2026-08-01", shift="Day").count() == 1


def test_reopen_action_is_audited(client, review_setup, super_admin, app):
    pid = review_setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    _submit(client, "2026-08-01", "Day")
    client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "audit this reopen"})
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="reopen_review", entity_id="2026-08-01|Day").first()
        assert entry is not None


def test_resubmission_after_reopen_is_audited(client, review_setup, super_admin, app):
    pid = review_setup["product"]["id"]
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    _submit(client, "2026-08-01", "Day")
    client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "fix"})
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    _submit(client, "2026-08-01", "Day")
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entries = AuditLog.query.filter_by(action="submit_review", entity_id="2026-08-01|Day").all()
        assert len(entries) == 2  # original submission + resubmission, both preserved


def test_ledger_reconciles_after_reopen_and_opening_stock_correction(client, review_setup, super_admin):
    pid = review_setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _mark_reviewed(client, "2026-08-01", "Day", pid)
    _submit(client, "2026-08-01", "Day")
    client.post("/api/daily-review/reopen", json={"date": "2026-08-01", "shift": "Day", "reason": "opening was wrong"})

    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 12, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "corrected count",
    })
    row = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert row["opening"]["base_qty"] == 1200
    assert row["closing"]["base_qty"] == row["opening"]["base_qty"] + row["production"]["base_qty"] + row["return_"]["base_qty"] - row["issued"]["base_qty"]


def test_render_current_view_routes_elevated_roles_to_review_screen_when_submitted():
    idx = INDEX_HTML.index("async function renderCurrentView(){")
    end = INDEX_HTML.index("\n// ---------- Operator Daily Figures redesign", idx)
    body = INDEX_HTML[idx:end]
    assert "currentIdx === 0" in body
    assert "summary.session.status === 'submitted'" in body
    assert "showReviewScreen(date, shift)" in body


def test_render_current_view_check_is_elevated_role_gated():
    idx = INDEX_HTML.index("async function renderCurrentView(){")
    end = INDEX_HTML.index("\n// ---------- Operator Daily Figures redesign", idx)
    body = INDEX_HTML[idx:end]
    assert "isElevated && currentIdx === 0" in body


def test_reopen_review_button_still_present_in_review_screen():
    assert 'id="reopenReviewBtn"' in INDEX_HTML
    assert "Reopen this submitted review" in INDEX_HTML


# =====================================================================
# SECTION 18 — VIEWER DASHBOARD QUICK ACTIONS
# =====================================================================

def test_viewer_does_not_see_quick_actions_markup():
    idx = DASHBOARD_HTML.index("function renderQuickActions(role){")
    end = DASHBOARD_HTML.index("\n}", idx)
    body = DASHBOARD_HTML[idx:end]
    assert "role === 'viewer'" in body
    assert "classList.add('hidden')" in body


def test_manager_and_super_admin_still_see_quick_actions_markup():
    idx = DASHBOARD_HTML.index("function renderQuickActions(role){")
    end = DASHBOARD_HTML.index("\n}", idx)
    body = DASHBOARD_HTML[idx:end]
    assert "Open Operations" in body
    assert "Reset Daily Values" in body
    assert "Open Admin" in body


def test_dashboard_figures_unchanged_regardless_of_role(client, setup, login_as):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    as_super_admin = client.get("/api/dashboard?date=2026-08-01").get_json()

    login_as("tux_dash_viewer", "password123", "viewer")
    as_viewer = client.get("/api/dashboard?date=2026-08-01").get_json()
    assert as_super_admin["stock_summary"] == as_viewer["stock_summary"]


def test_dashboard_backend_permission_unchanged(client, login_as):
    login_as("tux_dash_op", "password123", "operator")
    res = client.get("/api/dashboard?date=2026-08-01")
    assert res.status_code == 403  # unchanged — Operator never had dashboard access


# =====================================================================
# SECTION 19 — DASHBOARD TABLE HORIZONTAL SCROLLING
# =====================================================================

def test_dfig_table_has_scroll_container():
    assert '.dfig-table-wrap{' in DASHBOARD_HTML
    idx = DASHBOARD_HTML.index(".dfig-table-wrap{")
    block = DASHBOARD_HTML[idx:DASHBOARD_HTML.index("}", idx)]
    assert "overflow-x:auto" in block
    assert "-webkit-overflow-scrolling:touch" in block


def test_dfig_table_has_sensible_minimum_width():
    idx = DASHBOARD_HTML.index("table.dfig-table{")
    block = DASHBOARD_HTML[idx:DASHBOARD_HTML.index("}", idx)]
    import re
    m = re.search(r"min-width:\s*(\d+)px", block)
    assert m and int(m.group(1)) >= 500


def test_dfig_numeric_cells_do_not_wrap():
    idx = DASHBOARD_HTML.index("table.dfig-table td{")
    block = DASHBOARD_HTML[idx:DASHBOARD_HTML.index("}", idx)]
    assert "white-space:nowrap" in block


def test_dfig_closing_stock_stays_in_the_same_row():
    idx = DASHBOARD_HTML.index("function figureRow(")
    end = DASHBOARD_HTML.index("\nfunction _dailyFiguresTableShell", idx)
    body = DASHBOARD_HTML[idx:end]
    assert body.count("<tr>") == 1
    assert body.count("</tr>") == 1
    assert "dfig-closing" in body


def test_dfig_all_six_columns_present():
    assert '<th>Product</th><th>Opening Stock</th><th>Production</th><th>Returns</th><th>Issued</th><th>Closing Stock</th>' in DASHBOARD_HTML


def test_dfig_no_full_page_horizontal_overflow_introduced():
    # .dfig-table-wrap scrolls on its own; the page-level containers (.shell,
    # body) must never gain horizontal overflow. (.product-table-wrap is a
    # separate, pre-existing scroll container for a different section —
    # unrelated to this round, left untouched.)
    shell_idx = DASHBOARD_HTML.index(".shell{")
    shell_block = DASHBOARD_HTML[shell_idx:DASHBOARD_HTML.index("}", shell_idx)]
    assert "overflow-x" not in shell_block
    body_idx = DASHBOARD_HTML.index("body{")
    body_block = DASHBOARD_HTML[body_idx:DASHBOARD_HTML.index("}", body_idx)]
    assert "overflow-x" not in body_block


def test_dfig_not_converted_to_cards():
    assert "figure-vals" not in DASHBOARD_HTML
    assert "figure-row-head" not in DASHBOARD_HTML


def test_dfig_product_names_may_wrap_safely():
    idx = DASHBOARD_HTML.index("table.dfig-table td:first-child{")
    block = DASHBOARD_HTML[idx:DASHBOARD_HTML.index("}", idx)]
    assert "white-space:normal" in block


def test_dfig_values_rendered_unmodified_from_backend(client, setup, super_admin):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 7, "packs": 0, "pieces": 0},
    })
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-DFIG-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")

    data = client.get("/api/dashboard?date=2026-08-01").get_json()
    row = next(r for r in data["daily_figures_today"] if r["product_id"] == pid)
    # date_range_summary() rows carry the base_qty as a separate flat key
    # alongside the nested cartons/packs/pieces split (see
    # stock_service.date_range_summary()) — unaffected by this round.
    assert row["opening_base_qty"] == 700
    assert row["issued_base_qty"] == 200
    assert row["closing_base_qty"] == 500
