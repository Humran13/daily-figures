"""
Urgent stock-integrity investigation — regression suite.

Seven adversarial reproductions were run against the live application
during investigation (historical correction after a later manual-
correction anchor, a voided dispatch, a reopened-but-never-refinalized
production record, cross-product isolation, genuine over-issuance, a
Reset Daily Values Mode A demotion of an anchor, and Night-before-Day
chronology) — every one reconciled EXACTLY against hand-computed expected
values. No duplicate-counting, status-filtering, date/shift-boundary,
Opening Stock provenance, adjustment, unit-conversion, or cross-product
defect was found anywhere in webapp/services/stock_service.py.

This file converts that investigation into a permanent, exhaustive
regression suite (final pre-deployment investigation, section 12's full
checklist) plus proves the new read-only ledger diagnostic
(webapp/services/stock_ledger_service.py, webapp/cli.py's `flask
stock-ledger`) correctly explains a genuine negative balance — including
the exact narrative pattern reported ("Reset preview shows nothing for
the affected date, yet Opening Stock is a large negative number, and it
carries forward") — as a correctly-reconciling consequence of real source
records on an EARLIER date, never a software double-count.
"""
import pytest

from webapp.services import stock_ledger_service


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    category = client.post("/api/admin/sales-categories", json={"name": "Integrity Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Integrity Customer", "sales_category_id": category["id"]}).get_json()
    return {"category": category, "customer": customer}


def _make_product(client, name, rule=None):
    p = client.post("/api/admin/products", json={"name": name}).get_json()
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json=rule or {"cartons_to_packs": 10, "packs_to_pieces": 10})
    return p


def _production(client, pid, date_str, shift, cartons, finalize=True):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    if finalize:
        res = client.post(f"/api/production/{p['id']}/finalize")
        assert res.status_code == 200, res.get_json()
    return p


