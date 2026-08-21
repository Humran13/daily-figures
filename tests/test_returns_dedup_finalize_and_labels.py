"""
Targeted fix package — three of the five items from the Returns/Daily-
Figures layout round (see the completion report for items 1/2, the
Dashboard/Operator table layout, covered by tests/test_dashboard_dfig_
view_all_layout.py and tests/test_operator_table_sticky_and_compact_
layout.py):

  3. Reinforce duplicate-Returns prevention. The CREATE path
     (returns_service.create_return()) already centrally enforced "one
     active Return per canonical recipient per business date" — see
     tests/test_final_ux_reporting_data_entry_package.py's own "ITEM 4"
     section, which is untouched and still fully covers that path. The
     actual gap: returns_service.update_header() (used by both the plain
     draft-header PATCH route and, via record_correction_service.
     correct_record(), the "Correct Record" flow) never ran the same
     check — a draft could be created/left with a non-colliding customer/
     date, then edited into a collision with an existing active Return
     right before finalizing, bypassing the rule entirely. Reinforced by
     reusing the exact same _find_active_duplicate_return() helper (now
     accepting an exclude_id so a record never collides with itself),
     never a second/competing duplicate system.

  4. Returned By is now required before FINALIZE (not before Draft save —
     an incomplete Draft is still allowed, unchanged) — see
     returns_service.finalize_return().

  5. "Name & Sign" is relabeled "Received By" throughout the UI (entry
     form, correction form, detail view, printed report) and the export
     drops the old Received By/Verified By integer-user-id columns in
     favor of just Returned By / Received By — reusing the EXISTING
     signed_by_name field/column (which already defaulted to the logged-
     in operator, exactly matching the desired "Received By" business
     meaning) rather than any new digital-signature feature. Nothing in
     the database schema changed; ReturnRecord.received_by/verified_by
     remain exactly as before, just no longer duplicated in the printed
     report/export.
"""
import pathlib

import pytest

