"""
Production hotfix — Compact Corporate proved carry-forward still failed
in a scenario Compact Standard's fix never exercised: 1 August was
completed ("Already inputted by admin") BEFORE 30 July's Returns (1.11
Ctns), Production (100 Ctns), and Issued (5 Ctns) were ever entered. At
that moment there was genuinely nothing before 1 August — no anchor, no
finalized movement — so it correctly became this product's "first-ever
period" anchor (opening_stock_source=initial_manual). That was correct
*at the time*, but once 30 July's history was entered afterward, nothing
ever re-examined whether 1 August was still entitled to that trust — a
bare opening_stock_is_override boolean can't express "this was only ever
true because nothing earlier had been entered yet."

The fix is architectural: webapp/models/daily_figure.py's
opening_stock_source distinguishes WHY a row might be trusted, and
webapp/services/stock_service.py's _is_trusted_anchor() re-checks
initial_manual/legacy_inferred rows against finalized history on EVERY
read (not once, not only via a migration) — only manual_correction
(a provably deliberate, differing elevated correction) is exempt. See
migrations/versions/06658bb730c0_add_opening_stock_source_provenance.py
for the corrective reclassification of existing data.

Everything here operates by product_id and each product's own packaging
rule; no product name, ID, category, or packaging family is ever
special-cased.
"""
import pytest

from webapp.extensions import db as _db
from webapp.models.daily_figure import (
    OPENING_STOCK_SOURCE_DERIVED,
    OPENING_STOCK_SOURCE_INITIAL_MANUAL,
    OPENING_STOCK_SOURCE_LEGACY_INFERRED,
    OPENING_STOCK_SOURCE_MANUAL_CORRECTION,
    OPENING_STOCK_SOURCE_RESET_CREATED,
    DailyFigure,
)


def _make_product(client, name, cartons_to_packs=10, packs_to_pieces=10, carton_to_pieces=None):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    payload = {"cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces} \
        if carton_to_pieces is None else {"carton_to_pieces": carton_to_pieces}
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json=payload)
    return product