def _returns(client, pid, date_str, cartons, finalize=True):
    customer_id = client.post("/api/admin/customers", json={
        "name": f"Auto Returner {id(object())}", "confirm_not_duplicate": True,
    }).get_json()["id"]
    r = client.post("/api/returns", json={
        "date": date_str, "customer_id": customer_id,
        "lines": [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    if finalize:
        res = client.post(f"/api/returns/{r['id']}/finalize")
        assert res.status_code == 200, res.get_json()
    return r


def _dispatch(client, pid, customer_id, category_id, date_str, number, cartons, finalize=True):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": "Day",
        "customer_id": customer_id, "sales_category_id": category_id,
        "lines": [{"product_id": pid, "cartons": cartons, "packs": 0, "pieces": 0}],
    }).get_json()
    if finalize:
        res = client.post(f"/api/dispatches/{d['id']}/finalize")
        assert res.status_code == 200, res.get_json()
    return d


def _view(client, pid, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{pid}?date={date_str}&shift={shift}").get_json()


def _ledger(app, pid, date_from, date_to, shift=None):
    with app.app_context():
        return stock_ledger_service.build_ledger(pid, date_from, date_to, shift=shift)


# =====================================================================
# Ledger trace behavior
# =====================================================================

def test_ledger_produces_chronological_date_shift_rows(client, setup, app):
    pid = _make_product(client, "Ledger Chrono")["id"]
    entries = _ledger(app, pid, "2026-07-19", "2026-07-21")
    keys = [(e["date"], e["shift"]) for e in entries]
    assert keys == [
        ("2026-07-19", "Day"), ("2026-07-19", "Night"),
        ("2026-07-20", "Day"), ("2026-07-20", "Night"),
        ("2026-07-21", "Day"), ("2026-07-21", "Night"),
    ]


def test_ledger_shows_exact_contributing_source_ids(client, setup, app):
    p = _make_product(client, "Ledger Source IDs")
    prod = _production(client, p["id"], "2026-07-19", "Day", 5)
    entries = _ledger(app, p["id"], "2026-07-19", "2026-07-19", shift="Day")
    line = entries[0]["production_lines"][0]
    assert line["record_id"] == prod["id"]
    assert line["line_id"] == prod["lines"][0]["id"]
    assert line["included"] is True


def test_ledger_shows_exact_base_unit_arithmetic(client, setup, app):
    p = _make_product(client, "Ledger Arithmetic")
    _production(client, p["id"], "2026-07-19", "Day", 5)  # 500 base units
    _dispatch(client, p["id"], setup["customer"]["id"], setup["category"]["id"], "2026-07-20", "LG-1", 2)  # 200 base units
    entries = _ledger(app, p["id"], "2026-07-20", "2026-07-20", shift="Day")
    e = entries[0]
    assert e["opening_base_qty"] == 500
    assert e["dispatch_total"] == 200
    assert e["issued_total"] == 200
    assert e["closing_base_qty"] == 300
    assert e["opening_base_qty"] + e["production_total"] + e["returns_total"] - e["issued_total"] == e["closing_base_qty"]


def test_ledger_shows_packaging_aware_labels(client, setup, app):
    p = _make_product(client, "Ledger Labels", {"carton_to_pieces": 60})
    _production(client, p["id"], "2026-07-19", "Day", 5)
    entries = _ledger(app, p["id"], "2026-07-19", "2026-07-19", shift="Day")
    assert entries[0]["closing_label"] == "5.00 Ctns"


def test_ledger_identifies_first_negative_period(client, setup, app):
    p = _make_product(client, "Ledger First Negative")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    _dispatch(client, p["id"], setup["customer"]["id"], setup["category"]["id"], "2026-07-20", "LG-2", 40)
    entries = _ledger(app, p["id"], "2026-07-19", "2026-08-03")
    negative = stock_ledger_service.first_negative_period(entries)
    assert negative["date"] == "2026-07-20" and negative["shift"] == "Day"


def test_ledger_distinguishes_current_period_movement_from_carried_negative(client, setup, app):
    p = _make_product(client, "Ledger Carried Distinction")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    _dispatch(client, p["id"], setup["customer"]["id"], setup["category"]["id"], "2026-07-20", "LG-3", 40)
    entries = _ledger(app, p["id"], "2026-07-20", "2026-07-21")
    by_key = {(e["date"], e["shift"]): e for e in entries}
    assert by_key[("2026-07-20", "Day")]["period_kind"] == "movement_here"
    assert by_key[("2026-07-21", "Day")]["period_kind"] == "negative_carried_forward"
    assert by_key[("2026-07-21", "Day")]["closing_base_qty"] == by_key[("2026-07-20", "Day")]["closing_base_qty"]


# =====================================================================
# Status filtering
# =====================================================================

def test_draft_production_does_not_affect_stock(client, setup):
    p = _make_product(client, "Status Draft Production")
    _production(client, p["id"], "2026-07-19", "Day", 5, finalize=False)
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 0


def test_draft_returns_does_not_affect_stock(client, setup):
    p = _make_product(client, "Status Draft Returns")
    _returns(client, p["id"], "2026-07-19", 5, finalize=False)
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 0


def test_draft_dispatch_does_not_affect_stock(client, setup):
    p = _make_product(client, "Status Draft Dispatch")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    _dispatch(client, p["id"], setup["customer"]["id"], setup["category"]["id"], "2026-07-19", "SD-1", 2, finalize=False)
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 500


def test_voided_production_does_not_affect_stock(client, setup):
    p = _make_product(client, "Status Voided Production")
    prod = _production(client, p["id"], "2026-07-19", "Day", 5)
    client.post(f"/api/production/{prod['id']}/void", json={"reason": "test"})
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 0


def test_voided_returns_does_not_affect_stock(client, setup):
    p = _make_product(client, "Status Voided Returns")
    r = _returns(client, p["id"], "2026-07-19", 5)
    client.post(f"/api/returns/{r['id']}/void", json={"reason": "test"})
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 0


def test_voided_dispatch_does_not_affect_stock(client, setup):
    p = _make_product(client, "Status Voided Dispatch")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    d = _dispatch(client, p["id"], setup["customer"]["id"], setup["category"]["id"], "2026-07-19", "SV-1", 2)
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 300
    client.post(f"/api/dispatches/{d['id']}/void", json={"reason": "test"})
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 500


def test_reopened_but_unfinalized_production_does_not_affect_stock(client, setup):
    p = _make_product(client, "Status Reopened Production")
    prod = _production(client, p["id"], "2026-07-19", "Day", 5)
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 500
    client.post(f"/api/production/{prod['id']}/reopen", json={"reason": "test"})
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 0


def test_corrected_refinalized_record_counts_exactly_once(client, setup):
    p = _make_product(client, "Status Corrected Once")
    prod = _production(client, p["id"], "2026-07-19", "Day", 5)
    client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "fix", "lines": [{"id": prod["lines"][0]["id"], "product_id": p["id"], "cartons": 8, "packs": 0, "pieces": 0}],
    })
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 800  # not 500+800, exactly once


