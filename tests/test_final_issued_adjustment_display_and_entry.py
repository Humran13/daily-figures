"""
Targeted fix — Issued stock-adjustment display + safe packaging-aware
adjustment entry (final round following the Napkins Standard Issued
discrepancy investigation):

  - The Issued breakdown popup (static/index.html's openIssuedBreakdown())
    used to print a StockAdjustment's raw delta_base_qty directly
    ("+123") instead of running it through the same centralized
    packaging-aware formatter (fromBaseUnitsPreview()+qtyLabel()) every
    other quantity on the page already uses — for Napkins Standard
    (6 packs/carton, 10 pieces/pack), 123 base units is "+2.03 Ctns", not
    "+123 cartons". Fixed to reuse the same formatter, never a second
    implementation.
  - adjustIssued()'s raw prompt() ("Adjustment amount in pieces") is
    replaced with a packaging-aware modal: Increase/Reduce Issued type,
    Cartons/Packs/Pieces inputs (reusing the exact qtyInputsHtml()/
    readQtyInputs() helpers Opening Stock already uses), a required
    Preview/confirm step, then save.
  - Manager/Super Admin can now permanently delete an erroneous
    StockAdjustment (new DELETE /api/daily-figures/adjustments/<id>),
    mirroring the exact same permanent-delete pattern already used for
    Dispatch/Returns/Production.

Does not change the Issued formula (Dispatch + StockAdjustment, still
summed in exact integer base units) or the Napkins 6x10 packaging rule.
"""
import json
import pathlib

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, cartons_to_packs=6, packs_to_pieces=10):
    """Defaults to the Napkins Standard/Corporate rule (6x10) — the exact
    rule the investigation traced this bug through."""
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces,
    })
    return product


@pytest.fixture
def napkins(client, super_admin):
    return _make_product(client, "Napkins Standard Test", cartons_to_packs=6, packs_to_pieces=10)


def _daily_figure_view(client, product_id, date, shift="Day"):
    return client.get(f"/api/daily-figures/{product_id}?date={date}&shift={shift}").get_json()


def _issued_base(client, product_id, date, shift="Day"):
    return _daily_figure_view(client, product_id, date, shift)["issued"]["base_qty"]


# =====================================================================
# SECTION 10 — DISPLAY: adjustment rows use the centralized formatter
# =====================================================================

def _issued_breakdown_function_body():
    idx = INDEX_HTML.index("async function openIssuedBreakdown(product, date, shift){")
    end = INDEX_HTML.index("\n}\n", idx)
    return INDEX_HTML[idx:end]


def test_adjustment_row_no_longer_prints_raw_delta_base_qty():
    body = _issued_breakdown_function_body()
    # The exact old bug pattern must be gone.
    assert "${a.delta_base_qty>0?'+':''}${a.delta_base_qty}" not in body


def test_adjustment_row_uses_the_centralized_formatter():
    body = _issued_breakdown_function_body()
    assert "fromBaseUnitsPreview(" in body
    assert "qtyLabel(split" in body
    # Reuses the product's own packaging rule, exactly like the dispatch
    # rows immediately above it — never a second/independent conversion.
    assert "product.packaging_rule" in body


def test_adjustment_sign_is_preserved_separately_from_magnitude():
    body = _issued_breakdown_function_body()
    assert "a.delta_base_qty > 0 ? '+' : '-'" in body


def test_no_raw_base_quantity_labelled_ctns_in_adjustment_row():
    # qtyLabel() is the only thing allowed to emit the "Ctns" suffix — the
    # adjustment row's own template literal (not the surrounding comments)
    # must never hard-code "Ctns" itself; it must come from qtyLabel().
    idx = INDEX_HTML.index("const adjustmentRows = (detail.adjustments||[]).map(a => {")
    end = INDEX_HTML.index("}).join('');", idx)
    row_template = INDEX_HTML[idx:end]
    assert "Ctns" not in row_template


# =====================================================================
# SECTION 11 — ENTRY: packaging-aware adjustment form
# =====================================================================

def test_adjust_issued_no_longer_uses_raw_prompt():
    idx = INDEX_HTML.index("function adjustIssued(product, date, shift){")
    end = INDEX_HTML.index("\n}\n", idx)
    body = INDEX_HTML[idx:end]
    assert "prompt(" not in body
    assert "Adjustment amount in pieces" not in body