def _finalize_production(client, product_id, date_str, shift, cartons, packs=0, pieces=0):
    p = client.post("/api/production", json={
        "date": date_str, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    res = client.post(f"/api/production/{p['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return p


def _finalize_return(client, product_id, date_str, cartons, packs=0, pieces=0):
    r = client.post("/api/returns", json={
        "date": date_str,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    res = client.post(f"/api/returns/{r['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return r


def _finalize_dispatch(client, product_id, customer_id, date_str, shift, cartons, number, packs=0, pieces=0):
    d = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date_str, "shift": shift, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    res = client.post(f"/api/dispatches/{d['id']}/finalize")
    assert res.status_code == 200, res.get_json()
    return d


def _view(client, product_id, date_str, shift="Day"):
    return client.get(f"/api/daily-figures/{product_id}?date={date_str}&shift={shift}").get_json()


def _figure(pid, date, shift="Day"):
    return DailyFigure.query.filter_by(product_id=pid, date=date, shift=shift).first()


@pytest.fixture
def setup(client, login_as):
    login_as("root", "password123", "super_admin")
    product = _make_product(client, "Compact Corporate Hotfix")
    category = client.post("/api/admin/sales-categories", json={"name": "Corp Category"}).get_json()
    customer = client.post("/api/admin/customers", json={
        "name": "Corp Customer", "sales_category_id": category["id"],
    }).get_json()
    return {"product": product, "customer": customer}


# =====================================================================
# The exact proven failure: 30 July history entered AFTER 1 August is
# already completed ("Already inputted by admin")
# =====================================================================

def test_compact_corporate_exact_repro_already_inputted_before_history(client, setup):
    pid = setup["product"]["id"]

    # 1 August completed FIRST — genuinely the first-ever period at this
    # moment (super_admin = "admin"), submitted as 0 since nothing is
    # known yet. This legitimately becomes an initial_manual anchor.
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0}, "notes": "Already inputted by admin",
    })
    assert res.status_code == 200
    figure = _figure(pid, "2026-08-01")
    assert figure.opening_stock_source == OPENING_STOCK_SOURCE_INITIAL_MANUAL

    before = _view(client, pid, "2026-08-01")
    assert before["opening"]["base_qty"] == 0  # correct at the time — nothing existed before it yet

    # NOW enter 30 July's historical Returns/Production/Dispatch —
    # exactly the reported repro: 1.11 Ctns Returns, 100 Ctns Production,
    # 5 Ctns Issued -> Closing 96.11 Ctns (9611 base units).
    _finalize_return(client, pid, "2026-07-30", 1, 1, 1)
    _finalize_production(client, pid, "2026-07-30", "Day", 100)
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-30", "Day", 5, "CORP-1")

    view_30_july = _view(client, pid, "2026-07-30")
    assert view_30_july["opening"]["base_qty"] == 0
    assert view_30_july["return_"]["base_qty"] == 111
    assert view_30_july["production"]["base_qty"] == 10000
    assert view_30_july["issued"]["base_qty"] == 500
    assert view_30_july["closing"]["base_qty"] == 9611  # 96.11 Ctns

    # 1 August must recalculate immediately — no reopen, no reset, no new
    # migration, and its "Already inputted" completion must still show
    # live derived values.
    after = _view(client, pid, "2026-08-01")
    assert after["opening"]["base_qty"] == 9611
    assert after["opening"]["cartons"] == 96 and after["opening"]["packs"] == 1 and after["opening"]["pieces"] == 1
    assert after["production"]["base_qty"] == 0
    assert after["return_"]["base_qty"] == 0
    assert after["issued"]["base_qty"] == 0
    assert after["closing"]["base_qty"] == 9611

    # The row that was once "the first period" must no longer be trusted
    # as an anchor now that real history predates it.
    assert _figure(pid, "2026-08-01").opening_stock_source == OPENING_STOCK_SOURCE_INITIAL_MANUAL  # label unchanged...
    from webapp.services.stock_service import _is_trusted_anchor
    assert _is_trusted_anchor(_figure(pid, "2026-08-01")) is False  # ...but no longer trusted, live


# =====================================================================
# Later period created before each individual kind of historical movement
# =====================================================================

def test_later_period_created_before_historical_production(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-30", "Day", 20)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 20


def test_later_period_created_before_historical_returns(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_return(client, pid, "2026-07-30", 4)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 4


def test_later_period_created_before_historical_dispatch(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-30", "Day", 3, "CORP-2")
    assert _view(client, pid, "2026-08-01")["opening"]["base_qty"] == -300


# =====================================================================
# Historical movement changed AFTER a later row already exists
# =====================================================================

def test_historical_movement_added_after_already_inputted_completion(client, login_as, setup):
    pid = setup["product"]["id"]
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_corp_1", "password123", "operator")
    # Operator completes ("Already Inputted") 1 August first.
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    login_as("root2", "password123", "super_admin")
    _finalize_production(client, pid, "2026-07-30", "Day", 15)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 15


def test_historical_movement_added_after_no_activity_completion(client, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_corp_2", "password123", "operator")
    res = client.post("/api/daily-entry-status/no-activity", json={
        "product_id": pid, "date": "2026-08-02", "shift": "Day",
    })
    assert res.status_code == 200
    _finalize_production(client, pid, "2026-07-30", "Day", 25)
    assert _view(client, pid, "2026-08-02")["opening"]["cartons"] == 25


def test_historical_movement_edited_after_a_later_row_exists(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    p = _finalize_production(client, pid, "2026-07-30", "Day", 10)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 10

    reopen_res = client.post(f"/api/production/{p['id']}/reopen", json={"reason": "correcting quantity"})
    assert reopen_res.status_code == 200
    line_id = client.get(f"/api/production/{p['id']}").get_json()["lines"][0]["id"]
    client.patch(f"/api/production/{p['id']}/lines/{line_id}", json={"cartons": 30, "packs": 0, "pieces": 0})
    client.post(f"/api/production/{p['id']}/finalize")

    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 30


def test_historical_movement_voided_after_a_later_row_exists(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    p = _finalize_production(client, pid, "2026-07-30", "Day", 10)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 10
    client.post(f"/api/production/{p['id']}/void", json={"reason": "mistake"})
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 0


def test_historical_movement_reopened_and_refinalized(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    p = _finalize_production(client, pid, "2026-07-30", "Day", 10)
    client.post(f"/api/production/{p['id']}/reopen", json={"reason": "fix"})
    client.post(f"/api/production/{p['id']}/finalize")
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 10


# =====================================================================
# Ordinary actions must not create an override; explicit corrections must
# =====================================================================

def test_ordinary_save_and_next_does_not_create_an_override(client, login_as, setup):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-30", "Day", 12)
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_corp_3", "password123", "operator")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 999, "packs": 0, "pieces": 0},
    })
    assert res.get_json()["opening"]["cartons"] == 12  # forced to derived, submission ignored
    assert _figure(pid, "2026-08-01").opening_stock_source == OPENING_STOCK_SOURCE_DERIVED


def test_ordinary_super_admin_save_does_not_create_an_override(client, setup):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-30", "Day", 12)
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 12, "packs": 0, "pieces": 0},  # matches derived exactly
    })
    assert res.status_code == 200
    assert _figure(pid, "2026-08-01").opening_stock_source == OPENING_STOCK_SOURCE_DERIVED


def test_ordinary_manager_save_does_not_create_an_override(client, login_as, setup):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-30", "Day", 12)
    login_as("mgr_corp_1", "password123", "manager")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 12, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert _figure(pid, "2026-08-01").opening_stock_source == OPENING_STOCK_SOURCE_DERIVED


def test_explicit_manager_correction_does_create_an_override(client, login_as, setup):
    """Final reset-safety correction, section 4 — Opening Stock becomes
    manual_correction only when the caller explicitly says so
    (opening_stock_explicitly_edited=True) and supplies a reason; merely
    differing from live derivation is no longer sufficient by itself."""
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-30", "Day", 12)
    login_as("mgr_corp_2", "password123", "manager")
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0}, "notes": "physical stock count correction",
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical stock count correction",
    })
    assert res.status_code == 200
    assert res.get_json()["opening"]["cartons"] == 500
    assert _figure(pid, "2026-08-01").opening_stock_source == OPENING_STOCK_SOURCE_MANUAL_CORRECTION


def test_explicit_super_admin_correction_does_create_an_override(client, setup):
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-30", "Day", 12)
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 777, "packs": 0, "pieces": 0}, "notes": "physical stock count correction",
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical stock count correction",
    })
    assert res.status_code == 200
    assert _figure(pid, "2026-08-01").opening_stock_source == OPENING_STOCK_SOURCE_MANUAL_CORRECTION


