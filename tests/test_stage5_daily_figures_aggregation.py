"""
Stage 5: Daily Figures aggregation from the three source books (Dispatch,
Returns, Production). Covers the shift rules (Dispatch/Returns are Day-only;
Production is genuinely Day-or-Night), recalculation when a source record
changes after being finalized, no-duplicate-aggregation, and per-product
packaging regression (Napkins/KingMax/JumboMax use the exact same
webapp/services/packaging.py conversion as every other module — never a
second, duplicated implementation).
"""
import pytest


@pytest.fixture
def super_admin(login_as):
    return login_as("root", "password123", "super_admin")


def _make_product(client, name, cartons_to_packs=None, packs_to_pieces=None, carton_to_pieces=None):
    product = client.post("/api/admin/products", json={"name": name}).get_json()
    body = {"carton_to_pieces": carton_to_pieces} if carton_to_pieces else \
        {"cartons_to_packs": cartons_to_packs, "packs_to_pieces": packs_to_pieces}
    client.post(f"/api/admin/products/{product['id']}/packaging-rules", json=body)
    return product


@pytest.fixture
def setup(client, super_admin):
    group_a = _make_product(client, "Compact Corporate Test", 10, 10)
    napkin = _make_product(client, "Napkins Corporate Test", 6, 10)      # Napkin rule: 6 packs/carton, 10 pieces/pack
    kingmax = _make_product(client, "KingMax Test", carton_to_pieces=60)
    jumbomax = _make_product(client, "JumboMax Test", carton_to_pieces=24)
    category = client.post("/api/admin/sales-categories", json={"name": "Test Category"}).get_json()
    customer = client.post("/api/admin/customers", json={"name": "Dalca", "sales_category_id": category["id"]}).get_json()
    return {"group_a": group_a, "napkin": napkin, "kingmax": kingmax, "jumbomax": jumbomax,
            "customer": customer, "category": category}