def test_adjust_form_reuses_shared_qty_inputs_helpers():
    # Same helpers Opening Stock already uses (qtyInputsHtml()/
    # readQtyInputs()) — never a second quantity-entry implementation.
    idx = INDEX_HTML.index("function adjustIssued(product, date, shift){")
    end = INDEX_HTML.index("\n}\n", idx)
    body = INDEX_HTML[idx:end]
    assert "qtyInputsHtml('adj'" in body


def test_adjust_type_select_offers_increase_and_reduce():
    idx = INDEX_HTML.index('id="adjustType"')
    end = INDEX_HTML.index("</select>", idx)
    body = INDEX_HTML[idx:end]
    assert 'value="increase"' in body
    assert 'value="reduce"' in body


def test_adjust_preview_confirm_panel_exists_and_requires_confirmation():
    assert 'id="adjustConfirmPanel"' in INDEX_HTML
    assert 'id="adjustConfirmSaveBtn"' in INDEX_HTML
    idx = INDEX_HTML.index("adjustPreviewBtn').addEventListener")
    end = INDEX_HTML.index("adjustConfirmBackBtn').addEventListener", idx)
    body = INDEX_HTML[idx:end]
    # Preview computes and displays base units without saving anything —
    # no api()/fetch call in the preview step itself.
    assert "apiPost(" not in body
    assert "adjustConfirmPanel').classList.remove('hidden')" in body


def test_adjust_confirm_save_sends_signed_base_units_not_raw_form_values():
    idx = INDEX_HTML.index("adjustConfirmSaveBtn').addEventListener")
    end = INDEX_HTML.index("\n});", idx)
    body = INDEX_HTML[idx:end]
    assert "delta_base_qty: signedBase" in body
    assert "apiPost('/api/daily-figures/adjustments'" in body


def test_adjust_pack_validation_rejects_out_of_range_packs():
    idx = INDEX_HTML.index("adjustPreviewBtn').addEventListener")
    end = INDEX_HTML.index("adjustConfirmBackBtn').addEventListener", idx)
    body = INDEX_HTML[idx:end]
    assert "qty.packs >= rule.cartons_to_packs" in body


def test_adjust_modal_never_asks_for_raw_base_units_directly():
    idx = INDEX_HTML.index('id="adjustFormPanel"')
    end = INDEX_HTML.index('id="adjustConfirmPanel"', idx)
    body = INDEX_HTML[idx:end]
    assert "base_qty" not in body
    assert "base unit" not in body.lower()


# =====================================================================
# SECTION 11 — ENTRY: exact packaging math (mirrors the JS toBaseUnits()
# via the backend's own to_base_units(), already the authoritative,
# already-tested implementation the frontend function mirrors exactly)
# =====================================================================

def test_napkins_2c_0p_3pc_converts_to_exactly_123_base_units(app):
    with app.app_context():
        from webapp.services.packaging import to_base_units
        from webapp.models.packaging_rule import PackagingRule
        rule = PackagingRule(cartons_to_packs=6, packs_to_pieces=10)
        assert to_base_units(2, 0, 3, rule) == 123


def test_napkins_123_cartons_converts_to_exactly_7380_base_units(app):
    with app.app_context():
        from webapp.services.packaging import to_base_units
        from webapp.models.packaging_rule import PackagingRule
        rule = PackagingRule(cartons_to_packs=6, packs_to_pieces=10)
        assert to_base_units(123, 0, 0, rule) == 7380


def test_123_base_units_and_123_cartons_never_produce_the_same_quantity(app):
    with app.app_context():
        from webapp.services.packaging import to_base_units
        from webapp.models.packaging_rule import PackagingRule
        rule = PackagingRule(cartons_to_packs=6, packs_to_pieces=10)
        as_base_units = 123
        as_cartons = to_base_units(123, 0, 0, rule)
        assert as_base_units != as_cartons


def test_napkins_pack_digit_maximum_is_five(client, napkins):
    products = client.get("/api/admin/products").get_json()
    rule = next(p for p in products if p["id"] == napkins["id"])["packaging_rule"]
    assert rule["cartons_to_packs"] == 6  # valid pack digits: 0 through 5