def test_explicit_edit_flag_without_a_role_still_requires_reason(client, setup):
    """A missing opening_correction_reason is rejected even for an
    otherwise-authorized elevated user — final reset-safety correction,
    section 4/6."""
    pid = setup["product"]["id"]
    _finalize_production(client, pid, "2026-07-30", "Day", 12)
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 777, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True,
    })
    assert res.status_code == 400


def test_manual_correction_survives_movement_entered_before_it(client, setup):
    """The one case where earlier movement must NOT demote an anchor —
    that's the entire point of a correction. A genuine correction can
    only be established against a period that already has SOME derived
    value to differ from (a genuinely-first-ever period has nothing to
    differ from yet, so any submission there is honored as
    initial_manual, not manual_correction — see
    test_genuine_initial_stock_remains_preserved)."""
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    # A deliberate, explicit correction on 1 August: derives to 10 (no
    # movement yet between 07-01 and 08-01), submitted as 500 — genuinely
    # differs, and explicitly flagged as a correction with a reason.
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical count",
    })
    assert _figure(pid, "2026-08-01").opening_stock_source == OPENING_STOCK_SOURCE_MANUAL_CORRECTION
    _finalize_production(client, pid, "2026-07-30", "Day", 999)  # entered AFTER the correction
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 500  # still trusted


def test_reset_does_not_create_an_override(client, setup):
    """Final reset-safety correction, section 2/3 — the default mode
    (figures_only / Status Only) never touches Opening Stock at all
    anymore, so a routine (non-explicit) submission that would previously
    have been silently ignored into `derived` stays exactly that, and
    reset leaves it fully untouched too — no override is ever created by
    either the routine save or the reset."""
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},  # no explicit-edit flag — ignored
    })
    assert _figure(pid, "2026-07-15").opening_stock_source == OPENING_STOCK_SOURCE_DERIVED
    client.post("/api/daily-reset", json={
        "date": "2026-07-15", "shift": "Day", "product_id": pid, "reason": "test",
    })
    assert _figure(pid, "2026-07-15").opening_stock_source == OPENING_STOCK_SOURCE_DERIVED


