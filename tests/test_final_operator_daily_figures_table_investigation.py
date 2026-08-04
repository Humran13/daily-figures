"""
Operator Daily Figures — simple read-only activity table.

Replaces the Operator's per-product Next/Skip/Review card flow with one
compact, read-only table showing only products with genuine activity for
the selected Date + Shift. Server-side role branching (static/index.html's
renderCurrentView(), driven by the session-authoritative role from
GET /api/session — never a client-forgeable value) selects
renderOperatorTable() instead of renderEntryCard() for the Operator role
only; Manager/Super Administrator/Viewer are completely unaffected.

The table is backed by ONE new batched, read-only endpoint,
GET /api/daily-figures/operator-summary, which reuses
stock_service.daily_figure_view() per product (the exact same function
every other stock surface in this app already calls) — never a second,
independently-reconstructed calculation, and never itself a write path
(GET-only; the only way to change Opening Stock remains POST
/api/daily-figures, unchanged and still fully role/permission-gated
there).
"""
import pathlib

import pytest

INDEX_HTML = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, rule=None):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule or {"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _finalize_production(client, pid, date_str, shift, cartons):
    prod = client.post("/api/production", json={
        "date": date_str, "shift": shift, "lines": [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    return prod


def _summary(client, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/operator-summary?date={date_str}&shift={shift}").get_json()


# =====================================================================
# FRONTEND MARKUP / ROUTING (string-based, matching this project's
# established convention for static-file content checks)
# =====================================================================

def test_page_title_is_daily_figures_not_per_product():
    assert "<h1>Daily Figures</h1>" in INDEX_HTML
    assert "Per-Product Daily Figures" not in INDEX_HTML
    assert "Per-product Daily Figures" not in INDEX_HTML


def test_operator_table_renderer_exists_and_never_creates_editable_inputs():
    idx = INDEX_HTML.index("async function renderOperatorTable(){")
    end = INDEX_HTML.index("\n// ---------- entry wizard ----------", idx)
    body = INDEX_HTML[idx:end]
    assert "<input" not in body
    assert "Next Product" not in body
    assert "Skip Product" not in body
    assert "Submit Daily Figures Review" not in body


def test_operator_table_uses_one_batched_endpoint_not_per_product_calls():
    idx = INDEX_HTML.index("async function renderOperatorTable(){")
    end = INDEX_HTML.index("\n// ---------- entry wizard ----------", idx)
    body = INDEX_HTML[idx:end]
    # Load-error correction — a raw fetch() (not the shared apiGet()
    # helper, which never surfaces a non-2xx status as an error) is used
    # exactly once, so a genuine server error is distinguishable from an
    # empty-but-healthy 200 response.
    assert body.count("fetch(") == 1
    assert "/api/daily-figures/operator-summary" in body


def test_role_dispatch_is_server_authoritative_not_forged():
    idx = INDEX_HTML.index("function renderCurrentView(){")
    end = INDEX_HTML.index("\n// ---------- Operator Daily Figures redesign", idx)
    body = INDEX_HTML[idx:end]
    assert "currentUser.role === 'operator'" in body


def test_no_summary_cards_or_explanatory_note_rendered():
    # The table is always shown regardless of activity (see
    # test_operator_table_renderer_exists_and_never_creates_editable_inputs
    # and the ordering tests below) so no separate "preview" messaging is
    # needed — a plain read-only table only, no summary cards/note block.
    idx = INDEX_HTML.index("async function renderOperatorTable(){")
    end = INDEX_HTML.index("\nfunction _operatorTableHtml", idx)
    body = INDEX_HTML[idx:end]
    assert "op-summary" not in body
    assert "Products worked on" not in body
    assert "Production records" not in body
    assert "Return records" not in body
    assert "Dispatch records" not in body
    assert "No finalized activity has been recorded for this period." not in body
    assert "op-summary" not in INDEX_HTML


def test_manager_editing_interface_markup_unchanged():
    # renderEntryCard (the Manager/Super Admin/Operator-with-permission
    # editable card flow) still exists, still builds Next/Skip/Review
    # controls — never removed or replaced.
    assert "async function renderEntryCard(){" in INDEX_HTML
    assert "Submit Daily Figures Review" in INDEX_HTML or "reviewNextBtn" in INDEX_HTML


# =====================================================================
# BACKEND: FILTERING
# =====================================================================

def test_product_with_nonzero_production_appears(client, super_admin):
    p = _make_product(client, "Prod Activity Product")
    _finalize_production(client, p["id"], "2026-08-01", "Day", 3)
    data = _summary(client, "2026-08-01")
    assert p["id"] in [r["product_id"] for r in data["products"]]


def test_product_with_nonzero_returns_appears(client, super_admin):
    p = _make_product(client, "Returns Activity Product")
    cat = client.post("/api/admin/sales-categories", json={"name": "Op Cat"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Op Cust", "sales_category_id": cat["id"]}).get_json()
    ret = client.post("/api/returns", json={
        "date": "2026-08-01", "customer_id": cust["id"], "lines": [{"product_id": p["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{ret['id']}/finalize")
    data = _summary(client, "2026-08-01")
    assert p["id"] in [r["product_id"] for r in data["products"]]


def test_product_with_nonzero_issued_appears(client, super_admin):
    p = _make_product(client, "Issued Activity Product")
    cat = client.post("/api/admin/sales-categories", json={"name": "Op Cat 2"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Op Cust 2", "sales_category_id": cat["id"]}).get_json()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "OP-1", "date": "2026-08-01", "shift": "Day", "customer_id": cust["id"],
        "lines": [{"product_id": p["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    data = _summary(client, "2026-08-01")
    assert p["id"] in [r["product_id"] for r in data["products"]]


def _trigger_activity(client, date_str, shift):
    """Creates a throwaway product with real finalized Production on the
    given period — forces the endpoint into "activity" mode, so the
    target (non-qualifying) product's position AFTER it can be tested
    meaningfully."""
    trigger = _make_product(client, f"Activity Trigger {date_str} {shift}")
    _finalize_production(client, trigger["id"], date_str, shift, 1)
    return trigger


def _index_of(data, product_id):
    return [r["product_id"] for r in data["products"]].index(product_id)


def test_product_with_all_movements_zero_still_appears_but_after_worked_on(client, super_admin):
    """Display correction — a product is never filtered out for having
    zero movement; it is still listed with its real (zero) figures, just
    ordered after any genuinely worked-on product."""
    p = _make_product(client, "Zero Movement Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    trigger = _trigger_activity(client, "2026-08-01", "Day")
    data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"
    assert p["id"] in [r["product_id"] for r in data["products"]]
    assert _index_of(data, trigger["id"]) < _index_of(data, p["id"])
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["production"]["base_qty"] == 0  # real value, never invented, never nulled


def test_passive_opening_closing_only_product_still_appears_after_worked_on(client, super_admin):
    p = _make_product(client, "Passive Carry Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 40, "packs": 0, "pieces": 0},
    })
    trigger = _trigger_activity(client, "2026-08-15", "Day")
    data = _summary(client, "2026-08-15")  # a later date — carries 40 cartons forward, no movement of its own
    assert data["mode"] == "activity"
    assert p["id"] in [r["product_id"] for r in data["products"]]
    assert _index_of(data, trigger["id"]) < _index_of(data, p["id"])
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["opening"]["base_qty"] == 4000  # Opening Stock alone never makes it "worked on"


def test_no_activity_only_product_still_appears_after_worked_on(client, super_admin, login_as):
    p = _make_product(client, "No Activity Only Product")
    trigger = _trigger_activity(client, "2026-08-01", "Day")
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_no_activity", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={"product_id": p["id"], "date": "2026-08-01", "shift": "Day"})
    data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"
    assert p["id"] in [r["product_id"] for r in data["products"]]
    assert _index_of(data, trigger["id"]) < _index_of(data, p["id"])


def test_draft_only_production_still_appears_after_worked_on(client, super_admin):
    p = _make_product(client, "Draft Only Product")
    client.post("/api/production", json={
        "date": "2026-08-01", "shift": "Day", "lines": [{"product_id": p["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })  # never finalized
    trigger = _trigger_activity(client, "2026-08-01", "Day")
    data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"
    assert p["id"] in [r["product_id"] for r in data["products"]]
    assert _index_of(data, trigger["id"]) < _index_of(data, p["id"])
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["production"]["base_qty"] == 0  # the draft never counts toward the real figure


def test_voided_only_dispatch_still_appears_after_worked_on(client, super_admin):
    p = _make_product(client, "Voided Only Product")
    cat = client.post("/api/admin/sales-categories", json={"name": "Op Cat 3"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Op Cust 3", "sales_category_id": cat["id"]}).get_json()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "OP-VOID-1", "date": "2026-08-01", "shift": "Day", "customer_id": cust["id"],
        "lines": [{"product_id": p["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "test void"})
    trigger = _trigger_activity(client, "2026-08-01", "Day")
    data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"
    assert p["id"] in [r["product_id"] for r in data["products"]]
    assert _index_of(data, trigger["id"]) < _index_of(data, p["id"])
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["issued"]["base_qty"] == 0  # the voided dispatch never counts


def test_multiple_qualifying_products_all_appear(client, super_admin):
    p1 = _make_product(client, "Multi Qualify 1")
    p2 = _make_product(client, "Multi Qualify 2")
    _finalize_production(client, p1["id"], "2026-08-01", "Day", 1)
    _finalize_production(client, p2["id"], "2026-08-01", "Day", 2)
    data = _summary(client, "2026-08-01")
    ids = [r["product_id"] for r in data["products"]]
    assert p1["id"] in ids and p2["id"] in ids


def test_product_order_is_deterministic(client, super_admin):
    p1 = _make_product(client, "Order Product A")
    p2 = _make_product(client, "Order Product B")
    _finalize_production(client, p1["id"], "2026-08-01", "Day", 1)
    _finalize_production(client, p2["id"], "2026-08-01", "Day", 1)
    first = [r["product_id"] for r in _summary(client, "2026-08-01")["products"]]
    second = [r["product_id"] for r in _summary(client, "2026-08-01")["products"]]
    assert first == second


# =====================================================================
# DATE AND SHIFT
# =====================================================================

def test_day_table_uses_day_activity(client, super_admin):
    p = _make_product(client, "Day Table Product")
    _finalize_production(client, p["id"], "2026-08-01", "Day", 5)
    data = _summary(client, "2026-08-01", "Day")
    assert p["id"] in [r["product_id"] for r in data["products"]]


def test_night_table_uses_night_production(client, super_admin):
    p = _make_product(client, "Night Table Product")
    _finalize_production(client, p["id"], "2026-08-01", "Night", 5)
    data = _summary(client, "2026-08-01", "Night")
    assert data["mode"] == "activity"
    assert p["id"] in [r["product_id"] for r in data["products"]]
    night_row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert night_row["production"]["base_qty"] == 500
    # Day has no activity of its own — correctly falls back to preview
    # mode; the product still appears (never filtered), with its real
    # (zero) Day figures — Night's production never leaks onto Day.
    day_data = _summary(client, "2026-08-01", "Day")
    assert day_data["mode"] == "preview"
    day_row = next(r for r in day_data["products"] if r["product_id"] == p["id"])
    assert day_row["production"]["base_qty"] == 0


def test_day_only_returns_do_not_leak_into_night_figures(client, super_admin):
    p = _make_product(client, "Returns Day Only Product")
    cat = client.post("/api/admin/sales-categories", json={"name": "Op Cat 4"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Op Cust 4", "sales_category_id": cat["id"]}).get_json()
    ret = client.post("/api/returns", json={
        "date": "2026-08-01", "customer_id": cust["id"], "lines": [{"product_id": p["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{ret['id']}/finalize")
    _trigger_activity(client, "2026-08-01", "Night")
    night_data = _summary(client, "2026-08-01", "Night")
    assert night_data["mode"] == "activity"
    row = next(r for r in night_data["products"] if r["product_id"] == p["id"])
    assert row["return_"]["base_qty"] == 0  # the Day-only return never counts toward Night


def test_day_only_dispatch_does_not_leak_into_night_figures(client, super_admin):
    p = _make_product(client, "Dispatch Day Only Product")
    cat = client.post("/api/admin/sales-categories", json={"name": "Op Cat 5"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Op Cust 5", "sales_category_id": cat["id"]}).get_json()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "OP-NIGHT-1", "date": "2026-08-01", "shift": "Day", "customer_id": cust["id"],
        "lines": [{"product_id": p["id"], "cartons": 2, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    _trigger_activity(client, "2026-08-01", "Night")
    night_data = _summary(client, "2026-08-01", "Night")
    assert night_data["mode"] == "activity"
    row = next(r for r in night_data["products"] if r["product_id"] == p["id"])
    assert row["issued"]["base_qty"] == 0  # the Day-only dispatch never counts toward Night


def test_selected_date_is_respected(client, super_admin):
    p = _make_product(client, "Date Respect Product")
    _finalize_production(client, p["id"], "2026-08-01", "Day", 5)
    day1 = _summary(client, "2026-08-01")
    assert day1["mode"] == "activity"
    assert p["id"] in [r["product_id"] for r in day1["products"]]

    day2 = _summary(client, "2026-08-02")
    assert day2["mode"] == "preview"  # no activity on 08-02 anywhere
    day2_row = next(r for r in day2["products"] if r["product_id"] == p["id"])
    assert day2_row["production"]["base_qty"] == 0  # 08-01's production never leaks onto 08-02


def test_activity_from_another_date_does_not_count_toward_this_date(client, super_admin):
    p = _make_product(client, "Other Date Product")
    _finalize_production(client, p["id"], "2026-07-01", "Day", 5)
    trigger = _trigger_activity(client, "2026-08-01", "Day")
    data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"
    assert p["id"] in [r["product_id"] for r in data["products"]]
    assert _index_of(data, trigger["id"]) < _index_of(data, p["id"])  # not itself "worked on" on 08-01
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["production"]["base_qty"] == 0


# =====================================================================
# QUANTITIES
# =====================================================================

def test_quantities_are_correct_and_formula_reconciles(client, super_admin):
    p = _make_product(client, "Quantities Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 3)
    row = next(r for r in _summary(client, "2026-08-01")["products"] if r["product_id"] == p["id"])
    assert row["opening"]["base_qty"] == 1000
    assert row["production"]["base_qty"] == 300
    assert row["return_"]["base_qty"] == 0
    assert row["issued"]["base_qty"] == 0
    assert row["closing"]["base_qty"] == 1300
    assert row["closing"]["base_qty"] == row["opening"]["base_qty"] + row["production"]["base_qty"] + row["return_"]["base_qty"] - row["issued"]["base_qty"]


def test_negative_closing_notation_is_correct(client, super_admin):
    p = _make_product(client, "Negative Closing Op Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day", "delta_base_qty": 1000, "reason": "over-issue",
    })
    row = next(r for r in _summary(client, "2026-08-01")["products"] if r["product_id"] == p["id"])
    assert row["closing"]["base_qty"] == -500
    from webapp.services.quantity_format import qty_label
    label = qty_label(row["closing"]["cartons"], row["closing"]["packs"], row["closing"]["pieces"], row["packaging_rule"])
    assert label == "-5.00 Ctns"
    assert "-500" not in label


def test_no_float_arithmetic_in_operator_summary(client, super_admin):
    p = _make_product(client, "No Float Op Product")
    _finalize_production(client, p["id"], "2026-08-01", "Day", 7)
    row = next(r for r in _summary(client, "2026-08-01")["products"] if r["product_id"] == p["id"])
    for part in ("opening", "production", "return_", "issued", "closing"):
        for key in ("base_qty", "cartons", "packs", "pieces"):
            assert isinstance(row[part][key], int)


# =====================================================================
# PACKAGING
# =====================================================================

def test_napkins_corporate_uses_6_packs_per_carton(client, super_admin):
    p = _make_product(client, "Napkins Corporate", rule={"cartons_to_packs": 6, "packs_to_pieces": 10})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 1)
    row = next(r for r in _summary(client, "2026-08-01")["products"] if r["product_id"] == p["id"])
    assert row["packaging_rule"]["cartons_to_packs"] == 6
    assert row["packaging_rule"]["packs_to_pieces"] == 10


def test_no_pack_tier_product_notation_correct(client, super_admin):
    p = _make_product(client, "KingMax Style Product", rule={"carton_to_pieces": 60})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 2, "packs": 0, "pieces": 3},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 1)
    row = next(r for r in _summary(client, "2026-08-01")["products"] if r["product_id"] == p["id"])
    from webapp.services.quantity_format import qty_label
    label = qty_label(row["closing"]["cartons"], row["closing"]["packs"], row["closing"]["pieces"], row["packaging_rule"])
    assert label == "3.03 Ctns"


# =====================================================================
# ROLE REGRESSION
# =====================================================================

def test_operator_summary_endpoint_is_get_only(client, super_admin):
    res = client.post("/api/daily-figures/operator-summary", json={"date": "2026-08-01", "shift": "Day"})
    assert res.status_code == 405


def test_unauthenticated_request_is_rejected(client):
    res = client.get("/api/daily-figures/operator-summary?date=2026-08-01&shift=Day")
    assert res.status_code in (401, 403)


def test_operator_can_read_the_summary_endpoint(client, super_admin, login_as):
    p = _make_product(client, "Operator Read Product")
    _finalize_production(client, p["id"], "2026-08-01", "Day", 2)
    login_as("op_summary_read", "password123", "operator")
    res = client.get("/api/daily-figures/operator-summary?date=2026-08-01&shift=Day")
    assert res.status_code == 200
    assert p["id"] in [r["product_id"] for r in res.get_json()["products"]]


# =====================================================================
# CROSS-SURFACE CONSISTENCY
# =====================================================================

def test_operator_summary_equals_dashboard(client, super_admin):
    p = _make_product(client, "Cross Surface Op Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 8, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 2)

    op_row = next(r for r in _summary(client, "2026-08-01")["products"] if r["product_id"] == p["id"])
    dashboard = client.get("/api/dashboard?date=2026-08-01").get_json()
    dash_row = next(r for r in dashboard["stock_summary"] if r["product_id"] == p["id"])

    assert op_row["opening"]["base_qty"] == dash_row["opening_base_qty"]
    assert op_row["production"]["base_qty"] == dash_row["production_base_qty"]
    assert op_row["closing"]["base_qty"] == dash_row["closing_base_qty"]


def test_operator_summary_equals_daily_figure_view(client, super_admin, app):
    p = _make_product(client, "Ledger Equals Op Product")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 8, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 2)

    op_row = next(r for r in _summary(client, "2026-08-01")["products"] if r["product_id"] == p["id"])
    with app.app_context():
        from webapp.models.product import Product
        from webapp.extensions import db as _db
        from webapp.services import stock_service as svc
        product = _db.session.get(Product, p["id"])
        view = svc.daily_figure_view(product, "2026-08-01", "Day")
    assert op_row["opening"]["base_qty"] == view["opening"]["base_qty"]
    assert op_row["closing"]["base_qty"] == view["closing"]["base_qty"]


# =====================================================================
# EMPTY STATE
# =====================================================================

def test_no_activity_is_not_treated_as_an_api_error(client, super_admin):
    p = _make_product(client, "No Activity Empty State Product")
    res = client.get("/api/daily-figures/operator-summary?date=2026-09-01&shift=Day")
    assert res.status_code == 200
    data = res.get_json()
    assert "error" not in data
    assert data["mode"] == "preview"
    assert data["products_worked_on"] == 0
    assert p["id"] in [r["product_id"] for r in data["products"]]


def test_zero_activity_products_appear_with_their_real_zero_figures(client, super_admin):
    """Display correction — with no activity anywhere, every product is
    still listed (preview mode, never an empty products:[] response),
    each with its real (genuinely zero) movement figures — never
    filtered out, never replaced with a placeholder."""
    ids = []
    for i in range(3):
        p = _make_product(client, f"Zero Activity Product {i}")
        client.post("/api/daily-figures", json={
            "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
            "opening": {"cartons": 10, "packs": 0, "pieces": 0},
        })
        ids.append(p["id"])
    data = _summary(client, "2026-08-01")
    assert data["mode"] == "preview"
    row_ids = [r["product_id"] for r in data["products"]]
    for pid in ids:
        assert pid in row_ids
    for r in data["products"]:
        if r["product_id"] in ids:
            assert r["production"]["base_qty"] == 0
            assert r["return_"]["base_qty"] == 0
            assert r["issued"]["base_qty"] == 0
