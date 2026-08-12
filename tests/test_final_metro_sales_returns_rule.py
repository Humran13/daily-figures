"""
Metro Sales Returns business rule (targeted correction):

Metro Sales (currently Dakar/Derrick, identified via the canonical
Customer.sales_category_id -> SalesCategory.name == "Metro Sales"
relationship, never by recipient display name) reports Returns digitally
every day, but the goods only physically transfer back into store stock
on Monday. So:

  - A Metro Sales Return is recorded normally on ANY day and always
    remains visible in Returns History (Preview/Edit/Print/Delete/audit
    all continue to work on it).
  - It contributes to Daily Figures/stock ONLY when it is BOTH finalized
    AND its own Return Date (never created_at/updated_at/today) falls on
    a Monday.
  - Every other Returns customer/category is completely unaffected — they
    keep posting on finalize, any day of the week, exactly as before.

THE single authoritative rule: webapp/services/returns_service.py's
is_return_stock_posting_eligible() (per-record) and
stock_posting_return_filter() (the identical rule expressed as a SQL
filter, for stock_service.py's bulk aggregate queries and
stock_ledger_service.py's CLI diagnostics). No other module re-derives
"is this Metro Sales" or "is this Monday" independently.
"""
import json
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
RETURNS_HTML = (STATIC / "returns.html").read_text(encoding="utf-8")

METRO_SALES_CATEGORY_NAME = "Metro Sales"

# A real calendar week, verified: 2026-08-03 is a Monday.
MONDAY = "2026-08-03"
TUESDAY = "2026-08-04"
WEDNESDAY = "2026-08-05"
THURSDAY = "2026-08-06"
FRIDAY = "2026-08-07"
SATURDAY = "2026-08-08"
SUNDAY = "2026-08-09"
NON_MONDAYS = [TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY]
ALL_WEEK = [MONDAY] + NON_MONDAYS


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, cartons_to_packs=10, packs_to_pieces=10):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces,
    })
    return product


@pytest.fixture
def setup(client, super_admin):
    product = _make_product(client, "Metro Test Product")
    metro_category = client.post("/api/admin/sales-categories", json={"name": METRO_SALES_CATEGORY_NAME}).get_json()
    normal_category = client.post("/api/admin/sales-categories", json={"name": "Ordinary Sales"}).get_json()
    dakar = client.post("/api/admin/customers", json={
        "name": "Dakar", "sales_category_id": metro_category["id"], "confirm_not_duplicate": True,
    }).get_json()
    derrick = client.post("/api/admin/customers", json={
        "name": "Derrick", "sales_category_id": metro_category["id"], "confirm_not_duplicate": True,
    }).get_json()
    normal_customer = client.post("/api/admin/customers", json={
        "name": "Ordinary Shop", "sales_category_id": normal_category["id"], "confirm_not_duplicate": True,
    }).get_json()
    return {
        "product": product, "metro_category": metro_category, "normal_category": normal_category,
        "dakar": dakar, "derrick": derrick, "normal_customer": normal_customer,
    }


def _create_return(client, product_id, customer_id, date, cartons=5):
    return client.post("/api/returns", json={
        "date": date, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": 0, "pieces": 0}],
    })


def _finalize(client, return_id):
    return client.post(f"/api/returns/{return_id}/finalize")


def _return_base_qty(client, product_id, date):
    row = client.get(f"/api/daily-figures/{product_id}?date={date}&shift=Day").get_json()
    return row["return_"]["base_qty"]


def _closing(client, product_id, date):
    row = client.get(f"/api/daily-figures/{product_id}?date={date}&shift=Day").get_json()
    return row["closing"]["base_qty"]


def _opening(client, product_id, date):
    row = client.get(f"/api/daily-figures/{product_id}?date={date}&shift=Day").get_json()
    return row["opening"]["base_qty"]


def _set_opening(client, product_id, date, cartons):
    client.post("/api/daily-figures", json={
        "product_id": product_id, "date": date, "shift": "Day",
        "opening": {"cartons": cartons, "packs": 0, "pieces": 0},
    })


# =====================================================================
# SECTION 19 — BASIC METRO RULE (all seven weekdays)
# =====================================================================

