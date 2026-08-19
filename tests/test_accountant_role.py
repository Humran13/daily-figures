"""
Accountant role — a fifth canonical role alongside Super Admin/Manager/
Operator/Viewer. Reuses the existing role architecture (webapp/models/
user.py's ROLES) and the existing correction-request review workflow
(webapp/services/correction_request_service.py) — no second/competing
permission system or request-processing engine.

Access shape:
  - Viewer-level read access: Dashboard, Daily Figures, History & Exports
    (including exports) — same authoritative read APIs Viewer already
    uses, never a second calculation path.
  - PLUS Requests: sees the Requests nav/badge, can list/approve/reject
    correction requests exactly like Manager/Super Admin. Approving never
    grants Accountant a direct edit — the one-time 24-hour grant always
    goes to the original requesting Operator (see
    webapp/services/correction_request_service.py's approve_request()).
  - No operational mutation authority whatsoever: no create/edit/void/
    delete on Dispatch/Returns/Production, no stock adjustments, no
    Opening Stock edits, no Reset Daily Values, no Admin/user management.

There is no standalone "verified" request status in this app (statuses
are pending/approved/rejected/completed/expired — see
webapp/models/correction_request.py) — Accountant reviews with the same
Approve/Reject controls Manager/Super Admin already have.
"""
import pytest