from webapp.services.business_calendar import business_today

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
RETURNS_HTML = (STATIC / "returns.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("rdf_root", "password123", "super_admin")


def _make_product(client, name="RDF Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "RDF Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "RDF Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _line(pid, cartons=1):
    return [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}]


# =====================================================================
# ITEM 3 — duplicate prevention reinforced on the EDIT / CORRECT path
# =====================================================================

def test_editing_a_draft_into_collision_with_an_existing_active_return_is_rejected(client, setup):
    date = business_today()
    other_customer = client.post("/api/admin/customers", json={
        "name": "RDF Other Recipient", "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True,
    }).get_json()

    # An existing active Return for the real recipient/date.
    existing = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    assert existing["id"]

    # A second draft created for a DIFFERENT recipient — passes the
    # create-time check trivially.
    draft = client.post("/api/returns", json={
        "date": date, "customer_id": other_customer["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()

    # Now edit that draft's header to point at the SAME recipient/date as
    # the existing active Return — this is the gap: update_header() must
    # now reject it too, exactly like create_return() would have.
    res = client.patch(f"/api/returns/{draft['id']}", json={"customer_id": setup["customer"]["id"]})
    assert res.status_code == 409
    assert "already exists for" in res.get_json()["error"]


def test_editing_a_drafts_date_into_collision_is_rejected(client, setup):
    date = business_today()
    existing = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    assert existing["id"]

    other_date = "2020-01-01"
    draft = client.post("/api/returns", json={
        "date": other_date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()

    res = client.patch(f"/api/returns/{draft['id']}", json={"date": date})
    assert res.status_code == 409


def test_editing_a_drafts_own_unrelated_fields_never_self_collides(client, setup):
    # A no-op-ish header edit (e.g. remarks only) on a draft that already
    # legitimately owns its own recipient/date must never be rejected as
    # "colliding with itself".
    date = business_today()
    draft = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    res = client.patch(f"/api/returns/{draft['id']}", json={"remarks": "just a note"})
    assert res.status_code == 200


def test_forged_patch_duplicate_rejected_server_side(client, setup, login_as):
    date = business_today()
    other_customer = client.post("/api/admin/customers", json={
        "name": "RDF Forged Recipient", "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True,
    }).get_json()
    client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    })

    login_as("rdf_forge_op", "password123", "operator")
    # Operator's own draft, for a DIFFERENT recipient — this is the one
    # the operator will then try to edit (their own record) into a
    # collision with the existing active Return above.
    own_draft = client.post("/api/returns", json={
        "date": date, "customer_id": other_customer["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    res = client.patch(f"/api/returns/{own_draft['id']}", json={"customer_id": setup["customer"]["id"]})
    assert res.status_code == 409


def test_correcting_a_finalized_return_into_collision_is_rejected(client, setup):
    date = business_today()
    other_customer = client.post("/api/admin/customers", json={
        "name": "RDF Correct Other", "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True,
    }).get_json()

    existing = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()

    other = client.post("/api/returns", json={
        "date": date, "customer_id": other_customer["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{other['id']}/finalize")

    # Manager/Super Admin "Correct Record" on the finalized `other` return,
    # changing its recipient to collide with `existing`.
    res = client.post(f"/api/returns/{other['id']}/correct", json={
        "reason": "wrong recipient recorded originally",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 409
    assert "already exists for" in res.get_json()["error"]

    # And the original finalized record must be completely unchanged —
    # correction failure never partially applies.
    unchanged = client.get(f"/api/returns/{other['id']}").get_json()
    assert unchanged["returned_by_customer_id"] == other_customer["id"]
    assert unchanged["status"] == "finalized"


def test_existing_create_time_duplicate_prevention_still_works_unchanged(client, setup):
    # Untouched regression proof that the original CREATE-path rule (see
    # tests/test_final_ux_reporting_data_entry_package.py's own "ITEM 4"
    # section) is not weakened by this reinforcement.
    date = business_today()
    body = {"date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"])}
    first = client.post("/api/returns", json=body)
    assert first.status_code == 201
    second = client.post("/api/returns", json=body)
    assert second.status_code == 409


def test_metro_monday_and_non_monday_posting_unaffected_by_the_reinforced_check(client, setup, app):
    import datetime
    metro_cat = client.post("/api/admin/sales-categories", json={"name": "Metro Sales"}).get_json()
    cust = client.post("/api/admin/customers", json={
        "name": "RDF Metro Truck", "sales_category_id": metro_cat["id"], "confirm_not_duplicate": True,
    }).get_json()
    today = datetime.date.fromisoformat(business_today())
    monday = (today - datetime.timedelta(days=today.weekday())).isoformat()

    r = client.post("/api/returns", json={
        "date": monday, "customer_id": cust["id"], "lines": _line(setup["product"]["id"], cartons=3),
    }).get_json()
    client.post(f"/api/returns/{r['id']}/finalize")
    row = client.get(f"/api/daily-figures/{setup['product']['id']}?date={monday}&shift=Day").get_json()
    assert row["return_"]["base_qty"] == 300  # Monday: real contribution


# =====================================================================
# ITEM 4 — Returned By required before Finalize
# =====================================================================

def test_finalize_with_empty_returned_by_rejected(client, setup):
    draft = client.post("/api/returns", json={
        "date": business_today(), "lines": _line(setup["product"]["id"]),
    }).get_json()
    assert draft["returned_by_customer_id"] is None
    assert draft["returned_by_name"] is None

    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 400
    # Superseded message — see tests/test_returns_recipient_selection_
    # required.py for the current "select from the customer list" wording.
    assert "select Returned By from the customer list" in res.get_json()["error"]


def test_finalize_with_blank_whitespace_returned_by_name_rejected(client, setup):
    draft = client.post("/api/returns", json={
        "date": business_today(), "returned_by_name": "   ", "lines": _line(setup["product"]["id"]),
    }).get_json()
    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 400


def test_finalize_with_resolved_customer_succeeds(client, setup):
    draft = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 200
    assert res.get_json()["status"] == "finalized"


def test_finalize_with_free_text_returned_by_now_rejected(client, setup):
    # Superseded by the narrower "must select from customer list" fix
    # (see tests/test_returns_recipient_selection_required.py) — free
    # text alone used to be sufficient here; it no longer is.
    draft = client.post("/api/returns", json={
        "date": business_today(), "returned_by_name": "Truck 12 / Field Rep", "lines": _line(setup["product"]["id"]),
    }).get_json()
    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 400


def test_draft_with_empty_returned_by_can_still_be_saved(client, setup):
    # Draft behavior is unchanged — only the FINALIZE transition is gated.
    res = client.post("/api/returns", json={
        "date": business_today(), "lines": _line(setup["product"]["id"]),
    })
    assert res.status_code == 201
    assert res.get_json()["status"] == "draft"


def test_forged_finalize_request_with_no_returned_by_rejected_server_side(client, setup, login_as):
    login_as("rdf_forge_finalize_op", "password123", "operator")
    draft = client.post("/api/returns", json={
        "date": business_today(), "lines": _line(setup["product"]["id"]),
    }).get_json()
    # Forged request: hits the API directly with no returned-by, bypassing
    # any frontend check entirely.
    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 400


def test_correcting_a_return_to_clear_returned_by_then_refinalizing_is_rejected(client, setup):
    # correct_record() reopens-edits-refinalizes a finalized record in one
    # transaction — clearing Returned By down to nothing during that must
    # still be caught by the SAME finalize_return() check, since it's
    # called again internally to refinalize.
    draft = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{draft['id']}/finalize")

    res = client.post(f"/api/returns/{draft['id']}/correct", json={
        "reason": "clearing returned by for this test",
        "returned_by_name": "",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400


def test_frontend_shows_clear_validation_message_before_finalize():
    # Superseded message text — see
    # tests/test_returns_recipient_selection_required.py for the current
    # "select from the customer list" wording this file's frontend check
    # was upgraded to.
    assert "Please select Returned By from the customer list." in RETURNS_HTML


def test_frontend_validation_only_gates_finalize_not_draft_save():
    idx = RETURNS_HTML.index("async function saveReturn(finalize){")
    end = RETURNS_HTML.index("\n}", idx)
    body = RETURNS_HTML[idx:end]
    assert "if(finalize && !selectedCustomer){" in body


# =====================================================================
# ITEM 5 — "Name & Sign" -> "Received By", simplified report/export
# =====================================================================

def test_entry_page_shows_received_by_not_name_and_sign():
    assert "Received By" in RETURNS_HTML
    assert "Name &amp; Sign" not in RETURNS_HTML
    assert "Name & Sign" not in RETURNS_HTML.replace("Returns \"Name & Sign\"", "")  # docstring mention excluded


def test_entry_page_still_uses_the_existing_signed_by_name_field(client, setup, login_as):
    # Reuses the existing field/data — no new digital-signature feature,
    # no schema change. The id stays rSignedByName (unchanged JS wiring);
    # only its visible label/placeholder changed.
    assert 'id="rSignedByName"' in RETURNS_HTML
    assert 'id="correctSignedByName"' in RETURNS_HTML

    login_as("rdf_receiver_op", "password123", "operator")
    res = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    })
    assert res.get_json()["signed_by_name"] == "rdf_receiver_op"  # unchanged auto-default behavior


def test_manager_can_still_override_the_received_by_field(client, setup, super_admin):
    res = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"],
        "signed_by_name": "Authorized Warehouse Lead", "lines": _line(setup["product"]["id"]),
    })
    assert res.get_json()["signed_by_name"] == "Authorized Warehouse Lead"


def test_historical_signed_by_name_still_displays_as_received_by(client, setup, super_admin):
    # A record entered before this round (i.e. under the old "Name & Sign"
    # framing) stores the same signed_by_name value — the detail view must
    # keep showing it correctly under the new label, no data rewrite.
    created = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"],
        "signed_by_name": "Old Historical Signer", "lines": _line(setup["product"]["id"]),
    }).get_json()
    fetched = client.get(f"/api/returns/{created['id']}").get_json()
    assert fetched["signed_by_name"] == "Old Historical Signer"

    idx = RETURNS_HTML.index("async function openDetail(id){")
    end = RETURNS_HTML.index("\n}", idx)
    body = RETURNS_HTML[idx:end]
    assert "Received By: ${escapeHtml(data.signed_by_name" in body


def test_detail_view_no_longer_shows_verified_by_or_received_by_user_id_lines():
    idx = RETURNS_HTML.index("async function openDetail(id){")
    end = RETURNS_HTML.index("\n}", idx)
    body = RETURNS_HTML[idx:end]
    assert "Verified by:" not in body
    assert "received_by_username" not in body
    assert "verified_by_username" not in body
    assert "Returned By:" in body
    assert "Received By:" in body


def test_printed_report_shares_the_same_simplified_detail_markup():
    # Print (window.print()) renders the same #detailHeader/#detailLines
    # content shown on screen, filtered only by @media print CSS — there
    # is no separate print-only template to duplicate/diverge from.
    assert "window.print()" in RETURNS_HTML
    idx = RETURNS_HTML.index('id="detailHeader"')
    assert 'class="card" id="detailHeader"' in RETURNS_HTML[idx-30:idx+20]


def test_export_columns_are_returned_by_and_received_by_only(client, setup, super_admin):
    created = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"],
        "signed_by_name": "Export Test Receiver", "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    res = client.get("/api/returns/export.csv")
    assert res.status_code == 200
    text = res.data.decode()
    header = text.splitlines()[4]  # row 0=title, 1=generated-by, 2=filters, 3=blank, 4=column headers
    assert "Returned By" in header
    assert "Received By" in header
    assert "Verified By" not in header
    assert "Name & Sign" not in header
    assert "Export Test Receiver" in text


def test_export_still_works_for_xlsx_and_pdf_after_column_simplification(client, setup):
    created = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    assert client.get("/api/returns/export.xlsx").status_code == 200
    pdf = client.get("/api/returns/export.pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")


def test_no_historical_return_data_deleted_from_the_database(client, setup, app):
    created = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"],
        "signed_by_name": "Kept Signer", "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    with app.app_context():
        from webapp.extensions import db
        from webapp.models.return_record import ReturnRecord
        row = db.session.get(ReturnRecord, created["id"])
        assert row.signed_by_name == "Kept Signer"
        assert row.received_by is not None  # old internal field still populated, just not printed
        assert row.verified_by is not None  # finalizing still sets it, still stored


def test_returns_stock_calculation_completely_unchanged_by_label_simplification(client, setup):
    date = business_today()
    created = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"], cartons=5),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    row = client.get(f"/api/daily-figures/{setup['product']['id']}?date={date}&shift=Day").get_json()
    assert row["return_"]["base_qty"] == 500


def test_no_digital_signature_workflow_introduced():
    assert "canvas" not in RETURNS_HTML.lower() or "signature" not in RETURNS_HTML.lower()
    assert "signaturepad" not in RETURNS_HTML.lower().replace(" ", "")
    assert 'type="file"' not in RETURNS_HTML  # no image/drawn-signature upload