@pytest.mark.parametrize("date,expected_posts", [
    (MONDAY, True), (TUESDAY, False), (WEDNESDAY, False), (THURSDAY, False),
    (FRIDAY, False), (SATURDAY, False), (SUNDAY, False),
])
def test_metro_finalized_return_posts_only_on_monday(client, setup, date, expected_posts):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], date, cartons=5).get_json()
    res = _finalize(client, r["id"])
    assert res.status_code == 200
    assert _return_base_qty(client, pid, date) == (500 if expected_posts else 0)
    # Still a real, retrievable Returns History record regardless.
    fetched = client.get(f"/api/returns/{r['id']}").get_json()
    assert fetched["id"] == r["id"]
    assert fetched["status"] == "finalized"


def test_all_seven_metro_records_remain_valid_history_records(client, setup):
    pid = setup["product"]["id"]
    ids = []
    for date in ALL_WEEK:
        r = _create_return(client, pid, setup["dakar"]["id"], date, cartons=1).get_json()
        _finalize(client, r["id"])
        ids.append(r["id"])
    listed_ids = {row["id"] for row in client.get("/api/returns?limit=200").get_json()["results"]}
    assert set(ids).issubset(listed_ids)
    assert len(ids) == 7


# =====================================================================
# SECTION 20 — DAKAR / DEREK (Derrick)
# =====================================================================

def test_dakar_non_monday_return_recorded_not_posted(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, WEDNESDAY) == 0
    assert client.get(f"/api/returns/{r['id']}").get_json()["status"] == "finalized"


def test_derrick_non_monday_return_recorded_not_posted(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["derrick"]["id"], FRIDAY, cartons=3).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, FRIDAY) == 0
    assert client.get(f"/api/returns/{r['id']}").get_json()["status"] == "finalized"


def test_dakar_monday_return_posts(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, MONDAY) == 500


def test_derrick_monday_return_posts(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["derrick"]["id"], MONDAY, cartons=4).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, MONDAY) == 400


def test_renaming_dakar_does_not_break_the_rule(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, WEDNESDAY) == 0

    client.patch(f"/api/admin/customers/{setup['dakar']['id']}", json={"name": "Dakar Renamed Entirely"})
    # Same customer ID, same sales_category_id — the rule follows the
    # canonical relationship, not the name, so re-checking must be
    # unaffected by the rename.
    still_zero = _return_base_qty(client, pid, WEDNESDAY)
    assert still_zero == 0

    r2 = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=2).get_json()
    _finalize(client, r2["id"])
    assert _return_base_qty(client, pid, MONDAY) == 200


# =====================================================================
# SECTION 21 — NORMAL RETURNS MUST NOT CHANGE
# =====================================================================

@pytest.mark.parametrize("date", ALL_WEEK)
def test_normal_customer_return_posts_every_day_of_the_week(client, setup, date):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["normal_customer"]["id"], date, cartons=3).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, date) == 300


def test_metro_restriction_does_not_leak_to_other_customers(client, setup):
    pid = setup["product"]["id"]
    metro = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, metro["id"])
    normal = _create_return(client, pid, setup["normal_customer"]["id"], WEDNESDAY, cartons=2).get_json()
    _finalize(client, normal["id"])
    # Only the normal customer's 2 cartons post; Dakar's 5 do not.
    assert _return_base_qty(client, pid, WEDNESDAY) == 200


# =====================================================================
# SECTION 22 — STATUS
# =====================================================================

def test_monday_metro_draft_does_not_post(client, setup):
    pid = setup["product"]["id"]
    _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=5)
    assert _return_base_qty(client, pid, MONDAY) == 0


def test_monday_metro_finalized_posts(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=5).get_json()
    assert _return_base_qty(client, pid, MONDAY) == 0
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, MONDAY) == 500


def test_non_monday_metro_draft_does_not_post(client, setup):
    pid = setup["product"]["id"]
    _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5)
    assert _return_base_qty(client, pid, WEDNESDAY) == 0


def test_non_monday_metro_finalized_does_not_post(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, WEDNESDAY) == 0


def test_finalizing_non_monday_metro_return_does_not_change_store_stock(client, setup):
    pid = setup["product"]["id"]
    _set_opening(client, pid, WEDNESDAY, 100)
    before = _closing(client, pid, WEDNESDAY)
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    after = _closing(client, pid, WEDNESDAY)
    assert before == after == 10000