def _make_product(client, name="Acct Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def super_admin(login_as):
    return login_as("acct_root", "password123", "super_admin")


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "Acct Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Acct Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _historical_dispatch(client, app, setup, dispatch_number="ACC-D1", cartons=5):
    import datetime
    from webapp.services.business_calendar import utcnow
    d = client.post("/api/dispatches", json={
        "dispatch_number": dispatch_number, "date": "2020-01-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    with app.app_context():
        from webapp.extensions import db
        from webapp.models.dispatch import Dispatch
        row = db.session.get(Dispatch, d["id"])
        row.created_at = utcnow() - datetime.timedelta(hours=25)
        db.session.commit()
    return d


# =====================================================================
# SECTION 18 — ROLE CREATION
# =====================================================================

def test_accountant_is_a_valid_role():
    from webapp.models.user import ROLE_ACCOUNTANT, ROLES
    assert ROLE_ACCOUNTANT == "accountant"
    assert ROLE_ACCOUNTANT in ROLES


def test_existing_roles_unchanged():
    from webapp.models.user import ROLE_MANAGER, ROLE_OPERATOR, ROLE_SUPER_ADMIN, ROLE_VIEWER
    assert ROLE_SUPER_ADMIN == "super_admin"
    assert ROLE_MANAGER == "manager"
    assert ROLE_OPERATOR == "operator"
    assert ROLE_VIEWER == "viewer"


def test_super_admin_can_create_accountant_user(client, super_admin):
    res = client.post("/api/admin/users", json={
        "username": "acct1", "password": "password123", "role": "accountant",
    })
    assert res.status_code == 201
    assert res.get_json()["role"] == "accountant"


def test_super_admin_can_edit_user_to_accountant(client, super_admin, login_as):
    created = client.post("/api/admin/users", json={
        "username": "acct2", "password": "password123", "role": "viewer",
    }).get_json()
    res = client.patch(f"/api/admin/users/{created['id']}", json={"role": "accountant"})
    assert res.status_code == 200
    assert res.get_json()["role"] == "accountant"


def test_manager_cannot_create_accountant_user(client, login_as):
    login_as("acct_mgr_nope", "password123", "manager")
    res = client.post("/api/admin/users", json={
        "username": "acct3", "password": "password123", "role": "accountant",
    })
    assert res.status_code == 403


def test_accountant_can_log_in(client, super_admin):
    client.post("/api/admin/users", json={"username": "acctlogin", "password": "password123", "role": "accountant"})
    client.post("/api/logout")
    res = client.post("/api/login", json={"username": "acctlogin", "password": "password123"})
    assert res.status_code == 200
    assert res.get_json()["user"]["role"] == "accountant"


def test_role_serializes_correctly_in_session(client, login_as):
    login_as("acct_sess", "password123", "accountant")
    res = client.get("/api/session")
    assert res.get_json()["user"]["role"] == "accountant"


def test_role_label_displays_cleanly_not_raw_internal_format():
    from pathlib import Path
    app_shell_js = (Path(__file__).parent.parent / "static" / "app-shell.js").read_text(encoding="utf-8")
    assert "accountant: 'Accountant'," in app_shell_js
    assert "ROLE_ACCOUNTANT" not in app_shell_js
    assert "accountant_role" not in app_shell_js


# =====================================================================
# SECTION 19 — READ ACCESS PARITY WITH VIEWER
# =====================================================================

@pytest.mark.parametrize("role", ["viewer", "accountant"])
def test_dashboard_api_accessible_read_only(client, login_as, role):
    login_as(f"acct_read_{role}", "password123", role)
    res = client.get("/api/dashboard?date=2026-07-28")
    assert res.status_code == 200


def test_accountant_and_viewer_get_identical_dashboard_data(client, super_admin, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })

    login_as("acct_parity_viewer", "password123", "viewer")
    viewer_data = client.get("/api/dashboard?date=2026-08-01").get_json()
    client.post("/api/logout")

    login_as("acct_parity_accountant", "password123", "accountant")
    accountant_data = client.get("/api/dashboard?date=2026-08-01").get_json()

    # generated_at is a wall-clock stamp of when each request ran, not
    # derived report data — the two calls are seconds apart, so it's
    # expected to differ even though every other field must match exactly.
    viewer_data.pop("generated_at", None)
    accountant_data.pop("generated_at", None)
    assert accountant_data == viewer_data


@pytest.mark.parametrize("role", ["viewer", "accountant"])
def test_recipient_totals_reporting_accessible(client, login_as, role):
    login_as(f"acct_totals_{role}", "password123", role)
    res = client.get("/api/reports/recipient-totals?date_from=2026-07-01&date_to=2026-07-28&group_by=category")
    assert res.status_code == 200


def test_accountant_can_read_daily_figures(client, login_as, setup):
    login_as("acct_df_read", "password123", "accountant")
    res = client.get("/api/daily-figures?date=2026-07-28&shift=Day")
    assert res.status_code == 200


def test_accountant_can_read_dispatch_history(client, login_as, setup):
    login_as("acct_hist_dispatch", "password123", "accountant")
    res = client.get("/api/dispatches")
    assert res.status_code == 200


def test_accountant_can_read_returns_history(client, login_as, setup):
    login_as("acct_hist_returns", "password123", "accountant")
    res = client.get("/api/returns")
    assert res.status_code == 200


def test_accountant_can_read_production_history(client, login_as, setup):
    login_as("acct_hist_production", "password123", "accountant")
    res = client.get("/api/production")
    assert res.status_code == 200


def test_accountant_can_use_dispatch_export(client, login_as, setup):
    login_as("acct_export_dispatch", "password123", "accountant")
    res = client.get("/api/dispatches/export.csv")
    assert res.status_code == 200


def test_accountant_can_use_daily_figures_export(client, login_as, setup):
    login_as("acct_export_df", "password123", "accountant")
    res = client.get("/api/daily-figures/export.csv?date_from=2026-07-01&date_to=2026-07-28")
    assert res.status_code == 200


def test_accountant_can_use_returns_export(client, login_as, setup):
    login_as("acct_export_returns", "password123", "accountant")
    res = client.get("/api/returns/export.csv")
    assert res.status_code == 200


def test_accountant_can_use_production_export(client, login_as, setup):
    login_as("acct_export_production", "password123", "accountant")
    res = client.get("/api/production/export.csv")
    assert res.status_code == 200


# =====================================================================
# SECTION 20 — MUTATION DENIAL (server-side, not just hidden buttons)
# =====================================================================

def test_accountant_cannot_create_dispatch(client, login_as, setup):
    login_as("acct_no_create_dispatch", "password123", "accountant")
    res = client.post("/api/dispatches", json={
        "dispatch_number": "ACC-FORBID-1", "date": "2026-07-28", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_accountant_cannot_void_dispatch(client, app, super_admin, login_as, setup):
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-VOID-1")
    login_as("acct_no_void_dispatch", "password123", "accountant")
    res = client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "forged void attempt"})
    assert res.status_code == 403


def test_accountant_cannot_delete_dispatch(client, app, super_admin, login_as, setup):
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-DEL-1")
    login_as("acct_no_delete_dispatch", "password123", "accountant")
    res = client.delete(f"/api/dispatches/{d['id']}")
    assert res.status_code == 403


def test_accountant_cannot_correct_dispatch_directly(client, app, super_admin, login_as, setup):
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-CORRECT-1")
    login_as("acct_no_correct_dispatch", "password123", "accountant")
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "forged direct correction attempt",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_accountant_cannot_create_returns(client, login_as, setup):
    login_as("acct_no_create_returns", "password123", "accountant")
    res = client.post("/api/returns", json={
        "date": "2026-07-28", "returned_by": "x", "received_by": "y",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403


def test_accountant_cannot_create_production(client, login_as, setup):
    login_as("acct_no_create_production", "password123", "accountant")
    res = client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day", "product_id": setup["product"]["id"],
        "quantity": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


def test_accountant_cannot_upsert_daily_figures(client, login_as, setup):
    login_as("acct_no_daily_figures", "password123", "accountant")
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


def test_accountant_cannot_create_stock_adjustment(client, login_as, setup):
    login_as("acct_no_adjustment", "password123", "accountant")
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "forged adjustment attempt",
    })
    assert res.status_code == 403


def test_accountant_still_cannot_adjust_even_with_operator_flags_enabled(client, super_admin, login_as, setup):
    client.patch("/api/admin/operator-daily-figure-permissions", json={
        "can_edit_opening": True, "can_create_adjustments": True,
    })
    login_as("acct_flags_no_adjust", "password123", "accountant")
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "should still fail",
    })
    assert res.status_code == 403


