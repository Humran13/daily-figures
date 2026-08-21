"""
Narrow follow-up fix: Returned By must now be a SELECTED, canonical
Customer (returned_by_customer_id) before a Return can be finalized —
free text alone (returned_by_name_snapshot with no customer_id) is no
longer sufficient. This tightens the previous round's "Returned By is
required before finalize" rule (which accepted free text) — see
tests/test_returns_dedup_finalize_and_labels.py's now-superseded
test_finalize_with_free_text_returned_by_now_rejected for that history.

Root cause this closes: an Operator could type a name matching an
existing customer (e.g. "Dakar") into the free-text field without ever
clicking the actual autocomplete match, silently sending returned_by_name
with no customer_id — that record then had no canonical relationship at
all, so the existing duplicate check (which keys on customer_id) could
never see it as colliding with a later, properly-linked "Dakar" Return.

Fix (webapp/services/returns_service.finalize_return()): reject finalize
whenever returned_by_customer_id is None, regardless of what
returned_by_name_snapshot holds. This is the ONLY change — draft saving,
_resolve_returned_by()'s existing "customer must exist" check, and the
centralized duplicate helper (_find_active_duplicate_return(), reused
unchanged from the previous round) are all untouched. Historical rows
(status=finalized, customer_id=None, inserted directly to simulate data
that predates this rule) are never rewritten and remain fully readable —
this only gates the ACT of transitioning DRAFT -> FINALIZED, which never
runs against an already-finalized historical row unless someone chooses
to correct it (at which point the same rule reasonably applies, like any
other finalize).

Frontend (static/returns.html): saveReturn(finalize) now requires
selectedCustomer specifically (set only by clicking an autocomplete
result — see the #rReturnedBySearch results-click handler), not just any
typed text.
"""
import pytest

from webapp.services.business_calendar import business_today


@pytest.fixture
def super_admin(login_as):
    return login_as("rsr_root", "password123", "super_admin")


def _make_product(client, name="RSR Product"):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    return product


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client)
    category = client.post("/api/admin/sales-categories", json={"name": "RSR Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "RSR Recipient", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {"product": product, "category": category, "customer": customer}


def _line(pid, cartons=1):
    return [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}]


# =====================================================================
# SECTION 11 — recipient selection
# =====================================================================

def test_typed_but_not_selected_customer_name_rejected_at_finalize(client, setup):
    # Simulates exactly the reported bug: the operator types "RSR
    # Recipient" (a real, existing customer's name) into the free-text
    # field but never clicks the autocomplete match — the frontend sends
    # only returned_by_name, no customer_id.
    draft = client.post("/api/returns", json={
        "date": business_today(), "returned_by_name": setup["customer"]["name"],
        "lines": _line(setup["product"]["id"]),
    }).get_json()
    assert draft["returned_by_customer_id"] is None
    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 400
    assert "select Returned By from the customer list" in res.get_json()["error"]


def test_valid_selected_customer_finalize_allowed(client, setup):
    draft = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"],
        "lines": _line(setup["product"]["id"]),
    }).get_json()
    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 200
    assert res.get_json()["status"] == "finalized"


def test_forged_api_free_text_only_rejected(client, setup, login_as):
    # Server-side enforcement, reached directly, no frontend involved.
    login_as("rsr_forge_op", "password123", "operator")
    draft = client.post("/api/returns", json={
        "date": business_today(), "returned_by_name": "Some Random Truck",
        "lines": _line(setup["product"]["id"]),
    }).get_json()
    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 400


def test_forged_api_completely_blank_returned_by_rejected(client, setup):
    draft = client.post("/api/returns", json={
        "date": business_today(), "lines": _line(setup["product"]["id"]),
    }).get_json()
    assert draft["returned_by_customer_id"] is None
    assert draft["returned_by_name"] is None
    res = client.post(f"/api/returns/{draft['id']}/finalize")
    assert res.status_code == 400