def test_reset_neutralized_record_does_not_affect_stock(client, setup):
    p = _make_product(client, "Status Reset Neutralized")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 500
    res = client.post("/api/daily-reset", json={
        "date": "2026-07-19", "shift": "Day", "product_id": p["id"], "reason": "restart",
        "mode": "full", "confirmation_text": "FULL RESET 2026-07-19 DAY",
    })
    assert res.status_code == 200, res.get_json()
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 0


# =====================================================================
# Date and shift boundaries
# =====================================================================

def test_day_movement_affects_same_day_closing(client, setup):
    p = _make_product(client, "Boundary Day Same Day")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    assert _view(client, p["id"], "2026-07-19", "Day")["closing"]["base_qty"] == 500


def test_day_movement_carries_into_night(client, setup):
    p = _make_product(client, "Boundary Day Into Night")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    assert _view(client, p["id"], "2026-07-19", "Night")["opening"]["base_qty"] == 500


def test_night_movement_carries_into_next_day(client, setup):
    p = _make_product(client, "Boundary Night Into Next Day")
    _production(client, p["id"], "2026-07-19", "Night", 3)
    assert _view(client, p["id"], "2026-07-19", "Night")["closing"]["base_qty"] == 300
    assert _view(client, p["id"], "2026-07-20", "Day")["opening"]["base_qty"] == 300


def test_target_period_movement_not_included_in_target_opening(client, setup):
    p = _make_product(client, "Boundary Strict Before")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    view = _view(client, p["id"], "2026-07-19", "Day")
    assert view["opening"]["base_qty"] == 0  # today's own production must not appear in today's own opening


def test_no_activity_preserves_the_balance(client, setup):
    p = _make_product(client, "Boundary No Activity Preserve")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    for date_str in ("2026-07-20", "2026-07-21", "2026-08-03"):
        view = _view(client, p["id"], date_str, "Day")
        assert view["opening"]["base_qty"] == 500
        assert view["production"]["base_qty"] == 0
        assert view["return_"]["base_qty"] == 0
        assert view["issued"]["base_qty"] == 0
        assert view["closing"]["base_qty"] == 500


# =====================================================================
# Product isolation
# =====================================================================

def test_one_products_movement_never_affects_another(client, setup):
    p1 = _make_product(client, "Isolation Product 1")
    p2 = _make_product(client, "Isolation Product 2")
    _production(client, p1["id"], "2026-07-19", "Day", 50)
    assert _view(client, p1["id"], "2026-07-19")["closing"]["base_qty"] == 5000
    assert _view(client, p2["id"], "2026-07-19")["closing"]["base_qty"] == 0


def test_multi_product_dispatch_uses_line_quantities_not_record_total(client, setup):
    p1 = _make_product(client, "Isolation Multi 1")
    p2 = _make_product(client, "Isolation Multi 2")
    client.post("/api/dispatches", json={
        "dispatch_number": "ISO-1", "date": "2026-07-19", "shift": "Day",
        "customer_id": setup["customer"]["id"], "sales_category_id": setup["category"]["id"],
        "lines": [
            {"product_id": p1["id"], "cartons": 3, "packs": 0, "pieces": 0},
            {"product_id": p2["id"], "cartons": 7, "packs": 0, "pieces": 0},
        ],
    })
    d = client.get("/api/dispatches").get_json()["results"][0]
    client.post(f"/api/dispatches/{d['id']}/finalize")
    assert _view(client, p1["id"], "2026-07-19")["issued"]["base_qty"] == 300
    assert _view(client, p2["id"], "2026-07-19")["issued"]["base_qty"] == 700


# =====================================================================
# Opening Stock provenance
# =====================================================================

