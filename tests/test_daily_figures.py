import pytest


@pytest.fixture
def setup(client, login_as):
    """super_admin session, one Group-A product, one customer."""
    login_as("root", "password123", "super_admin")
    product = client.post("/api/admin/products", json={"name": "Compact Corporate Test"}).get_json()
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json={
        "cartons_to_packs": 10, "packs_to_pieces": 10,
    })
    category = client.post("/api/admin/sales-categories", json={"name": "Test Sales Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Dalca", "sales_category_id": category["id"]}).get_json()
    return {"product": product, "customer": customer, "category": category}


def _finalize_dispatch(client, product_id, customer_id, date, shift, cartons, packs, pieces, number):
    created = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "shift": shift, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    client.post(f"/api/dispatches/{created['id']}/finalize")
    return created


def _finalize_return(client, product_id, date, cartons, packs, pieces):
    """As of Stage 5, Return is sourced from the finalized Returns Book, not
    from the daily-figures POST — see webapp/services/stock_service.py's
    return_base_qty()."""
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    created = client.post("/api/returns", json={
        "date": date, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    return created


def _finalize_production(client, product_id, date, shift, cartons, packs, pieces):
    """As of Stage 5, Production is sourced from the finalized Production
    Book, not from the daily-figures POST — see
    webapp/services/stock_service.py's production_base_qty()."""
    created = client.post("/api/production", json={
        "date": date, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    return created


def test_first_entry_requires_opening(client, setup):
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 1, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 400


def test_issued_derives_from_finalized_dispatch(client, setup):
    _finalize_dispatch(client, setup["product"]["id"], setup["customer"]["id"],
                        "2026-07-28", "Day", 2, 3, 4, "DF-1")

    view = client.get(f"/api/daily-figures/{setup['product']['id']}?date=2026-07-28&shift=Day").get_json()
    assert view["issued"]["base_qty"] == 234
    assert (view["issued"]["cartons"], view["issued"]["packs"], view["issued"]["pieces"]) == (2, 3, 4)


def test_draft_dispatch_does_not_count_as_issued(client, setup):
    client.post("/api/dispatches", json={
        "dispatch_number": "DF-2", "date": "2026-07-28", "shift": "Day",
        "customer_id": setup["customer"]["id"],
        "lines": [{"product_id": setup["product"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    })
    view = client.get(f"/api/daily-figures/{setup['product']['id']}?date=2026-07-28&shift=Day").get_json()
    assert view["issued"]["base_qty"] == 0


def test_voided_dispatch_does_not_count_as_issued(client, setup):
    created = _finalize_dispatch(client, setup["product"]["id"], setup["customer"]["id"],
                                  "2026-07-28", "Day", 1, 0, 0, "DF-3")
    client.post(f"/api/dispatches/{created['id']}/void", json={"reason": "mistake"})
    view = client.get(f"/api/daily-figures/{setup['product']['id']}?date=2026-07-28&shift=Day").get_json()
    assert view["issued"]["base_qty"] == 0


def test_opening_carries_forward_from_prior_closing(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-27", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    # closing day1 = 1000 pieces (no issued yet)
    day2_view_before_save = client.get(f"/api/daily-figures/{pid}?date=2026-07-28&shift=Day").get_json()
    assert day2_view_before_save["opening_editable"] is False
    assert day2_view_before_save["opening"]["base_qty"] == 1000


def test_second_save_same_day_does_not_require_opening_again(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    # re-save without opening (frontend still always sends it, but the
    # service must not silently wipe an already-locked-in opening either)
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
        "return_": {"cartons": 1, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["base_qty"] == 500


def test_closing_formula(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},   # 1000
    })
    _finalize_return(client, pid, "2026-07-28", 0, 5, 0)                                            # 50
    _finalize_production(client, pid, "2026-07-28", "Day", 1, 0, 0)                                 # 100
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-28", "Day", 0, 2, 0, "DF-4")  # 20 issued

    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-28&shift=Day").get_json()
    assert view["closing"]["base_qty"] == 1000 + 50 + 100 - 20


def test_issued_detail_lists_contributing_dispatch(client, setup):
    pid = setup["product"]["id"]
    created = _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-28", "Day", 1, 0, 0, "DF-5")
    detail = client.get(f"/api/daily-figures/issued-detail?product_id={pid}&date=2026-07-28&shift=Day").get_json()
    assert detail["total_from_dispatches"] == 100
    assert detail["dispatches"][0]["dispatch_number"] == "DF-5"
    assert detail["dispatches"][0]["customer_name"] == "Dalca"


def test_manual_adjustment_affects_issued_and_is_audited(client, setup, app):
    pid = setup["product"]["id"]
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 15, "reason": "Known dispatch never logged digitally",
    })
    assert res.status_code == 201

    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-28&shift=Day").get_json()
    assert view["issued"]["base_qty"] == 15
    assert view["issued"]["from_adjustments"] == 15

    from webapp.models.audit_log import AuditLog
    with app.app_context():
        entry = AuditLog.query.filter_by(action="create", entity_type="stock_adjustment").first()
        assert entry is not None


def test_adjustment_requires_reason(client, setup):
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "",
    })
    assert res.status_code == 400


def test_operator_cannot_create_adjustment(client, setup, login_as):
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.post("/api/daily-figures/adjustments", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 5, "reason": "test",
    })
    assert res.status_code == 403


def test_operator_cannot_upsert_daily_figure_by_default(client, setup, login_as):
    """Stage 1: Operator Daily-Figures editing defaults to OFF (role-wide
    permission flags, all False until a Super Administrator enables one) —
    see tests/test_stage1_roles_navigation.py for the full per-field matrix."""
    client.post("/api/logout")
    login_as("op1", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


def test_viewer_cannot_upsert_daily_figure(client, setup, login_as):
    client.post("/api/logout")
    login_as("viewer1", "password123", "viewer")
    res = client.post("/api/daily-figures", json={
        "product_id": setup["product"]["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 403


def test_negative_closing_flagged_not_crashed(client, setup):
    """Final legacy-migration investigation, section 9/13 — a negative
    Closing is never clamped, never replaced by a warning dict, and is
    fully expressible in signed book notation even for a sub-carton
    magnitude (-50 base units is less than one whole carton on this
    product's 10x10 rule, so the sign falls onto `packs`, not `cartons` —
    see stock_service._split_or_none())."""
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures/adjustments", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "delta_base_qty": 50, "reason": "force negative closing for test",
    })
    view = client.get(f"/api/daily-figures/{pid}?date=2026-07-28&shift=Day").get_json()
    assert view["closing"]["base_qty"] == -50
    assert "warning" not in view["closing"]
    assert view["closing"]["cartons"] == 0
    assert view["closing"]["packs"] == -5
    assert view["closing"]["pieces"] == 0
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["closing"]["cartons"], view["closing"]["packs"], view["closing"]["pieces"], view["packaging_rule"]) == "-0.50 Ctns"


def test_history_lists_recent_figures(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    res = client.get("/api/daily-figures/history")
    rows = res.get_json()
    assert any(r["product_id"] == pid and r["date"] == "2026-07-28" for r in rows)


def test_export_csv_returns_csv(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    res = client.get("/api/daily-figures/export.csv")
    assert res.status_code == 200
    assert b"Compact Corporate Test" in res.data


def test_product_without_packaging_rule_rejected(client, setup):
    unconfigured = client.post("/api/admin/products", json={"name": "Unconfigured Product"}).get_json()
    res = client.post("/api/daily-figures", json={
        "product_id": unconfigured["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 1, "packs": 0, "pieces": 0},
        "return_": {"cartons": 0, "packs": 0, "pieces": 0},
        "production": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 400