def test_create_adjustment_increase_stores_positive_delta(client, napkins):
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": napkins["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    })
    assert res.status_code == 201
    assert res.get_json()["delta_base_qty"] == 123
    assert _issued_base(client, napkins["id"], "2026-08-01") == 123


def test_create_adjustment_reduce_stores_negative_delta(client, napkins):
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": napkins["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": -123, "reason": "correction",
    })
    assert res.status_code == 201
    assert res.get_json()["delta_base_qty"] == -123
    assert _issued_base(client, napkins["id"], "2026-08-01") == -123


def test_no_float_conversion_anywhere_in_packaging_module():
    import inspect
    from webapp.services import packaging
    source = inspect.getsource(packaging)
    assert "float(" not in source
    assert "Decimal(" not in source


# =====================================================================
# SECTION 12 — DELETE / CORRECTION
# =====================================================================

def test_manager_can_delete_erroneous_adjustment(client, napkins, login_as):
    login_as("adj_del_mgr", "password123", "manager")
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": napkins["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    res = client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "entered in error — meant cartons, not base units", "confirm": True,
    })
    assert res.status_code == 200


def test_super_admin_can_delete_erroneous_adjustment(client, napkins, super_admin):
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": napkins["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    res = client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "entered in error", "confirm": True,
    })
    assert res.status_code == 200


def test_operator_cannot_delete_adjustment(client, napkins, super_admin, login_as):
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": napkins["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    login_as("adj_del_op", "password123", "operator")
    res = client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "x", "confirm": True,
    })
    assert res.status_code == 403


def test_delete_requires_reason(client, napkins, super_admin):
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": napkins["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    res = client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={"confirm": True})
    assert res.status_code == 400


def test_delete_requires_explicit_confirmation(client, napkins, super_admin):
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": napkins["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    res = client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={"reason": "x"})
    assert res.status_code == 400


def test_delete_audit_snapshot_survives(client, napkins, super_admin, app):
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": napkins["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "meant 2.03 Ctns not 123 base units", "confirm": True,
    })
    with app.app_context():
        from webapp.models.audit_log import AuditLog
        entry = AuditLog.query.filter_by(
            action="permanent_delete_stock_adjustment", entity_id=str(created["id"]),
        ).first()
        assert entry is not None
        before = json.loads(entry.before_json)
        assert before["operation"] == "permanent_delete_stock_adjustment"
        assert before["deletion_reason"] == "meant 2.03 Ctns not 123 base units"
        assert before["delta_base_qty"] == 123


def test_delete_removes_stock_effect_exactly_once(client, napkins, super_admin):
    pid = napkins["id"]
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    assert _issued_base(client, pid, "2026-08-01") == 123

    res = client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "erroneous", "confirm": True,
    })
    assert res.status_code == 200
    assert _issued_base(client, pid, "2026-08-01") == 0


def test_daily_figures_recalculates_after_adjustment_delete(client, napkins, super_admin):
    pid = napkins["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    view_before = _daily_figure_view(client, pid, "2026-08-01")
    # 10 cartons x 60 base units/carton (6x10) = 600; minus the erroneous 123 Issued.
    assert view_before["closing"]["base_qty"] == 600 - 123

    client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "erroneous", "confirm": True,
    })
    view_after = _daily_figure_view(client, pid, "2026-08-01")
    assert view_after["closing"]["base_qty"] == 600
    assert view_after["issued"]["base_qty"] == 0


def test_following_opening_closing_carries_correctly_after_delete(client, napkins, super_admin):
    pid = napkins["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "erroneous", "confirm": True,
    })
    day1_closing = _daily_figure_view(client, pid, "2026-08-01")["closing"]["base_qty"]
    day2_opening = _daily_figure_view(client, pid, "2026-08-02")["opening"]["base_qty"]
    assert day1_closing == day2_opening == 600


def test_repeated_delete_cannot_change_stock_twice(client, napkins, super_admin):
    pid = napkins["id"]
    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    first = client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "x", "confirm": True,
    })
    assert first.status_code == 200
    after_first = _issued_base(client, pid, "2026-08-01")
    second = client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={
        "reason": "x", "confirm": True,
    })
    assert second.status_code == 404
    after_second = _issued_base(client, pid, "2026-08-01")
    assert after_first == after_second == 0


