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
import uuid

import pytest

from webapp.services.business_calendar import business_today

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


# Rename was replaced by a full Edit action this round — see the
# CUSTOMER FULL EDIT (Rename -> Edit) section further down for its
# replacement tests (test_admin_html_has_edit_customer_action,
# test_admin_html_edit_customer_save_checks_role_explicitly, etc.).


# =====================================================================
# SECTION 15 — RETURNS DEFAULT SIGNER
# =====================================================================

def _create_return(client, product_id, cartons=1, **kwargs):
    body = {"date": "2026-08-01", "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}]}
    # Returned By must resolve to a real SELECTED customer before finalize
    # (free text alone is no longer sufficient — see returns_service.
    # finalize_return()) — default to a fresh throwaway customer (unique
    # per call, so repeat calls on this helper's fixed date never collide
    # with the duplicate-Returns rule) so every existing caller that
    # doesn't care about the recipient still finalizes cleanly. A caller
    # passing its own customer_id/returned_by_name via kwargs overrides
    # this via body.update() below, same as before.
    if "customer_id" not in kwargs and "returned_by_name" not in kwargs:
        # Non-fatal: a caller logged in as a role that can't create
        # customers falls through with no customer_id — the return-
        # creation call right below then fails for that same role reason
        # anyway, which is what those permission tests actually assert on.
        cust_res = client.post("/api/admin/customers", json={
            "name": f"Auto Returner {uuid.uuid4().hex[:8]}", "confirm_not_duplicate": True,
        })
        if cust_res.status_code == 201:
            body["customer_id"] = cust_res.get_json()["id"]
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
# Final matrix (superseding the Preview-era set from an earlier round):
#   Manager/Super Admin:            Edit / Void / Delete / Print
#   Operator, own same-day record:  Edit / Void / Print
#   Operator, own historical record: Request Correction / Request Void / Print
#   Operator, someone else's record: Print only
#   Viewer:                          Print only
# Preview is fully removed (it duplicated the always-visible read-only
# detail view with no added value — no markup, no handler, no panel left
# behind). Reopen/Duplicate remain off the UI; their backend routes are
# untouched. Void is NEW this round — not a soft synonym for Delete: it
# preserves the record and excludes it from Issued/Daily Figures, see the
# dedicated Void backend test file for stock-effect coverage.
# =====================================================================

def _detail_actions_body(html, marker="const actions = document.getElementById('detailActions');"):
    idx = html.index(marker)
    end = html.index("actions.innerHTML = buttons.join('');", idx)
    return html[idx:end]


ALL_THREE_HTML = (DISPATCH_HTML, RETURNS_HTML, PRODUCTION_HTML)

_DELETE_BRANCH = (
    "if(isElevated && data.status !== 'void'){\n"
    "    buttons.push(`<button class=\"btn btn-danger\" data-action=\"delete\">Delete</button>`);\n"
    "  }"
)


def test_dispatch_elevated_actions_are_edit_void_delete_print():
    body = _detail_actions_body(DISPATCH_HTML)
    assert 'data-action="correct">Edit<' in body
    assert 'data-action="void">Void<' in body
    assert 'data-action="delete">Delete<' in body
    assert 'data-action="reopen"' not in body
    assert 'data-action="preview"' not in body
    tail = DISPATCH_HTML[DISPATCH_HTML.index(body):DISPATCH_HTML.index(body) + len(body) + 200]
    assert 'data-action="print">Print<' in tail


def test_returns_elevated_actions_are_edit_void_delete_print():
    body = _detail_actions_body(RETURNS_HTML)
    assert 'data-action="correct">Edit<' in body
    assert 'data-action="void">Void<' in body
    assert 'data-action="delete">Delete<' in body
    assert 'data-action="reopen"' not in body


def test_production_elevated_actions_are_edit_void_delete_print():
    body = _detail_actions_body(PRODUCTION_HTML)
    assert 'data-action="correct">Edit<' in body
    assert 'data-action="void">Void<' in body
    assert 'data-action="delete">Delete<' in body
    assert 'data-action="reopen"' not in body


def test_no_reopen_duplicate_or_preview_button_on_any_history_page():
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert 'data-action="duplicate"' not in body
        assert 'data-action="reopen"' not in body
        assert 'data-action="preview"' not in body
        assert ">Duplicate<" not in body
        assert ">Reopen<" not in body
        assert ">Preview<" not in body


def test_no_stale_action_labels_remain():
    # No old overlapping labels survive anywhere on the page: Preview,
    # Reopen, Duplicate, Edit Draft (as a literal button label — "Edit" is
    # now the only label used regardless of which underlying action
    # edit-draft/correct/request-correct it maps to), or Correct Record
    # (a panel heading only, never a top-level button label).
    for html in ALL_THREE_HTML:
        assert ">Preview<" not in html
        assert ">Reopen<" not in html
        assert ">Duplicate<" not in html
        assert ">Edit Draft<" not in html
        assert 'data-action="edit-draft">Edit Draft<' not in html
        assert 'id="previewPanel"' not in html
        assert "openPreviewPanel" not in html


def test_operator_button_set_includes_edit_print_via_owned_draft():
    # canEditDraft = data.status === 'draft' && (isElevated || ownsRecord) —
    # an Operator viewing a draft they own reaches Edit (edit-draft) and
    # Print. Delete never appears for them (Manager/Super Admin only,
    # regardless of draft/finalized) — but Void CAN appear on their own
    # same-day draft too (see test_operator_same_day_owned_draft_gets_
    # direct_edit_and_void below): the backend has always allowed voiding
    # a draft directly, so the button isn't restricted to finalized-only.
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert "const canEditDraft = data.status === 'draft' && (isElevated || ownsRecord);" in body
        assert 'data-action="edit-draft">Edit<' in body


def test_operator_within_edit_window_or_with_grant_gets_direct_edit():
    # withinEditWindow (24h from created_at) OR hasActiveGrant (an
    # approved, unconsumed correction-request grant) — an Operator's own
    # record reaches the SAME direct Edit action Manager/Super Admin get,
    # via the shared (isElevated || withinEditWindow || hasActiveGrant)
    # gate; never a separate/duplicated code path. Void is a completely
    # separate, unconditional-on-age gate — see test_void_is_reachable_
    # on_any_non_void_status_including_draft below.
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert "if(isElevated || withinEditWindow || hasActiveGrant){" in body
        assert 'data-action="correct">Edit<' in body


def test_operator_past_edit_window_with_no_grant_gets_request_correction_only():
    # Past the 24-hour direct Edit window with no active grant — loses
    # direct Edit and instead sees Request Correction, queued for
    # Manager/Super Admin approval rather than a silent rewrite of
    # history. Void remains directly available regardless (see below) —
    # there is no "Request Void" at all.
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert "} else if(isOperator && ownsRecord){" in body
        assert 'data-action="request-correct">Request Correction<' in body
        assert 'data-action="request-void"' not in body
        assert ">Request Void<" not in body


def test_delete_is_reachable_only_through_isElevated_gate():
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert _DELETE_BRANCH in body


def test_single_edit_action_per_record_state_no_duplicates():
    # Draft-edit, void-branch Correct/Request-Correction, and
    # else-branch (elevated/window/grant) Correct/Request-Correction are
    # three mutually exclusive branches of one if/else-if/else-if/else
    # chain — never more than one Edit-family action pushed for the same
    # record. The literal Edit/Request-Correction button markup now
    # appears TWICE in the SOURCE (once per non-draft branch) because a
    # voided record follows different eligibility rules than a non-void
    # finalized one (section 1 of the Full targeted Operator correction/
    # void/requests/notification package: a void record is never
    # directly editable by anyone, including Manager/Super Admin — only
    # an active approval grant reaches Edit there).
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert body.count('data-action="edit-draft">Edit<') == 1
        assert body.count('data-action="correct">Edit<') == 2
        assert body.count('data-action="request-correct">Request Correction<') == 2
        idx_draft = body.index("if(data.status === 'draft'){")
        idx_void = body.index("} else if(data.status === 'void'){", idx_draft)
        idx_else = body.index("} else {", idx_void)
        assert idx_void > idx_draft  # one shared if/else-if/else-if/else chain, not independent ifs
        assert idx_else > idx_void


_VOID_BLOCK = (
    "  if(data.status !== 'void'){\n"
    "    if(isElevated || (isOperator && ownsRecord)){\n"
    "      buttons.push(`<button class=\"btn btn-ghost\" data-action=\"void\">Void</button>`);\n"
    "    }\n"
    "  }"
)


def test_void_is_reachable_on_any_non_void_status_including_draft():
    # The backend has always allowed voiding a still-draft record directly
    # (e.g. "customer cancelled before we ever finalized this") — the
    # button must not artificially hide just because status is 'draft';
    # only an already-void record excludes it. Deliberately unconditional
    # on record age — there is no "Request Void" at all.
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert _VOID_BLOCK in body
        assert body.count('data-action="void">Void<') == 1


def test_operator_cannot_correct_a_finalized_record_they_do_not_own(client, setup, login_as):
    # An Operator may now correct a FINALIZED record — but only one they
    # themselves created (see the ownership check in dispatches.py/
    # returns.py/production.py's correct() routes). These records were all
    # created while logged in as super_admin (via `setup`), so the
    # Operator below owns none of them and must still be refused.
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-NOEDIT-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")

    created = _create_return(client, setup["product"]["id"]).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")

    login_as("tux_noedit_op", "password123", "operator")
    assert client.post(f"/api/dispatches/{d['id']}/correct", json={"reason": "x", "lines": []}).status_code == 403
    assert client.post(f"/api/returns/{created['id']}/correct", json={"reason": "x", "lines": []}).status_code == 403
    assert client.post(f"/api/production/{prod['id']}/correct", json={"reason": "x", "lines": []}).status_code == 403


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


# ---------------------------------------------------------------------
# Draft "Edit" for Returns/Production — the single Edit button on a draft
# now opens a real draft-editing workflow (matching Dispatch's pre-existing
# Edit Draft), built on the header/line endpoints (update_header +
# add_line/update_line/remove_line) that already existed server-side and
# already allowed the owning Operator via can_edit() — this only wires the
# frontend to them, it does not add new backend permissions.
# ---------------------------------------------------------------------

def test_operator_can_edit_own_draft_return_via_existing_endpoints(client, setup, login_as):
    login_as("tux_ret_edit_op", "password123", "operator")
    pid = setup["product"]["id"]
    created = _create_return(client, pid, cartons=2).get_json()

    patch_res = client.patch(f"/api/returns/{created['id']}", json={"remarks": "edited by owner"})
    assert patch_res.status_code == 200
    line_res = client.patch(
        f"/api/returns/{created['id']}/lines/{created['lines'][0]['id']}",
        json={"cartons": 5, "packs": 0, "pieces": 0},
    )
    assert line_res.status_code == 200

    client.post(f"/api/returns/{created['id']}/finalize")
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["return_"]["base_qty"] == 500
    assert len(client.get(f"/api/returns/{created['id']}").get_json()["lines"]) == 1  # edited in place, not duplicated


def test_operator_cannot_edit_another_users_draft_return(client, setup, super_admin, login_as):
    pid = setup["product"]["id"]
    created = _create_return(client, pid).get_json()  # created while logged in as super_admin
    login_as("tux_ret_edit_op2", "password123", "operator")
    res = client.patch(f"/api/returns/{created['id']}", json={"remarks": "not mine to edit"})
    assert res.status_code == 403


def test_operator_cannot_edit_return_lines_once_finalized_via_draft_endpoints(client, setup, login_as):
    login_as("tux_ret_edit_op3", "password123", "operator")
    pid = setup["product"]["id"]
    created = _create_return(client, pid).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    res = client.patch(f"/api/returns/{created['id']}", json={"remarks": "too late"})
    # can_edit() already denies an Operator on a non-draft record before the
    # draft-only status guard is even reached — 403, not 409 (a Manager/
    # Super Admin, who always passes can_edit, would hit the 409 instead).
    assert res.status_code == 403


def test_manager_cannot_edit_return_header_once_finalized_without_reopening(client, setup, super_admin):
    pid = setup["product"]["id"]
    created = _create_return(client, pid).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    res = client.patch(f"/api/returns/{created['id']}", json={"remarks": "too late"})
    assert res.status_code == 409  # "reopen it first" — the draft-only guard still applies


def test_returns_draft_edit_adding_and_removing_lines_does_not_leave_duplicates(client, setup, login_as):
    pid = setup["product"]["id"]
    other = _make_product(client, "TUX Other Product")  # created while logged in as super_admin
    login_as("tux_ret_edit_op4", "password123", "operator")
    created = _create_return(client, pid, cartons=1).get_json()
    original_line_id = created["lines"][0]["id"]

    add_res = client.post(f"/api/returns/{created['id']}/lines", json={"product_id": other["id"], "cartons": 1, "packs": 0, "pieces": 0})
    assert add_res.status_code == 201
    del_res = client.delete(f"/api/returns/{created['id']}/lines/{original_line_id}")
    assert del_res.status_code == 200

    lines = client.get(f"/api/returns/{created['id']}").get_json()["lines"]
    assert len(lines) == 1
    assert lines[0]["product_id"] == other["id"]


def test_operator_can_edit_own_draft_production_via_existing_endpoints(client, setup, login_as):
    login_as("tux_prod_edit_op", "password123", "operator")
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()

    patch_res = client.patch(f"/api/production/{prod['id']}", json={"remarks": "edited by owner"})
    assert patch_res.status_code == 200
    line_res = client.patch(
        f"/api/production/{prod['id']}/lines/{prod['lines'][0]['id']}",
        json={"cartons": 4, "packs": 0, "pieces": 0},
    )
    assert line_res.status_code == 200

    client.post(f"/api/production/{prod['id']}/finalize")
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["production"]["base_qty"] == 400
    assert len(client.get(f"/api/production/{prod['id']}").get_json()["lines"]) == 1


def test_operator_cannot_edit_another_users_draft_production(client, setup, super_admin, login_as):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()  # created while logged in as super_admin
    login_as("tux_prod_edit_op2", "password123", "operator")
    res = client.patch(f"/api/production/{prod['id']}", json={"remarks": "not mine to edit"})
    assert res.status_code == 403


def test_operator_cannot_edit_production_lines_once_finalized_via_draft_endpoints(client, setup, login_as):
    login_as("tux_prod_edit_op3", "password123", "operator")
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    res = client.patch(f"/api/production/{prod['id']}", json={"remarks": "too late"})
    assert res.status_code == 403  # can_edit() denies before the draft-only status guard is reached


def test_manager_cannot_edit_production_header_once_finalized_without_reopening(client, setup, super_admin):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    res = client.patch(f"/api/production/{prod['id']}", json={"remarks": "too late"})
    assert res.status_code == 409  # "reopen it first" — the draft-only guard still applies


def test_returns_html_edit_draft_reuses_header_and_line_endpoints():
    assert "async function saveEditedReturnDraft(finalize, msg){" in RETURNS_HTML
    idx = RETURNS_HTML.index("async function saveEditedReturnDraft(finalize, msg){")
    end = RETURNS_HTML.index("\nfunction openEditDraft(data){", idx)
    body = RETURNS_HTML[idx:end]
    assert "method:'PATCH', body: JSON.stringify(headerBody)" in body
    assert "/lines`, {method:'POST'" in body
    assert "/lines/${line.id}`, {method:'PATCH'" in body
    assert "/lines/${origId}`, {method:'DELETE'}" in body


def test_production_html_edit_draft_reuses_header_and_line_endpoints():
    assert "async function saveEditedProductionDraft(finalize, msg){" in PRODUCTION_HTML
    idx = PRODUCTION_HTML.index("async function saveEditedProductionDraft(finalize, msg){")
    end = PRODUCTION_HTML.index("\nfunction openEditDraft(data){", idx)
    body = PRODUCTION_HTML[idx:end]
    assert "method:'PATCH', body: JSON.stringify(headerBody)" in body
    assert "/lines`, {method:'POST'" in body
    assert "/lines/${line.id}`, {method:'PATCH'" in body
    assert "/lines/${origId}`, {method:'DELETE'}" in body


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


def test_repeating_delete_is_safe_returns(client, setup, super_admin):
    pid = setup["product"]["id"]
    created = _create_return(client, pid).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    first = client.delete(f"/api/returns/{created['id']}", json={"reason": "x", "confirm": True})
    assert first.status_code == 200
    second = client.delete(f"/api/returns/{created['id']}", json={"reason": "x", "confirm": True})
    assert second.status_code == 404  # already gone — not a crash, not a double-delete


def test_repeating_delete_is_safe_production(client, setup, super_admin):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    first = client.delete(f"/api/production/{prod['id']}", json={"reason": "x", "confirm": True})
    assert first.status_code == 200
    second = client.delete(f"/api/production/{prod['id']}", json={"reason": "x", "confirm": True})
    assert second.status_code == 404


def test_delete_removes_no_unrelated_data_returns(client, setup, super_admin):
    pid = setup["product"]["id"]
    keep = _create_return(client, pid, cartons=3).get_json()
    client.post(f"/api/returns/{keep['id']}/finalize")
    doomed = _create_return(client, pid, cartons=1).get_json()
    client.post(f"/api/returns/{doomed['id']}/finalize")

    res = client.delete(f"/api/returns/{doomed['id']}", json={"reason": "entered in error", "confirm": True})
    assert res.status_code == 200

    still_there = client.get(f"/api/returns/{keep['id']}")
    assert still_there.status_code == 200
    assert still_there.get_json()["lines"][0]["cartons"] == 3


def test_delete_removes_no_unrelated_data_production(client, setup, super_admin):
    pid = setup["product"]["id"]
    keep = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{keep['id']}/finalize")
    doomed = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{doomed['id']}/finalize")

    res = client.delete(f"/api/production/{doomed['id']}", json={"reason": "entered in error", "confirm": True})
    assert res.status_code == 200

    still_there = client.get(f"/api/production/{keep['id']}")
    assert still_there.status_code == 200
    assert still_there.get_json()["lines"][0]["cartons"] == 3


# =====================================================================
# SECTION — CORRECT / VOID / DELETE PANEL SAFETY
# (mutual exclusivity: opening any one of these three panels hides the
#  other two, so a stale form is never left showing behind/above the one
#  currently in use. Request Void no longer exists at all — Void is
#  always a direct action now, see record_correction_service.
#  operator_can_directly_void().)
# =====================================================================

def _js_function_body(html, start_marker):
    idx = html.index(start_marker)
    end = html.index("\n}\n", idx)
    return html[idx:end]


def test_opening_correct_panel_hides_void_and_delete_panels():
    for html in ALL_THREE_HTML:
        body = _js_function_body(html, "function openCorrectPanel(data, mode){")
        assert "voidPanel').classList.add('hidden')" in body
        assert "deletePanel').classList.add('hidden')" in body


def test_opening_void_panel_hides_correct_and_delete_panels():
    for html in ALL_THREE_HTML:
        body = _js_function_body(html, "function openVoidPanel(data){")
        assert "correctPanel').classList.add('hidden')" in body
        assert "deletePanel').classList.add('hidden')" in body


def test_opening_delete_panel_hides_correct_and_void_panels():
    for html in ALL_THREE_HTML:
        body = _js_function_body(html, "function openDeletePanel(data){")
        assert "correctPanel').classList.add('hidden')" in body
        assert "voidPanel').classList.add('hidden')" in body


def test_no_request_void_panel_or_handler_remains():
    for html in ALL_THREE_HTML:
        assert "requestVoidPanel" not in html
        assert "openRequestVoidPanel" not in html
        assert "requestVoidConfirmBtn" not in html


def test_void_action_requires_a_reason_before_calling_api():
    for html in ALL_THREE_HTML:
        idx = html.index("voidConfirmBtn').addEventListener('click'")
        end = html.index("});", idx) + 3
        body = html[idx:end]
        assert "if(!reason){" in body
        assert body.index("if(!reason){") < body.index("api(")


def test_void_button_never_appears_alongside_delete_button_label_confusion():
    # Void and Delete are visually/semantically distinct actions offered
    # together (Manager/Super Admin, same-day-owning Operator) — Void must
    # never render with Delete's danger styling or vice versa.
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert 'data-action="void">Void</button>' in body
        assert 'btn-ghost" data-action="void"' in body
        assert 'btn-danger" data-action="delete"' in body


def test_correct_and_void_panels_end_to_end_never_touch_backend_on_open(client, setup, login_as):
    # Belt-and-braces backend proof alongside the markup checks above:
    # merely opening a detail record (what every action button click
    # starts from) never issues a mutating request for any role, on
    # either a draft or a finalized record.
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-DETAIL-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")

    for username, role in (("tux_detail_op", "operator"), ("tux_detail_mgr", "manager"), ("tux_detail_sa", "super_admin")):
        login_as(username, "password123", role)
        before = client.get(f"/api/dispatches/{d['id']}").get_json()
        after_get = client.get(f"/api/dispatches/{d['id']}").get_json()
        assert before["status"] == after_get["status"] == "finalized"
        assert before["updated_at"] == after_get["updated_at"]
        client.post("/api/logout")


# =====================================================================
# SECTION — PRINT
# =====================================================================

def test_print_action_present_on_all_three_history_pages():
    for html in ALL_THREE_HTML:
        assert 'data-action="print">Print<' in html
        assert "window.print()" in html


def test_print_action_makes_no_api_call_on_any_history_page():
    for html in ALL_THREE_HTML:
        line = html[html.index("if(action === 'print'){"):]
        line = line[:line.index("\n")]
        assert "api(" not in line


def test_print_available_to_operator_manager_super_admin_in_markup():
    # Print is pushed unconditionally (never gated by isElevated/isViewer),
    # so every authenticated role that can open a record reaches it.
    for html in ALL_THREE_HTML:
        body = _detail_actions_body(html)
        assert "buttons.push(`<button class=\"btn btn-ghost\" data-action=\"print\">Print</button>`);" in body


def test_printed_view_contains_record_detail_without_nav_controls():
    # @media print hides header/.tabs/.no-print (nav chrome, filters,
    # action buttons) but never touches #detailHeader/#detailLines (both
    # plain .card, no no-print class) — those are exactly what prints.
    for html in ALL_THREE_HTML:
        idx = html.index("@media print {")
        block = html[idx:html.index("}\n", idx) + 1]
        assert "header, .tabs, .no-print { display:none !important; }" in block
        assert 'id="detailHeader"' in html
        assert 'class="card" id="detailHeader"' in html  # plain card, not tagged no-print
        assert 'class="card" id="detailLines"' in html


# =====================================================================
# SECTION — DISPATCH FULL EDIT
# =====================================================================

def test_dispatch_edit_form_displays_dispatch_number_read_only():
    idx = DISPATCH_HTML.index('id="correctPanel"')
    end = DISPATCH_HTML.index('id="correctDate"', idx)
    body = DISPATCH_HTML[idx:end]
    assert 'id="correctDispatchNumber"' in body
    assert "readonly" in body and "disabled" in body


def test_dispatch_edit_form_populates_dispatch_number_from_record():
    idx = DISPATCH_HTML.index("function openCorrectPanel(data, mode){")
    end = DISPATCH_HTML.index("\n}\n", idx)
    body = DISPATCH_HTML[idx:end]
    assert "correctDispatchNumber').value = data.dispatch_number" in body


def test_dispatch_edit_form_already_exposes_full_record():
    # Recipient/Sales Category/product lines/Cartons/Packs/Pieces/Notes/
    # correction-reason were already present before this round (see
    # correctSalesCategory/correctRecipientFieldWrap/correctLineList/
    # correctNotes/correctReason) — this proves the "complete record" gap
    # was specifically the missing Dispatch Number, not these fields.
    idx = DISPATCH_HTML.index('id="correctPanel"')
    end = DISPATCH_HTML.index("id=\"deletePanel\"", idx)
    body = DISPATCH_HTML[idx:end]
    for marker in ("correctSalesCategory", "correctRecipientFieldWrap", "correctCustomerSearch",
                   "correctLineList", "correctAddProductSelect", "correctNotes", "correctReason"):
        assert marker in body


def test_manager_can_correct_one_dispatch_line_without_deleting_the_record(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-LINE-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _issued_base_qty(client, pid, "2026-08-01") == 500

    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "wrong cartons entered", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    # Old contribution gone, corrected one counted exactly once — not kept
    # AND added, not doubled, not left as a second hidden record.
    assert _issued_base_qty(client, pid, "2026-08-01") == 300
    still = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert still["id"] == d["id"]
    assert len(still["lines"]) == 1
    all_dispatches = client.get("/api/dispatches?limit=200").get_json()["results"]
    assert sum(1 for r in all_dispatches if r["dispatch_number"] == "TUX-LINE-1") == 1


def test_dispatch_date_correction_moves_issued_to_new_date(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-DATEMOVE-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _issued_base_qty(client, pid, "2026-08-01") == 400

    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "entered on the wrong date", "notes": None, "date": "2026-08-03",
        "lines": [{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert _issued_base_qty(client, pid, "2026-08-01") == 0    # old date loses it
    assert _issued_base_qty(client, pid, "2026-08-03") == 400  # new date gains it exactly once


def test_dashboard_recipient_count_reflects_dispatch_after_line_correction(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-DASH-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    before = client.get("/api/dashboard?date=2026-08-01").get_json()

    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fixing quantity", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    })
    after = client.get("/api/dashboard?date=2026-08-01").get_json()
    # Same single recipient either way — correcting a line's quantity must
    # not create or lose a unique-recipient count.
    assert before["unique_recipients_today"] == after["unique_recipients_today"] == 1


# =====================================================================
# SECTION — PRODUCTION FULL EDIT
# =====================================================================

def test_production_edit_form_contains_date_and_shift():
    idx = PRODUCTION_HTML.index('id="correctPanel"')
    end = PRODUCTION_HTML.index('id="correctLineList"', idx)
    body = PRODUCTION_HTML[idx:end]
    assert 'id="correctDate"' in body
    assert 'id="correctShift"' in body


def test_production_edit_save_sends_date_and_shift():
    idx = PRODUCTION_HTML.index("correctSaveBtn').addEventListener('click'")
    end = PRODUCTION_HTML.index("});", idx) + 3
    body = PRODUCTION_HTML[idx:end]
    assert "const date = document.getElementById('correctDate').value" in body
    assert "const shift = document.getElementById('correctShift').value;" in body


def test_manager_can_change_production_date(client, setup, super_admin):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    row_old = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert row_old["production"]["base_qty"] == 300

    res = client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "entered on the wrong date", "notes": None, "date": "2026-08-05",
        "lines": [{"id": prod["lines"][0]["id"], "product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    row_old_after = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    row_new = client.get(f"/api/daily-figures/{pid}?date=2026-08-05&shift=Day").get_json()
    assert row_old_after["production"]["base_qty"] == 0   # old date loses it
    assert row_new["production"]["base_qty"] == 300        # new date gains it exactly once

    updated = client.get(f"/api/production/{prod['id']}").get_json()
    assert updated["id"] == prod["id"]
    assert updated["date"] == "2026-08-05"


def test_super_admin_can_change_production_date(client, setup, super_admin):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    res = client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-06",
        "lines": [{"id": prod["lines"][0]["id"], "product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert client.get(f"/api/production/{prod['id']}").get_json()["date"] == "2026-08-06"


def test_production_date_correction_preserves_exact_quantity(client, setup, super_admin):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 7, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-07",
        "lines": [{"id": prod["lines"][0]["id"], "product_id": pid, "cartons": 7, "packs": 0, "pieces": 0}],
    })
    row = client.get(f"/api/daily-figures/{pid}?date=2026-08-07&shift=Day").get_json()
    assert row["production"]["base_qty"] == 700  # exact — no float drift, no double count


def test_production_date_correction_carries_following_opening_forward(client, setup, super_admin):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    row1_before = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    closing_before = row1_before["closing"]["base_qty"]
    assert closing_before == 1500  # 1000 opening + 500 production

    client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-02",
        "lines": [{"id": prod["lines"][0]["id"], "product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })
    row1_after = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert row1_after["closing"]["base_qty"] == 1000  # production no longer contributes here
    row2 = client.get(f"/api/daily-figures/{pid}?date=2026-08-02&shift=Day").get_json()
    assert row2["opening"]["base_qty"] == row1_after["closing"]["base_qty"]  # carry-forward intact
    assert row2["closing"]["base_qty"] == row2["opening"]["base_qty"] + row2["production"]["base_qty"] + row2["return_"]["base_qty"] - row2["issued"]["base_qty"]


def test_production_date_correction_does_not_create_duplicate_record(client, setup, super_admin):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-09",
        "lines": [{"id": prod["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    all_prod = client.get("/api/production?limit=200").get_json()["results"]
    assert sum(1 for r in all_prod if r["id"] == prod["id"]) == 1


def test_operator_cannot_correct_finalized_production_date_they_do_not_own(client, setup, login_as):
    # prod is created here under the super_admin session `setup` leaves
    # active, so the Operator logged in below owns none of it.
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    login_as("tux_prod_date_op", "password123", "operator")
    res = client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "x", "notes": None, "date": "2026-08-02",
        "lines": [{"id": prod["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 403  # not the owner — Operator ownership check still applies


def test_shift_correction_rejected_for_non_production_source(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-SHIFT-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    # Not reachable via the dispatch route (it never sends "shift"), but
    # the service itself must still refuse it if ever called that way —
    # belt-and-braces against a future route wiring mistake.
    from webapp.services.record_correction_service import correct_record, RecordCorrectionError
    import pytest as _pytest
    from webapp.models.user import User
    actor = User.query.filter_by(username="root").first()
    with _pytest.raises(RecordCorrectionError):
        correct_record(
            "dispatch", d["id"], lines=[{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
            notes=None, reason="x", actor=actor, shift="Night",
        )


# =====================================================================
# SECTION — RETURNS FULL EDIT
# =====================================================================

def test_returns_edit_form_contains_date_recipient_and_signer():
    idx = RETURNS_HTML.index('id="correctPanel"')
    end = RETURNS_HTML.index('id="correctLineList"', idx)
    body = RETURNS_HTML[idx:end]
    assert 'id="correctDate"' in body
    assert 'id="correctReturnedBySearch"' in body
    assert 'id="correctSignedByName"' in body


def test_returns_edit_save_sends_date_recipient_and_signer():
    idx = RETURNS_HTML.index("correctSaveBtn').addEventListener('click'")
    end = RETURNS_HTML.index("});", idx) + 3
    body = RETURNS_HTML[idx:end]
    assert "const date = document.getElementById('correctDate').value" in body
    assert "const customer_id = correctSelectedCustomer" in body
    assert "const signed_by_name = document.getElementById('correctSignedByName').value" in body


def test_manager_can_change_return_date(client, setup, super_admin):
    pid = setup["product"]["id"]
    created = _create_return(client, pid, cartons=2).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["return_"]["base_qty"] == 200

    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "wrong date entered", "notes": None, "date": "2026-08-04",
        "lines": [{"id": created["lines"][0]["id"], "product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()["return_"]["base_qty"] == 0
    assert client.get(f"/api/daily-figures/{pid}?date=2026-08-04&shift=Day").get_json()["return_"]["base_qty"] == 200
    updated = client.get(f"/api/returns/{created['id']}").get_json()
    assert updated["id"] == created["id"]
    assert updated["date"] == "2026-08-04"


def test_super_admin_can_change_return_date(client, setup, super_admin):
    pid = setup["product"]["id"]
    created = _create_return(client, pid, cartons=1).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-08",
        "lines": [{"id": created["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert client.get(f"/api/returns/{created['id']}").get_json()["date"] == "2026-08-08"


def test_return_day_only_rule_still_enforced_after_date_correction(client, setup, super_admin):
    # Returns has no shift column at all — correcting the date must never
    # introduce one; the record stays a Day-only workflow throughout.
    pid = setup["product"]["id"]
    created = _create_return(client, pid, cartons=1).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-10",
        "lines": [{"id": created["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    updated = client.get(f"/api/returns/{created['id']}").get_json()
    assert "shift" not in updated


def test_returns_date_correction_carries_following_opening_forward(client, setup, super_admin):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    created = _create_return(client, pid, cartons=3).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    row1_before = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert row1_before["closing"]["base_qty"] == 1300  # 1000 opening + 300 returns

    client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-02",
        "lines": [{"id": created["lines"][0]["id"], "product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    })
    row1_after = client.get(f"/api/daily-figures/{pid}?date=2026-08-01&shift=Day").get_json()
    assert row1_after["closing"]["base_qty"] == 1000
    row2 = client.get(f"/api/daily-figures/{pid}?date=2026-08-02&shift=Day").get_json()
    assert row2["opening"]["base_qty"] == row1_after["closing"]["base_qty"]
    assert row2["closing"]["base_qty"] == row2["opening"]["base_qty"] + row2["production"]["base_qty"] + row2["return_"]["base_qty"] - row2["issued"]["base_qty"]


def test_returns_date_correction_does_not_create_duplicate_record(client, setup, super_admin):
    pid = setup["product"]["id"]
    created = _create_return(client, pid, cartons=1).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": "2026-08-11",
        "lines": [{"id": created["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    all_returns = client.get("/api/returns?limit=200").get_json()["results"]
    assert sum(1 for r in all_returns if r["id"] == created["id"]) == 1


def test_manager_can_change_return_signer_via_correction(client, setup, super_admin):
    pid = setup["product"]["id"]
    created = _create_return(client, pid).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "signer was wrong", "notes": None, "signed_by_name": "Corrected Signer",
        "lines": [{"id": created["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert client.get(f"/api/returns/{created['id']}").get_json()["signed_by_name"] == "Corrected Signer"


def test_manager_can_change_returned_by_recipient_via_correction(client, setup, super_admin):
    pid = setup["product"]["id"]
    wrong_customer = client.post("/api/admin/customers", json={
        "name": "TUX Wrong Recipient", "confirm_not_duplicate": True,
    }).get_json()
    created = _create_return(client, pid, customer_id=wrong_customer["id"]).get_json()
    fin = client.post(f"/api/returns/{created['id']}/finalize")
    assert fin.status_code == 200
    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "wrong recipient recorded", "notes": None, "customer_id": setup["customer"]["id"],
        "lines": [{"id": created["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    updated = client.get(f"/api/returns/{created['id']}").get_json()
    assert updated["returned_by_customer_id"] == setup["customer"]["id"]
    assert updated["returned_by_name"] == "TUX Recipient"


def test_returned_by_correction_rejected_for_non_returns_source(client, setup, super_admin):
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    from webapp.services.record_correction_service import correct_record, RecordCorrectionError
    import pytest as _pytest
    from webapp.models.user import User
    actor = User.query.filter_by(username="root").first()
    with _pytest.raises(RecordCorrectionError):
        correct_record(
            "production", prod["id"], lines=[{"id": prod["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
            notes=None, reason="x", actor=actor, returned_by_name="nope",
        )


# =====================================================================
# SECTION — CUSTOMER FULL EDIT (Rename -> Edit)
# =====================================================================

def test_admin_html_rename_button_is_gone():
    assert "data-rename-customer" not in ADMIN_HTML
    # Sales-category rename (a separate, untouched feature) legitimately
    # keeps its own "Rename" button further down the page — scope this
    # check to the customers panel specifically, which sits entirely
    # before panel-categories in the markup.
    customers_panel_markup = ADMIN_HTML.split('id="panel-categories"')[0]
    assert ">Rename<" not in customers_panel_markup


def test_admin_html_customer_rename_prompt_flow_removed():
    # The old flow prompted for a name only; it must be fully gone, not
    # just relabeled — the replacement is a real multi-field panel.
    assert 'Rename "${current ? current.name : \'\'}" to:' not in ADMIN_HTML


def test_admin_html_has_edit_customer_action():
    assert "data-edit-customer" in ADMIN_HTML
    assert 'data-edit-customer="${c.id}">Edit<' in ADMIN_HTML


def test_admin_html_customer_edit_panel_has_all_four_fields():
    idx = ADMIN_HTML.index('id="customerEditPanel"')
    end = ADMIN_HTML.index("</div>\n    </div>", idx)
    body = ADMIN_HTML[idx:end]
    assert 'id="editCustomerName"' in body
    assert 'id="editCustomerCategory"' in body
    assert 'id="editCustomerSalesCategory"' in body
    assert 'id="editCustomerStatus"' in body


def test_admin_html_open_customer_edit_prefills_current_values():
    idx = ADMIN_HTML.index("function openCustomerEdit(id){")
    end = ADMIN_HTML.index("\n}\n", idx)
    body = ADMIN_HTML[idx:end]
    assert "editCustomerName').value = current.name" in body
    assert "editCustomerCategory').value = current.category" in body
    assert "editCustomerSalesCategory').value = current.sales_category_id" in body
    assert "editCustomerStatus').value = String(!!current.active)" in body


def test_admin_html_edit_customer_save_checks_role_explicitly():
    idx = ADMIN_HTML.index("editCustomerSaveBtn').addEventListener('click'")
    end = ADMIN_HTML.index("});", idx) + 3
    body = ADMIN_HTML[idx:end]
    assert "currentUserRole !== 'super_admin'" in body


def test_super_admin_can_open_and_save_full_customer_edit(client, setup):
    cust_id = setup["customer"]["id"]
    res = client.patch(f"/api/admin/customers/{cust_id}", json={
        "name": "ABC Supermarket", "category": "customer", "active": True,
    })
    assert res.status_code == 200
    updated = res.get_json()
    assert updated["name"] == "ABC Supermarket"
    assert updated["id"] == cust_id


def test_super_admin_can_edit_customer_sales_category(client, setup):
    cust_id = setup["customer"]["id"]
    new_cat = client.post("/api/admin/sales-categories", json={"name": "Standard Sales"}).get_json()
    res = client.patch(f"/api/admin/customers/{cust_id}", json={"sales_category_id": new_cat["id"]})
    assert res.status_code == 200
    assert res.get_json()["sales_category_id"] == new_cat["id"]


def test_super_admin_can_edit_customer_category_field(client, setup):
    cust_id = setup["customer"]["id"]
    res = client.patch(f"/api/admin/customers/{cust_id}", json={"category": "salesperson"})
    assert res.status_code == 200
    assert res.get_json()["category"] == "salesperson"


def test_super_admin_can_edit_customer_status(client, setup):
    cust_id = setup["customer"]["id"]
    res = client.patch(f"/api/admin/customers/{cust_id}", json={"active": False})
    assert res.status_code == 200
    assert res.get_json()["active"] is False


def test_customer_edit_preserves_id_and_dispatch_links(client, setup):
    cust_id = setup["customer"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-CUSTEDIT-1", "date": "2026-08-01", "customer_id": cust_id,
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.patch(f"/api/admin/customers/{cust_id}", json={
        "name": "ABC Supermarket", "category": "customer", "active": True,
    })
    still = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert still["customer_id"] == cust_id
    assert still["customer_name"] == "ABC Supermarket"


def test_customer_edit_duplicate_validation_still_enforced(client, setup):
    _make_customer(client, setup["category"]["id"], "Full Edit Duplicate Target")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"name": "Full Edit Duplicate Target"})
    assert res.status_code == 409
    assert res.get_json()["warning"] == "similar_customers_exist"


def test_customer_edit_does_not_partially_save_on_validation_failure(client, setup):
    cust_id = setup["customer"]["id"]
    before = next(c for c in client.get("/api/admin/customers").get_json() if c["id"] == cust_id)
    res = client.patch(f"/api/admin/customers/{cust_id}", json={
        "category": "salesperson", "sales_category_id": 9999999,
    })
    assert res.status_code == 400
    after = next(c for c in client.get("/api/admin/customers").get_json() if c["id"] == cust_id)
    assert after["category"] == before["category"]  # category change was NOT partially applied


def test_customer_edit_audit_captures_before_after_actor(client, setup, app):
    cust_id = setup["customer"]["id"]
    client.patch(f"/api/admin/customers/{cust_id}", json={"category": "salesperson", "active": False})
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="update", entity_type="customer", entity_id=str(cust_id)).order_by(AuditLog.id.desc()).first()
        assert entry is not None
        before = json.loads(entry.before_json)
        after = json.loads(entry.after_json)
        assert before["id"] == cust_id
        assert after["category"] == "salesperson"
        assert after["active"] is False
        assert entry.user_id is not None
        assert entry.created_at is not None


def test_manager_cannot_edit_customer_name_but_can_edit_other_full_edit_fields(client, setup, login_as):
    login_as("tux_custedit_mgr", "password123", "manager")
    cust_id = setup["customer"]["id"]
    denied = client.patch(f"/api/admin/customers/{cust_id}", json={"name": "Manager Full Edit Attempt"})
    assert denied.status_code == 403
    allowed = client.patch(f"/api/admin/customers/{cust_id}", json={"category": "salesperson", "active": False})
    assert allowed.status_code == 200


def test_operator_cannot_edit_customer(client, setup, login_as):
    login_as("tux_custedit_op", "password123", "operator")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"category": "salesperson"})
    assert res.status_code == 403


def test_viewer_cannot_edit_customer(client, setup, login_as):
    login_as("tux_custedit_viewer", "password123", "viewer")
    res = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"category": "salesperson"})
    assert res.status_code == 403


def test_customer_metadata_edit_does_not_affect_stock(client, setup):
    pid = setup["product"]["id"]
    cust_id = setup["customer"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-CUSTSTOCK-1", "date": "2026-08-01", "customer_id": cust_id,
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    before_issued = _issued_base_qty(client, pid, "2026-08-01")

    client.patch(f"/api/admin/customers/{cust_id}", json={
        "name": "ABC Supermarket", "category": "salesperson", "active": True,
    })

    after_issued = _issued_base_qty(client, pid, "2026-08-01")
    assert before_issued == after_issued == 300


# =====================================================================
# SECTION — OPERATOR EDIT ON FINALIZED RECORDS THEY OWN
# (final role-matrix correction: the Operator Edit button must not
#  disappear merely because the record they entered is now finalized —
#  server-enforced by an ownership check in each /correct route, never
#  only by hiding the button.)
# =====================================================================

def test_operator_can_edit_finalized_dispatch_they_created(client, setup, login_as):
    # Final round correction: Operator finalized-edit access is now
    # same-day only (see record_correction_service.operator_can_directly_
    # edit()) — this test proves the still-allowed case (today); the
    # newly-forbidden historical case is covered in
    # test_final_operator_same_day_edit_window.py.
    today = business_today()
    login_as("tux_op_finedit_disp", "password123", "operator")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-OPFIN-D1", "date": today, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _issued_base_qty(client, setup["product"]["id"], today) == 500

    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "fixing my own mistake", "notes": "corrected",
        "lines": [{"id": d["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 3, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    # Old contribution gone, corrected one counted exactly once.
    assert _issued_base_qty(client, setup["product"]["id"], today) == 300
    updated = client.get(f"/api/dispatches/{d['id']}").get_json()
    assert updated["id"] == d["id"]  # same record, not a duplicate
    assert len(updated["lines"]) == 1
    all_dispatches = client.get("/api/dispatches?limit=200").get_json()["results"]
    assert sum(1 for r in all_dispatches if r["dispatch_number"] == "TUX-OPFIN-D1") == 1


def test_operator_can_edit_finalized_return_they_created(client, setup, login_as):
    today = business_today()
    login_as("tux_op_finedit_ret", "password123", "operator")
    created = _create_return(client, setup["product"]["id"], cartons=4, date=today).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date={today}&shift=Day").get_json()["return_"]["base_qty"] == 400

    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "fixing my own mistake", "notes": None,
        "lines": [{"id": created["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert client.get(f"/api/daily-figures/{setup['product']['id']}?date={today}&shift=Day").get_json()["return_"]["base_qty"] == 200
    updated = client.get(f"/api/returns/{created['id']}").get_json()
    assert updated["id"] == created["id"]
    assert len(updated["lines"]) == 1
    all_returns = client.get("/api/returns?limit=200").get_json()["results"]
    assert sum(1 for r in all_returns if r["id"] == created["id"]) == 1


def test_operator_can_edit_finalized_production_they_created(client, setup, login_as):
    today = business_today()
    login_as("tux_op_finedit_prod", "password123", "operator")
    pid = setup["product"]["id"]
    prod = client.post("/api/production", json={
        "date": today, "shift": "Day", "lines": [{"product_id": pid, "cartons": 6, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    assert client.get(f"/api/daily-figures/{pid}?date={today}&shift=Day").get_json()["production"]["base_qty"] == 600

    res = client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "fixing my own mistake", "notes": None,
        "lines": [{"id": prod["lines"][0]["id"], "product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert client.get(f"/api/daily-figures/{pid}?date={today}&shift=Day").get_json()["production"]["base_qty"] == 400
    updated = client.get(f"/api/production/{prod['id']}").get_json()
    assert updated["id"] == prod["id"]
    assert len(updated["lines"]) == 1
    all_prod = client.get("/api/production?limit=200").get_json()["results"]
    assert sum(1 for r in all_prod if r["id"] == prod["id"]) == 1


def test_operator_finalized_dispatch_date_correction_moves_contribution_once(client, setup, login_as):
    today = business_today()
    other_day = "2026-08-03" if today != "2026-08-03" else "2026-08-04"
    login_as("tux_op_finedit_datemove", "password123", "operator")
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-OPFIN-DATE1", "date": today, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _issued_base_qty(client, pid, today) == 400

    # The record itself is still same-day at correction time — moving its
    # date to a DIFFERENT (non-today) date is still a same-day EDIT (the
    # record being edited is today's), so it stays allowed.
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "entered on the wrong date", "notes": None, "date": other_day,
        "lines": [{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 4, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert _issued_base_qty(client, pid, today) == 0
    assert _issued_base_qty(client, pid, other_day) == 400


def test_operator_cannot_delete_finalized_record_they_own(client, setup, login_as):
    today = business_today()
    login_as("tux_op_finedit_nodelete", "password123", "operator")
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-OPFIN-NODEL", "date": today, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    res = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "x", "confirm": True})
    assert res.status_code == 403  # Delete stays Manager/Super Admin only — Operator edit access never implies delete access


def test_operator_finalized_edit_is_audited(client, setup, login_as, app):
    today = business_today()
    login_as("tux_op_finedit_audit", "password123", "operator")
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-OPFIN-AUDIT1", "date": today, "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "operator self-correction", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="correct_record", entity_type="dispatch", entity_id=str(d["id"])).first()
        assert entry is not None
        assert entry.username == "tux_op_finedit_audit"
        before = json.loads(entry.before_json)
        after = json.loads(entry.after_json)
        assert before["actor_role"] == "operator"
        assert after["reason"] == "operator self-correction"
        assert entry.created_at is not None


def test_manager_still_sees_full_button_set_after_operator_change(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "TUX-MGRSTILL-1", "date": "2026-08-01", "customer_id": setup["customer"]["id"],
        "sales_category_id": setup["category"]["id"],
        "lines": [{"product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    # Manager/Super Admin still correct anything, ownership never applies to them.
    res = client.post(f"/api/dispatches/{d['id']}/correct", json={
        "reason": "manager correction unaffected", "notes": None,
        "lines": [{"id": d["lines"][0]["id"], "product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    res_delete = client.delete(f"/api/dispatches/{d['id']}", json={"reason": "x", "confirm": True})
    assert res_delete.status_code == 200


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