def test_accountant_cannot_reset_daily_values(client, login_as, setup):
    login_as("acct_no_reset", "password123", "accountant")
    res = client.post("/api/daily-reset/preview", json={
        "date": "2020-01-01", "shift": "Day", "product_id": setup["product"]["id"],
    })
    assert res.status_code == 403


def test_accountant_cannot_reach_admin_mutation_apis(client, login_as, setup):
    login_as("acct_no_admin", "password123", "accountant")
    res = client.post("/api/admin/users", json={"username": "x", "password": "password123", "role": "viewer"})
    assert res.status_code == 403
    res2 = client.post("/api/admin/products", json={"name": "forged product"})
    assert res2.status_code == 403


def test_accountant_cannot_activate_ledger_cutover(client, login_as, setup):
    login_as("acct_no_cutover", "password123", "accountant")
    res = client.post("/api/ledger-cutover", json={
        "effective_date": "2026-07-28", "effective_shift": "Day", "reason": "forged cutover attempt",
    })
    assert res.status_code == 403


def test_accountant_cannot_delete_stock_adjustment(client, super_admin, login_as, setup):
    pid = setup["product"]["id"]
    adj = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "seed adjustment",
    }).get_json()
    login_as("acct_no_delete_adj", "password123", "accountant")
    res = client.delete(f"/api/daily-figures/adjustments/{adj['id']}")
    assert res.status_code == 403


# =====================================================================
# SECTION 21 — REQUESTS: navigation, badge, list, approve, reject
# =====================================================================

def test_accountant_sees_requests_navigation():
    from pathlib import Path
    app_shell_js = (Path(__file__).parent.parent / "static" / "app-shell.js").read_text(encoding="utf-8")
    idx = app_shell_js.index("items.push({ key: 'requests'")
    guard_line = app_shell_js[max(0, idx - 150):idx]
    assert "role === 'accountant'" in guard_line


def test_accountant_can_fetch_pending_count_badge(client, app, login_as, setup):
    login_as("acct_badge_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-BADGE-1")
    client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "accountant badge visibility check",
        "payload": {"lines": []},
    })
    client.post("/api/logout")

    login_as("acct_badge_reviewer", "password123", "accountant")
    res = client.get("/api/correction-requests/pending-count")
    assert res.status_code == 200
    assert res.get_json()["count"] == 1


