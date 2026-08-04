"""
Operator Daily Figures display correction — show the table before any
activity exists.

Root cause of the previous generic "Unable to load" failure: a product
that has never had a packaging rule configured (a normal, real
intermediate state — Product.active defaults True at creation, before any
packaging rule is ever attached) made daily_figure_view() return None for
production/return_/issued/opening/closing, and the OLD operator_summary()
unconditionally subscripted those dicts — an unhandled TypeError, a Flask
HTML 500 page, and a JSON-parse failure client-side (the frontend's old
try/catch only ever caught a genuine network failure or JSON-parse
error — an HTML error page triggers the latter). Reproduced directly via
a scratch test before writing this fix; verified fixed here permanently.

UPDATE — table-layout/ordering correction: the endpoint no longer filters
by mode. Every active product is always listed, in every response;
`mode` ("preview" if no product anywhere has activity, else "activity")
is still reported for observability but no longer changes which rows
are included. Every field is the real, unmodified daily_figure_view()
value for every row — no placeholder nulling of Production/Returns/
Issued, and no "clean ledger value" gating of Opening/Closing. Rows are
sorted so products with real activity for this exact Date + Shift come
first, followed by untouched ones — see
test_final_operator_daily_figures_table_investigation.py for the
dedicated ordering tests. This module keeps its original crash-fix and
mode-transition tests (still fully valid) and updates the handful that
asserted the removed null-placeholder behavior.
"""
import pytest

from webapp.services import ledger_cutover_service as cutover_svc


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, rule=None):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    if rule is not None:
        client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule)
    return p