def test_forged_api_nonexistent_customer_id_rejected_at_create(client, setup):
    # _resolve_returned_by() already refuses a nonexistent customer_id —
    # this is the existing "customer must be real" guarantee this fix
    # relies on; a bad id can never even become a draft's recipient.
    res = client.post("/api/returns", json={
        "date": business_today(), "customer_id": 999999,
        "lines": _line(setup["product"]["id"]),
    })
    assert res.status_code == 400
    assert "does not exist" in res.get_json()["error"]


def test_valid_customer_id_accepted_end_to_end(client, setup):
    res = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"],
        "lines": _line(setup["product"]["id"]),
    })
    assert res.status_code == 201
    created = res.get_json()
    assert created["returned_by_customer_id"] == setup["customer"]["id"]
    fin = client.post(f"/api/returns/{created['id']}/finalize")
    assert fin.status_code == 200


def test_draft_may_still_be_saved_with_free_text_only(client, setup):
    # Draft behavior is completely unaffected — only Finalize is gated.
    res = client.post("/api/returns", json={
        "date": business_today(), "returned_by_name": "Not Yet Selected",
        "lines": _line(setup["product"]["id"]),
    })
    assert res.status_code == 201
    assert res.get_json()["status"] == "draft"


def test_frontend_requires_selected_customer_not_just_typed_text():
    from pathlib import Path
    returns_html = (Path(__file__).parent.parent / "static" / "returns.html").read_text(encoding="utf-8")
    idx = returns_html.index("async function saveReturn(finalize){")
    end = returns_html.index("\n}", idx)
    body = returns_html[idx:end]
    assert "if(finalize && !selectedCustomer){" in body
    assert "Please select Returned By from the customer list." in body
    # The stale "any typed text is enough" check must be gone.
    assert "hasReturnedBy" not in body


def test_frontend_focuses_the_field_after_rejection():
    from pathlib import Path
    returns_html = (Path(__file__).parent.parent / "static" / "returns.html").read_text(encoding="utf-8")
    idx = returns_html.index("async function saveReturn(finalize){")
    end = returns_html.index("\n}", idx)
    body = returns_html[idx:end]
    assert "document.getElementById('rReturnedBySearch').focus();" in body


def test_autocomplete_and_free_text_option_both_still_present():
    # Section 3: "keep the existing Returned By autocomplete/dropdown...
    # the user may type into the field to search" — typing/searching is
    # NOT removed, only accepting the typed-only result at finalize is.
    from pathlib import Path
    returns_html = (Path(__file__).parent.parent / "static" / "returns.html").read_text(encoding="utf-8")
    assert 'id="rReturnedBySearch"' in returns_html
    assert 'id="returnedByResults"' in returns_html
    assert "Use \"${escapeHtml(q)}\" as free text (no customer match)" in returns_html


# =====================================================================
# SECTION 2 / historical compatibility
# =====================================================================