def test_accountant_can_list_pending_requests(client, app, login_as, setup):
    login_as("acct_list_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-LIST-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "accountant listing visibility check",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_list_reviewer", "password123", "accountant")
    res = client.get("/api/correction-requests?status=pending")
    assert res.status_code == 200
    ids = [r["id"] for r in res.get_json()]
    assert req["id"] in ids


def test_accountant_can_open_request_details(client, app, login_as, setup):
    login_as("acct_detail_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-DETAIL-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "accountant detail visibility check",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_detail_reviewer", "password123", "accountant")
    res = client.get(f"/api/correction-requests?record_type=dispatch&record_id={d['id']}")
    assert res.status_code == 200
    assert any(r["id"] == req["id"] for r in res.get_json())


def test_accountant_can_approve_request(client, app, login_as, setup):
    login_as("acct_approve_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-APPROVE-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "accountant approval check",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_approve_reviewer", "password123", "accountant")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={"review_note": "looks fine"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "approved"


def test_accountant_can_reject_request(client, app, login_as, setup):
    login_as("acct_reject_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-REJECT-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "accountant rejection check",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_reject_reviewer", "password123", "accountant")
    res = client.post(f"/api/correction-requests/{req['id']}/reject", json={"review_note": "not sufficiently justified"})
    assert res.status_code == 200
    assert res.get_json()["status"] == "rejected"


def test_accountant_rejection_still_requires_a_review_note(client, app, login_as, setup):
    login_as("acct_reject_noreason_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-REJECT-NR-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "accountant rejection reason enforcement check",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_reject_noreason_reviewer", "password123", "accountant")
    res = client.post(f"/api/correction-requests/{req['id']}/reject", json={"review_note": ""})
    assert res.status_code == 400


def test_accountant_approval_grants_operator_not_accountant(client, app, login_as, setup):
    login_as("acct_grant_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-GRANT-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "grant must go to the requesting operator, not the accountant reviewer",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_grant_reviewer", "password123", "accountant")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})

    # Accountant reviewer holds no grant for this record (grant-status is
    # Operator-only in the first place, matching every other role).
    res_accountant = client.get(f"/api/correction-requests/grant-status?record_type=dispatch&record_id={d['id']}")
    assert res_accountant.status_code == 403

    # The original requesting Operator does hold it.
    client.post("/api/login", json={"username": "acct_grant_op", "password": "password123"})
    own_grant = client.get(f"/api/correction-requests/grant-status?record_type=dispatch&record_id={d['id']}").get_json()
    assert own_grant["has_active_grant"] is True


def test_accountant_approval_grant_is_requester_specific_and_one_use(client, app, login_as, setup):
    login_as("acct_specific_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-SPECIFIC-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "requester-specific one-use grant check",
        "payload": {"lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}]},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_specific_reviewer", "password123", "accountant")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={})
    client.post("/api/logout")

    client.post("/api/login", json={"username": "acct_specific_op", "password": "password123"})
    grant = client.get(f"/api/correction-requests/grant-status?record_type=dispatch&record_id={d['id']}").get_json()
    assert grant["has_active_grant"] is True

    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "using the accountant-approved grant",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200

    grant_after = client.get(f"/api/correction-requests/grant-status?record_type=dispatch&record_id={d['id']}").get_json()
    assert grant_after["has_active_grant"] is False  # consumed — one use only

    req_after = client.get(f"/api/correction-requests?record_id={d['id']}&record_type=dispatch").get_json()
    assert [r for r in req_after if r["id"] == req["id"]][0]["status"] == "completed"


def test_request_audit_identifies_accountant_as_reviewer(client, app, login_as, setup):
    login_as("acct_audit_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-AUDIT-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "audit attribution check for accountant reviewer",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_audit_reviewer", "password123", "accountant")
    client.post(f"/api/correction-requests/{req['id']}/approve", json={"review_note": "audited approval"})

    with app.app_context():
        import json as _json
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="approve", entity_type="correction_request", entity_id=str(req["id"])).first()
        assert entry is not None
        assert entry.username == "acct_audit_reviewer"
        after = _json.loads(entry.after_json)
        assert after["reviewer_role"] == "accountant"