def test_full_reset_recalculates_a_prior_override_to_a_non_authoritative_marker(client, setup):
    """Full Reset (unlike Status Only) still legitimately clears Opening
    Stock for its OWN target period — even a genuine prior correction —
    but never to an authoritative zero: it is recalculated from the real
    previous chronological Closing Stock and stored non-authoritatively
    (reset_created), never manual_correction."""
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical count",
    })
    assert _figure(pid, "2026-07-15").opening_stock_source == OPENING_STOCK_SOURCE_MANUAL_CORRECTION

    client.post("/api/daily-reset", json={
        "date": "2026-07-15", "shift": "Day", "product_id": pid, "mode": "full", "reason": "test",
        "confirmation_text": "FULL RESET 2026-07-15 DAY",
    })
    figure = _figure(pid, "2026-07-15")
    assert figure.opening_stock_source == OPENING_STOCK_SOURCE_RESET_CREATED
    assert figure.opening_base_qty == 1000  # recalculated from 07-01's real Closing (10 cartons), never 0


def test_no_activity_does_not_create_an_override(client, login_as, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.patch("/api/admin/operator-daily-figure-permissions", json={"can_edit_opening": True})
    login_as("op_corp_4", "password123", "operator")
    client.post("/api/daily-entry-status/no-activity", json={
        "product_id": pid, "date": "2026-07-10", "shift": "Day",
    })
    assert _figure(pid, "2026-07-10") is None  # no row at all


# =====================================================================
# Genuine anchors preserved
# =====================================================================

def test_genuine_initial_stock_remains_preserved(client, setup):
    pid = setup["product"]["id"]
    res = client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    assert res.status_code == 200
    assert _figure(pid, "2026-07-01").opening_stock_source == OPENING_STOCK_SOURCE_INITIAL_MANUAL


def test_genuine_historical_corrections_remain_preserved(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-01", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},
    })
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-15", "shift": "Day",
        "opening": {"cartons": 500, "packs": 0, "pieces": 0},
        "opening_stock_explicitly_edited": True, "opening_correction_reason": "physical count",
    })
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 500


# =====================================================================
# A legacy-inferred row does not freeze carry-forward
# =====================================================================

def test_legacy_inferred_row_does_not_freeze_carry_forward(client, login_as, app):
    login_as("root", "password123", "super_admin")
    product = _make_product(client, "Legacy Inferred Corp")
    pid = product["id"]
    with app.app_context():
        from webapp.models.product import Product
        from webapp.models.user import User
        prod = _db.session.get(Product, pid)
        rule = prod.current_packaging_rule()
        user = User.query.filter_by(username="root").first()
        row = DailyFigure(
            product_id=pid, date="2026-08-01", shift="Day",
            opening_cartons=0, opening_packs=0, opening_pieces=0, opening_base_qty=0,
            opening_stock_is_override=True, opening_stock_source=OPENING_STOCK_SOURCE_LEGACY_INFERRED,
            return_cartons=0, return_packs=0, return_pieces=0, return_base_qty=0,
            production_cartons=0, production_packs=0, production_pieces=0, production_base_qty=0,
            packaging_rule_id=rule.id, created_by=user.id, updated_by=user.id,
        )
        _db.session.add(row)
        _db.session.commit()

    _finalize_production(client, pid, "2026-07-19", "Day", 8)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 8


# =====================================================================
# Multiple products carry independently; no product-specific logic
# =====================================================================

def test_multiple_products_carry_independently(client, login_as):
    login_as("root", "password123", "super_admin")
    products = [_make_product(client, f"Independent Corp {i}") for i in range(3)]
    for i, product in enumerate(products):
        client.post("/api/daily-figures", json={
            "product_id": product["id"], "date": "2026-08-01", "shift": "Day",
            "opening": {"cartons": 0, "packs": 0, "pieces": 0},
        })
    _finalize_production(client, products[1]["id"], "2026-07-30", "Day", 50)
    for i, product in enumerate(products):
        expected = 50 if i == 1 else 0
        assert _view(client, product["id"], "2026-08-01")["opening"]["cartons"] == expected