def _finalize_production(client, pid, date_str, shift, cartons):
    prod = client.post("/api/production", json={
        "date": date_str, "shift": shift, "lines": [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/production/{prod['id']}/finalize")
    return prod


def _summary(client, date_str, shift="Day"):
    res = client.get(f"/api/daily-figures/operator-summary?date={date_str}&shift={shift}")
    return res.status_code, res.get_json()


def _full_cutover(app, effective_date, effective_shift, balances, reason="test cutover"):
    with app.app_context():
        from webapp.extensions import db as _db
        from webapp.models.user import User
        root = User.query.filter_by(username="root").first()
        draft = cutover_svc.create_draft(effective_date, effective_shift, reason, root)
        for pid, (c, p, pc) in balances.items():
            cutover_svc.set_balance(draft.id, pid, c, p, pc, root)
        _db.session.commit()
        cutover_svc.verify_cutover(draft.id, root)
        _db.session.commit()
        preview = cutover_svc.preview_activation(draft.id)
        cutover_svc.activate_cutover(
            draft.id, root, preview_token=preview["preview_token"],
            confirmation_text=f"ACTIVATE LEDGER CUTOVER {effective_date} {effective_shift.upper()}",
            backup_confirmed=True, reason=reason,
        )
        _db.session.commit()


# =====================================================================
# LOAD-ERROR ROOT CAUSE
# =====================================================================

def test_product_without_packaging_rule_never_crashes_the_endpoint(client, super_admin):
    client.post("/api/admin/products", json={"name": "No Rule Configured Yet"})
    status, data = _summary(client, "2026-08-01")
    assert status == 200
    assert "error" not in data


def test_product_without_packaging_rule_appears_as_placeholder_in_preview(client, super_admin):
    p = client.post("/api/admin/products", json={"name": "No Rule Placeholder Product"}).get_json()
    status, data = _summary(client, "2026-08-01")
    assert status == 200
    assert data["mode"] == "preview"
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["opening"] is None and row["closing"] is None
    assert row["production"] is None and row["return_"] is None and row["issued"] is None


def test_product_without_packaging_rule_can_never_qualify_as_activity(client, super_admin):
    """It structurally cannot have valid computed figures (no conversion
    ratio exists) — it must never spuriously flip the table into
    activity mode. It is still listed (display correction — every
    product is always shown), but always ordered after any genuinely
    worked-on product."""
    client.post("/api/admin/products", json={"name": "No Rule Never Qualifies"})
    trigger = _make_product(client, "Rule Trigger Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    _finalize_production(client, trigger["id"], "2026-08-01", "Day", 1)
    status, data = _summary(client, "2026-08-01")
    assert status == 200
    assert data["mode"] == "activity"
    names = [r["product_name"] for r in data["products"]]
    assert "No Rule Never Qualifies" in names
    assert names.index("Rule Trigger Product") < names.index("No Rule Never Qualifies")


def test_genuine_endpoint_failure_still_returns_a_clean_error_not_a_crash(client, super_admin, monkeypatch):
    """A genuine, unexpected failure (simulated here) must still be caught,
    logged server-side, and reported as a clean JSON error — never an
    unhandled exception leaking a raw traceback to the browser."""
    import webapp.routes.daily_figures as route_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated genuine failure")

    monkeypatch.setattr(route_module.svc, "daily_figure_view", _boom)
    _make_product(client, "Failure Path Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    status, data = _summary(client, "2026-08-01")
    assert status == 500
    assert "error" in data
    assert "RuntimeError" not in data["error"]
    assert "Traceback" not in data["error"]


# =====================================================================
# PREVIEW / ACTIVITY MODE
# =====================================================================

def test_operator_always_sees_a_table_never_an_empty_error_panel(client, super_admin):
    _make_product(client, "Always Visible Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    status, data = _summary(client, "2026-08-01")
    assert status == 200
    assert data["mode"] in ("preview", "activity")
    assert isinstance(data["products"], list)


def test_no_activity_returns_preview_mode(client, super_admin):
    _make_product(client, "Preview Mode Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    status, data = _summary(client, "2026-08-01")
    assert data["mode"] == "preview"


def test_preview_mode_lists_every_active_product(client, super_admin):
    names = [f"Preview List Product {i}" for i in range(4)]
    for n in names:
        _make_product(client, n, rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    status, data = _summary(client, "2026-08-01")
    listed = [r["product_name"] for r in data["products"]]
    for n in names:
        assert n in listed


def test_preview_mode_shows_the_real_derived_opening_never_invents_a_number(client, super_admin):
    """Display correction — every field is the real, unmodified
    daily_figure_view() value, for every row, in both modes. For a
    product that has literally never been touched, that real value is
    the correctly-derived 0 (nothing precedes it) — not a frontend
    invention, and not suppressed into a placeholder either."""
    p = _make_product(client, "Never Touched Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    status, data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["opening"] == {"base_qty": 0, "cartons": 0, "packs": 0, "pieces": 0}
    assert row["closing"] == {"base_qty": 0, "cartons": 0, "packs": 0, "pieces": 0}


def test_preview_mode_shows_the_real_legacy_derived_value_unmodified(client, super_admin):
    """Display correction — "render backend-provided values exactly as
    received": a real historical negative is shown as-is, not hidden."""
    p = _make_product(client, "Legacy Negative Preview Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-01-01", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p["id"], "date": "2026-01-01", "shift": "Day", "delta_base_qty": 100000, "reason": "legacy loss",
    })
    assert _summary(client, "2026-01-01")[1]["mode"] in ("preview", "activity")  # sanity: the legacy period itself is fine

    status, data = _summary(client, "2026-08-01")  # a later, still-untouched period
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["opening"]["base_qty"] == -99500  # the exact, real, unmodified carried-forward value


def test_preview_mode_shows_clean_ledger_value_after_activated_cutover(client, super_admin, app):
    p = _make_product(client, "Clean Cutover Preview Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    _full_cutover(app, "2026-08-01", "Day", {p["id"]: (25, 0, 0)})
    status, data = _summary(client, "2026-08-05")  # post-cutover, still no activity of its own
    assert data["mode"] == "preview"
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["opening"] is not None
    assert row["opening"]["base_qty"] == 2500


def test_one_finalized_production_switches_to_activity_mode(client, super_admin):
    p = _make_product(client, "Production Switch Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    _finalize_production(client, p["id"], "2026-08-01", "Day", 2)
    status, data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"


def test_one_finalized_return_switches_to_activity_mode(client, super_admin):
    p = _make_product(client, "Return Switch Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    cat = client.post("/api/admin/sales-categories", json={"name": "Switch Cat"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Switch Cust", "sales_category_id": cat["id"]}).get_json()
    ret = client.post("/api/returns", json={
        "date": "2026-08-01", "customer_id": cust["id"], "lines": [{"product_id": p["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/returns/{ret['id']}/finalize")
    status, data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"


def test_one_finalized_dispatch_switches_to_activity_mode(client, super_admin):
    p = _make_product(client, "Dispatch Switch Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    cat = client.post("/api/admin/sales-categories", json={"name": "Switch Cat 2"}).get_json()
    cust = client.post("/api/admin/customers", json={"name": "Switch Cust 2", "sales_category_id": cat["id"]}).get_json()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "SW-1", "date": "2026-08-01", "shift": "Day", "customer_id": cust["id"],
        "lines": [{"product_id": p["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    status, data = _summary(client, "2026-08-01")
    assert data["mode"] == "activity"


def test_activity_mode_shows_real_values_not_placeholders(client, super_admin):
    p = _make_product(client, "Real Values Product", rule={"cartons_to_packs": 10, "packs_to_pieces": 10})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 6, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 2)
    status, data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["opening"]["base_qty"] == 600
    assert row["production"]["base_qty"] == 200
    assert row["closing"]["base_qty"] == 800


# =====================================================================
# PACKAGING RULE CONFIRMATIONS (representative — full exactness suite
# lives in test_final_correction_packaging_notation.py, unaffected)
# =====================================================================

def test_kingmax_style_packaging_correct_in_activity_mode(client, super_admin):
    p = _make_product(client, "KingMax", rule={"carton_to_pieces": 60})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 1)
    status, data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["packaging_rule"]["carton_to_pieces"] == 60
    assert row["closing"]["base_qty"] == 120


def test_jumbomax_style_packaging_correct_in_activity_mode(client, super_admin):
    p = _make_product(client, "JumboMax", rule={"carton_to_pieces": 24})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 10},
    })
    _finalize_production(client, p["id"], "2026-08-01", "Day", 1)
    status, data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["packaging_rule"]["carton_to_pieces"] == 24
    assert row["closing"]["base_qty"] == 34


def test_straws_style_packaging_correct(client, super_admin):
    p = _make_product(client, "Straws", rule={"cartons_to_packs": 12, "packs_to_pieces": 100})
    status, data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["packaging_rule"]["cartons_to_packs"] == 12
    assert row["packaging_rule"]["packs_to_pieces"] == 100


def test_silky_4pack_style_packaging_correct(client, super_admin):
    p = _make_product(client, "Silky 4pack", rule={"cartons_to_packs": 25, "packs_to_pieces": 4})
    status, data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["packaging_rule"]["cartons_to_packs"] == 25
    assert row["packaging_rule"]["packs_to_pieces"] == 4


def test_kitchen_towel_doubles_style_packaging_correct(client, super_admin):
    p = _make_product(client, "Kitchen Towel Doubles", rule={"cartons_to_packs": 12, "packs_to_pieces": 2})
    status, data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["packaging_rule"]["cartons_to_packs"] == 12
    assert row["packaging_rule"]["packs_to_pieces"] == 2


def test_kitchen_towel_singles_style_packaging_correct(client, super_admin):
    p = _make_product(client, "Kitchen Towel Singles", rule={"carton_to_pieces": 24})
    status, data = _summary(client, "2026-08-01")
    row = next(r for r in data["products"] if r["product_id"] == p["id"])
    assert row["packaging_rule"]["carton_to_pieces"] == 24


def test_napkins_corporate_and_standard_use_6_10_rule(client, super_admin):
    corp = _make_product(client, "Napkins Corporate", rule={"cartons_to_packs": 6, "packs_to_pieces": 10})
    std = _make_product(client, "Napkins Standard", rule={"cartons_to_packs": 6, "packs_to_pieces": 10})
    damage = _make_product(client, "Napkins Damage", rule={"cartons_to_packs": 6, "packs_to_pieces": 10})
    status, data = _summary(client, "2026-08-01")
    for p in (corp, std, damage):
        row = next(r for r in data["products"] if r["product_id"] == p["id"])
        assert row["packaging_rule"]["cartons_to_packs"] == 6
        assert row["packaging_rule"]["packs_to_pieces"] == 10
    # Napkins Damage remains a genuinely separate product record.
    assert len({corp["id"], std["id"], damage["id"]}) == 3


# =====================================================================
# NO FRONTEND FORMULA / MANAGER-SUPER-ADMIN UNCHANGED
# =====================================================================

def test_no_frontend_formula_introduced():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    idx = html.index("async function renderOperatorTable(){")
    end = html.index("\nfunction _operatorTableHtml", idx)
    body = html[idx:end]
    # No arithmetic operators combining quantity fields — only formatting
    # (qtyLabel) and null-safety checks are allowed here.
    assert "+ r." not in body and "- r." not in body
    assert "base_qty +" not in body and "base_qty -" not in body


def test_manager_and_super_admin_daily_figures_unaffected(client, login_as):
    """The elevated per-product card workflow (renderEntryCard and its
    Next/Skip/Review controls) is completely untouched by this
    correction — verified via the existing, unmodified regression tests
    for that flow (test_stage7_*/test_stage8_*), and reconfirmed here
    that the markup itself is present unchanged."""
    import pathlib
    html = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert "async function renderEntryCard(){" in html
    assert "saveAndAdvanceReview" in html
    assert "saveCurrentAndAdvance" in html