# =====================================================================
# SECTION 13 — REGRESSION SAFETY (targeted spot checks; the complete
# project test suite is run alongside this file for full breadth)
# =====================================================================

def test_dispatch_issued_total_unaffected_by_adjustment_changes(client, napkins, super_admin):
    pid = napkins["id"]
    category = client.post("/api/admin/sales-categories", json={"name": "Adj Regression Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Adj Regression Customer", "sales_category_id": category["id"], "confirm_not_duplicate": True,
    }).get_json()
    d = client.post("/api/dispatches", json={
        "dispatch_number": "ADJ-REG-1", "date": "2026-08-01", "customer_id": customer["id"],
        "sales_category_id": category["id"],
        "lines": [{"product_id": pid, "cartons": 123, "packs": 0, "pieces": 0}],
    }).get_json()
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _issued_base(client, pid, "2026-08-01") == 7380  # 123 cartons x 60 — the correct, unaffected figure

    created = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    }).get_json()
    assert _issued_base(client, pid, "2026-08-01") == 7503  # 7380 + 123, matching the investigation's 125.03 Ctns

    client.delete(f"/api/daily-figures/adjustments/{created['id']}", json={"reason": "erroneous", "confirm": True})
    assert _issued_base(client, pid, "2026-08-01") == 7380  # back to the correct dispatch-only figure


def test_legitimate_adjustment_still_included_in_issued_total(client, napkins, super_admin):
    # Deleting a bad adjustment must not make the whole feature unusable —
    # a legitimate one still counts exactly as before.
    pid = napkins["id"]
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-08-05", "shift": "Day",
        "delta_base_qty": 60, "reason": "genuine stock correction — 1 carton found damaged in transit",
    })
    assert res.status_code == 201
    assert _issued_base(client, pid, "2026-08-05") == 60


def test_napkins_packaging_rule_unchanged(client, napkins):
    products = client.get("/api/admin/products").get_json()
    rule = next(p for p in products if p["id"] == napkins["id"])["packaging_rule"]
    assert rule["cartons_to_packs"] == 6
    assert rule["packs_to_pieces"] == 10


def test_kingmax_style_no_pack_tier_adjustment_still_works(client, super_admin):
    kingmax = client.post("/api/admin/products", json={"name": "KingMax Adj Test"}).get_json()
    client.post(f"/api/admin/products/{kingmax['id']}/packaging-rules", json={"carton_to_pieces": 60})
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": kingmax["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 65, "reason": "correction",
    })
    assert res.status_code == 201
    # 65 base units on a 60-per-carton rule = 1 carton, 5 loose pieces.
    view = _daily_figure_view(client, kingmax["id"], "2026-08-01")
    assert view["issued"]["cartons"] == 1
    assert view["issued"]["pieces"] == 5


def test_jumbomax_style_no_pack_tier_adjustment_still_works(client, super_admin):
    jumbomax = client.post("/api/admin/products", json={"name": "JumboMax Adj Test"}).get_json()
    client.post(f"/api/admin/products/{jumbomax['id']}/packaging-rules", json={"carton_to_pieces": 24})
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": jumbomax["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 25, "reason": "correction",
    })
    assert res.status_code == 201
    view = _daily_figure_view(client, jumbomax["id"], "2026-08-01")
    assert view["issued"]["cartons"] == 1
    assert view["issued"]["pieces"] == 1


def test_compact_10x10_adjustment_still_works(client, super_admin):
    compact = _make_product(client, "Compact Adj Test", cartons_to_packs=10, packs_to_pieces=10)
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": compact["id"], "date": "2026-08-01", "shift": "Day",
        "delta_base_qty": 123, "reason": "correction",
    })
    assert res.status_code == 201
    view = _daily_figure_view(client, compact["id"], "2026-08-01")
    # 123 base units on a 10x10 rule = 1 carton, 2 packs, 3 pieces (never
    # to be confused with Napkins' 6x10 split of the SAME 123 base units).
    assert view["issued"]["cartons"] == 1
    assert view["issued"]["packs"] == 2
    assert view["issued"]["pieces"] == 3