@pytest.mark.parametrize("name", [
    "Compact Standard", "Compact Corporate", "Lavex", "Premium", "Mambo",
    "Silky 4pack", "Napkins Corporate", "Napkins Standard", "Napkins Damage",
    "KingMax", "JumboMax", "Straws", "Kitchen Towel Doubles",
])
def test_every_current_product_family_covered(client, login_as, name):
    login_as("root", "password123", "super_admin")
    product = _make_product(client, f"{name} Hotfix2")
    pid = product["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-30", "Day", 9)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 9, name


def test_newly_created_product_uses_the_same_logic(client, login_as):
    login_as("root", "password123", "super_admin")
    product = _make_product(client, "Brand New Product Never Seen Before")
    pid = product["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-30", "Day", 6)
    assert _view(client, pid, "2026-08-01")["opening"]["cartons"] == 6


# =====================================================================
# Day-to-Night and Night-to-next-Day carry-forward, out of order
# =====================================================================

def test_day_to_night_carry_forward_out_of_order(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-30", "shift": "Night",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-30", "Day", 5)
    assert _view(client, pid, "2026-07-30", "Night")["opening"]["cartons"] == 5


def test_night_to_next_day_carry_forward_out_of_order(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-31", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-30", "Night", 7)
    assert _view(client, pid, "2026-07-31", "Day")["opening"]["cartons"] == 7


# =====================================================================
# Consistent results everywhere
# =====================================================================

def test_dashboard_daily_figures_and_history_agree(client, setup, app):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-30", "Day", 11)

    daily_figures_view = _view(client, pid, "2026-08-01")
    with app.app_context():
        from webapp.services.stock_service import opening_base_qty_at
        dashboard_opening = opening_base_qty_at(pid, "2026-08-01", "Day")
    assert daily_figures_view["opening"]["base_qty"] == dashboard_opening == 1100

    history_res = client.get(f"/api/daily-figures/history?product_id={pid}&date=2026-08-01&shift=Day")
    history_rows = history_res.get_json()
    assert history_rows[0]["opening"]["base_qty"] == 1100


# =====================================================================
# No duplicate rows, no fake movement records
# =====================================================================

def test_no_duplicate_rows_or_fake_movements_from_recalculation(client, setup):
    pid = setup["product"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    _finalize_production(client, pid, "2026-07-30", "Day", 9)
    for _ in range(5):
        _view(client, pid, "2026-08-01")
    assert DailyFigure.query.filter_by(product_id=pid).count() == 1
    from webapp.models.production_record import ProductionRecord
    assert ProductionRecord.query.count() == 1  # repeated views create no fake records


# =====================================================================
# Packaging safety across multiple structures
# =====================================================================

@pytest.mark.parametrize("cartons_to_packs,packs_to_pieces,carton_to_pieces,production_qty", [
    (10, 10, None, (2, 3, 4)),
    (None, None, 60, (1, 0, 15)),
    (6, 10, None, (1, 2, 4)),
])
def test_packaging_safety_across_structures_out_of_order(client, login_as, cartons_to_packs, packs_to_pieces, carton_to_pieces, production_qty):
    login_as("root", "password123", "super_admin")
    product = _make_product(
        client, f"Corp Packaging {cartons_to_packs}-{packs_to_pieces}-{carton_to_pieces}",
        cartons_to_packs=cartons_to_packs, packs_to_pieces=packs_to_pieces, carton_to_pieces=carton_to_pieces,
    )
    pid = product["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-08-01", "shift": "Day",
        "opening": {"cartons": 0, "packs": 0, "pieces": 0},
    })
    c, p, pc = production_qty
    _finalize_production(client, pid, "2026-07-30", "Day", c, p, pc)
    view = _view(client, pid, "2026-08-01")
    assert view["opening"]["cartons"] == c
    assert view["opening"]["packs"] == p
    assert view["opening"]["pieces"] == pc
    assert isinstance(view["opening"]["base_qty"], int)