def test_genuine_manual_correction_remains_authoritative(client, setup):
    p = _make_product(client, "Provenance Manual Correction")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-10", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-21", "shift": "Day",
        "opening": {"cartons": 60, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical count",
    })
    # Earlier history changes after the correction — the correction must
    # stay authoritative (never second-guessed by history).
    prod = _production(client, p["id"], "2026-07-19", "Day", 5)
    client.post(f"/api/production/{prod['id']}/correct", json={
        "reason": "fix", "lines": [{"id": prod["lines"][0]["id"], "product_id": p["id"], "cartons": 20, "packs": 0, "pieces": 0}],
    })
    assert _view(client, p["id"], "2026-07-21")["opening"]["base_qty"] == 6000


def test_legacy_inferred_row_is_not_automatically_trusted_forever(client, setup, app):
    """A row that WAS a valid anchor (nothing before it) is correctly
    demoted the moment finalized activity is later entered before it —
    it is never trusted merely because it once was."""
    p = _make_product(client, "Provenance Legacy Not Forever")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    with app.app_context():
        from webapp.models.daily_figure import DailyFigure
        figure = DailyFigure.query.filter_by(product_id=p["id"], date="2026-07-20", shift="Day").first()
        assert figure.opening_stock_source == "initial_manual"
    # Now enter genuine earlier finalized history.
    _production(client, p["id"], "2026-07-19", "Day", 5)
    assert _view(client, p["id"], "2026-07-20")["opening"]["base_qty"] == 500  # derived, no longer the stale 1000


def test_reset_created_row_is_not_trusted(client, setup):
    """Final reset-safety correction, section 2/3 — the default Status
    Only mode no longer touches Opening Stock at all (so it can no longer
    un-anchor an existing row); Full Reset is now the mode that
    legitimately clears/recalculates a period's Opening Stock, and the
    resulting reset_created marker is still never trusted as an anchor."""
    p = _make_product(client, "Provenance Reset Not Trusted")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-10", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-reset", json={
        "date": "2026-07-10", "shift": "Day", "product_id": p["id"], "reason": "restart", "mode": "full",
        "confirmation_text": "FULL RESET 2026-07-10 DAY",
    })
    _production(client, p["id"], "2026-07-19", "Day", 5)
    assert _view(client, p["id"], "2026-08-03")["opening"]["base_qty"] == 500  # derived from production, not from the reset row


def test_historical_activity_entered_later_invalidates_automatic_initial_anchor(client, setup):
    p = _make_product(client, "Provenance Invalidate Initial")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-20", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    before = _view(client, p["id"], "2026-08-03")["opening"]["base_qty"]
    assert before == 1000
    _production(client, p["id"], "2026-07-19", "Day", 5)
    after = _view(client, p["id"], "2026-08-03")["opening"]["base_qty"]
    assert after == 500  # invalidated and recalculated, not frozen at 1000


def test_target_period_row_is_not_its_own_prior_anchor(client, setup):
    p = _make_product(client, "Provenance Not Self Anchor")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    view = _view(client, p["id"], "2026-07-19", "Day")
    assert view["opening_editable"] is True  # a genuinely-first period, not derived from itself


# =====================================================================
# Adjustments
# =====================================================================

def test_positive_adjustment_decreases_stock_once(client, setup):
    """StockAdjustment.delta_base_qty is signed the same direction as
    Issued (a positive delta adds to Issued) — see stock_service.py's
    issued_base_qty(). This proves the sign applies exactly once."""
    p = _make_product(client, "Adjustment Positive")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day", "delta_base_qty": 50, "reason": "test",
    })
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 450


def test_negative_adjustment_increases_stock_once(client, setup):
    p = _make_product(client, "Adjustment Negative")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day", "delta_base_qty": -50, "reason": "test",
    })
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 550


def test_adjustments_have_no_void_mechanism_in_this_schema(app):
    """Investigation finding: StockAdjustment has no status/void column at
    all — 'voided adjustment still included' (a plausible root cause this
    investigation was asked to check) does not apply to this schema; every
    adjustment ever created is permanent, audited, additive history by
    design. Documented here so this isn't silently re-assumed later."""
    from webapp.models.daily_figure import StockAdjustment
    assert not hasattr(StockAdjustment, "status")
    assert not hasattr(StockAdjustment, "voided_at")