def test_viewer_still_cannot_review_requests(client, app, login_as, setup):
    login_as("acct_viewer_deny_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-VIEWERDENY-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "viewer must remain unable to review requests",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_viewer_deny_viewer", "password123", "viewer")
    assert client.get("/api/correction-requests/pending-count").status_code == 403
    assert client.post(f"/api/correction-requests/{req['id']}/approve", json={}).status_code == 403


def test_operator_still_cannot_review_requests(client, app, login_as, setup):
    login_as("acct_op_deny_op1", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-OPDENY-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "operator must remain unable to review requests",
        "payload": {"lines": []},
    }).get_json()
    assert client.post(f"/api/correction-requests/{req['id']}/approve", json={}).status_code == 403


def test_manager_and_super_admin_review_authority_unaffected(client, app, super_admin, login_as, setup):
    login_as("acct_unaffected_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-UNAFFECTED-1")
    req = client.post("/api/correction-requests", json={
        "record_type": "dispatch", "record_id": d["id"], "action": "correct",
        "reason": "manager/super admin review authority must be unaffected",
        "payload": {"lines": []},
    }).get_json()
    client.post("/api/logout")

    login_as("acct_unaffected_mgr", "password123", "manager")
    res = client.post(f"/api/correction-requests/{req['id']}/approve", json={"review_note": "manager still approves fine"})
    assert res.status_code == 200


# =====================================================================
# SECTION 22 — NOTIFICATIONS / REVIEWER AUDIENCE
# =====================================================================

def test_accountant_included_in_new_request_notification_audience(app, client, login_as, setup):
    # A correction request may only be created by the record's own
    # creator (webapp/routes/correction_requests.py's create_request()),
    # so the historical dispatch must be created by the SAME operator who
    # later submits the request — not by setup's super_admin session.
    login_as("acct_notify_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-NOTIFY-1")

    login_as("acct_notify_mgr", "password123", "manager")
    login_as("acct_notify_super", "password123", "super_admin")
    login_as("acct_notify_accountant", "password123", "accountant")
    login_as("acct_notify_viewer", "password123", "viewer")

    client.post("/api/login", json={"username": "acct_notify_op", "password": "password123"})
    with app.app_context():
        from webapp.services import push_service
        calls = []
        original = push_service.notify_users

        def _spy(user_ids, title, body, url, badge_count=None):
            calls.append(list(user_ids))
            return original(user_ids, title, body, url, badge_count=badge_count)

        push_service.notify_users = _spy
        try:
            client.post("/api/correction-requests", json={
                "record_type": "dispatch", "record_id": d["id"], "action": "correct",
                "reason": "accountant must be in the reviewer notification audience",
                "payload": {"lines": []},
            })
        finally:
            push_service.notify_users = original

        from webapp.models.user import User
        accountant_id = User.query.filter_by(username="acct_notify_accountant").first().id
        manager_id = User.query.filter_by(username="acct_notify_mgr").first().id
        super_id = User.query.filter_by(username="acct_notify_super").first().id
        viewer_id = User.query.filter_by(username="acct_notify_viewer").first().id

    assert len(calls) == 1
    audience = calls[0]
    assert accountant_id in audience
    assert manager_id in audience
    assert super_id in audience
    assert viewer_id not in audience


def test_no_duplicate_notification_for_a_single_request(app, client, login_as, setup):
    login_as("acct_dup_op", "password123", "operator")
    d = _historical_dispatch(client, app, setup, dispatch_number="ACC-DUP-1")
    login_as("acct_dup_accountant", "password123", "accountant")
    client.post("/api/login", json={"username": "acct_dup_op", "password": "password123"})
    with app.app_context():
        from webapp.services import push_service
        calls = []
        original = push_service.notify_users
        push_service.notify_users = lambda *a, **k: calls.append(1)
        try:
            client.post("/api/correction-requests", json={
                "record_type": "dispatch", "record_id": d["id"], "action": "correct",
                "reason": "must fire exactly once regardless of reviewer audience size",
                "payload": {"lines": []},
            })
        finally:
            push_service.notify_users = original
    assert len(calls) == 1