def _finalize_dispatch(client, product_id, customer_id, date, shift, cartons, packs, pieces, number):
    created = client.post("/api/dispatches", json={
        "dispatch_number": number, "date": date, "shift": shift, "customer_id": customer_id,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    return client.post(f"/api/dispatches/{created['id']}/finalize").get_json(), created["id"]


def _finalize_return(client, product_id, date, cartons=0, packs=0, pieces=0):
    created = client.post("/api/returns", json={
        "date": date, "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    client.post(f"/api/returns/{created['id']}/finalize")
    return created


def _finalize_production(client, product_id, date, shift, cartons=0, packs=0, pieces=0):
    created = client.post("/api/production", json={
        "date": date, "shift": shift,
        "lines": [{"product_id": product_id, "cartons": cartons, "packs": packs, "pieces": pieces}],
    }).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    return created


def _view(client, product_id, date, shift):
    return client.get(f"/api/daily-figures/{product_id}?date={date}&shift={shift}").get_json()


# ---------- Day aggregation from each book ----------

def test_day_dispatch_aggregates_into_issued(client, setup):
    _finalize_dispatch(client, setup["group_a"]["id"], setup["customer"]["id"], "2026-07-28", "Day", 1, 0, 0, "AGG-1")
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert view["issued"]["base_qty"] == 100


def test_day_returns_aggregate_into_return(client, setup):
    _finalize_return(client, setup["group_a"]["id"], "2026-07-28", cartons=1)
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert view["return_"]["base_qty"] == 100
    assert view["return_"]["from_returns_book"] == 100


def test_day_production_aggregates_into_production(client, setup):
    _finalize_production(client, setup["group_a"]["id"], "2026-07-28", "Day", cartons=1)
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert view["production"]["base_qty"] == 100
    assert view["production"]["from_production_book"] == 100


def test_night_production_aggregates_into_night_daily_figure(client, setup):
    _finalize_production(client, setup["group_a"]["id"], "2026-07-28", "Night", cartons=2)
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Night")
    assert view["production"]["base_qty"] == 200


# ---------- Dispatch/Returns must never contribute to Night ----------

def test_finalized_dispatch_never_contributes_to_night_issued(client, setup):
    _finalize_dispatch(client, setup["group_a"]["id"], setup["customer"]["id"], "2026-07-28", "Day", 1, 0, 0, "AGG-2")
    night_view = _view(client, setup["group_a"]["id"], "2026-07-28", "Night")
    assert night_view["issued"]["base_qty"] == 0


def test_finalized_return_never_contributes_to_night(client, setup):
    """Returns has no shift of its own — it only ever counts toward Day,
    confirmed here by checking Night sees nothing from it."""
    _finalize_return(client, setup["group_a"]["id"], "2026-07-28", cartons=1)
    night_view = _view(client, setup["group_a"]["id"], "2026-07-28", "Night")
    assert night_view["return_"]["base_qty"] == 0
    assert night_view["return_"]["from_returns_book"] == 0


def test_day_production_never_contributes_to_night(client, setup):
    _finalize_production(client, setup["group_a"]["id"], "2026-07-28", "Day", cartons=1)
    night_view = _view(client, setup["group_a"]["id"], "2026-07-28", "Night")
    assert night_view["production"]["base_qty"] == 0


def test_night_production_never_contributes_to_day(client, setup):
    _finalize_production(client, setup["group_a"]["id"], "2026-07-28", "Night", cartons=1)
    day_view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert day_view["production"]["base_qty"] == 0


def test_returns_book_has_no_shift_field_at_all(client, setup):
    """Confirms the API never invented a Night-Returns workflow."""
    created = client.post("/api/returns", json={
        "date": "2026-07-28",
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0}],
    }).get_json()
    assert "shift" not in created


# ---------- draft records must not count ----------

def test_draft_return_does_not_count(client, setup):
    client.post("/api/returns", json={
        "date": "2026-07-28",
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert view["return_"]["base_qty"] == 0


def test_draft_production_does_not_count(client, setup):
    client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day",
        "lines": [{"product_id": setup["group_a"]["id"], "cartons": 5, "packs": 0, "pieces": 0}],
    })
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert view["production"]["base_qty"] == 0


def test_voided_return_does_not_count(client, setup):
    created = _finalize_return(client, setup["group_a"]["id"], "2026-07-28", cartons=1)
    client.post(f"/api/returns/{created['id']}/void", json={"reason": "mistake"})
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert view["return_"]["base_qty"] == 0


def test_voided_production_does_not_count(client, setup):
    created = _finalize_production(client, setup["group_a"]["id"], "2026-07-28", "Day", cartons=1)
    client.post(f"/api/production/{created['id']}/void", json={"reason": "mistake"})
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert view["production"]["base_qty"] == 0


# ---------- recalculation after a source record changes ----------

def test_daily_figure_recalculates_when_return_is_edited_after_reopen(client, setup):
    created = _finalize_return(client, setup["group_a"]["id"], "2026-07-28", cartons=1)  # 100
    assert _view(client, setup["group_a"]["id"], "2026-07-28", "Day")["return_"]["base_qty"] == 100

    client.post(f"/api/returns/{created['id']}/reopen", json={"reason": "correction"})
    line_id = created["lines"][0]["id"]
    client.patch(f"/api/returns/{created['id']}/lines/{line_id}", json={"cartons": 3})  # now 300
    client.post(f"/api/returns/{created['id']}/finalize")

    assert _view(client, setup["group_a"]["id"], "2026-07-28", "Day")["return_"]["base_qty"] == 300


def test_daily_figure_recalculates_when_production_is_voided(client, setup):
    created = _finalize_production(client, setup["group_a"]["id"], "2026-07-28", "Night", cartons=1)  # 100
    assert _view(client, setup["group_a"]["id"], "2026-07-28", "Night")["production"]["base_qty"] == 100

    client.post(f"/api/production/{created['id']}/void", json={"reason": "spoiled batch"})
    assert _view(client, setup["group_a"]["id"], "2026-07-28", "Night")["production"]["base_qty"] == 0


def test_closing_stock_recalculates_when_source_dispatch_is_voided(client, setup):
    _, dispatch_id = _finalize_dispatch(client, setup["group_a"]["id"], setup["customer"]["id"],
                                         "2026-07-28", "Day", 1, 0, 0, "AGG-3")
    client.post("/api/daily-figures", json={
        "product_id": setup["group_a"]["id"], "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 5, "packs": 0, "pieces": 0},
    })
    before = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert before["closing"]["base_qty"] == 500 - 100

    client.post(f"/api/dispatches/{dispatch_id}/void", json={"reason": "correction"})
    after = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert after["closing"]["base_qty"] == 500


# ---------- no duplicate aggregation ----------

def test_multiple_return_lines_for_same_product_across_records_sum_once_each(client, setup):
    _finalize_return(client, setup["group_a"]["id"], "2026-07-28", cartons=1)  # 100
    _finalize_return(client, setup["group_a"]["id"], "2026-07-28", cartons=2)  # 200
    view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    assert view["return_"]["base_qty"] == 300  # not doubled, not tripled


def test_multi_line_single_record_sums_correctly_without_duplication(client, setup):
    created = client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day",
        "lines": [
            {"product_id": setup["group_a"]["id"], "cartons": 1, "packs": 0, "pieces": 0},
            {"product_id": setup["napkin"]["id"], "cartons": 1, "packs": 0, "pieces": 0},
        ],
    }).get_json()
    client.post(f"/api/production/{created['id']}/finalize")
    group_a_view = _view(client, setup["group_a"]["id"], "2026-07-28", "Day")
    napkin_view = _view(client, setup["napkin"]["id"], "2026-07-28", "Day")
    assert group_a_view["production"]["base_qty"] == 100
    assert napkin_view["production"]["base_qty"] == 60  # 1 carton = 6 packs * 10 pieces