def test_historical_free_text_finalized_return_remains_readable(client, app, setup):
    with app.app_context():
        from datetime import datetime, timezone
        from webapp.extensions import db
        from webapp.models.product import Product
        from webapp.models.return_record import STATUS_FINALIZED, ReturnLine, ReturnRecord
        legacy = ReturnRecord(
            date="2019-06-15", returned_by_customer_id=None,
            returned_by_name_snapshot="Old Legacy Truck Route",
            signed_by_name="legacy_operator", received_by=None, verified_by=None,
            remarks=None, status=STATUS_FINALIZED,
            created_by=None, finalized_by=None,
            finalized_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.session.add(legacy)
        db.session.flush()
        rule = db.session.get(Product, setup["product"]["id"]).current_packaging_rule()
        db.session.add(ReturnLine(
            return_id=legacy.id, product_id=setup["product"]["id"],
            cartons=2, packs=0, pieces=0, base_unit_qty=200, packaging_rule_id=rule.id,
        ))
        db.session.commit()
        legacy_id = legacy.id

    res = client.get(f"/api/returns/{legacy_id}")
    assert res.status_code == 200
    data = res.get_json()
    assert data["returned_by_customer_id"] is None
    assert data["returned_by_name"] == "Old Legacy Truck Route"
    assert data["status"] == "finalized"


def test_historical_free_text_return_appears_in_list_and_export(client, app, setup):
    with app.app_context():
        from datetime import datetime, timezone
        from webapp.extensions import db
        from webapp.models.product import Product
        from webapp.models.return_record import STATUS_FINALIZED, ReturnLine, ReturnRecord
        legacy = ReturnRecord(
            date="2019-06-16", returned_by_customer_id=None,
            returned_by_name_snapshot="Export Legacy Route",
            signed_by_name="legacy_operator2", received_by=None, verified_by=None,
            remarks=None, status=STATUS_FINALIZED,
            created_by=None, finalized_by=None,
            finalized_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.session.add(legacy)
        db.session.flush()
        rule = db.session.get(Product, setup["product"]["id"]).current_packaging_rule()
        db.session.add(ReturnLine(
            return_id=legacy.id, product_id=setup["product"]["id"],
            cartons=1, packs=0, pieces=0, base_unit_qty=100, packaging_rule_id=rule.id,
        ))
        db.session.commit()
        legacy_id = legacy.id

    listed = client.get("/api/returns?limit=200").get_json()["results"]
    assert any(r["id"] == legacy_id for r in listed)

    export = client.get("/api/returns/export.csv")
    assert export.status_code == 200
    assert "Export Legacy Route" in export.data.decode()


def test_no_historical_row_is_rewritten_by_this_fix(client, app, setup):
    with app.app_context():
        from datetime import datetime, timezone
        from webapp.extensions import db
        from webapp.models.product import Product
        from webapp.models.return_record import STATUS_FINALIZED, ReturnLine, ReturnRecord
        legacy = ReturnRecord(
            date="2019-06-17", returned_by_customer_id=None,
            returned_by_name_snapshot="Untouched Legacy Route",
            signed_by_name="legacy_operator3", received_by=None, verified_by=None,
            remarks=None, status=STATUS_FINALIZED,
            created_by=None, finalized_by=None,
            finalized_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.session.add(legacy)
        db.session.flush()
        rule = db.session.get(Product, setup["product"]["id"]).current_packaging_rule()
        db.session.add(ReturnLine(
            return_id=legacy.id, product_id=setup["product"]["id"],
            cartons=1, packs=0, pieces=0, base_unit_qty=100, packaging_rule_id=rule.id,
        ))
        db.session.commit()
        legacy_id = legacy.id

    # Just reading it (GET, list, export) must never mutate the row.
    client.get(f"/api/returns/{legacy_id}")
    client.get("/api/returns")
    client.get("/api/returns/export.csv")

    with app.app_context():
        from webapp.extensions import db
        from webapp.models.return_record import ReturnRecord
        row = db.session.get(ReturnRecord, legacy_id)
        assert row.returned_by_customer_id is None
        assert row.returned_by_name_snapshot == "Untouched Legacy Route"
        assert row.status == "finalized"


# =====================================================================
# SECTION 12 — duplicate rule now reliable (every finalized Return has a
# real customer_id) — most of this rule's coverage already exists
# unchanged in tests/test_final_ux_reporting_data_entry_package.py's
# "ITEM 4" section (draft/finalized/void/different-customer/different-
# date/free-text-exempt/Dakar/Derrick-merge/Metro Monday+non-Monday) and
# tests/test_returns_dedup_finalize_and_labels.py's edit/correction-path
# reinforcement — this section adds only what's genuinely new here: a
# customer RENAME case, and an end-to-end "selection, not raw API
# shortcut" proof.
# =====================================================================

def test_renamed_customer_still_collides_on_duplicate_check(client, setup, super_admin):
    date = business_today()
    first = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    })
    assert first.status_code == 201

    rename = client.patch(f"/api/admin/customers/{setup['customer']['id']}", json={"name": "RSR Recipient Renamed"})
    assert rename.status_code == 200

    # Same canonical customer_id, just renamed — duplicate check is
    # keyed on the stable id, never re-derived from the (now different)
    # display name.
    second = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    })
    assert second.status_code == 409
    assert "RSR Recipient Renamed" in second.get_json()["error"]