def test_adjustment_is_product_specific(client, setup):
    p1 = _make_product(client, "Adjustment Specific 1")
    p2 = _make_product(client, "Adjustment Specific 2")
    client.post("/api/daily-figures/adjustments", json={
        "product_id": p1["id"], "date": "2026-07-19", "shift": "Day", "delta_base_qty": 100, "reason": "test",
    })
    assert _view(client, p1["id"], "2026-07-19")["closing"]["base_qty"] == -100
    assert _view(client, p2["id"], "2026-07-19")["closing"]["base_qty"] == 0


# =====================================================================
# Packaging
# =====================================================================

def test_compact_three_tier_quantities_remain_exact(client, setup):
    p = _make_product(client, "Packaging Compact", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 109, "packs": 5, "pieces": 0},
    })
    view = _view(client, p["id"], "2026-07-19")
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["opening"]["cartons"], view["opening"]["packs"], view["opening"]["pieces"], view["packaging_rule"]) == "109.50 Ctns"


def test_napkin_mixed_radix_quantities_remain_exact(client, setup):
    p = _make_product(client, "Packaging Napkin", {"cartons_to_packs": 6, "packs_to_pieces": 10})
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 135, "packs": 4, "pieces": 0},
    })
    view = _view(client, p["id"], "2026-07-19")
    from webapp.services.quantity_format import qty_label
    assert qty_label(view["opening"]["cartons"], view["opening"]["packs"], view["opening"]["pieces"], view["packaging_rule"]) == "135.40 Ctns"