# ---------- Opening/Closing formula, unaffected by this stage ----------

def test_closing_formula_combines_all_three_books_and_issued(client, setup):
    pid = setup["group_a"]["id"]
    client.post("/api/daily-figures", json={
        "product_id": pid, "date": "2026-07-28", "shift": "Day",
        "opening": {"cartons": 10, "packs": 0, "pieces": 0},   # 1000
    })
    _finalize_return(client, pid, "2026-07-28", packs=5)               # 50
    _finalize_production(client, pid, "2026-07-28", "Day", cartons=1)  # 100
    _finalize_dispatch(client, pid, setup["customer"]["id"], "2026-07-28", "Day", 0, 2, 0, "AGG-4")  # 20

    view = _view(client, pid, "2026-07-28", "Day")
    assert view["closing"]["base_qty"] == 1000 + 50 + 100 - 20


# ---------- per-product packaging regression ----------

def test_napkin_packaging_rule_unchanged_6_packs_10_pieces(client, setup):
    created = _finalize_return(client, setup["napkin"]["id"], "2026-07-28", cartons=1, packs=2, pieces=3)
    assert created["lines"][0]["base_unit_qty"] == (1 * 6 + 2) * 10 + 3  # 83


def test_kingmax_no_pack_unit_carton_to_pieces_60(client, setup):
    created = _finalize_production(client, setup["kingmax"]["id"], "2026-07-28", "Day", cartons=2, pieces=10)
    assert created["lines"][0]["base_unit_qty"] == 2 * 60 + 10  # 130


def test_jumbomax_no_pack_unit_carton_to_pieces_24(client, setup):
    created = _finalize_production(client, setup["jumbomax"]["id"], "2026-07-28", "Day", cartons=3, pieces=1)
    assert created["lines"][0]["base_unit_qty"] == 3 * 24 + 1  # 73


def test_kingmax_rejects_a_packs_value(client, setup):
    """No-pack-unit products must not accept a decimal-pack interface at all."""
    res = client.post("/api/production", json={
        "date": "2026-07-28", "shift": "Day",
        "lines": [{"product_id": setup["kingmax"]["id"], "cartons": 1, "packs": 1, "pieces": 0}],
    })
    assert res.status_code == 400


def test_decimal_book_value_is_not_reinterpreted_as_base_10_fraction(client, setup):
    """A book quantity like '1 carton 2 packs 4 pieces' must convert via the
    product's actual packaging rule, never as 1.24 in base-10 — e.g. for
    Napkins (6 packs/carton, 10 pieces/pack) 1c2p4pc = (1*6+2)*10+4 = 84,
    not 124."""
    created = _finalize_return(client, setup["napkin"]["id"], "2026-07-28", cartons=1, packs=2, pieces=4)
    assert created["lines"][0]["base_unit_qty"] == 84
    assert created["lines"][0]["base_unit_qty"] != 124