def test_selecting_from_dropdown_end_to_end_then_duplicate_rejected(client, setup):
    # Mirrors the real frontend flow as closely as an API test can: the
    # first Return is created by "selecting" the real customer_id (what
    # clicking the autocomplete result sends), finalized, and a second
    # attempt for the same recipient/date is rejected.
    date = business_today()
    first = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    assert client.post(f"/api/returns/{first['id']}/finalize").status_code == 200

    second = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    })
    assert second.status_code == 409


def test_second_duplicate_never_becomes_stock_effective(client, setup):
    date = business_today()
    first = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"], cartons=3),
    }).get_json()
    client.post(f"/api/returns/{first['id']}/finalize")

    second = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"], cartons=99),
    })
    assert second.status_code == 409

    row = client.get(f"/api/daily-figures/{setup['product']['id']}?date={date}&shift=Day").get_json()
    assert row["return_"]["base_qty"] == 300  # only the first return's contribution, never the rejected second


# =====================================================================
# SECTION 13 — correction flow
# =====================================================================

def test_correction_to_another_valid_customer_works_without_collision(client, setup):
    other = client.post("/api/admin/customers", json={
        "name": "RSR Other Valid Recipient", "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True,
    }).get_json()
    created = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "wrong recipient selected originally", "customer_id": other["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert client.get(f"/api/returns/{created['id']}").get_json()["returned_by_customer_id"] == other["id"]


def test_correction_into_customer_date_collision_returns_409(client, setup):
    other = client.post("/api/admin/customers", json={
        "name": "RSR Collision Target", "sales_category_id": setup["category"]["id"], "confirm_not_duplicate": True,
    }).get_json()
    date = business_today()
    existing = client.post("/api/returns", json={
        "date": date, "customer_id": other["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    assert existing["id"]

    created = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "trying to move into a colliding recipient", "customer_id": other["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 409


def test_correction_cannot_set_recipient_to_free_text_only(client, setup):
    created = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "trying to downgrade to free text", "returned_by_name": "Some Unlinked Truck",
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 400
    # The original valid customer link must remain untouched — a failed
    # correction never partially applies.
    assert client.get(f"/api/returns/{created['id']}").get_json()["returned_by_customer_id"] == setup["customer"]["id"]


def test_correction_excludes_the_record_itself_from_duplicate_check(client, setup):
    # A no-op-ish correction (same customer/date, just fixing quantity)
    # must never be rejected as "colliding with itself".
    created = client.post("/api/returns", json={
        "date": business_today(), "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"]),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")

    res = client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "just fixing the quantity", "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200


def test_stock_calculation_unchanged_by_correction_other_than_the_correction_itself(client, setup):
    date = business_today()
    created = client.post("/api/returns", json={
        "date": date, "customer_id": setup["customer"]["id"], "lines": _line(setup["product"]["id"], cartons=2),
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    before = client.get(f"/api/daily-figures/{setup['product']['id']}?date={date}&shift=Day").get_json()
    assert before["return_"]["base_qty"] == 200

    client.post(f"/api/returns/{created['id']}/correct", json={
        "reason": "quantity was wrong", "customer_id": setup["customer"]["id"],
        "lines": [{"id": created["lines"][0]["id"], "product_id": setup["product"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    after = client.get(f"/api/daily-figures/{setup['product']['id']}?date={date}&shift=Day").get_json()
    assert after["return_"]["base_qty"] == 500  # updated to reflect the legitimate correction, nothing else changed