# =====================================================================
# SECTION 23 — DATE CORRECTION
# =====================================================================

def test_wednesday_metro_corrected_to_monday_becomes_active_exactly_once(client, setup, super_admin):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, WEDNESDAY) == 0
    assert _return_base_qty(client, pid, MONDAY) == 0

    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "Metro return actually posts this Monday", "notes": None, "date": MONDAY,
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert _return_base_qty(client, pid, WEDNESDAY) == 0
    assert _return_base_qty(client, pid, MONDAY) == 500  # exactly once

    all_returns = client.get("/api/returns?limit=200").get_json()["results"]
    assert sum(1 for row in all_returns if row["id"] == r["id"]) == 1  # no duplicate


def test_monday_metro_corrected_to_wednesday_contribution_disappears_exactly_once(client, setup, super_admin):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, MONDAY) == 500

    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "entered on the wrong date", "notes": None, "date": WEDNESDAY,
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert _return_base_qty(client, pid, MONDAY) == 0     # old period loses it
    assert _return_base_qty(client, pid, WEDNESDAY) == 0  # new period is Metro + non-Monday, still 0


def test_date_correction_old_and_new_period_recalculate(client, setup, super_admin):
    # 100 cartons opening x 100 base units/carton (10 packs/carton x 10
    # pieces/pack) = 10000 base units; the 5-carton return is 500.
    pid = setup["product"]["id"]
    _set_opening(client, pid, MONDAY, 100)
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])

    client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": MONDAY,
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })
    closing_wed_after = _closing(client, pid, WEDNESDAY)
    closing_mon_after = _closing(client, pid, MONDAY)
    assert closing_mon_after == 10000 + 500  # MON gains it, now posting
    # WED's own return contribution is gone, but its OPENING correctly
    # carries forward from MONDAY's now-higher closing (no other activity
    # in between) — same total, arrived at via the carry-forward chain
    # rather than WED's own (now nonexistent) return contribution.
    assert closing_wed_after == 10500


def test_date_correction_subsequent_opening_carries_correctly(client, setup, super_admin):
    pid = setup["product"]["id"]
    _set_opening(client, pid, MONDAY, 100)
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])

    client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": MONDAY,
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })
    monday_closing = _closing(client, pid, MONDAY)
    tuesday_opening = _opening(client, pid, TUESDAY)
    assert tuesday_opening == monday_closing == 10500


def test_date_correction_no_duplicate_return_created(client, setup, super_admin):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "wrong date", "notes": None, "date": MONDAY,
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    all_returns = client.get("/api/returns?limit=200").get_json()["results"]
    assert sum(1 for row in all_returns if row["id"] == r["id"]) == 1


# =====================================================================
# SECTION 24 — RECIPIENT / CATEGORY CORRECTION
# =====================================================================

def test_normal_wednesday_corrected_to_metro_becomes_zero(client, setup, super_admin):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["normal_customer"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, WEDNESDAY) == 500

    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "wrong recipient recorded", "notes": None, "customer_id": setup["dakar"]["id"],
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert _return_base_qty(client, pid, WEDNESDAY) == 0


def test_metro_wednesday_corrected_to_normal_becomes_active(client, setup, super_admin):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    assert _return_base_qty(client, pid, WEDNESDAY) == 0

    res = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "wrong recipient recorded", "notes": None, "customer_id": setup["normal_customer"]["id"],
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })
    assert res.status_code == 200
    assert _return_base_qty(client, pid, WEDNESDAY) == 500


def test_recipient_correction_no_duplicate_record(client, setup, super_admin):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["normal_customer"]["id"], WEDNESDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "wrong recipient", "notes": None, "customer_id": setup["dakar"]["id"],
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 1, "packs": 0, "pieces": 0}],
    })
    all_returns = client.get("/api/returns?limit=200").get_json()["results"]
    assert sum(1 for row in all_returns if row["id"] == r["id"]) == 1


def test_recipient_correction_carry_forward_reconciles(client, setup, super_admin):
    pid = setup["product"]["id"]
    _set_opening(client, pid, WEDNESDAY, 50)
    r = _create_return(client, pid, setup["normal_customer"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    closing_before = _closing(client, pid, WEDNESDAY)
    assert closing_before == 5000 + 500  # 50 ctns opening + 5 ctns return, both x100 base units/carton

    client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "wrong recipient — actually Metro", "notes": None, "customer_id": setup["dakar"]["id"],
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 5, "packs": 0, "pieces": 0}],
    })
    closing_after = _closing(client, pid, WEDNESDAY)
    thursday_opening = _opening(client, pid, THURSDAY)
    assert closing_after == 5000  # return no longer posts
    assert thursday_opening == closing_after