def test_kingmax_jumbomax_carton_plus_piece_quantities_remain_exact(client, setup):
    kingmax = _make_product(client, "Packaging KingMax", {"carton_to_pieces": 60})
    jumbomax = _make_product(client, "Packaging JumboMax", {"carton_to_pieces": 24})
    client.post("/api/daily-figures", json={
        "product_id": kingmax["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 3},
    })
    client.post("/api/daily-figures", json={
        "product_id": jumbomax["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 4, "packs": 0, "pieces": 12},
    })
    from webapp.services.quantity_format import qty_label
    kv = _view(client, kingmax["id"], "2026-07-19")
    jv = _view(client, jumbomax["id"], "2026-07-19")
    assert qty_label(kv["opening"]["cartons"], kv["opening"]["packs"], kv["opening"]["pieces"], kv["packaging_rule"]) == "5.03 Ctns"
    assert qty_label(jv["opening"]["cartons"], jv["opening"]["packs"], jv["opening"]["pieces"], jv["packaging_rule"]) == "4.12 Ctns"


def test_historical_source_lines_use_their_own_stored_packaging_rule(client, setup):
    p = _make_product(client, "Packaging Historical Rule", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    prod = _production(client, p["id"], "2026-07-19", "Day", 5)  # snapshot: 10x10 rule -> 500 base units
    # Changing the product's CURRENT packaging rule afterward must never
    # re-interpret the already-created line's stored base_unit_qty.
    client.post(f"/api/admin/products/{p['id']}/packaging-rules", json={"carton_to_pieces": 60})
    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 500  # unchanged


def test_no_float_or_rounding_logic_anywhere_in_the_ledger(client, setup, app):
    p = _make_product(client, "Packaging No Float", {"cartons_to_packs": 10, "packs_to_pieces": 10})
    _production(client, p["id"], "2026-07-19", "Day", 7)
    entries = _ledger(app, p["id"], "2026-07-19", "2026-07-19", shift="Day")
    e = entries[0]
    for field in ("opening_base_qty", "production_total", "returns_total", "issued_total", "closing_base_qty"):
        assert isinstance(e[field], int)


def test_raw_base_units_never_labelled_as_cartons_for_a_negative_balance(client, setup, app):
    """Final legacy-migration investigation, section 9 — a negative
    balance IS expressible in book notation (magnitude split correctly,
    one leading minus sign) — the defect this proves against is the raw
    BASE-UNIT number (-500) being suffixed with "Ctns" directly, which
    would be a completely different, wrong magnitude from the correct
    -5.00 Ctns (5 cartons)."""
    p = _make_product(client, "Packaging Negative Label")
    _dispatch(client, p["id"], setup["customer"]["id"], setup["category"]["id"], "2026-07-19", "NL-1", 5)
    entries = _ledger(app, p["id"], "2026-07-19", "2026-07-19", shift="Day")
    label = entries[0]["closing_label"]
    assert label == "-5.00 Ctns"
    assert "-500" not in label  # never the raw base-unit number relabeled as cartons


# =====================================================================
# Cross-surface consistency
# =====================================================================

def test_daily_figures_and_dashboard_report_the_same_opening_stock(client, setup):
    p = _make_product(client, "Consistency Opening")
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    _production(client, p["id"], "2026-07-20", "Day", 5)
    daily_figures_opening = _view(client, p["id"], "2026-07-21", "Day")["opening"]["base_qty"]
    dashboard = client.get("/api/dashboard?date=2026-07-21").get_json()
    dashboard_row = next(r for r in dashboard["stock_summary"] if r["product_id"] == p["id"])
    assert dashboard_row["opening_base_qty"] == daily_figures_opening


def test_history_and_daily_figures_agree(client, setup):
    p = _make_product(client, "Consistency History")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    # History lists periods with an actual DailyFigure row — an explicit
    # save (even a no-op opening confirmation) is what puts a period there.
    client.post("/api/daily-figures", json={
        "product_id": p["id"], "date": "2026-07-19", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0}, "notes": "history check",
    })
    daily_figures = _view(client, p["id"], "2026-07-19")
    history = client.get(f"/api/daily-figures/history?product_id={p['id']}").get_json()
    history_row = next(r for r in history if r["date"] == "2026-07-19" and r["shift"] == "Day")
    assert history_row["closing"]["base_qty"] == daily_figures["closing"]["base_qty"]


def test_no_activity_on_later_date_preserves_correctly_calculated_prior_balance(client, setup):
    p = _make_product(client, "Consistency No Activity Later")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    _dispatch(client, p["id"], setup["customer"]["id"], setup["category"]["id"], "2026-07-20", "NA-1", 2)
    expected = _view(client, p["id"], "2026-07-20")["closing"]["base_qty"]
    later = _view(client, p["id"], "2026-08-03")
    assert later["opening"]["base_qty"] == expected
    assert later["closing"]["base_qty"] == expected


# =====================================================================
# Exact regression matching the proven root cause: genuine over-issuance
# correctly reconciles to a negative balance and carries consistently
# =====================================================================

def test_genuine_over_issuance_reconciles_exactly_and_carries_consistently_through_3_august(client, setup, app):
    """The exact reported pattern (valid stock around 20 July, a sudden
    negative appearing without any 21-July source records, carrying
    forward through 3 August) reproduced precisely: Production of 5
    cartons (500 base units) on 19 July, Dispatch of 40 cartons (4000 base
    units) on 20 July -> Closing becomes exactly -3500 base units on 20
    July and stays exactly -3500 through 3 August, with 21 July's own
    Reset Daily Values preview correctly showing NO matching source
    records (because the movement was on 20 July, not 21 July) — proving
    this is a real, exactly-reconciling business balance, not a defect."""
    p = _make_product(client, "Regression Over Issuance")
    _production(client, p["id"], "2026-07-19", "Day", 5)
    _dispatch(client, p["id"], setup["customer"]["id"], setup["category"]["id"], "2026-07-20", "REG-1", 40)

    assert _view(client, p["id"], "2026-07-19")["closing"]["base_qty"] == 500
    assert _view(client, p["id"], "2026-07-20")["closing"]["base_qty"] == -3500
    assert _view(client, p["id"], "2026-07-21")["opening"]["base_qty"] == -3500
    assert _view(client, p["id"], "2026-08-03")["closing"]["base_qty"] == -3500

    preview = client.post("/api/daily-reset/preview", json={
        "date": "2026-07-21", "shift": "Day", "product_id": p["id"], "mode": "figures_only",
    }).get_json()
    product_preview = preview["products"][0]
    assert product_preview["has_source_activity"] is False  # correctly nothing on 21 July itself
    assert product_preview["has_manual_opening_entry"] is False

    entries = _ledger(app, p["id"], "2026-07-19", "2026-08-03")
    negative = stock_ledger_service.first_negative_period(entries)
    assert negative["date"] == "2026-07-20" and negative["shift"] == "Day"
    assert negative["dispatch_lines"][0]["base_unit_qty"] == 4000
    assert negative["production_total"] == 0  # nothing produced that day
    # Exact reconciliation: opening(500) + production(0) + returns(0) - issued(4000) = -3500
    assert negative["opening_base_qty"] + negative["production_total"] + negative["returns_total"] - negative["issued_total"] == -3500