def test_missing_push_config_stays_safe_for_accountant(client, login_as, app):
    # notify_users() is a graceful no-op without VAPID configuration —
    # confirms accountant's inclusion in the audience never requires real
    # push infrastructure to be configured for tests (or production).
    with app.app_context():
        from webapp.services import push_service
        assert push_service.is_configured() is False


def test_operator_never_gets_reviewer_controls_despite_notification_audience_change(client, login_as, setup):
    login_as("acct_op_no_controls", "password123", "operator")
    res = client.get("/api/correction-requests/pending-count")
    assert res.status_code == 403


# =====================================================================
# SECTION 23 — NAVIGATION
# =====================================================================

def test_accountant_navigation_contains_required_items():
    from pathlib import Path
    app_shell_js = (Path(__file__).parent.parent / "static" / "app-shell.js").read_text(encoding="utf-8")
    idx = app_shell_js.index("function reportingNavItems(role, flags)")
    body = app_shell_js[idx:app_shell_js.index("\n  }", idx)]
    # Dashboard/Daily Figures/History & Exports are unconditional (any
    # reporting role sees them when their module flag is on).
    assert "label: 'Dashboard'" in body
    assert "label: 'Daily Figures'" in body
    assert "label: 'History & Exports'" in body
    # Requests is now explicitly gated to include accountant.
    req_idx = body.index("items.push({ key: 'requests'")
    guard = body[max(0, req_idx - 150):req_idx]
    assert "role === 'accountant'" in guard


def test_accountant_navigation_excludes_operations_reset_and_admin():
    from pathlib import Path
    app_shell_js = (Path(__file__).parent.parent / "static" / "app-shell.js").read_text(encoding="utf-8")
    idx = app_shell_js.index("function reportingNavItems(role, flags)")
    body = app_shell_js[idx:app_shell_js.index("\n  }", idx)]
    ops_idx = body.index("label: 'Operations'")
    ops_guard = body[max(0, ops_idx - 150):ops_idx]
    assert "role === 'accountant'" not in ops_guard
    reset_idx = body.index("label: 'Reset Daily Values'")
    reset_guard = body[max(0, reset_idx - 150):reset_idx]
    assert "role === 'accountant'" not in reset_guard
    admin_idx = body.index("label: 'Admin'")
    admin_guard = body[max(0, admin_idx - 100):admin_idx]
    assert "role === 'accountant'" not in admin_guard


def test_accountant_can_reach_dashboard_page(client, login_as):
    login_as("acct_nav_dashboard", "password123", "accountant")
    res = client.get("/dashboard.html")
    assert res.status_code == 200


def test_accountant_can_reach_requests_page(client, login_as):
    login_as("acct_nav_requests", "password123", "accountant")
    res = client.get("/requests.html")
    assert res.status_code == 200


def test_accountant_redirected_away_from_admin_page(client, login_as):
    login_as("acct_nav_admin", "password123", "accountant")
    res = client.get("/admin.html")
    assert res.status_code == 302
    assert res.headers["Location"] != "/admin.html"


def test_accountant_redirected_away_from_reset_page(client, login_as):
    login_as("acct_nav_reset", "password123", "accountant")
    res = client.get("/reset-daily-values.html")
    assert res.status_code == 302
    assert res.headers["Location"] != "/reset-daily-values.html"


def test_other_roles_navigation_unaffected():
    from pathlib import Path
    app_shell_js = (Path(__file__).parent.parent / "static" / "app-shell.js").read_text(encoding="utf-8")
    idx = app_shell_js.index("function reportingNavItems(role, flags)")
    body = app_shell_js[idx:app_shell_js.index("\n  }", idx)]
    assert "(role === 'manager' || role === 'super_admin') && enabled(flags, 'dispatch')" in body
    assert "if (role === 'super_admin' && enabled(flags, 'daily_figures'))" in body
    assert "if (role === 'super_admin') items.push({ key: 'admin'" in body