# =====================================================================
# SECTION 25 — DELETE
# =====================================================================

def test_deleting_non_monday_metro_return_does_not_alter_stock(client, setup, super_admin):
    pid = setup["product"]["id"]
    _set_opening(client, pid, WEDNESDAY, 20)
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    before = _closing(client, pid, WEDNESDAY)
    assert before == 2000  # only opening (20 ctns x100); the Metro return never posted

    res = client.delete(f"/api/returns/{r['id']}", json={"reason": "entered in error", "confirm": True})
    assert res.status_code == 200
    after = _closing(client, pid, WEDNESDAY)
    assert after == before == 2000


def test_deleting_monday_metro_return_removes_its_stock_contribution(client, setup, super_admin):
    pid = setup["product"]["id"]
    _set_opening(client, pid, MONDAY, 20)
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    before = _closing(client, pid, MONDAY)
    assert before == 2000 + 500

    res = client.delete(f"/api/returns/{r['id']}", json={"reason": "entered in error", "confirm": True})
    assert res.status_code == 200
    after = _closing(client, pid, MONDAY)
    assert after == 2000


def test_repeated_deletion_cannot_change_stock_twice(client, setup, super_admin):
    pid = setup["product"]["id"]
    _set_opening(client, pid, MONDAY, 20)
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    first = client.delete(f"/api/returns/{r['id']}", json={"reason": "x", "confirm": True})
    assert first.status_code == 200
    after_first = _closing(client, pid, MONDAY)
    second = client.delete(f"/api/returns/{r['id']}", json={"reason": "x", "confirm": True})
    assert second.status_code == 404  # already gone
    after_second = _closing(client, pid, MONDAY)
    assert after_first == after_second == 2000


def test_delete_audit_behavior_intact_for_metro_return(client, setup, super_admin, app):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    client.delete(f"/api/returns/{r['id']}", json={"reason": "audit check metro", "confirm": True})
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(action="permanent_delete_return", entity_id=str(r["id"])).first()
        assert entry is not None
        before = json.loads(entry.before_json)
        assert before["deletion_reason"] == "audit check metro"


# =====================================================================
# SECTION 26 — HISTORY
# =====================================================================

def test_non_monday_metro_return_remains_in_returns_history(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    listed = client.get("/api/returns?limit=200").get_json()["results"]
    assert any(row["id"] == r["id"] for row in listed)


def test_monday_metro_return_remains_in_returns_history(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    listed = client.get("/api/returns?limit=200").get_json()["results"]
    assert any(row["id"] == r["id"] for row in listed)


def test_edit_void_delete_print_work_for_metro_returns_in_markup():
    # No new "cancelled"/"rejected" status was introduced for Metro Sales
    # specifically — void (a later round's standardized action, replacing
    # the removed Preview button) is the same generic action every Returns
    # record can reach; nothing about it is Metro-Sales-specific.
    assert 'data-action="correct">Edit<' in RETURNS_HTML
    assert 'data-action="void">Void<' in RETURNS_HTML
    assert 'data-action="delete">Delete<' in RETURNS_HTML
    assert 'data-action="print">Print<' in RETURNS_HTML


def test_manager_super_admin_delete_continues_working_on_metro_return(client, setup, super_admin):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    res = client.delete(f"/api/returns/{r['id']}", json={"reason": "x", "confirm": True})
    assert res.status_code == 200


def test_operator_role_permissions_unchanged_for_metro_returns(client, setup, login_as):
    # Uses today's business date (not a fixed Metro weekday) — the same-day
    # Operator edit window (a later, separate round) requires it, and this
    # test is only checking the Delete/Edit permission split, not the
    # Metro Monday rule itself.
    from webapp.services.business_calendar import business_today
    today = business_today()
    pid = setup["product"]["id"]
    login_as("metro_op1", "password123", "operator")
    r = _create_return(client, pid, setup["dakar"]["id"], today, cartons=1).get_json()
    _finalize(client, r["id"])
    # Operator cannot Delete, Metro or not — unchanged permission matrix.
    res = client.delete(f"/api/returns/{r['id']}", json={"reason": "x", "confirm": True})
    assert res.status_code == 403
    # Operator CAN correct their own finalized return while still same-day
    # (unrelated round, unaffected by the Metro rule — a plain permission
    # check).
    res2 = client.post(f"/api/returns/{r['id']}/correct", json={
        "reason": "operator self-correction", "notes": None,
        "lines": [{"id": r["lines"][0]["id"], "product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    })
    assert res2.status_code == 200


def test_no_new_status_introduced_for_metro_returns(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    fetched = client.get(f"/api/returns/{r['id']}").get_json()
    assert fetched["status"] in ("draft", "finalized", "void")


def test_collapsed_by_default_history_markup_unchanged():
    assert '<div class="date-group collapsed">' in RETURNS_HTML


# =====================================================================
# SECTION 27 — CROSS-SURFACE CONSISTENCY
# =====================================================================

def test_non_monday_metro_return_consistent_across_all_stock_surfaces(client, setup, super_admin):
    pid = setup["product"]["id"]
    _set_opening(client, pid, WEDNESDAY, 100)
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])

    # Returns History: record exists.
    assert any(row["id"] == r["id"] for row in client.get("/api/returns?limit=200").get_json()["results"])
    # Daily Figures Returns: 0 contribution.
    assert _return_base_qty(client, pid, WEDNESDAY) == 0
    # Closing Stock: unchanged by that return (just the opening).
    assert _closing(client, pid, WEDNESDAY) == 10000
    # Next Opening: unchanged by that return.
    assert _opening(client, pid, THURSDAY) == 10000

    # Stock-ledger CLI diagnostic: 0 contribution from that specific line.
    from webapp.services.stock_ledger_service import _returns_lines
    total, lines, _note = _returns_lines(pid, WEDNESDAY, "Day")
    assert total == 0
    matching = [ln for ln in lines if ln["record_id"] == r["id"]]
    assert matching and matching[0]["included"] is False


def test_monday_metro_return_included_exactly_once_across_all_stock_surfaces(client, setup, super_admin):
    pid = setup["product"]["id"]
    _set_opening(client, pid, MONDAY, 100)
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=5).get_json()
    _finalize(client, r["id"])

    assert any(row["id"] == r["id"] for row in client.get("/api/returns?limit=200").get_json()["results"])
    assert _return_base_qty(client, pid, MONDAY) == 500
    assert _closing(client, pid, MONDAY) == 10500
    assert _opening(client, pid, TUESDAY) == 10500

    from webapp.services.stock_ledger_service import _returns_lines
    total, lines, _note = _returns_lines(pid, MONDAY, "Day")
    assert total == 500
    matching = [ln for ln in lines if ln["record_id"] == r["id"]]
    assert matching and matching[0]["included"] is True


# =====================================================================
# CENTRAL RULE — direct unit-level proof
# =====================================================================

def test_is_metro_sales_return_uses_sales_category_relationship_not_name(client, setup, app):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=1).get_json()
    r2 = _create_return(client, pid, setup["normal_customer"]["id"], WEDNESDAY, cartons=1).get_json()

    with app.app_context():
        from webapp.extensions import db
        from webapp.models.return_record import ReturnRecord
        from webapp.services import returns_service

        record = db.session.get(ReturnRecord, r["id"])
        assert returns_service.is_metro_sales_return(record) is True

        record2 = db.session.get(ReturnRecord, r2["id"])
        assert returns_service.is_metro_sales_return(record2) is False


def test_free_text_returned_by_is_never_metro_sales(client, setup, app):
    pid = setup["product"]["id"]
    res = client.post("/api/returns", json={
        "date": WEDNESDAY, "returned_by_name": "Random Truck",
        "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    })
    r = res.get_json()
    _finalize(client, r["id"])
    # No linked customer at all -> never Metro Sales -> posts normally.
    assert _return_base_qty(client, pid, WEDNESDAY) == 300


# =====================================================================
# API-LEVEL FLAGS FOR THE FRONTEND (no independent UI decision)
# =====================================================================

def test_api_exposes_stock_posting_flags_on_get_return(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    fetched = client.get(f"/api/returns/{r['id']}").get_json()
    assert fetched["is_metro_sales_return"] is True
    assert fetched["stock_posting_eligible"] is False

    r2 = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=1).get_json()
    _finalize(client, r2["id"])
    fetched2 = client.get(f"/api/returns/{r2['id']}").get_json()
    assert fetched2["stock_posting_eligible"] is True


def test_api_exposes_stock_posting_flags_on_list_returns(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["normal_customer"]["id"], WEDNESDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    listed = client.get("/api/returns?limit=200").get_json()["results"]
    row = next(x for x in listed if x["id"] == r["id"])
    assert row["is_metro_sales_return"] is False
    assert row["stock_posting_eligible"] is True


def test_returns_html_badge_derives_from_backend_flags_only():
    idx = RETURNS_HTML.index("function metroStockBadge(r){")
    end = RETURNS_HTML.index("\n}\n", idx)
    body = RETURNS_HTML[idx:end]
    assert "r.is_metro_sales_return" in body
    assert "r.stock_posting_eligible" in body
    # Never independently computing a weekday/Monday check in JS.
    assert "getDay" not in body
    assert "weekday" not in body.lower()


# =====================================================================
# SECTION 28 — REGRESSION SAFETY (targeted spot checks; full breadth
# covered by the complete project suite run alongside this file)
# =====================================================================

def test_normal_return_signer_default_unaffected(client, setup, login_as):
    login_as("metro_signer_op", "password123", "operator")
    pid = setup["product"]["id"]
    res = _create_return(client, pid, setup["normal_customer"]["id"], WEDNESDAY, cartons=1)
    assert res.get_json()["signed_by_name"] == "metro_signer_op"


def test_dispatch_unaffected_by_metro_returns_rule(client, setup, super_admin):
    pid = setup["product"]["id"]
    d = client.post("/api/dispatches", json={
        "dispatch_number": "METRO-DISP-1", "date": WEDNESDAY, "customer_id": setup["normal_customer"]["id"],
        "sales_category_id": setup["normal_category"]["id"],
        "lines": [{"product_id": pid, "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200
    row = client.get(f"/api/daily-figures/{pid}?date={WEDNESDAY}&shift=Day").get_json()
    assert row["issued"]["base_qty"] == 200


def test_production_unaffected_by_metro_returns_rule(client, setup, super_admin):
    pid = setup["product"]["id"]
    p = client.post("/api/production", json={
        "date": WEDNESDAY, "shift": "Day", "lines": [{"product_id": pid, "cartons": 3, "packs": 0, "pieces": 0}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200
    row = client.get(f"/api/daily-figures/{pid}?date={WEDNESDAY}&shift=Day").get_json()
    assert row["production"]["base_qty"] == 300


def test_dashboard_activity_count_still_counts_the_metro_return_record(client, setup, super_admin):
    # "How many Return records were finalized today" is a record-activity
    # count, not a stock-posting total — a non-Monday Metro return still
    # counts here (see webapp/services/dashboard_service.py's
    # _activity_counts(), deliberately left untouched by this round).
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=1).get_json()
    _finalize(client, r["id"])
    data = client.get(f"/api/dashboard?date={WEDNESDAY}").get_json()
    assert data["activity"]["returns"]["finalized"] >= 1


def test_dashboard_stock_figures_exclude_non_monday_metro_return(client, setup, super_admin):
    pid = setup["product"]["id"]
    _set_opening(client, pid, WEDNESDAY, 10)
    r = _create_return(client, pid, setup["dakar"]["id"], WEDNESDAY, cartons=5).get_json()
    _finalize(client, r["id"])
    data = client.get(f"/api/dashboard?date={WEDNESDAY}").get_json()
    row = next(x for x in data["daily_figures_today"] if x["product_id"] == pid)
    assert row["return_base_qty"] == 0
    assert row["closing_base_qty"] == 1000


def test_packaging_rules_unaffected(client, setup):
    pid = setup["product"]["id"]
    r = _create_return(client, pid, setup["dakar"]["id"], MONDAY, cartons=2).get_json()
    _finalize(client, r["id"])
    # 2 cartons x (10 packs/carton x 10 pieces/pack) = 200 base units, exact
    # integer — packaging math itself is completely untouched by this round.
    assert _return_base_qty(client, pid, MONDAY) == 200
